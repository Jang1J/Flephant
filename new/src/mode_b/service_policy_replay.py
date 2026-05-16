"""Cost-aware service-policy replay for KIS paper-auto rehearsal.

This module is deliberately read-only: it never calls KIS, never submits
orders, and never mutates the production registry. It replays a candidate
bundle through the same real-bar feature panel used by C12, then applies the
paper-auto cash-account policy:

- cash equity account
- long-only entries
- sells reduce existing holdings only
- no naked short exposure
- max order count and quantity caps from risk_config.yaml
- commission + slippage from risk_config.yaml
"""
from __future__ import annotations

import math
import pickle
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from src.mode_b.validation_tools import (
    BacktestEngine,
    BundleLoadFailed,
    DataUnavailable,
    NaNInMetrics,
    add_neutral_candidate_alpha_features,
)
from src.mode_b.service_policy_verifier import (
    normalize_service_policy_universe,
    service_policy_universe_hash,
)
from src.utils.config_loader import load as config_load
from src.utils.safe_cast import safe_bool, safe_float
from src.utils.ticker_utils import pad_ticker

_KST = ZoneInfo("Asia/Seoul")
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ARTIFACTS_ROOT = _REPO_ROOT / "artifacts"


@dataclass(frozen=True)
class ServicePolicyConfig:
    """Service-policy parameters loaded from risk_config.yaml."""

    initial_capital: float
    top_k_fraction: float
    max_orders_per_cycle: int
    max_order_qty_per_order: int
    max_names: int
    max_single_name: float
    min_cash: float
    daily_turnover_cap: float
    commission_bps: float
    slippage_bps: float
    annualization_factor: int
    min_daily_return_std: float
    decision_stride_bars: int
    min_holding_bars: int
    rebalance_cooldown_bars: int
    no_trade_score_spread: float
    allow_position_pyramiding: bool
    turnover_budget_hard_stop: bool
    min_expected_net_alpha_bps: float
    expected_net_alpha_source: str
    min_service_policy_sharpe: float
    trade_probability_gate_enabled: bool
    min_trade_probability: float

    @property
    def total_cost_bps(self) -> float:
        return self.commission_bps + self.slippage_bps

    @property
    def total_cost_rate(self) -> float:
        return self.total_cost_bps / 10_000.0

    @property
    def min_material_return_bps(self) -> float:
        """Config-derived materiality floor: one one-way cost budget."""
        return max(0.0, self.total_cost_bps)

    @classmethod
    def from_config(cls) -> "ServicePolicyConfig":
        backtest_cfg = config_load("risk_config.yaml", "backtest") or {}
        eval_cfg = config_load("risk_config.yaml", "evaluation") or {}
        paper_auto_cfg = config_load("risk_config.yaml", "paper_auto_trading") or {}
        position_cfg = config_load("risk_config.yaml", "position_limits") or {}
        turnover_cfg = config_load("risk_config.yaml", "turnover_cap") or {}
        cost_cfg = config_load("risk_config.yaml", "execution_cost_model") or {}
        cost_components = cost_cfg.get("components", {}) or {}
        label_cfg = config_load("risk_config.yaml", "label") or {}
        replay_cfg = config_load("risk_config.yaml", "service_policy_replay") or {}
        trade_gate_cfg = (
            (config_load("risk_config.yaml", "cost_aware_retraining") or {})
            .get("trade_probability_gate", {})
            or {}
        )

        return cls(
            initial_capital=float(backtest_cfg.get("initial_capital", 100_000_000.0)),
            top_k_fraction=float(eval_cfg.get("top_k_fraction", 0.25)),
            max_orders_per_cycle=int(paper_auto_cfg.get("max_orders_per_cycle", 3)),
            max_order_qty_per_order=int(paper_auto_cfg.get("max_order_qty_per_order", 1)),
            max_names=int(position_cfg.get("max_names", 10)),
            max_single_name=float(position_cfg.get("max_single_name", 0.20)),
            min_cash=float(position_cfg.get("min_cash", 0.10)),
            daily_turnover_cap=float(turnover_cfg.get("daily_max", 0.30)),
            commission_bps=float(cost_components.get("commission_bps", 0.0)),
            slippage_bps=float(cost_components.get("slippage_bps", 0.0)),
            annualization_factor=int(eval_cfg.get("annualization_factor", 252)),
            min_daily_return_std=float(eval_cfg.get("min_daily_pnl_std", 1e-8)),
            decision_stride_bars=max(
                1,
                int(replay_cfg.get("decision_stride_bars", label_cfg.get("horizon_bars", 1))),
            ),
            min_holding_bars=max(0, int(replay_cfg.get("min_holding_bars", 0))),
            rebalance_cooldown_bars=max(0, int(replay_cfg.get("rebalance_cooldown_bars", 0))),
            no_trade_score_spread=max(0.0, float(replay_cfg.get("no_trade_score_spread", 0.0))),
            allow_position_pyramiding=safe_bool(
                replay_cfg.get("allow_position_pyramiding", False),
                default=False,
            ),
            turnover_budget_hard_stop=safe_bool(
                replay_cfg.get("turnover_budget_hard_stop", True),
                default=True,
            ),
            min_expected_net_alpha_bps=float(
                replay_cfg.get("min_expected_net_alpha_bps", cost_components.get("slippage_bps", 0.0))
            ),
            expected_net_alpha_source=str(
                replay_cfg.get("expected_net_alpha_source", "rank_score")
            ),
            min_service_policy_sharpe=float(replay_cfg.get("min_service_policy_sharpe", 0.0)),
            trade_probability_gate_enabled=safe_bool(
                trade_gate_cfg.get("enabled"),
                default=False,
            ),
            min_trade_probability=safe_float(
                trade_gate_cfg.get("min_probability"),
                default=0.5,
                min_value=0.0,
                max_value=1.0,
            ),
        )


class ServicePolicyReplayEngine:
    """Replay a candidate bundle under KIS paper cash-account constraints."""

    def __init__(
        self,
        *,
        artifacts_root: Path | None = None,
        engine: BacktestEngine | None = None,
        policy: ServicePolicyConfig | None = None,
    ) -> None:
        self._artifacts_root = artifacts_root or _ARTIFACTS_ROOT
        self._engine = engine or BacktestEngine(artifacts_root=self._artifacts_root)
        self._policy = policy or ServicePolicyConfig.from_config()

    def run(
        self,
        bundle_id: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        universe: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run service-policy replay for a candidate bundle.

        start_date/end_date are YYYYMMDD inclusive. When omitted, the first C12
        test fold implied by current Mode B PIT snapshot is used.
        """
        if not bundle_id or not bundle_id.strip():
            raise BundleLoadFailed("bundle_id is empty")

        replay_start, replay_end = self._resolve_replay_window(start_date, end_date)
        active_universe = normalize_service_policy_universe(
            universe or self._load_active_universe()
        )
        if not active_universe:
            raise DataUnavailable("active universe is empty")

        model_callable, _, candidate_artifact = self._engine._resolve_candidate_model(bundle_id)
        feature_cols = [str(col) for col in candidate_artifact.get("feature_cols", []) or []]
        if not feature_cols:
            raise BundleLoadFailed("candidate feature_cols empty")

        from src.data.dataset_builder import DatasetBuilder

        builder = DatasetBuilder(artifacts_dir=self._artifacts_root / "data")
        add_neutral_candidate_alpha_features(builder, feature_cols)
        panel = self._engine._build_replay_panel(
            builder,
            active_universe,
            replay_start,
            replay_end,
        )
        target_col = BacktestEngine._candidate_target_col(
            candidate_artifact,
            builder.target_col,
        )
        if target_col != builder.target_col:
            panel = builder.relabel_panel_for_target(panel, target_col)

        missing_features = [col for col in feature_cols if col not in panel.columns]
        if missing_features:
            raise BundleLoadFailed(
                "candidate feature manifest mismatch in service replay: "
                f"missing={missing_features}"
            )
        if target_col not in panel.columns:
            raise DataUnavailable(f"target column missing from replay panel: {target_col}")

        result = self._simulate_panel(
            panel=panel,
            model_callable=model_callable,
            feature_cols=feature_cols,
            target_col=target_col,
            policy=self._policy,
            trade_probability_model=self._load_trade_probability_model(candidate_artifact),
        )
        result.update({
            "schema_version": "1.0.0",
            "kind": "service_policy_replay",
            "bundle_id": bundle_id,
            "model_version": self._model_version_from_artifact(candidate_artifact),
            "candidate_artifact": candidate_artifact,
            "date_range": {"start": replay_start, "end": replay_end},
            "universe": active_universe,
            "universe_count": len(active_universe),
            "universe_hash": service_policy_universe_hash(active_universe),
            "universe_policy": (
                "operator_override" if universe is not None else "final_dataset_gate"
            ),
            "target_col": target_col,
            "valid_rows": int(len(panel)),
            "external_kis_api": False,
            "registry_mutated": False,
            "generated_at": datetime.now(_KST).isoformat(),
        })
        return result

    def _resolve_replay_window(
        self,
        start_date: str | None,
        end_date: str | None,
    ) -> tuple[str, str]:
        if bool(start_date) != bool(end_date):
            raise DataUnavailable("start_date and end_date must be provided together")
        if start_date and end_date:
            replay_start = self._normalize_yyyymmdd(start_date)
            replay_end = self._normalize_yyyymmdd(end_date)
            latest_allowed_end = self._latest_pit_snapshot_yyyymmdd()
            if replay_end > latest_allowed_end:
                raise DataUnavailable(
                    "explicit service replay end_date exceeds latest PIT snapshot: "
                    f"end_date={replay_end} latest_allowed={latest_allowed_end}"
                )
            return replay_start, replay_end

        now = datetime.now(_KST)
        pit_cfg = config_load("risk_config.yaml", "pit_safety") or {}
        snapshot_hour = int(pit_cfg.get("snapshot_hour", 18))
        today_snapshot = now.replace(hour=snapshot_hour, minute=0, second=0, microsecond=0)
        default_end = today_snapshot if now >= today_snapshot else today_snapshot - timedelta(days=1)
        vt_cfg = config_load("risk_config.yaml", "validation_tools.backtest_engine") or {}
        wf_cfg = config_load("risk_config.yaml", "walk_forward") or {}
        purge_bars = int(vt_cfg.get("purge_bars", 60))
        embargo_bars = int(vt_cfg.get("embargo_bars", 78))
        from src.utils.trading_calendar import kospi_trading_start_date

        trading_days_needed = (
            int(wf_cfg.get("train_window_days", 60))
            + int(math.ceil(purge_bars / 390))
            + int(math.ceil(embargo_bars / 390))
            + int(wf_cfg.get("test_window_days", 20))
            + max(0, int(wf_cfg.get("n_splits", 8)) - 1)
            * int(wf_cfg.get("step_days", wf_cfg.get("test_window_days", 20)))
        )
        default_start_date = kospi_trading_start_date(default_end.date(), trading_days_needed)
        default_start = default_end.replace(
            year=default_start_date.year,
            month=default_start_date.month,
            day=default_start_date.day,
        )
        folds = self._engine._build_folds(default_start, default_end, purge_bars, embargo_bars)
        if not folds:
            raise DataUnavailable("default C12 fold window could not be resolved")
        first_fold = folds[0]
        test_dates = first_fold.get("test_trading_dates") or []
        if test_dates:
            replay_start = str(test_dates[0])
            replay_end = str(test_dates[-1])
        else:
            replay_start = first_fold["test_start"].strftime("%Y%m%d")
            replay_end = (first_fold["test_end"] - timedelta(days=1)).strftime("%Y%m%d")
        return replay_start, replay_end

    @staticmethod
    def _latest_pit_snapshot_yyyymmdd() -> str:
        now = datetime.now(_KST)
        pit_cfg = config_load("risk_config.yaml", "pit_safety") or {}
        snapshot_hour = int(pit_cfg.get("snapshot_hour", 18))
        today_snapshot = now.replace(hour=snapshot_hour, minute=0, second=0, microsecond=0)
        latest_snapshot = today_snapshot if now >= today_snapshot else today_snapshot - timedelta(days=1)
        return latest_snapshot.strftime("%Y%m%d")

    @staticmethod
    def _normalize_yyyymmdd(value: str) -> str:
        value = str(value).replace("-", "")
        if len(value) != 8 or not value.isdigit():
            raise DataUnavailable(f"date must be YYYYMMDD: {value!r}")
        return value

    @staticmethod
    def _load_active_universe() -> list[str]:
        universe_cfg = config_load("universe_config.yaml") or {}
        gate_cfg = (
            config_load(
                "risk_config.yaml",
                "backtest_agent.deploy_decision_gate.final_dataset_gate",
            )
            or {}
        )
        include_pending = safe_bool(
            gate_cfg.get("include_pending_data_tickers"),
            default=safe_bool(
                (universe_cfg.get("backtest_universe_mode") or {}).get("allow_pending"),
                default=False,
            ),
        )
        allowed_stock_statuses = {"active"}
        if include_pending:
            allowed_stock_statuses = {
                str(status)
                for status in (
                    gate_cfg.get("allowed_stock_statuses")
                    or ["active", "pending_data"]
                )
            }
        allowed_sector_statuses = {"confirmed"}
        if include_pending:
            allowed_sector_statuses = {
                str(status)
                for status in (
                    gate_cfg.get("allowed_sector_statuses")
                    or ["confirmed", "confirmed_pending_data"]
                )
            }
        sectors = universe_cfg.get("sectors", {}) or {}
        universe: list[str] = []
        for sector_data in sectors.values():
            if (
                not isinstance(sector_data, dict)
                or str(sector_data.get("status")) not in allowed_sector_statuses
            ):
                continue
            for stock in sector_data.get("stocks", []) or []:
                if str(stock.get("status")) in allowed_stock_statuses:
                    universe.append(pad_ticker(stock["ticker"]))
        if universe:
            return normalize_service_policy_universe(universe)
        fallback = universe_cfg.get("backtest_universe_mode", {}).get("fallback_tickers", [])
        return normalize_service_policy_universe(fallback)

    @staticmethod
    def _model_version_from_artifact(candidate_artifact: dict[str, Any]) -> str:
        metadata_path = Path(str(candidate_artifact.get("metadata_path", "")))
        if metadata_path.is_file():
            try:
                import json

                with metadata_path.open("r", encoding="utf-8") as fh:
                    metadata = json.load(fh)
                if isinstance(metadata, dict):
                    return str(metadata.get("version") or metadata.get("model_version") or "")
            except Exception as e:
                _ = e
                return ""
        return ""

    @staticmethod
    def _candidate_metadata(candidate_artifact: dict[str, Any]) -> dict[str, Any]:
        metadata = candidate_artifact.get("metadata")
        if isinstance(metadata, dict):
            return metadata
        metadata_path = Path(str(candidate_artifact.get("metadata_path", "")))
        if metadata_path.is_file():
            try:
                import json

                with metadata_path.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                return data if isinstance(data, dict) else {}
            except Exception as e:
                _ = e
        return {}

    @staticmethod
    def _load_trade_probability_model(candidate_artifact: dict[str, Any]) -> Any | None:
        metadata = ServicePolicyReplayEngine._candidate_metadata(candidate_artifact)
        classifier = metadata.get("trade_no_trade_classifier")
        if not isinstance(classifier, dict) or classifier.get("status") != "PASS":
            return None
        raw_path = str(classifier.get("model_path") or "").strip()
        if not raw_path:
            return None
        path = Path(raw_path)
        if not path.is_absolute():
            path = _REPO_ROOT / path
        try:
            with path.open("rb") as fh:
                return pickle.load(fh)
        except Exception as e:
            _ = e
            return None

    def _simulate_panel(
        self,
        *,
        panel: Any,
        model_callable: Any,
        feature_cols: list[str],
        target_col: str,
        policy: ServicePolicyConfig,
        initial_holdings: dict[str, int] | None = None,
        trade_probability_model: Any | None = None,
    ) -> dict[str, Any]:
        cash = float(policy.initial_capital)
        holdings: dict[str, int] = {
            pad_ticker(t): int(qty)
            for t, qty in (initial_holdings or {}).items()
            if int(qty) > 0
        }
        holding_since_cycle: dict[str, int] = {ticker: 0 for ticker in holdings}
        last_order_cycle_by_ticker: dict[str, int] = {}
        latest_prices: dict[str, float] = {}
        orders: list[dict[str, Any]] = []
        predicted_signals: list[float] = []
        actual_returns: list[float] = []
        daily_equity: dict[str, float] = {}
        daily_turnover_notional: dict[str, float] = {}
        daily_start_equity: dict[str, float] = {}
        max_orders_observed = 0
        naked_short_attempts = 0
        min_cash_ratio_observed = 1.0
        cooldown_skipped_orders = 0
        min_holding_skipped_sells = 0
        turnover_budget_skipped_orders = 0
        already_held_skipped_buys = 0
        trade_gate_stats: dict[str, Any] = {
            "enabled": policy.trade_probability_gate_enabled,
            "applied": False,
            "min_probability": policy.min_trade_probability,
            "cycles": 0,
            "candidates_seen": 0,
            "candidates_rejected": 0,
            "missing_probability": 0,
            "model_loaded": trade_probability_model is not None,
        }

        for cycle_idx, (ts_close, ts_group) in enumerate(panel.groupby(level="ts_close", sort=True)):
            bar_preds: list[tuple[str, float, float, float]] = []
            trade_probs_by_ticker: dict[str, float] = {}
            for (ticker, _), row in ts_group.iterrows():
                ticker_s = pad_ticker(ticker)
                try:
                    features = [float(row[col]) for col in feature_cols]
                    actual_ret = float(row[target_col])
                    price = float(row["close"])
                except Exception as e:
                    raise DataUnavailable(
                        f"invalid replay row ticker={ticker_s} ts={ts_close}: {e}"
                    ) from e
                if (
                    any(math.isnan(value) for value in features)
                    or math.isnan(actual_ret)
                    or math.isnan(price)
                    or price <= 0
                ):
                    continue
                pred = float(model_callable(features))
                if math.isnan(pred):
                    raise NaNInMetrics(
                        f"service replay prediction NaN: ticker={ticker_s} ts={ts_close}"
                    )
                latest_prices[ticker_s] = price
                bar_preds.append((ticker_s, pred, actual_ret, price))
                trade_prob = self._predict_trade_probability(
                    trade_probability_model,
                    features,
                )
                if trade_prob is not None:
                    trade_probs_by_ticker[ticker_s] = trade_prob
                predicted_signals.append(pred)
                actual_returns.append(actual_ret)

            if not bar_preds:
                continue

            ts_text = ts_close.isoformat() if hasattr(ts_close, "isoformat") else str(ts_close)
            day_key = ts_text[:10]
            equity_before = self._equity(cash, holdings, latest_prices)
            daily_start_equity.setdefault(day_key, equity_before)

            # The candidate label is horizon-based (currently 5m). Service replay
            # should not churn every 1m bar when the trained signal target updates
            # every label.horizon_bars. Prices/equity are still marked every bar.
            if cycle_idx % policy.decision_stride_bars != 0:
                equity_after = self._equity(cash, holdings, latest_prices)
                cash_ratio = cash / max(equity_after, 1.0)
                min_cash_ratio_observed = min(min_cash_ratio_observed, cash_ratio)
                daily_equity[day_key] = equity_after
                continue

            gated_bar_preds, trade_gate_state = self._apply_trade_probability_gate(
                bar_preds,
                policy,
                trade_probs_by_ticker,
                trade_probability_model=trade_probability_model,
            )
            if trade_gate_state.get("applied"):
                trade_gate_stats["applied"] = True
                trade_gate_stats["cycles"] += 1
                trade_gate_stats["candidates_seen"] += int(trade_gate_state.get("n_input", 0))
                trade_gate_stats["candidates_rejected"] += int(
                    trade_gate_state.get("n_rejected", 0)
                )
                trade_gate_stats["missing_probability"] += int(
                    trade_gate_state.get("missing_probability", 0)
                )
            elif policy.trade_probability_gate_enabled and not trade_gate_stats.get("reason"):
                trade_gate_stats["reason"] = trade_gate_state.get("reason")
            desired = self._desired_tickers(gated_bar_preds, policy)
            pred_by_ticker = {ticker: pred for ticker, pred, _, _ in bar_preds}
            price_by_ticker = {ticker: price for ticker, _, _, price in bar_preds}
            orders_this_cycle = 0

            held_not_desired = [
                ticker for ticker, qty in holdings.items()
                if qty > 0 and ticker not in desired and ticker in price_by_ticker
            ]
            held_not_desired.sort(key=lambda ticker: pred_by_ticker.get(ticker, 0.0))
            for ticker in held_not_desired:
                if orders_this_cycle >= policy.max_orders_per_cycle:
                    break
                if self._in_cooldown(
                    ticker,
                    cycle_idx,
                    last_order_cycle_by_ticker,
                    policy,
                ):
                    cooldown_skipped_orders += 1
                    continue
                qty_available = int(holdings.get(ticker, 0))
                if qty_available <= 0:
                    naked_short_attempts += 1
                    continue
                held_age = cycle_idx - int(holding_since_cycle.get(ticker, cycle_idx))
                if held_age < policy.min_holding_bars:
                    min_holding_skipped_sells += 1
                    continue
                qty = min(policy.max_order_qty_per_order, qty_available)
                gross_notional = float(qty * price_by_ticker[ticker])
                if not self._can_add_turnover(
                    day_key,
                    gross_notional,
                    daily_turnover_notional,
                    daily_start_equity,
                    policy,
                ):
                    turnover_budget_skipped_orders += 1
                    continue
                order = self._execute_order(
                    side="sell",
                    ticker=ticker,
                    qty=qty,
                    price=price_by_ticker[ticker],
                    ts=ts_text,
                    cash=cash,
                    holdings=holdings,
                    policy=policy,
                )
                cash = float(order.pop("_cash_after"))
                if int(order.get("holding_after", 0)) <= 0:
                    holding_since_cycle.pop(ticker, None)
                last_order_cycle_by_ticker[ticker] = cycle_idx
                daily_turnover_notional[day_key] = (
                    daily_turnover_notional.get(day_key, 0.0)
                    + float(order["gross_notional"])
                )
                orders.append(order)
                orders_this_cycle += 1

            desired_sorted = sorted(desired, key=lambda ticker: pred_by_ticker[ticker], reverse=True)
            for ticker in desired_sorted:
                if orders_this_cycle >= policy.max_orders_per_cycle:
                    break
                if ticker not in price_by_ticker:
                    continue
                if self._in_cooldown(
                    ticker,
                    cycle_idx,
                    last_order_cycle_by_ticker,
                    policy,
                ):
                    cooldown_skipped_orders += 1
                    continue
                if (
                    not policy.allow_position_pyramiding
                    and int(holdings.get(ticker, 0)) > 0
                ):
                    already_held_skipped_buys += 1
                    continue
                price = price_by_ticker[ticker]
                qty = policy.max_order_qty_per_order
                equity_now = self._equity(cash, holdings, latest_prices)
                if not self._can_buy(ticker, qty, price, cash, holdings, equity_now, policy):
                    continue
                gross_notional = float(qty * price)
                if not self._can_add_turnover(
                    day_key,
                    gross_notional,
                    daily_turnover_notional,
                    daily_start_equity,
                    policy,
                ):
                    turnover_budget_skipped_orders += 1
                    continue
                holding_before = int(holdings.get(ticker, 0))
                order = self._execute_order(
                    side="buy",
                    ticker=ticker,
                    qty=qty,
                    price=price,
                    ts=ts_text,
                    cash=cash,
                    holdings=holdings,
                    policy=policy,
                )
                cash = float(order.pop("_cash_after"))
                if holding_before <= 0:
                    holding_since_cycle[ticker] = cycle_idx
                last_order_cycle_by_ticker[ticker] = cycle_idx
                daily_turnover_notional[day_key] = (
                    daily_turnover_notional.get(day_key, 0.0)
                    + float(order["gross_notional"])
                )
                orders.append(order)
                orders_this_cycle += 1

            max_orders_observed = max(max_orders_observed, orders_this_cycle)
            equity_after = self._equity(cash, holdings, latest_prices)
            cash_ratio = cash / max(equity_after, 1.0)
            min_cash_ratio_observed = min(min_cash_ratio_observed, cash_ratio)
            daily_equity[day_key] = equity_after

        if not predicted_signals:
            raise DataUnavailable("service replay produced no predictions")

        metrics = self._metrics(policy, daily_equity)
        turnover = self._daily_turnover(
            daily_turnover_notional,
            daily_start_equity,
        )
        max_daily_turnover = max(turnover.values(), default=0.0)
        buy_orders = sum(1 for order in orders if order["side"] == "buy")
        sell_orders = sum(1 for order in orders if order["side"] == "sell")
        policy_checks = {
            "no_naked_short_exposure": naked_short_attempts == 0,
            "order_caps_respected": max_orders_observed <= policy.max_orders_per_cycle,
            "cash_guard_respected": min_cash_ratio_observed >= policy.min_cash - 1e-12,
            "long_only_entry": True,
            "broker_mode": "kis_virtual_cash_equity_replay",
        }
        blockers = self._gate_blockers(
            metrics=metrics,
            policy=policy,
            max_daily_turnover=max_daily_turnover,
            policy_checks=policy_checks,
        )
        status = "PASS" if not blockers else "BLOCKED"
        policy_checks["deploy_candidate_by_service_policy"] = status == "PASS"

        return {
            "status": status,
            "gate": {
                "status": status,
                "blockers": blockers,
                "requires_no_naked_short": True,
                "requires_order_caps": True,
                "requires_daily_turnover_lte": policy.daily_turnover_cap,
                "requires_positive_material_return_bps": policy.min_material_return_bps,
                "requires_service_policy_sharpe_gt": policy.min_service_policy_sharpe,
            },
            "metrics": metrics,
            "policy": asdict(policy) | {
                "total_cost_bps": policy.total_cost_bps,
                "total_cost_rate": policy.total_cost_rate,
            },
            "policy_checks": policy_checks,
            "order_stats": {
                "total_orders": len(orders),
                "buy_orders": buy_orders,
                "sell_orders": sell_orders,
                "sell_fraction": sell_orders / max(len(orders), 1),
                "max_orders_observed_per_cycle": max_orders_observed,
                "max_daily_turnover": max_daily_turnover,
                "ending_holding_count": sum(1 for qty in holdings.values() if qty > 0),
                "naked_short_attempts": naked_short_attempts,
                "min_cash_ratio_observed": min_cash_ratio_observed,
                "cooldown_skipped_orders": cooldown_skipped_orders,
                "min_holding_skipped_sells": min_holding_skipped_sells,
                "turnover_budget_skipped_orders": turnover_budget_skipped_orders,
                "already_held_skipped_buys": already_held_skipped_buys,
                "trade_probability_rejected_candidates": int(
                    trade_gate_stats.get("candidates_rejected", 0)
                ),
            },
            "trade_probability_gate": trade_gate_stats,
            "daily_turnover": turnover,
            "daily_equity": daily_equity,
            "orders": orders,
            "signal_quality": {
                "ic": self._pearsonr(predicted_signals, actual_returns),
                "rank_ic": self._spearmanr(predicted_signals, actual_returns),
                "prediction_count": len(predicted_signals),
            },
        }

    @staticmethod
    def _predict_trade_probability(
        trade_probability_model: Any | None,
        features: list[float],
    ) -> float | None:
        if trade_probability_model is None:
            return None
        try:
            raw = np.asarray(trade_probability_model.predict([features]), dtype=float)
        except Exception as e:
            _ = e
            return None
        if raw.ndim == 2 and raw.shape[1] >= 2:
            raw = raw[:, -1]
        raw = raw.reshape(-1)
        if len(raw) < 1 or not math.isfinite(float(raw[0])):
            return None
        return safe_float(float(raw[0]), default=0.0, min_value=0.0, max_value=1.0)

    @staticmethod
    def _apply_trade_probability_gate(
        bar_preds: list[tuple[str, float, float, float]],
        policy: ServicePolicyConfig,
        trade_probs_by_ticker: dict[str, float],
        *,
        trade_probability_model: Any | None,
    ) -> tuple[list[tuple[str, float, float, float]], dict[str, Any]]:
        state: dict[str, Any] = {
            "enabled": policy.trade_probability_gate_enabled,
            "applied": False,
            "min_probability": policy.min_trade_probability,
        }
        if not policy.trade_probability_gate_enabled:
            state["reason"] = "disabled"
            return bar_preds, state
        if trade_probability_model is None:
            state["reason"] = "classifier_missing"
            return bar_preds, state
        filtered: list[tuple[str, float, float, float]] = []
        missing = 0
        rejected = 0
        for row in bar_preds:
            ticker = row[0]
            prob = trade_probs_by_ticker.get(ticker)
            if prob is None:
                missing += 1
                rejected += 1
                continue
            if prob < policy.min_trade_probability:
                rejected += 1
                continue
            filtered.append(row)
        state.update({
            "applied": True,
            "n_input": len(bar_preds),
            "n_passed": len(filtered),
            "n_rejected": rejected,
            "missing_probability": missing,
        })
        return filtered, state

    @staticmethod
    def _desired_tickers(
        bar_preds: list[tuple[str, float, float, float]],
        policy: ServicePolicyConfig,
    ) -> set[str]:
        n_assets = len(bar_preds)
        k_by_fraction = max(1, int(n_assets * policy.top_k_fraction))
        k = max(1, min(policy.max_orders_per_cycle, k_by_fraction))
        if policy.no_trade_score_spread > 0:
            scores = sorted(pred for _, pred, _, _ in bar_preds)
            n = len(scores)
            median = (
                scores[n // 2]
                if n % 2 == 1
                else (scores[n // 2 - 1] + scores[n // 2]) / 2.0
            )
            eligible = [
                row for row in bar_preds
                if float(row[1]) - median >= policy.no_trade_score_spread
            ]
            if not eligible:
                return set()
            bar_preds = eligible
        if (
            policy.min_expected_net_alpha_bps > 0
            and policy.expected_net_alpha_source == "calibrated_net_bps"
        ):
            eligible = [
                row for row in bar_preds
                if float(row[1]) >= policy.min_expected_net_alpha_bps
            ]
            if not eligible:
                return set()
            bar_preds = eligible
        top = sorted(bar_preds, key=lambda item: item[1], reverse=True)[:k]
        return {ticker for ticker, _, _, _ in top}

    @staticmethod
    def _can_buy(
        ticker: str,
        qty: int,
        price: float,
        cash: float,
        holdings: dict[str, int],
        equity: float,
        policy: ServicePolicyConfig,
    ) -> bool:
        notional = price * qty
        total_debit = notional * (1.0 + policy.total_cost_rate)
        if cash < total_debit:
            return False
        if (cash - total_debit) / max(equity, 1.0) < policy.min_cash:
            return False
        held_names = sum(1 for held_qty in holdings.values() if held_qty > 0)
        if holdings.get(ticker, 0) <= 0 and held_names >= policy.max_names:
            return False
        next_qty = holdings.get(ticker, 0) + qty
        if (next_qty * price) / max(equity, 1.0) > policy.max_single_name:
            return False
        return True

    @staticmethod
    def _in_cooldown(
        ticker: str,
        cycle_idx: int,
        last_order_cycle_by_ticker: dict[str, int],
        policy: ServicePolicyConfig,
    ) -> bool:
        if policy.rebalance_cooldown_bars <= 0:
            return False
        last_cycle = last_order_cycle_by_ticker.get(ticker)
        if last_cycle is None:
            return False
        return cycle_idx - int(last_cycle) < policy.rebalance_cooldown_bars

    @staticmethod
    def _can_add_turnover(
        day_key: str,
        gross_notional: float,
        daily_turnover_notional: dict[str, float],
        daily_start_equity: dict[str, float],
        policy: ServicePolicyConfig,
    ) -> bool:
        if not policy.turnover_budget_hard_stop:
            return True
        start_equity = float(daily_start_equity.get(day_key, policy.initial_capital))
        projected = float(daily_turnover_notional.get(day_key, 0.0)) + float(gross_notional)
        return projected / max(start_equity, 1.0) <= policy.daily_turnover_cap + 1e-12

    @staticmethod
    def _execute_order(
        *,
        side: str,
        ticker: str,
        qty: int,
        price: float,
        ts: str,
        cash: float,
        holdings: dict[str, int],
        policy: ServicePolicyConfig,
    ) -> dict[str, Any]:
        holding_before = int(holdings.get(ticker, 0))
        gross_notional = float(qty * price)
        cost_amount = gross_notional * policy.total_cost_rate
        if side == "buy":
            cash_after = cash - gross_notional - cost_amount
            holdings[ticker] = holding_before + qty
        elif side == "sell":
            if holding_before <= 0:
                raise DataUnavailable(f"naked short blocked but reached executor: {ticker}")
            qty = min(qty, holding_before)
            gross_notional = float(qty * price)
            cost_amount = gross_notional * policy.total_cost_rate
            cash_after = cash + gross_notional - cost_amount
            remaining = holding_before - qty
            if remaining > 0:
                holdings[ticker] = remaining
            else:
                holdings.pop(ticker, None)
        else:
            raise DataUnavailable(f"unknown order side: {side}")

        return {
            "ts": ts,
            "ticker": ticker,
            "side": side,
            "qty": qty,
            "price": price,
            "gross_notional": gross_notional,
            "cost_amount": cost_amount,
            "total_cost_bps": policy.total_cost_bps,
            "holding_before": holding_before,
            "holding_after": int(holdings.get(ticker, 0)),
            "_cash_after": cash_after,
        }

    @staticmethod
    def _equity(cash: float, holdings: dict[str, int], latest_prices: dict[str, float]) -> float:
        mark_to_market = sum(
            int(qty) * float(latest_prices.get(ticker, 0.0))
            for ticker, qty in holdings.items()
        )
        return cash + mark_to_market

    @staticmethod
    def _daily_turnover(
        turnover_notional: dict[str, float],
        daily_start_equity: dict[str, float],
    ) -> dict[str, float]:
        return {
            day: float(turnover_notional.get(day, 0.0)) / max(float(start_equity), 1.0)
            for day, start_equity in sorted(daily_start_equity.items())
        }

    @classmethod
    def _metrics(
        cls,
        policy: ServicePolicyConfig,
        daily_equity: dict[str, float],
    ) -> dict[str, float]:
        if not daily_equity:
            return {
                "initial_capital": policy.initial_capital,
                "final_equity": policy.initial_capital,
                "total_return": 0.0,
                "total_return_bps": 0.0,
                "arr": 0.0,
                "sr": 0.0,
                "mdd": 0.0,
                "days": 0,
            }

        ordered_equity = [float(v) for _, v in sorted(daily_equity.items())]
        final_equity = ordered_equity[-1]
        total_return = final_equity / max(policy.initial_capital, 1.0) - 1.0
        days = len(ordered_equity)
        if total_return <= -0.999999999:
            arr = -1.0
        else:
            arr = (1.0 + total_return) ** (
                policy.annualization_factor / max(days, 1)
            ) - 1.0

        daily_returns: list[float] = []
        previous = policy.initial_capital
        for equity in ordered_equity:
            daily_returns.append(equity / max(previous, 1.0) - 1.0)
            previous = equity
        mean_ret = sum(daily_returns) / max(len(daily_returns), 1)
        std_ret = cls._std(daily_returns)
        sr = mean_ret / max(std_ret, policy.min_daily_return_std) * math.sqrt(
            policy.annualization_factor
        )
        mdd = cls._max_drawdown([policy.initial_capital, *ordered_equity])

        return {
            "initial_capital": policy.initial_capital,
            "final_equity": final_equity,
            "total_return": total_return,
            "total_return_bps": total_return * 10_000.0,
            "arr": arr,
            "sr": sr,
            "mdd": mdd,
            "days": days,
        }

    @staticmethod
    def _gate_blockers(
        *,
        metrics: dict[str, float],
        policy: ServicePolicyConfig,
        max_daily_turnover: float,
        policy_checks: dict[str, Any],
    ) -> list[str]:
        blockers: list[str] = []
        if not safe_bool(policy_checks.get("no_naked_short_exposure"), default=False):
            blockers.append("naked_short_exposure")
        if not safe_bool(policy_checks.get("order_caps_respected"), default=False):
            blockers.append("order_cap_violation")
        if not safe_bool(policy_checks.get("cash_guard_respected"), default=False):
            blockers.append("cash_guard_violation")
        if max_daily_turnover > policy.daily_turnover_cap:
            blockers.append("daily_turnover_cap_violation")
        if float(metrics.get("total_return_bps", 0.0)) <= policy.min_material_return_bps:
            blockers.append("non_positive_or_immaterial_total_return")
        if float(metrics.get("sr", 0.0)) <= policy.min_service_policy_sharpe:
            blockers.append("service_policy_sharpe_below_threshold")
        return blockers

    @staticmethod
    def _std(values: list[float]) -> float:
        if len(values) <= 1:
            return 0.0
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        return math.sqrt(max(var, 0.0))

    @staticmethod
    def _max_drawdown(equity_curve: list[float]) -> float:
        peak = equity_curve[0] if equity_curve else 1.0
        max_dd = 0.0
        for equity in equity_curve:
            peak = max(peak, equity)
            dd = equity / max(peak, 1.0) - 1.0
            max_dd = min(max_dd, dd)
        return max_dd

    @classmethod
    def _pearsonr(cls, x: list[float], y: list[float]) -> float:
        if len(x) != len(y) or len(x) < 2:
            return 0.0
        mean_x = sum(x) / len(x)
        mean_y = sum(y) / len(y)
        cov = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y))
        den = math.sqrt(
            sum((a - mean_x) ** 2 for a in x) * sum((b - mean_y) ** 2 for b in y)
        )
        return cov / den if den > 0 else 0.0

    @classmethod
    def _spearmanr(cls, x: list[float], y: list[float]) -> float:
        if len(x) != len(y) or len(x) < 2:
            return 0.0
        return cls._pearsonr(cls._ranks(x), cls._ranks(y))

    @staticmethod
    def _ranks(values: list[float]) -> list[float]:
        indexed = sorted(enumerate(values), key=lambda item: item[1])
        ranks = [0.0] * len(values)
        i = 0
        while i < len(indexed):
            j = i + 1
            while j < len(indexed) and indexed[j][1] == indexed[i][1]:
                j += 1
            avg_rank = (i + j - 1) / 2.0 + 1.0
            for k in range(i, j):
                ranks[indexed[k][0]] = avg_rank
            i = j
        return ranks

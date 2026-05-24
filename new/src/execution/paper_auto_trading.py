"""KIS virtual 모의투자 자동매매 루프.

Pre-live gate를 통과한 뒤 Hot Path 산출물을 ExecutionGateway(paper)에 연결한다.
실계좌 주문은 항상 차단하고, KIS_MODE=virtual 환경에서만 동작한다.
"""
from __future__ import annotations

import json
import hashlib
import math
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.connectors.kis_rest import KISRestClient
from src.agents.hot.quant import QuantAgent
from src.data.dual_source_runner import (
    DEFAULT_DUAL_SOURCE_ARTIFACT_DIR,
    load_latest_scores,
)
from src.execution.execution_gateway import ExecutionGateway
from src.execution.kill_switch import KillSwitch
from src.models.ppo_allocator import PPOAllocator, PolicyNotLoadedError
from src.orchestration.hot_runner import HotRunner
from src.ops.audit_logger import AuditLogger
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.safe_cast import safe_bool, safe_float, safe_int, safe_lossless_int
from src.utils.ticker_utils import is_valid_ticker, pad_ticker
from src.utils.trading_calendar import is_kospi_trading_day

logger = get_logger("paper_auto_trading")
_KST = ZoneInfo("Asia/Seoul")
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _quant_actionability_float(key: str, default: float) -> float:
    try:
        cfg = config_load("risk_config.yaml", "quant_agent") or {}
        actionability = cfg.get("actionability") if isinstance(cfg, dict) else {}
        raw = actionability.get(key, default) if isinstance(actionability, dict) else default
        parsed = float(raw)
    except Exception as e:
        logger.warning("[paper_auto_trading] quant actionability 설정 로드 실패(%s): %s", key, e)
        return default
    return parsed if math.isfinite(parsed) and parsed >= 0.0 else default


_SCORE_EPSILON = _quant_actionability_float("score_epsilon", 1e-12)
_POSITION_WEIGHT_EPSILON = _quant_actionability_float("position_weight_epsilon", 1e-12)


class PaperAutoTradingError(RuntimeError):
    """paper auto trading readiness 실패."""


class PaperAutoTrader:
    """Hot Path 1분 루프를 KIS 모의투자 주문 경로에 연결한다."""

    def __init__(
        self,
        kis_client: Any | None = None,
        hot_runner: HotRunner | None = None,
        report_dir: Path | str | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        now_fn: Callable[[], datetime] | None = None,
        required_bundle_id: str | None = None,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self._cfg = config_load("risk_config.yaml", "paper_auto_trading")
        default_report_dir = Path(str(self._cfg["report_dir"]))
        if not default_report_dir.is_absolute():
            default_report_dir = _PROJECT_ROOT / default_report_dir

        self._kis_client = kis_client or KISRestClient()
        self._hot_runner = hot_runner or HotRunner(
            quant=QuantAgent(dual_source_loader=load_latest_scores),
            ppo=self._make_ppo_allocator(),
        )
        self._report_dir = Path(report_dir) if report_dir else default_report_dir
        self._report_dir.mkdir(parents=True, exist_ok=True)
        self._audit_logger = AuditLogger(
            log_path=self._report_dir / "paper_auto_execution_audit.jsonl"
        )
        self._sleep = sleep_fn or time.sleep
        self._now = now_fn or (lambda: datetime.now(_KST))
        self._required_bundle_id = str(required_bundle_id or "").strip() or None
        self._kill_switch = kill_switch or KillSwitch()

        self._confirm_start_phrase = str(self._cfg["confirm_start_phrase"])
        self._enforce_market_session = safe_bool(
            self._cfg.get("enforce_market_session", True),
            default=True,
        )
        self._market_open_time = self._parse_hhmm(
            self._cfg.get("market_open_time", "09:00"),
            default=dt_time(9, 0),
        )
        self._market_close_time = self._parse_hhmm(
            self._cfg.get("market_close_time", "15:30"),
            default=dt_time(15, 30),
        )
        self._require_virtual_mode = safe_bool(
            self._cfg.get("require_virtual_mode", True),
            default=True,
        )
        self._require_active_model = safe_bool(
            self._cfg.get("require_active_model", True),
            default=True,
        )
        self._max_orders_per_cycle = safe_int(
            self._cfg["max_orders_per_cycle"],
            default=1,
            min_value=1,
        )
        self._max_order_qty_per_order = safe_int(
            self._cfg["max_order_qty_per_order"],
            default=1,
            min_value=1,
        )
        self._allow_market_order = safe_bool(
            self._cfg.get("allow_market_order", False),
            default=False,
        )
        self._run_guard_passed = False

    @property
    def confirm_start_phrase(self) -> str:
        return self._confirm_start_phrase

    def run(
        self,
        *,
        tickers: list[str],
        cycles: int,
        interval_sec: float,
        confirm_phrase: str | None,
        write_report: bool = True,
    ) -> dict[str, Any]:
        """지정 ticker universe에 대해 paper auto cycle을 실행한다."""
        report = self._base_report(tickers=tickers, cycles=cycles, interval_sec=interval_sec)

        start_guard = self._start_guard(confirm_phrase)
        report["stages"]["start_guard"] = start_guard
        if start_guard["status"] != "PASS":
            report["status"] = "SKIP" if start_guard.get("safe_skip") else "FAIL"
            return self._finish_report(report, write_report)

        market_session_guard = self._market_session_check()
        report["stages"]["market_session_guard"] = market_session_guard
        if market_session_guard["status"] != "PASS":
            report["status"] = (
                "SKIP" if market_session_guard.get("safe_skip") else "FAIL"
            )
            return self._finish_report(report, write_report)

        mode_guard = self._paper_mode_check()
        report["stages"]["mode_guard"] = mode_guard
        if mode_guard["status"] != "PASS":
            report["status"] = "FAIL"
            return self._finish_report(report, write_report)

        model_guard = self._active_model_check()
        report["stages"]["active_model_guard"] = model_guard
        if model_guard["status"] != "PASS":
            report["status"] = "FAIL"
            return self._finish_report(report, write_report)

        feature_guard = self._serving_feature_readiness_check(tickers)
        report["stages"]["serving_feature_readiness"] = feature_guard
        if feature_guard["status"] != "PASS":
            report["status"] = "FAIL"
            return self._finish_report(report, write_report)

        cycles_int = safe_int(cycles, default=0, min_value=0)
        if cycles_int < 1:
            report["stages"]["cycles"] = {
                "status": "FAIL",
                "reason": "cycles_must_be_positive",
                "requested_cycles": cycles,
                "cycles": cycles_int,
                "items": [],
            }
            report["status"] = "FAIL"
            return self._finish_report(report, write_report)

        cycle_reports: list[dict[str, Any]] = []
        self._run_guard_passed = True
        try:
            try:
                if getattr(self._hot_runner, "state", None).value != "HOT_RUNNING":
                    self._hot_runner.start()
            except AttributeError:
                self._hot_runner.start()

            for idx in range(cycles_int):
                cycle = self.run_once(tickers=tickers, cycle_index=idx)
                cycle_reports.append(cycle)
                if cycle.get("status") == "FAIL":
                    break
                if idx < cycles_int - 1:
                    self._sleep(safe_float(interval_sec, default=0.0, min_value=0.0))
        finally:
            self._run_guard_passed = False

        report["stages"]["cycles"] = {
            "status": "PASS" if all(c.get("status") != "FAIL" for c in cycle_reports) else "FAIL",
            "items": cycle_reports,
            "summary": self._cycle_summary(cycle_reports),
        }
        report["status"] = self._overall_status(report)
        return self._finish_report(report, write_report)

    def run_once(self, *, tickers: list[str], cycle_index: int = 0) -> dict[str, Any]:
        started_at = datetime.now(_KST).isoformat()
        if not self._run_guard_passed:
            return {
                "status": "FAIL",
                "cycle_index": cycle_index,
                "started_at": started_at,
                "reason": "run_once_requires_start_guard",
            }
        market_session_guard = self._market_session_check()
        if market_session_guard["status"] != "PASS":
            return {
                "status": "PASS" if market_session_guard.get("safe_skip") else "FAIL",
                "cycle_index": cycle_index,
                "started_at": started_at,
                "market_session_guard": market_session_guard,
                "execution": None,
            }
        padded = [pad_ticker(str(t)) for t in tickers]
        balance = self._kis_client.get_balance()
        bars_by_ticker = self._fetch_recent_bars(padded)
        latest_prices = self._latest_prices(bars_by_ticker)
        portfolio_value = self._portfolio_value(balance)
        current_positions = self._current_positions(
            balance.get("positions", []),
            latest_prices,
            portfolio_value,
        )

        bars_batch = [
            bar
            for ticker in padded
            for bar in bars_by_ticker.get(ticker, [])
        ]
        hot_result = self._hot_runner.run_once(
            tickers=padded,
            bars_batch=bars_batch,
            current_positions=current_positions,
            latest_prices=latest_prices,
            portfolio_value=portfolio_value,
            asof=started_at,
            recent_bars=bars_by_ticker,
            dependency_status={
                "news": "skipped",
                "risk": "done",
                "quant": "done",
                "debate": "skipped",
            },
        )

        if hot_result.get("skipped"):
            return {
                "status": "FAIL",
                "cycle_index": cycle_index,
                "started_at": started_at,
                "reason": hot_result.get("reason", "hot_runner_skipped"),
                "hot_result": hot_result,
            }

        quant_guard = self._quant_output_guard(
            hot_result,
            requested_tickers=padded,
            current_positions=current_positions,
        )
        if quant_guard.get("status") != "PASS":
            return {
                "status": "FAIL",
                "cycle_index": cycle_index,
                "started_at": started_at,
                "reason": "quant_output_not_actionable",
                "quant_output_guard": quant_guard,
                "hot_result": hot_result,
                "execution": None,
            }

        final_decision = dict(hot_result.get("final_decision") or {})
        final_decision["order_deltas"] = [
            dict(od) for od in list(final_decision.get("order_deltas", []))
        ]
        order_guard = self._order_guard(final_decision)
        if order_guard["status"] != "PASS":
            return {
                "status": "PASS" if order_guard.get("safe_skip") else "FAIL",
                "cycle_index": cycle_index,
                "started_at": started_at,
                "order_guard": order_guard,
                "hot_result": hot_result,
                "execution": None,
            }

        gateway = ExecutionGateway(
            kill_switch=self._kill_switch,
            audit_logger=self._audit_logger,
            kis_client=self._kis_client,
            mode_override="paper",
            live_enabled_override=False,
        )
        execution = gateway.execute(final_decision)
        execution_report = execution.get("execution_report", {})
        execution_status = execution_report.get("status")
        broker_blockers = self._broker_rejection_blockers(execution_report)
        ok_statuses = {"submitted", "filled", "partial_filled"}
        order_history = self._order_history_verification(execution)
        status = "PASS" if execution_status in ok_statuses and not broker_blockers else "FAIL"
        if order_history.get("status") != "PASS":
            status = "FAIL"
        return {
            "status": status,
            "cycle_index": cycle_index,
            "started_at": started_at,
            "portfolio_value": portfolio_value,
            "n_bars": len(bars_batch),
            "order_guard": order_guard,
            "hot_result": hot_result,
            "execution": execution,
            "broker_blockers": broker_blockers,
            "order_history_verification": order_history,
        }

    def _make_ppo_allocator(self) -> PPOAllocator:
        if not safe_bool(
            self._cfg.get("use_latest_ppo_policy_if_available", True),
            default=True,
        ):
            return PPOAllocator()
        ppo_cfg = config_load("risk_config.yaml", "nightly_ppo_retrainer") or {}
        artifacts_path = Path(str(ppo_cfg.get("artifacts_path", "artifacts/ppo")))
        if not artifacts_path.is_absolute():
            artifacts_path = _PROJECT_ROOT / artifacts_path
        policy_path = artifacts_path / "latest_policy.pkl"
        if not policy_path.exists():
            return PPOAllocator()
        try:
            return PPOAllocator(policy_path=policy_path)
        except PolicyNotLoadedError as e:
            logger.warning("[paper_auto_trading] PPO policy 로드 실패, heuristic 유지: %s", e)
            return PPOAllocator()

    def _fetch_recent_bars(self, tickers: list[str]) -> dict[str, list[dict[str, Any]]]:
        warmup = int(config_load("risk_config.yaml", "quant_agent")["warmup_bars"])
        out: dict[str, list[dict[str, Any]]] = {}
        for ticker in tickers:
            bars = self._kis_client.inquire_minute_bar(ticker, n_bars=warmup)
            out[ticker] = list(bars)
        return out

    @staticmethod
    def _latest_prices(bars_by_ticker: dict[str, list[dict[str, Any]]]) -> dict[str, float]:
        prices: dict[str, float] = {}
        for ticker, bars in bars_by_ticker.items():
            if bars:
                prices[ticker] = safe_float(bars[-1].get("close", 0.0), default=0.0)
        return prices

    @staticmethod
    def _portfolio_value(balance: dict[str, Any]) -> float:
        bal = balance.get("balance") if isinstance(balance.get("balance"), dict) else {}
        for key in ("net_asset", "total_eval", "cash"):
            value = safe_float(bal.get(key, 0.0), default=0.0)
            if value > 0:
                return value
        return 0.0

    @staticmethod
    def _current_positions(
        positions: list[dict[str, Any]],
        latest_prices: dict[str, float],
        portfolio_value: float,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for pos in positions:
            ticker = pad_ticker(str(pos.get("ticker", "")))
            if ticker == "000000":
                continue
            qty = safe_lossless_int(pos.get("qty", 0), default=0, min_value=0)
            if qty <= 0:
                continue
            price = latest_prices.get(
                ticker,
                safe_float(pos.get("current_price", 0.0), default=0.0),
            )
            weight = (qty * price / portfolio_value) if portfolio_value > 0 and price > 0 else 0.0
            out.append({"ticker": ticker, "qty": qty, "weight": float(weight)})
        return out

    def _start_guard(self, confirm_phrase: str | None) -> dict[str, Any]:
        if confirm_phrase != self._confirm_start_phrase:
            return {
                "status": "SKIP",
                "safe_skip": True,
                "reason": "confirm_phrase_missing_or_mismatch",
                "required_phrase": self._confirm_start_phrase,
            }
        return {"status": "PASS"}

    def _market_session_check(self) -> dict[str, Any]:
        if not self._enforce_market_session:
            return {"status": "PASS", "enforced": False}
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=_KST)
        else:
            now = now.astimezone(_KST)
        if not is_kospi_trading_day(now.date()):
            return {
                "status": "SKIP",
                "safe_skip": True,
                "reason": "not_kospi_trading_day",
                "now": now.isoformat(),
            }
        now_time = now.time().replace(tzinfo=None)
        if now_time < self._market_open_time or now_time > self._market_close_time:
            return {
                "status": "SKIP",
                "safe_skip": True,
                "reason": "outside_market_session",
                "now": now.isoformat(),
                "market_open_time": self._market_open_time.strftime("%H:%M"),
                "market_close_time": self._market_close_time.strftime("%H:%M"),
            }
        return {
            "status": "PASS",
            "enforced": True,
            "now": now.isoformat(),
            "market_open_time": self._market_open_time.strftime("%H:%M"),
            "market_close_time": self._market_close_time.strftime("%H:%M"),
        }

    def _paper_mode_check(self) -> dict[str, Any]:
        mode = str(getattr(self._kis_client, "mode", "unknown")).lower()
        if self._require_virtual_mode and mode != "virtual":
            return {
                "status": "FAIL",
                "error_code": "PAPER_MODE_REQUIRED",
                "current_mode": mode,
            }
        return {"status": "PASS", "current_mode": mode}

    def _active_model_check(self) -> dict[str, Any]:
        quant = getattr(self._hot_runner, "_quant", None)
        has_model = safe_bool(getattr(quant, "has_model", False), default=False)
        metadata = getattr(quant, "model_metadata", None)
        bundle_id = (
            (metadata or {}).get("bundle_id")
            if isinstance(metadata, dict)
            else None
        )
        if self._require_active_model and (not has_model or not bundle_id):
            return {
                "status": "FAIL",
                "error_code": "ACTIVE_MODEL_REQUIRED",
                "message": "QuantAgent active model이 없어 paper auto trading을 시작할 수 없다.",
                "has_model": has_model,
                "bundle_id": bundle_id,
            }
        if self._required_bundle_id and bundle_id != self._required_bundle_id:
            return {
                "status": "FAIL",
                "error_code": "ACTIVE_MODEL_BUNDLE_MISMATCH",
                "message": "QuantAgent active model bundle_id가 요청 bundle_id와 다르다.",
                "has_model": has_model,
                "bundle_id": bundle_id,
                "required_bundle_id": self._required_bundle_id,
            }
        return {
            "status": "PASS",
            "has_model": has_model,
            "model_version": (metadata or {}).get("version") if isinstance(metadata, dict) else None,
            "bundle_id": bundle_id,
            "required_bundle_id": self._required_bundle_id,
        }

    def _serving_feature_readiness_check(self, tickers: list[str]) -> dict[str, Any]:
        quant = getattr(self._hot_runner, "_quant", None)
        required_cols = self._required_dual_source_cols_for_quant(quant)
        padded = [pad_ticker(str(t)) for t in tickers]
        if not required_cols:
            return {
                "status": "PASS",
                "required": False,
                "required_feature_cols": [],
            }

        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=_KST)
        else:
            now = now.astimezone(_KST)
        date_key = now.strftime("%Y%m%d")

        artifact_path = DEFAULT_DUAL_SOURCE_ARTIFACT_DIR / f"{date_key}.json"
        artifact_label = self._display_path(artifact_path)
        if not artifact_path.exists():
            return {
                "status": "FAIL",
                "reason": "required_feature_artifact_missing",
                "date_key": date_key,
                "artifact": artifact_label,
                "required_feature_cols": required_cols,
                "missing_artifacts": [artifact_label],
            }
        artifact_sha256 = self._file_sha256(artifact_path)

        rows = load_latest_scores(
            date_key,
            artifact_dir=DEFAULT_DUAL_SOURCE_ARTIFACT_DIR,
        )
        row_by_ticker = {
            pad_ticker(str(row.get("ticker", ""))): row
            for row in rows
            if isinstance(row, dict)
        }
        missing_tickers: list[str] = []
        missing_cols: dict[str, list[str]] = {}
        future_rows: list[dict[str, str]] = []
        invalid_cols: dict[str, list[str]] = {}
        missing_timestamp_tickers: list[str] = []
        date_mismatch_rows: list[dict[str, str]] = []

        for ticker in padded:
            row = row_by_ticker.get(ticker)
            if not row:
                missing_tickers.append(ticker)
                continue
            batch_date = row.get("batch_date")
            if batch_date in (None, ""):
                missing_timestamp_tickers.append(ticker)
            else:
                try:
                    batch_date_key = self._feature_date_key(batch_date)
                except (TypeError, ValueError):
                    date_mismatch_rows.append({
                        "ticker": ticker,
                        "field": "batch_date",
                        "value": str(batch_date),
                        "reason": "batch_date_parse_failed",
                    })
                else:
                    if batch_date_key != date_key:
                        date_mismatch_rows.append({
                            "ticker": ticker,
                            "field": "batch_date",
                            "value": str(batch_date),
                            "reason": "feature_artifact_date_mismatch",
                        })
            if row.get("snapshot_ts") in (None, ""):
                missing_timestamp_tickers.append(ticker)
            if row.get("generated_at") in (None, ""):
                missing_timestamp_tickers.append(ticker)
            for key in ("snapshot_ts", "generated_at"):
                raw_ts = row.get(key)
                try:
                    ts = self._parse_feature_ts(raw_ts)
                except (TypeError, ValueError):
                    future_rows.append({
                        "ticker": ticker,
                        "field": key,
                        "value": str(raw_ts),
                        "reason": "timestamp_parse_failed",
                    })
                    continue
                if ts.strftime("%Y%m%d") != date_key:
                    date_mismatch_rows.append({
                        "ticker": ticker,
                        "field": key,
                        "value": ts.isoformat(),
                        "reason": "feature_artifact_date_mismatch",
                    })
                if ts > now:
                    future_rows.append({
                        "ticker": ticker,
                        "field": key,
                        "value": ts.isoformat(),
                        "reason": "future_feature_artifact",
                    })
            for col in required_cols:
                if col not in row:
                    missing_cols.setdefault(ticker, []).append(col)
                    continue
                value = self._float_or_none(row.get(col))
                if value is None:
                    invalid_cols.setdefault(ticker, []).append(col)

        missing_timestamp_tickers = sorted(set(missing_timestamp_tickers))

        if (
            missing_tickers
            or missing_cols
            or future_rows
            or invalid_cols
            or missing_timestamp_tickers
            or date_mismatch_rows
        ):
            return {
                "status": "FAIL",
                "reason": "required_feature_artifact_not_ready",
                "date_key": date_key,
                "artifact": artifact_label,
                "artifact_sha256": artifact_sha256,
                "required_feature_cols": required_cols,
                "missing_tickers": missing_tickers,
                "missing_feature_cols_by_ticker": missing_cols,
                "invalid_feature_cols_by_ticker": invalid_cols,
                "missing_timestamp_tickers": missing_timestamp_tickers,
                "future_rows": future_rows,
                "date_mismatch_rows": date_mismatch_rows,
            }

        return {
            "status": "PASS",
            "required": True,
            "date_key": date_key,
            "artifact": artifact_label,
            "artifact_sha256": artifact_sha256,
            "required_feature_cols": required_cols,
            "ticker_count": len(padded),
            "matched_ticker_count": len(padded) - len(missing_tickers),
        }

    @staticmethod
    def _required_dual_source_cols_for_quant(quant: Any) -> list[str]:
        method = getattr(quant, "_required_dual_source_cols", None)
        if callable(method):
            return sorted(str(col) for col in method())
        metadata = getattr(quant, "model_metadata", None)
        feature_cols = (
            metadata.get("feature_cols", [])
            if isinstance(metadata, dict)
            else []
        )
        pp_cfg = config_load("risk_config.yaml", "preprocessor")
        dual_source_cols = set(pp_cfg.get("dual_source_feature_cols", []))
        return sorted(str(col) for col in feature_cols if col in dual_source_cols)

    @staticmethod
    def _parse_feature_ts(value: Any) -> datetime:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=_KST)
        return dt.astimezone(_KST)

    @staticmethod
    def _feature_date_key(value: Any) -> str:
        dt = PaperAutoTrader._parse_feature_ts(value)
        return dt.strftime("%Y%m%d")

    @staticmethod
    def _display_path(path: Path) -> str:
        try:
            return str(path.relative_to(_PROJECT_ROOT))
        except ValueError:
            return str(path)

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed):
            return None
        return parsed

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _order_guard(self, final_decision: dict[str, Any]) -> dict[str, Any]:
        if not safe_bool(final_decision.get("approved"), default=False):
            return {
                "status": "SKIP",
                "safe_skip": True,
                "reason": "fda_veto",
                "reason_code": final_decision.get("reason_code"),
            }
        raw_order_deltas = final_decision.get("order_deltas", [])
        if not isinstance(raw_order_deltas, list):
            return {
                "status": "FAIL",
                "reason": "order_deltas_must_be_list",
                "type": type(raw_order_deltas).__name__,
            }
        order_deltas = list(raw_order_deltas)
        if not order_deltas:
            return {
                "status": "SKIP",
                "safe_skip": True,
                "reason": "no_order_deltas",
            }
        if len(order_deltas) > self._max_orders_per_cycle:
            return {
                "status": "FAIL",
                "reason": "too_many_orders",
                "n_orders": len(order_deltas),
                "max_orders_per_cycle": self._max_orders_per_cycle,
            }
        violations: list[dict[str, Any]] = []
        for od in order_deltas:
            if not isinstance(od, dict):
                violations.append({
                    "ticker": None,
                    "reason": "order_delta_must_be_dict",
                    "type": type(od).__name__,
                })
                continue
            ticker = pad_ticker(str(od.get("ticker", "")))
            side = str(od.get("side", "")).lower()
            qty = safe_lossless_int(od.get("qty", 0), default=0)
            order_type = str(od.get("order_type", "00") or "00")
            price = safe_float(od.get("price", 0.0), default=0.0)
            if ticker == "000000" or not is_valid_ticker(ticker):
                violations.append({
                    "ticker": ticker,
                    "reason": "invalid_ticker",
                })
            if side not in {"buy", "sell"}:
                violations.append({
                    "ticker": ticker,
                    "reason": "side_must_be_buy_or_sell",
                    "side": side,
                })
            if qty <= 0:
                violations.append({
                    "ticker": ticker,
                    "reason": "qty_out_of_limit",
                    "qty": qty,
                    "max_order_qty_per_order": self._max_order_qty_per_order,
                })
            elif qty > self._max_order_qty_per_order:
                violations.append({
                    "ticker": ticker,
                    "reason": "qty_out_of_limit",
                    "qty": qty,
                    "max_order_qty_per_order": self._max_order_qty_per_order,
                })
            if order_type == "01" and not self._allow_market_order:
                violations.append({
                    "ticker": ticker,
                    "reason": "market_order_not_allowed",
                })
            if order_type != "01" and price <= 0:
                violations.append({
                    "ticker": ticker,
                    "reason": "positive_price_required",
                    "price": price,
                })
        if violations:
            return {"status": "FAIL", "reason": "order_guard_violations", "violations": violations}
        return {"status": "PASS", "n_orders": len(order_deltas)}

    @staticmethod
    def _quant_output_guard(
        hot_result: dict[str, Any],
        *,
        requested_tickers: list[str] | None = None,
        current_positions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        quant_output = hot_result.get("quant_output")
        if not isinstance(quant_output, dict):
            return {"status": "FAIL", "checked": False, "reason": "quant_output_missing"}
        mode = str(quant_output.get("mode") or "").lower()
        scores = quant_output.get("scores")
        score_stats = PaperAutoTrader._score_stats(scores)
        requested = {
            pad_ticker(str(ticker))
            for ticker in (requested_tickers or [])
            if str(ticker).strip()
        }
        held = {
            pad_ticker(str(pos.get("ticker", "")))
            for pos in (current_positions or [])
            if isinstance(pos, dict)
            and str(pos.get("ticker", "")).strip()
            and pad_ticker(str(pos.get("ticker", ""))) != "000000"
            and PaperAutoTrader._position_has_exposure(pos)
        }
        output_tickers = set(score_stats["finite_score_tickers"])
        missing_requested = sorted(requested - output_tickers)
        missing_held = sorted(held - output_tickers)
        base = {
            "checked": True,
            "mode": mode,
            **score_stats,
            "blocker": quant_output.get("blocker"),
            "missing_requested_tickers": missing_requested,
            "missing_held_tickers": missing_held,
        }
        if mode == "blocked" or quant_output.get("blocker"):
            return {
                "status": "FAIL",
                "reason": "quant_blocked",
                **base,
            }
        if mode != "active":
            return {
                "status": "FAIL",
                "reason": "quant_not_active",
                **base,
            }
        if score_stats["finite_score_count"] < 1:
            return {
                "status": "FAIL",
                "reason": "quant_scores_empty",
                **base,
            }
        if score_stats["invalid_score_tickers"]:
            return {
                "status": "FAIL",
                "reason": "quant_scores_invalid",
                **base,
            }
        if score_stats["nonzero_score_count"] < 1:
            return {
                "status": "FAIL",
                "reason": "quant_scores_all_zero",
                **base,
            }
        if not score_stats["rankable"]:
            return {
                "status": "FAIL",
                "reason": "quant_scores_not_rankable",
                **base,
            }
        if missing_requested or missing_held:
            return {
                "status": "FAIL",
                "reason": "quant_ticker_coverage_incomplete",
                **base,
            }
        return {"status": "PASS", **base}

    @staticmethod
    def _score_stats(scores: Any) -> dict[str, Any]:
        if not isinstance(scores, dict):
            return {
                "score_count": 0,
                "finite_score_count": 0,
                "nonzero_score_count": 0,
                "score_abs_sum": 0.0,
                "score_std": 0.0,
                "rankable": False,
                "finite_score_tickers": [],
                "invalid_score_tickers": [],
            }
        values: list[float] = []
        finite_score_tickers: list[str] = []
        invalid_score_tickers: list[str] = []
        for ticker, value in scores.items():
            padded = pad_ticker(str(ticker))
            try:
                parsed = float(value)
            except Exception:
                invalid_score_tickers.append(padded)
                continue
            if math.isfinite(parsed):
                values.append(parsed)
                finite_score_tickers.append(padded)
            else:
                invalid_score_tickers.append(padded)
        nonzero = [value for value in values if abs(value) > _SCORE_EPSILON]
        if len(values) > 1:
            mean = sum(values) / len(values)
            variance = sum((value - mean) ** 2 for value in values) / len(values)
            std = math.sqrt(variance)
        else:
            std = 0.0
        return {
            "score_count": len(scores),
            "finite_score_count": len(values),
            "nonzero_score_count": len(nonzero),
            "score_abs_sum": float(sum(abs(value) for value in values)),
            "score_std": float(std),
            "rankable": bool(len(values) == 1 and nonzero)
            or bool(len(values) > 1 and std > _SCORE_EPSILON),
            "finite_score_tickers": sorted(set(finite_score_tickers)),
            "invalid_score_tickers": sorted(set(invalid_score_tickers)),
        }

    @staticmethod
    def _position_has_exposure(position: dict[str, Any]) -> bool:
        try:
            qty = float(position.get("qty", 0.0))
        except Exception:
            qty = 0.0
        try:
            weight = float(position.get("weight", 0.0))
        except Exception:
            weight = 0.0
        return qty > 0.0 or weight > _POSITION_WEIGHT_EPSILON

    @staticmethod
    def _cycle_summary(cycles: list[dict[str, Any]]) -> dict[str, Any]:
        status_counts: dict[str, int] = {}
        quant_guard_failures: dict[str, int] = {}
        quant_blockers: dict[str, int] = {}
        quant_modes: dict[str, int] = {}
        score_nonempty_cycles = 0
        score_rankable_cycles = 0
        order_delta_count = 0
        execution_cycles = 0
        broker_rejection_count = 0
        fill_count = 0

        for cycle in cycles:
            status = str(cycle.get("status") or "UNKNOWN")
            status_counts[status] = status_counts.get(status, 0) + 1
            cycle_blockers: set[str] = set()

            hot_result = cycle.get("hot_result") if isinstance(cycle.get("hot_result"), dict) else {}
            quant_output = hot_result.get("quant_output") if isinstance(hot_result, dict) else {}
            if isinstance(quant_output, dict):
                mode = str(quant_output.get("mode") or "unknown").lower()
                quant_modes[mode] = quant_modes.get(mode, 0) + 1
                stats = PaperAutoTrader._score_stats(quant_output.get("scores"))
                if stats["finite_score_count"] > 0:
                    score_nonempty_cycles += 1
                if stats["rankable"]:
                    score_rankable_cycles += 1
                blocker = quant_output.get("blocker")
                if blocker:
                    cycle_blockers.add(str(blocker))

            quant_guard = (
                cycle.get("quant_output_guard")
                if isinstance(cycle.get("quant_output_guard"), dict)
                else {}
            )
            if quant_guard and quant_guard.get("status") != "PASS":
                reason = str(quant_guard.get("reason") or "unknown")
                quant_guard_failures[reason] = quant_guard_failures.get(reason, 0) + 1
                blocker = quant_guard.get("blocker")
                if blocker:
                    cycle_blockers.add(str(blocker))

            for key in cycle_blockers:
                quant_blockers[key] = quant_blockers.get(key, 0) + 1

            decision = hot_result.get("final_decision") if isinstance(hot_result, dict) else {}
            deltas = decision.get("order_deltas") if isinstance(decision, dict) else []
            if isinstance(deltas, list):
                order_delta_count += len([item for item in deltas if isinstance(item, dict)])

            execution = cycle.get("execution") if isinstance(cycle.get("execution"), dict) else {}
            if execution:
                execution_cycles += 1
                execution_report = (
                    execution.get("execution_report")
                    if isinstance(execution.get("execution_report"), dict)
                    else {}
                )
                rejections = execution_report.get("rejections")
                fills = execution_report.get("fills")
                broker_rejection_count += len(rejections) if isinstance(rejections, list) else 0
                fill_count += len(fills) if isinstance(fills, list) else 0

        return {
            "cycle_count": len(cycles),
            "status_counts": status_counts,
            "quant_modes": quant_modes,
            "quant_guard_failures": quant_guard_failures,
            "quant_blockers": quant_blockers,
            "score_nonempty_cycles": score_nonempty_cycles,
            "score_rankable_cycles": score_rankable_cycles,
            "order_delta_count": order_delta_count,
            "execution_cycles": execution_cycles,
            "broker_rejection_count": broker_rejection_count,
            "fill_count": fill_count,
        }

    def _order_history_verification(self, execution: dict[str, Any]) -> dict[str, Any]:
        if not hasattr(self._kis_client, "get_order_history"):
            return {"status": "FAIL", "reason": "kis_client_no_get_order_history"}
        fills = list(execution.get("execution_report", {}).get("fills", []))
        if not fills:
            return {"status": "FAIL", "reason": "no_broker_fills"}

        queries: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for fill in fills:
            if not isinstance(fill, dict):
                continue
            query = {
                "ticker": pad_ticker(str(fill.get("ticker", ""))),
                "side": str(fill.get("side", "all")).lower(),
                "order_id": str(fill.get("broker_order_id") or ""),
                "execution_filter": "all",
            }
            if not query["order_id"]:
                failures.append({
                    "query": query,
                    "error_code": "BROKER_ORDER_ID_MISSING",
                    "reason": "broker_order_id_missing",
                })
                continue
            try:
                try:
                    history = self._kis_client.get_order_history(**query)
                except TypeError:
                    history = self._kis_client.get_order_history(
                        ticker=query["ticker"],
                        side=query["side"],
                        execution_filter=query["execution_filter"],
                    )
                orders = list(history.get("orders", []))
                matched = self._filter_history_matches(orders, query)
                if not matched:
                    failures.append({
                        "query": query,
                        "error_code": "BROKER_ORDER_ID_NOT_FOUND_IN_HISTORY",
                        "matched_order_count": 0,
                    })
                queries.append({
                    "query": query,
                    "status": "PASS" if matched else "FAIL",
                    "matched_order_count": len(matched),
                    "matched_orders": matched,
                })
            except Exception as e:
                failures.append({"query": query, "error": str(e)})

        return {
            "status": "FAIL" if failures else "PASS",
            "queries": queries,
            "failures": failures,
        }

    @staticmethod
    def _broker_rejection_blockers(execution_report: dict[str, Any]) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        rejections = execution_report.get("rejections", [])
        for rejection in rejections if isinstance(rejections, list) else []:
            if not isinstance(rejection, dict):
                continue
            broker_response = rejection.get("broker_response")
            text = (
                f"{rejection.get('error', '')} {rejection.get('reason', '')} "
                f"{broker_response if isinstance(broker_response, dict) else ''}"
            )
            error_code = "BROKER_REJECTED_ORDER"
            if "msg_cd=40580000" in text or "장종료" in text:
                error_code = "BROKER_MARKET_CLOSED"
            blockers.append({
                "error_code": error_code,
                "ticker": rejection.get("ticker"),
                "side": rejection.get("side"),
                "qty": rejection.get("qty"),
                "reason": rejection.get("reason"),
                "error": rejection.get("error"),
            })
        return blockers

    @staticmethod
    def _filter_history_matches(
        orders: list[dict[str, Any]],
        query: dict[str, str],
    ) -> list[dict[str, Any]]:
        matched: list[dict[str, Any]] = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            order_ids = {
                str(order.get("order_id") or ""),
                str(order.get("broker_order_id") or ""),
                str(order.get("ODNO") or ""),
                str(order.get("odno") or ""),
            }
            if query.get("order_id") and query["order_id"] not in order_ids:
                continue
            if query.get("order_id") and query["order_id"] in order_ids:
                matched.append(order)
                continue
            if pad_ticker(str(order.get("ticker", ""))) != query.get("ticker"):
                continue
            if str(order.get("side", "")).lower() != query.get("side"):
                continue
            matched.append(order)
        return matched

    def _base_report(
        self,
        *,
        tickers: list[str],
        cycles: int,
        interval_sec: float,
    ) -> dict[str, Any]:
        return {
            "status": "PENDING",
            "action": "paper_auto_trade",
            "generated_at": datetime.now(_KST).isoformat(),
            "runtime": {
                "kis_mode": str(getattr(self._kis_client, "mode", "unknown")).lower(),
                "execution_mode": "paper",
                "live_enabled": False,
                "confirm_start_required": True,
            },
            "params": {
                "tickers": [pad_ticker(str(t)) for t in tickers],
                "cycles": int(cycles),
                "interval_sec": float(interval_sec),
                "required_bundle_id": self._required_bundle_id,
            },
            "stages": {},
            "failures": [],
        }

    @staticmethod
    def _parse_hhmm(value: Any, *, default: dt_time) -> dt_time:
        try:
            hour, minute = str(value).strip().split(":", 1)
            return dt_time(int(hour), int(minute))
        except Exception as e:
            logger.warning("[paper_auto_trading] 장중 시간 설정 파싱 실패: %s", e)
            return default

    def _finish_report(self, report: dict[str, Any], write_report: bool) -> dict[str, Any]:
        report["failures"] = self._collect_failures(report)
        if report["status"] == "PENDING":
            report["status"] = self._overall_status(report)
        if write_report:
            report_path = self._write_report(report)
            report["report_path"] = str(report_path)
            try:
                report["report_path_relative"] = str(report_path.relative_to(_PROJECT_ROOT))
            except ValueError:
                report["report_path_relative"] = str(report_path)
        return report

    def _write_report(self, report: dict[str, Any]) -> Path:
        ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
        path = self._report_dir / f"paper_auto_trade_{ts}.json"
        with path.open("w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        logger.info("[paper_auto_trading] report 저장: %s", path)
        return path

    @staticmethod
    def _collect_failures(report: dict[str, Any]) -> list[str]:
        failures: list[str] = []
        for name, stage in report.get("stages", {}).items():
            if isinstance(stage, dict) and stage.get("status") == "FAIL":
                failures.append(str(name))
        return failures

    @staticmethod
    def _overall_status(report: dict[str, Any]) -> str:
        statuses = [
            stage.get("status")
            for stage in report.get("stages", {}).values()
            if isinstance(stage, dict)
        ]
        if any(status == "FAIL" for status in statuses):
            return "FAIL"
        if statuses and all(status == "SKIP" for status in statuses):
            return "SKIP"
        return "PASS"

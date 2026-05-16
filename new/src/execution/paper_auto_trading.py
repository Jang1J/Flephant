"""KIS virtual 모의투자 자동매매 루프.

Pre-live gate를 통과한 뒤 Hot Path 산출물을 ExecutionGateway(paper)에 연결한다.
실계좌 주문은 항상 차단하고, KIS_MODE=virtual 환경에서만 동작한다.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.connectors.kis_rest import KISRestClient
from src.execution.execution_gateway import ExecutionGateway
from src.models.ppo_allocator import PPOAllocator, PolicyNotLoadedError
from src.orchestration.hot_runner import HotRunner
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.safe_cast import safe_bool, safe_float, safe_int, safe_lossless_int
from src.utils.ticker_utils import is_valid_ticker, pad_ticker

logger = get_logger("paper_auto_trading")
_KST = ZoneInfo("Asia/Seoul")
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


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
    ) -> None:
        self._cfg = config_load("risk_config.yaml", "paper_auto_trading")
        default_report_dir = Path(str(self._cfg["report_dir"]))
        if not default_report_dir.is_absolute():
            default_report_dir = _PROJECT_ROOT / default_report_dir

        self._kis_client = kis_client or KISRestClient()
        self._hot_runner = hot_runner or HotRunner(ppo=self._make_ppo_allocator())
        self._report_dir = Path(report_dir) if report_dir else default_report_dir
        self._report_dir.mkdir(parents=True, exist_ok=True)
        self._sleep = sleep_fn or time.sleep

        self._confirm_start_phrase = str(self._cfg["confirm_start_phrase"])
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
        )

        if hot_result.get("skipped"):
            return {
                "status": "FAIL",
                "cycle_index": cycle_index,
                "started_at": started_at,
                "reason": hot_result.get("reason", "hot_runner_skipped"),
                "hot_result": hot_result,
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
        if order_history.get("status") == "FAIL":
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
            qty = safe_int(pos.get("qty", 0), default=0, min_value=0)
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
        has_model = bool(getattr(quant, "has_model", False))
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
        return {
            "status": "PASS",
            "has_model": has_model,
            "model_version": (metadata or {}).get("version") if isinstance(metadata, dict) else None,
            "bundle_id": bundle_id,
        }

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

    def _order_history_verification(self, execution: dict[str, Any]) -> dict[str, Any]:
        if not hasattr(self._kis_client, "get_order_history"):
            return {"status": "SKIP", "reason": "kis_client_no_get_order_history"}
        fills = list(execution.get("execution_report", {}).get("fills", []))
        if not fills:
            return {"status": "SKIP", "reason": "no_broker_fills"}

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
            },
            "stages": {},
            "failures": [],
        }

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

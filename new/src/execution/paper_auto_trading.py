"""KIS virtual 모의투자 자동매매 루프.

Pre-live gate를 통과한 뒤 Hot Path 산출물을 ExecutionGateway(paper)에 연결한다.
실계좌 주문은 항상 차단하고, KIS_MODE=virtual 환경에서만 동작한다.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from src.agents.hot.quant import QuantAgent
from src.connectors.kis_rest import KISAPIError, KISRestClient
from src.data.dual_source_runner import load_latest_scores
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
        track_id: str | None = None,
        policy_hash: str | None = None,
        max_orders_per_cycle: int | None = None,
        max_order_qty_per_order: int | None = None,
        submit_orders: bool = True,
    ) -> None:
        self._cfg = config_load("risk_config.yaml", "paper_auto_trading")
        default_report_dir = Path(str(self._cfg["report_dir"]))
        if not default_report_dir.is_absolute():
            default_report_dir = _PROJECT_ROOT / default_report_dir

        self._kis_client = kis_client or KISRestClient()
        self._hot_runner = hot_runner or self._make_hot_runner()
        self._report_dir = Path(report_dir) if report_dir else default_report_dir
        self._report_dir.mkdir(parents=True, exist_ok=True)
        self._audit_logger = AuditLogger(
            log_path=self._report_dir / "paper_auto_execution_audit.jsonl"
        )
        self._sleep = sleep_fn or time.sleep
        self._now = now_fn or (lambda: datetime.now(_KST))
        self._required_bundle_id = str(required_bundle_id or "").strip() or None
        self._track_id = str(track_id or "").strip()
        self._policy_hash = str(policy_hash or "").strip()
        self._submit_orders = bool(submit_orders)
        self._kill_switch = kill_switch or KillSwitch()
        self._active_trade_universe: set[str] | None = None

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
            max_orders_per_cycle
            if max_orders_per_cycle is not None
            else self._cfg["max_orders_per_cycle"],
            default=1,
            min_value=1,
        )
        self._max_order_qty_per_order = safe_int(
            max_order_qty_per_order
            if max_order_qty_per_order is not None
            else self._cfg["max_order_qty_per_order"],
            default=1,
            min_value=1,
        )
        self._allow_market_order = safe_bool(
            self._cfg.get("allow_market_order", False),
            default=False,
        )
        self._max_consecutive_read_error_skips = safe_int(
            self._cfg.get("max_consecutive_read_error_skips", 3),
            default=3,
            min_value=0,
        )
        self._require_serving_feature_readiness = safe_bool(
            self._cfg.get("require_serving_feature_readiness", True),
            default=True,
        )
        self._fail_on_quant_blocked = safe_bool(
            self._cfg.get("fail_on_quant_blocked", True),
            default=True,
        )
        self._require_active_scores_for_no_order_skip = safe_bool(
            self._cfg.get("require_active_scores_for_no_order_skip", True),
            default=True,
        )
        self._consecutive_read_errors = 0
        self._run_guard_passed = False
        self._last_bar_fetch_metadata: dict[str, Any] = {}

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
        risk_warnings: list[dict[str, Any]] | None = None,
        write_report: bool = True,
    ) -> dict[str, Any]:
        """지정 ticker universe에 대해 paper auto cycle을 실행한다."""
        report = self._base_report(tickers=tickers, cycles=cycles, interval_sec=interval_sec)
        external_risk_warnings = self._normalize_risk_warnings(risk_warnings)
        report["stages"]["cold_path_risk_warnings"] = (
            self._cold_path_risk_warning_stage(external_risk_warnings)
        )

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

        ticker_universe_guard = self._requested_ticker_universe_guard(tickers)
        report["stages"]["requested_ticker_universe_guard"] = ticker_universe_guard
        if ticker_universe_guard["status"] != "PASS":
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
            for idx in range(cycles_int):
                try:
                    cycle = self.run_once(
                        tickers=tickers,
                        cycle_index=idx,
                        risk_warnings=external_risk_warnings,
                    )
                except Exception as e:
                    logger.warning(
                        "[paper_auto_trading] cycle 예외로 fail-closed 처리: cycle=%s error=%s",
                        idx,
                        e,
                    )
                    cycle = self._exception_cycle_report(
                        cycle_index=idx,
                        reason="paper_auto_cycle_exception",
                        error=e,
                        started_at=self._now_kst().isoformat(),
                    )
                if idx == 0 and isinstance(cycle.get("account_state"), dict):
                    report["account_initial_state"] = dict(cycle["account_state"])
                cycle_reports.append(cycle)
                if cycle.get("status") == "FAIL":
                    break
                if idx < cycles_int - 1:
                    self._sleep(safe_float(interval_sec, default=0.0, min_value=0.0))
        except Exception as e:
            logger.warning("[paper_auto_trading] run 예외로 fail-closed 처리: %s", e)
            cycle_reports.append(
                self._exception_cycle_report(
                    cycle_index=len(cycle_reports),
                    reason="paper_auto_run_exception",
                    error=e,
                    started_at=self._now_kst().isoformat(),
                )
            )
        finally:
            self._run_guard_passed = False

        report["stages"]["cycles"] = {
            "status": "PASS" if all(c.get("status") != "FAIL" for c in cycle_reports) else "FAIL",
            "items": cycle_reports,
        }
        report["status"] = self._overall_status(report)
        return self._finish_report(report, write_report)

    def run_once(
        self,
        *,
        tickers: list[str],
        cycle_index: int = 0,
        risk_warnings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not self._run_guard_passed:
            started_at = self._now_kst().isoformat()
            return {
                "status": "FAIL",
                "cycle_index": cycle_index,
                "started_at": started_at,
                "reason": "run_once_requires_start_guard",
            }
        market_session_guard = self._market_session_check()
        started_at = str(market_session_guard.get("now") or self._now_kst().isoformat())
        if market_session_guard["status"] != "PASS":
            return {
                "status": "PASS" if market_session_guard.get("safe_skip") else "FAIL",
                "cycle_index": cycle_index,
                "started_at": started_at,
                "market_session_guard": market_session_guard,
                "execution": None,
            }
        padded = [pad_ticker(str(t)) for t in tickers]
        try:
            balance = self._kis_client.get_balance()
            bars_by_ticker = self._fetch_recent_bars(padded, asof=started_at)
        except Exception as e:
            if self._is_transient_read_error(e):
                self._consecutive_read_errors += 1
                fail_closed = (
                    self._consecutive_read_errors
                    > self._max_consecutive_read_error_skips
                )
                logger.warning(
                    "[paper_auto_trading] KIS read 오류. cycle=%s consecutive=%d/%d "
                    "fail_closed=%s error=%s",
                    cycle_index,
                    self._consecutive_read_errors,
                    self._max_consecutive_read_error_skips,
                    fail_closed,
                    e,
                )
                return self._read_error_cycle_report(
                    cycle_index=cycle_index,
                    error=e,
                    consecutive_read_errors=self._consecutive_read_errors,
                    fail_closed=fail_closed,
                    started_at=started_at,
                )
            raise
        self._consecutive_read_errors = 0
        latest_prices = self._latest_prices(bars_by_ticker)
        portfolio_value = self._portfolio_value(balance)
        current_positions = self._current_positions(
            balance.get("positions", []),
            latest_prices,
            portfolio_value,
        )
        hot_path_bar_readiness = self._hot_path_bar_readiness(
            bars_by_ticker,
            required_bars=self._required_warmup_bars(),
            asof=started_at,
        )
        account_state = self._account_state_report(
            balance=balance,
            current_positions=current_positions,
            portfolio_value=portfolio_value,
            captured_at=started_at,
            mode=str(getattr(self._kis_client, "mode", "unknown")).lower(),
        )
        if hot_path_bar_readiness["status"] != "PASS":
            return {
                "status": "FAIL",
                "cycle_index": cycle_index,
                "started_at": started_at,
                "reason": "hot_path_bar_readiness",
                "hot_path_bar_readiness": hot_path_bar_readiness,
                "account_state": account_state,
                "execution": None,
            }

        bars_batch = [
            bar
            for ticker in padded
            for bar in bars_by_ticker.get(ticker, [])
        ]
        self._ensure_hot_runner_started()
        external_risk_warnings = self._normalize_risk_warnings(risk_warnings)
        hot_result = self._hot_runner.run_once(
            tickers=padded,
            bars_batch=bars_batch,
            current_positions=current_positions,
            latest_prices=latest_prices,
            portfolio_value=portfolio_value,
            asof=started_at,
            recent_bars=bars_by_ticker,
            risk_warnings=external_risk_warnings,
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
                "hot_path_bar_readiness": hot_path_bar_readiness,
                "account_state": account_state,
                "hot_result": hot_result,
            }
        if (
            hot_result.get("status") == "FAIL"
            and (
                self._fail_on_quant_blocked
                or hot_result.get("failure_stage") != "quant_feature_readiness"
            )
        ):
            return {
                "status": "FAIL",
                "cycle_index": cycle_index,
                "started_at": started_at,
                "reason": hot_result.get("failure_stage", "hot_runner_failed"),
                "hot_path_bar_readiness": hot_path_bar_readiness,
                "account_state": account_state,
                "hot_result": hot_result,
                "execution": None,
            }

        quant_signal_guard = self._quant_signal_guard(hot_result)
        if quant_signal_guard["status"] != "PASS":
            return {
                "status": "FAIL",
                "cycle_index": cycle_index,
                "started_at": started_at,
                "reason": "quant_signal_readiness",
                "quant_signal_guard": quant_signal_guard,
                "hot_path_bar_readiness": hot_path_bar_readiness,
                "account_state": account_state,
                "hot_result": hot_result,
                "execution": None,
            }

        final_decision = dict(hot_result.get("final_decision") or {})
        final_decision["order_deltas"] = [
            dict(od) for od in list(final_decision.get("order_deltas", []))
        ]
        order_count_caps_applied = self._cap_order_count(final_decision)
        order_caps_applied = self._cap_order_quantities(final_decision)
        order_guard = self._order_guard(final_decision, hot_result=hot_result)
        if order_guard["status"] != "PASS":
            return {
                "status": "PASS" if order_guard.get("safe_skip") else "FAIL",
                "cycle_index": cycle_index,
                "started_at": started_at,
                "cold_path_risk_warning_count": len(external_risk_warnings),
                "hot_path_bar_readiness": hot_path_bar_readiness,
                "account_state": account_state,
                "quant_signal_guard": quant_signal_guard,
                "order_guard": order_guard,
                "order_count_caps_applied": order_count_caps_applied,
                "order_caps_applied": order_caps_applied,
                "hot_result": hot_result,
                "execution": None,
            }

        if not self._submit_orders:
            shadow_execution = {
                "status": "NOT_SUBMITTED_SHADOW",
                "broker_order_submitted": False,
                "reason": "shadow_only_no_broker_submit",
                "would_submit_count": len(final_decision.get("order_deltas", [])),
                "execution_report": {
                    "status": "NOT_SUBMITTED_SHADOW",
                    "fills": [],
                    "rejections": [],
                    "orders": [],
                },
            }
            return {
                "status": "PASS",
                "cycle_index": cycle_index,
                "started_at": started_at,
                "reason": "shadow_only_no_broker_submit",
                "safe_skip": True,
                "cold_path_risk_warning_count": len(external_risk_warnings),
                "portfolio_value": portfolio_value,
                "n_bars": len(bars_batch),
                "hot_path_bar_readiness": hot_path_bar_readiness,
                "account_state": account_state,
                "quant_signal_guard": quant_signal_guard,
                "order_guard": order_guard,
                "order_count_caps_applied": order_count_caps_applied,
                "order_caps_applied": order_caps_applied,
                "hot_result": hot_result,
                "shadow_order_deltas": final_decision.get("order_deltas", []),
                "execution_order_deltas": final_decision.get("order_deltas", []),
                "would_submit_count": len(final_decision.get("order_deltas", [])),
                "broker_order_submitted": False,
                "shadow_execution": shadow_execution,
                "execution": shadow_execution,
                "broker_blockers": [],
                "order_history_verification": {
                    "status": "SKIP",
                    "safe_skip": True,
                    "reason": "shadow_only_no_broker_submit",
                },
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
            "cold_path_risk_warning_count": len(external_risk_warnings),
            "portfolio_value": portfolio_value,
            "n_bars": len(bars_batch),
            "hot_path_bar_readiness": hot_path_bar_readiness,
            "account_state": account_state,
            "quant_signal_guard": quant_signal_guard,
            "order_guard": order_guard,
            "order_count_caps_applied": order_count_caps_applied,
            "order_caps_applied": order_caps_applied,
            "hot_result": hot_result,
            "submitted_order_deltas": final_decision.get("order_deltas", []),
            "execution_order_deltas": final_decision.get("order_deltas", []),
            "broker_order_submitted": True,
            "execution": execution,
            "broker_blockers": broker_blockers,
            "order_history_verification": order_history,
        }

    @staticmethod
    def _exception_cycle_report(
        *,
        cycle_index: int,
        reason: str,
        error: Exception,
        started_at: str,
    ) -> dict[str, Any]:
        return {
            "status": "FAIL",
            "cycle_index": int(cycle_index),
            "started_at": started_at,
            "reason": reason,
            "hot_path_bar_readiness": {
                "status": "UNKNOWN",
                "reason": "cycle_exception_before_bar_readiness",
            },
            "exception_type": type(error).__name__,
            "error": str(error),
            "fail_closed": True,
            "execution": None,
        }

    def _read_error_cycle_report(
        self,
        *,
        cycle_index: int,
        error: Exception,
        consecutive_read_errors: int,
        fail_closed: bool,
        started_at: str,
    ) -> dict[str, Any]:
        return {
            "status": "FAIL" if fail_closed else "SKIP",
            "cycle_index": int(cycle_index),
            "started_at": started_at,
            "reason": (
                "paper_auto_read_error_budget_exhausted"
                if fail_closed
                else "paper_auto_read_transient_error_skip"
            ),
            "hot_path_bar_readiness": {
                "status": "UNKNOWN",
                "reason": "read_error_before_bar_readiness",
                "required_bars": self._required_warmup_bars(),
            },
            "safe_skip": not fail_closed,
            "exception_type": type(error).__name__,
            "error": str(error),
            "consecutive_read_errors": int(consecutive_read_errors),
            "max_consecutive_read_error_skips": self._max_consecutive_read_error_skips,
            "fail_closed": fail_closed,
            "execution": None,
        }

    @staticmethod
    def _is_transient_read_error(error: Exception) -> bool:
        text = str(error)
        non_transient_markers = (
            "msg_cd=OPSQ2000",
            "msg_cd=40580000",
            "KIS_MODE=real",
            "PAPER_MODE_REQUIRED",
        )
        if any(marker in text for marker in non_transient_markers):
            return False
        if isinstance(error, (ConnectionError, TimeoutError)):
            return True
        transient_kis_markers = (
            "HTTP 429",
            "Too Many Requests",
            "circuit breaker OPEN",
            "msg_cd=EGW00201",
            "msg_cd=EGW00133",
        )
        return isinstance(error, KISAPIError) and any(
            marker in text for marker in transient_kis_markers
        )

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

    def _make_hot_runner(self) -> HotRunner:
        quant = QuantAgent(dual_source_loader=load_latest_scores)
        return HotRunner(quant=quant, ppo=self._make_ppo_allocator())

    def _ensure_hot_runner_started(self) -> None:
        try:
            if getattr(self._hot_runner, "state", None).value == "HOT_RUNNING":
                return
        except AttributeError:
            pass
        self._hot_runner.start()

    def _fetch_recent_bars(
        self,
        tickers: list[str],
        *,
        asof: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        warmup = self._required_warmup_bars()
        out: dict[str, list[dict[str, Any]]] = {}
        topup_cfg = self._cfg.get("historical_warmup_topup") or {}
        topup_enabled = safe_bool(topup_cfg.get("enabled", False), default=False)
        metadata: dict[str, Any] = {
            "status": "PASS",
            "required_bars": int(warmup),
            "topup_enabled": topup_enabled,
            "future_bar_filtered": False,
            "future_rows_kept_for_readiness": True,
            "selection_policy": "kis_recent_then_pit_safe_historical_artifact_topup",
            "live_rows_by_ticker": {},
            "historical_topup_rows_by_ticker": {},
            "final_rows_by_ticker": {},
            "topup_cutoff_by_ticker": {},
            "topup_files_used_by_ticker": {},
            "topup_file_scan_count_by_ticker": {},
            "tickers": {},
        }
        for ticker in tickers:
            padded = pad_ticker(str(ticker))
            bars = list(self._kis_client.inquire_minute_bar(padded, n_bars=warmup))
            filtered_bars = self._filter_future_bars(bars, asof=asof)
            topped_up, topup_meta = self._historical_warmup_topup(
                padded,
                filtered_bars,
                warmup,
            )
            out[padded] = topped_up
            metadata["live_rows_by_ticker"][padded] = len(filtered_bars)
            metadata["historical_topup_rows_by_ticker"][padded] = int(
                topup_meta.get("historical_topup_count", 0)
            )
            metadata["final_rows_by_ticker"][padded] = len(topped_up)
            metadata["topup_cutoff_by_ticker"][padded] = topup_meta.get("cutoff_ts")
            metadata["topup_files_used_by_ticker"][padded] = topup_meta.get("files_used", [])
            metadata["topup_file_scan_count_by_ticker"][padded] = int(
                topup_meta.get("files_scanned_count", 0)
            )
            metadata["tickers"][padded] = {
                "raw_live_bar_count": len(bars),
                "live_bar_count": len(filtered_bars),
                "historical_topup_count": int(topup_meta.get("historical_topup_count", 0)),
                "final_bar_count": len(topped_up),
                "topup_needed": len(filtered_bars) < warmup,
                "topup_applied": int(topup_meta.get("historical_topup_count", 0)) > 0,
                "topup_enabled": bool(topup_meta.get("topup_enabled", topup_enabled)),
                "cutoff_ts": topup_meta.get("cutoff_ts"),
                "files_scanned": topup_meta.get("files_scanned", []),
                "files_used": topup_meta.get("files_used", []),
                "max_files_per_ticker": topup_meta.get("max_files_per_ticker"),
                "reason": topup_meta.get("reason"),
            }
        self._last_bar_fetch_metadata = metadata
        return out

    @staticmethod
    def _required_warmup_bars() -> int:
        return safe_int(
            (config_load("risk_config.yaml", "quant_agent") or {}).get("warmup_bars"),
            default=0,
            min_value=0,
        )

    def _hot_path_bar_readiness(
        self,
        bars_by_ticker: dict[str, list[dict[str, Any]]],
        *,
        required_bars: int,
        asof: str | None,
    ) -> dict[str, Any]:
        """Report whether the Hot Path has enough PIT-safe bars for scoring."""
        rows_by_ticker: dict[str, int] = {}
        missing_bars_by_ticker: dict[str, int] = {}
        latest_bar_ts_by_ticker: dict[str, str | None] = {}
        future_rows: list[dict[str, str]] = []
        invalid_rows: list[dict[str, str]] = []
        asof_ts = self._parse_bar_ts(asof)

        for raw_ticker, bars in bars_by_ticker.items():
            ticker = pad_ticker(str(raw_ticker))
            rows_by_ticker[ticker] = len(bars)
            if len(bars) < required_bars:
                missing_bars_by_ticker[ticker] = required_bars - len(bars)

            parsed_ts: list[datetime] = []
            for bar in bars:
                raw_ts = str(bar.get("ts_close") or "")
                bar_ts = self._parse_bar_ts(raw_ts)
                if bar_ts is None:
                    invalid_rows.append({"ticker": ticker, "ts_close": raw_ts})
                    continue
                parsed_ts.append(bar_ts)
                if asof_ts is not None and bar_ts > asof_ts:
                    future_rows.append({"ticker": ticker, "ts_close": raw_ts})
            latest_bar_ts_by_ticker[ticker] = (
                max(parsed_ts).isoformat() if parsed_ts else None
            )

        status = (
            "PASS"
            if not missing_bars_by_ticker and not future_rows and not invalid_rows
            else "FAIL"
        )
        return {
            "status": status,
            "required_bars": int(required_bars),
            "rows_by_ticker": rows_by_ticker,
            "missing_bars_by_ticker": missing_bars_by_ticker,
            "latest_bar_ts_by_ticker": latest_bar_ts_by_ticker,
            "future_rows": future_rows,
            "invalid_rows": invalid_rows,
            "bar_warmup_topup": dict(self._last_bar_fetch_metadata or {}),
        }

    def _filter_future_bars(
        self,
        bars: list[dict[str, Any]],
        *,
        asof: str | None,
    ) -> list[dict[str, Any]]:
        # Keep raw live rows so _hot_path_bar_readiness can fail closed on
        # invalid/future bars before HotRunner or broker submission.
        return list(bars)

    def _historical_warmup_topup(
        self,
        ticker: str,
        live_bars: list[dict[str, Any]],
        warmup: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Top up KIS's short recent-bar window with PIT-safe artifact bars."""
        topup_meta: dict[str, Any] = {
            "topup_enabled": False,
            "historical_topup_count": 0,
            "files_scanned": [],
            "files_used": [],
            "files_scanned_count": 0,
            "max_files_per_ticker": None,
            "cutoff_ts": None,
        }
        if len(live_bars) >= warmup:
            topup_meta["reason"] = "live_window_sufficient"
            final_bars = self._dedupe_sort_limit_bars(live_bars, warmup)
            return final_bars, topup_meta
        topup_cfg = self._cfg.get("historical_warmup_topup") or {}
        if not safe_bool(topup_cfg.get("enabled", False), default=False):
            topup_meta["reason"] = "topup_disabled"
            final_bars = self._dedupe_sort_limit_bars(live_bars, warmup)
            return final_bars, topup_meta

        historical, load_meta = self._load_historical_warmup_bars(
            ticker=ticker,
            live_bars=live_bars,
            missing=max(0, warmup - len(live_bars)),
            topup_cfg=topup_cfg,
        )
        topup_meta.update(load_meta)
        topup_meta["topup_enabled"] = True
        topup_meta["historical_topup_count"] = len(historical)
        topup_meta["reason"] = (
            "historical_topup_applied" if historical else "historical_topup_unavailable"
        )
        if historical:
            logger.info(
                "[paper_auto_trading] historical warmup topup: ticker=%s "
                "live=%d historical=%d warmup=%d",
                pad_ticker(str(ticker)),
                len(live_bars),
                len(historical),
                warmup,
            )
        final_bars = self._dedupe_sort_limit_bars([*historical, *live_bars], warmup)
        return final_bars, topup_meta

    def _load_historical_warmup_bars(
        self,
        *,
        ticker: str,
        live_bars: list[dict[str, Any]],
        missing: int,
        topup_cfg: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        metadata: dict[str, Any] = {
            "topup_enabled": True,
            "historical_topup_count": 0,
            "files_scanned": [],
            "files_used": [],
            "files_scanned_count": 0,
            "max_files_per_ticker": None,
            "cutoff_ts": None,
        }
        if missing <= 0:
            metadata["reason"] = "no_missing_bars"
            return [], metadata
        data_dir = Path(str(topup_cfg.get("data_dir", "artifacts/data")))
        if not data_dir.is_absolute():
            data_dir = _PROJECT_ROOT / data_dir
        ticker_dir = data_dir / pad_ticker(str(ticker))
        if not ticker_dir.exists():
            metadata["reason"] = "ticker_history_dir_missing"
            return [], metadata

        cutoff = self._historical_topup_cutoff(live_bars)
        max_files = safe_int(topup_cfg.get("max_files_per_ticker", 5), default=5, min_value=1)
        metadata["cutoff_ts"] = cutoff.isoformat() if cutoff is not None else None
        metadata["max_files_per_ticker"] = max_files
        candidate_files = sorted(ticker_dir.glob("bars_1m_*.parquet"), reverse=True)
        filtered: list[dict[str, Any]] = []
        for path in candidate_files[:max_files]:
            rel_path = str(path)
            try:
                rel_path = str(path.relative_to(_PROJECT_ROOT))
            except ValueError:
                pass
            metadata["files_scanned"].append(rel_path)
            rows = self._read_warmup_bar_file(path)
            before_count = len(filtered)
            for row in rows:
                ts = self._parse_bar_ts(row.get("ts_close"))
                if ts is None:
                    continue
                if cutoff is not None and ts >= cutoff:
                    continue
                filtered.append(row)
            if len(filtered) > before_count:
                metadata["files_used"].append(rel_path)
            if len(filtered) >= missing:
                break
        metadata["files_scanned_count"] = len(metadata["files_scanned"])
        final_rows = self._dedupe_sort_limit_bars(filtered, missing)
        metadata["historical_topup_count"] = len(final_rows)
        return final_rows, metadata

    def _historical_topup_cutoff(self, live_bars: list[dict[str, Any]]) -> datetime | None:
        timestamps = [
            ts
            for ts in (self._parse_bar_ts(bar.get("ts_close")) for bar in live_bars)
            if ts is not None
        ]
        if timestamps:
            return min(timestamps)
        now = self._now()
        if now.tzinfo is None:
            return now.replace(tzinfo=_KST)
        return now.astimezone(_KST)

    def _now_kst(self) -> datetime:
        now = self._now()
        if now.tzinfo is None:
            return now.replace(tzinfo=_KST)
        return now.astimezone(_KST)

    @staticmethod
    def _read_warmup_bar_file(path: Path) -> list[dict[str, Any]]:
        try:
            import pandas as pd
        except ImportError as e:
            logger.warning("[paper_auto_trading] pandas unavailable for warmup topup: %s", e)
            return []
        try:
            frame = pd.read_parquet(path)
        except Exception as e:
            logger.warning("[paper_auto_trading] warmup topup read failed: %s error=%s", path, e)
            return []
        records: list[dict[str, Any]] = []
        for record in frame.to_dict("records"):
            if not isinstance(record, dict):
                continue
            records.append(PaperAutoTrader._normalize_bar_record(record))
        return records

    @staticmethod
    def _normalize_bar_record(record: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = dict(record)
        ts = out.get("ts_close")
        if hasattr(ts, "isoformat"):
            out["ts_close"] = ts.isoformat()
        for key in ("open", "high", "low", "close", "volume"):
            if key in out:
                out[key] = safe_float(out.get(key), default=0.0)
        out["ticker"] = pad_ticker(str(out.get("ticker", "")))
        return out

    @staticmethod
    def _dedupe_sort_limit_bars(
        bars: Iterable[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for bar in bars:
            if not isinstance(bar, dict):
                continue
            ticker = pad_ticker(str(bar.get("ticker", "")))
            ts_close = str(bar.get("ts_close") or "")
            if ticker == "000000" or not ts_close:
                continue
            normalized = dict(bar)
            normalized["ticker"] = ticker
            by_key[(ticker, ts_close)] = normalized
        ordered = sorted(
            by_key.values(),
            key=lambda item: (
                pad_ticker(str(item.get("ticker", ""))),
                str(item.get("ts_close") or ""),
            ),
        )
        return ordered[-limit:]

    @staticmethod
    def _parse_bar_ts(raw: Any) -> datetime | None:
        if raw is None:
            return None
        try:
            ts = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        if ts.tzinfo is None:
            return ts.replace(tzinfo=_KST)
        return ts.astimezone(_KST)

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
            available_qty = safe_lossless_int(
                pos.get("available_qty", qty),
                default=qty,
                min_value=0,
            )
            available_qty = min(available_qty, qty)
            price = latest_prices.get(
                ticker,
                safe_float(pos.get("current_price", 0.0), default=0.0),
            )
            weight = (qty * price / portfolio_value) if portfolio_value > 0 and price > 0 else 0.0
            out.append({
                "ticker": ticker,
                "qty": qty,
                "available_qty": available_qty,
                "weight": float(weight),
            })
        return out

    @staticmethod
    def _account_state_report(
        *,
        balance: dict[str, Any],
        current_positions: list[dict[str, Any]],
        portfolio_value: float,
        captured_at: str,
        mode: str,
    ) -> dict[str, Any]:
        bal = balance.get("balance") if isinstance(balance.get("balance"), dict) else {}
        positions = [
            {
                "ticker": pad_ticker(str(pos.get("ticker", ""))),
                "qty": safe_lossless_int(pos.get("qty", 0), default=0, min_value=0),
                "available_qty": safe_lossless_int(
                    pos.get("available_qty", 0),
                    default=0,
                    min_value=0,
                ),
                "weight": safe_float(pos.get("weight", 0.0), default=0.0),
            }
            for pos in current_positions
        ]
        return {
            "status": "PASS",
            "captured_at": captured_at,
            "source": "kis_virtual_balance",
            "_mode": mode,
            "position_count": len(positions),
            "current_position_count": len(positions),
            "positions": positions,
            "cash": safe_float(bal.get("cash", 0.0), default=0.0),
            "net_asset": safe_float(bal.get("net_asset", 0.0), default=0.0),
            "portfolio_value": float(portfolio_value),
        }

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

    def _requested_ticker_universe_guard(self, tickers: list[str]) -> dict[str, Any]:
        requested: list[str] = []
        invalid: list[str] = []
        for raw_ticker in tickers:
            ticker = pad_ticker(str(raw_ticker).strip())
            if ticker == "000000" or not is_valid_ticker(ticker):
                invalid.append(str(raw_ticker))
                continue
            requested.append(ticker)

        if invalid:
            return {
                "status": "FAIL",
                "reason": "invalid_requested_ticker",
                "invalid_tickers": invalid,
            }
        if not requested:
            return {
                "status": "FAIL",
                "reason": "requested_tickers_empty",
            }

        active_universe = self._get_active_trade_universe()
        if not active_universe:
            return {
                "status": "FAIL",
                "reason": "active_trade_universe_empty",
            }

        blocked = sorted({ticker for ticker in requested if ticker not in active_universe})
        if blocked:
            return {
                "status": "FAIL",
                "reason": "requested_ticker_not_active_universe",
                "blocked_tickers": blocked,
                "requested_tickers": requested,
                "active_universe_count": len(active_universe),
                "active_universe_sample": sorted(active_universe)[:10],
            }

        return {
            "status": "PASS",
            "requested_tickers": requested,
            "active_universe_count": len(active_universe),
        }

    def _get_active_trade_universe(self) -> set[str]:
        if self._active_trade_universe is None:
            self._active_trade_universe = self._load_active_trade_universe()
        return set(self._active_trade_universe)

    @staticmethod
    def _load_active_trade_universe() -> set[str]:
        try:
            universe_cfg = config_load("universe_config.yaml", None) or {}
        except TypeError:
            universe_cfg = config_load("universe_config.yaml") or {}
        except Exception as e:
            logger.warning("[paper_auto_trading] active universe 로드 실패: %s", e)
            return set()

        active: set[str] = set()
        sectors = universe_cfg.get("sectors") or {}
        if not isinstance(sectors, dict):
            return active
        for sector in sectors.values():
            if not isinstance(sector, dict):
                continue
            if str(sector.get("status", "")).strip() != "confirmed":
                continue
            stocks = sector.get("stocks") or []
            if not isinstance(stocks, list):
                continue
            for stock in stocks:
                if not isinstance(stock, dict):
                    continue
                if str(stock.get("status", "")).strip() != "active":
                    continue
                ticker = pad_ticker(str(stock.get("ticker", "")))
                if ticker != "000000" and is_valid_ticker(ticker):
                    active.add(ticker)
        return active

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
        if not self._require_serving_feature_readiness:
            return {"status": "PASS", "required": False}
        quant = getattr(self._hot_runner, "_quant", None)
        checker = getattr(quant, "serving_feature_readiness", None)
        if callable(checker):
            asof = self._now().isoformat()
            result = checker([pad_ticker(str(t)) for t in tickers], asof)
            if not isinstance(result, dict):
                return {
                    "status": "FAIL",
                    "error_code": "SERVING_FEATURE_READINESS_INVALID",
                    "reason": "serving_feature_readiness_invalid",
                    "type": type(result).__name__,
                }
            return result

        metadata = getattr(quant, "model_metadata", None)
        feature_cols = (
            list(metadata.get("feature_cols") or [])
            if isinstance(metadata, dict)
            else []
        )
        pp_cfg = config_load("risk_config.yaml", "preprocessor")
        configured_ds_cols = list(pp_cfg.get("dual_source_feature_cols", []))
        required_ds_cols = [
            col for col in configured_ds_cols if col in set(feature_cols)
        ]
        if required_ds_cols:
            return {
                "status": "FAIL",
                "error_code": "SERVING_FEATURE_CHECKER_MISSING",
                "reason": "serving_feature_checker_missing",
                "required_dual_source_cols": required_ds_cols,
            }
        return {
            "status": "PASS",
            "required": True,
            "required_dual_source_cols": [],
        }

    def _cap_order_count(self, final_decision: dict[str, Any]) -> list[dict[str, Any]]:
        """Apply the paper-auto per-cycle order-count cap before broker submission."""
        raw_order_deltas = final_decision.get("order_deltas", [])
        if not isinstance(raw_order_deltas, list):
            return []
        if len(raw_order_deltas) <= self._max_orders_per_cycle:
            return []
        if any(not isinstance(od, dict) for od in raw_order_deltas):
            return []

        ranked = sorted(
            enumerate(raw_order_deltas),
            key=self._order_count_cap_sort_key,
        )
        kept = ranked[: self._max_orders_per_cycle]
        dropped = ranked[self._max_orders_per_cycle :]
        kept_orders = [dict(od) for _, od in kept]
        final_decision["order_deltas"] = kept_orders

        cap_record = {
            "original_count": len(raw_order_deltas),
            "capped_count": len(kept_orders),
            "max_orders_per_cycle": self._max_orders_per_cycle,
            "selection_policy": (
                "abs_delta_weight_desc_then_notional_desc_then_abs_score_desc_"
                "then_ticker_asc"
            ),
            "kept": [
                self._order_count_cap_item(index, od)
                for index, od in kept
            ],
            "dropped": [
                self._order_count_cap_item(index, od)
                for index, od in dropped
            ],
        }
        final_decision["paper_auto_order_count_caps_applied"] = [cap_record]
        logger.warning("[paper_auto_trading] 주문 개수 cap 적용: %s", cap_record)
        return [cap_record]

    @staticmethod
    def _order_count_cap_sort_key(item: tuple[int, dict[str, Any]]) -> tuple[float, float, float, str, int]:
        index, od = item
        ticker = pad_ticker(str(od.get("ticker", "")))
        delta_weight = abs(safe_float(od.get("delta_weight", 0.0), default=0.0))
        qty = safe_lossless_int(od.get("qty", 0), default=0, min_value=0)
        price = safe_float(od.get("price", 0.0), default=0.0, min_value=0.0)
        score = abs(
            safe_float(
                od.get("score", od.get("rank_score", 0.0)),
                default=0.0,
            )
        )
        notional = float(qty) * float(price)
        return (-delta_weight, -notional, -score, ticker, index)

    @staticmethod
    def _order_count_cap_item(index: int, od: dict[str, Any]) -> dict[str, Any]:
        ticker = pad_ticker(str(od.get("ticker", "")))
        qty = safe_lossless_int(od.get("qty", 0), default=0, min_value=0)
        price = safe_float(od.get("price", 0.0), default=0.0, min_value=0.0)
        return {
            "index": index,
            "ticker": ticker,
            "side": str(od.get("side", "")).lower(),
            "qty": qty,
            "notional": float(qty) * float(price),
            "delta_weight": safe_float(od.get("delta_weight", 0.0), default=0.0),
        }

    def _cap_order_quantities(self, final_decision: dict[str, Any]) -> list[dict[str, Any]]:
        """Apply the paper-auto per-order quantity cap before broker submission."""
        raw_order_deltas = final_decision.get("order_deltas", [])
        if not isinstance(raw_order_deltas, list):
            return []

        caps_applied: list[dict[str, Any]] = []
        for idx, od in enumerate(raw_order_deltas):
            if not isinstance(od, dict):
                continue
            raw_qty = od.get("qty", 0)
            qty = safe_lossless_int(raw_qty, default=0)
            if qty <= self._max_order_qty_per_order:
                continue
            capped_qty = self._max_order_qty_per_order
            od["qty"] = capped_qty
            caps_applied.append({
                "index": idx,
                "ticker": pad_ticker(str(od.get("ticker", ""))),
                "side": str(od.get("side", "")).lower(),
                "original_qty": qty,
                "capped_qty": capped_qty,
                "max_order_qty_per_order": self._max_order_qty_per_order,
            })

        if caps_applied:
            final_decision["paper_auto_order_caps_applied"] = caps_applied
            logger.warning("[paper_auto_trading] 주문 수량 cap 적용: %s", caps_applied)
        return caps_applied

    @staticmethod
    def _normalize_risk_warnings(
        risk_warnings: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """Cold Path sidecar 경고를 HotRunner/FDA 입력 형태로 정규화한다."""
        normalized: list[dict[str, Any]] = []
        for item in risk_warnings or []:
            if not isinstance(item, dict):
                continue
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
            warning = dict(payload)
            risk_level = str(warning.get("risk_level", "")).strip().lower()
            severity = str(warning.get("severity", "")).strip().lower()
            if severity not in {"low", "medium", "high", "critical"}:
                severity = (
                    risk_level
                    if risk_level in {"low", "medium", "high", "critical"}
                    else "low"
                )
            warning["severity"] = severity
            normalized.append(warning)
        return normalized

    @staticmethod
    def _cold_path_risk_warning_stage(
        risk_warnings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        reason_codes: list[str] = []
        severity_counts: dict[str, int] = {}
        for warning in risk_warnings:
            severity = str(warning.get("severity", "unknown")).lower()
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
            reason_code = str(
                warning.get("recommended_fda_reason_code")
                or warning.get("reason_code")
                or ""
            ).strip()
            if reason_code:
                reason_codes.append(reason_code)
        return {
            "status": "PASS",
            "count": len(risk_warnings),
            "severity_counts": severity_counts,
            "recommended_reason_codes": sorted(set(reason_codes)),
            "hot_path_llm_call": False,
            "note": (
                "External Cold Path risk warnings are forwarded to HotRunner/FDA "
                "without mutating weights."
            ),
        }

    def _order_guard(
        self,
        final_decision: dict[str, Any],
        *,
        hot_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
            if self._require_active_scores_for_no_order_skip:
                quant_output = (
                    hot_result.get("quant_output", {})
                    if isinstance(hot_result, dict)
                    else {}
                )
                scores = (
                    quant_output.get("scores", {})
                    if isinstance(quant_output, dict)
                    else {}
                )
                mode = (
                    str(quant_output.get("mode", "unknown"))
                    if isinstance(quant_output, dict)
                    else "unknown"
                )
                if mode != "active" or not isinstance(scores, dict) or not scores:
                    return {
                        "status": "FAIL",
                        "reason": "active_model_no_scores_no_order_deltas",
                        "quant_mode": mode,
                        "score_count": len(scores) if isinstance(scores, dict) else 0,
                        "require_active_scores_for_no_order_skip": True,
                    }
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
    def _quant_signal_guard(hot_result: dict[str, Any]) -> dict[str, Any]:
        """Block paper orders when Quant did not emit active, non-empty scores."""
        quant_output = hot_result.get("quant_output") if isinstance(hot_result, dict) else None
        if not isinstance(quant_output, dict):
            return {"status": "FAIL", "reason": "quant_output_not_reported"}
        mode = str(quant_output.get("mode", "unknown"))
        scores = quant_output.get("scores", {})
        score_count = len(scores) if isinstance(scores, dict) else 0
        if mode != "active" or not isinstance(scores, dict) or score_count == 0:
            return {
                "status": "FAIL",
                "reason": "active_model_quant_scores_unavailable",
                "quant_mode": mode,
                "score_count": score_count,
            }
        finite_scores: list[float] = []
        for value in scores.values():
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(parsed):
                finite_scores.append(parsed)
        rankable = (
            bool(len(finite_scores) == 1 and abs(finite_scores[0]) > 1e-12)
            or bool(len(finite_scores) > 1 and len(set(finite_scores)) > 1)
        )
        if len(finite_scores) != score_count or not rankable:
            return {
                "status": "FAIL",
                "reason": "active_model_quant_scores_not_rankable",
                "quant_mode": mode,
                "score_count": score_count,
                "finite_score_count": len(finite_scores),
                "rankable": rankable,
            }
        return {
            "status": "PASS",
            "reason": "active_quant_scores_present",
            "quant_mode": mode,
            "score_count": score_count,
            "finite_score_count": len(finite_scores),
            "rankable": rankable,
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
                "broker_submit_enabled": self._submit_orders,
                "shadow_only": not self._submit_orders,
            },
            "params": {
                "tickers": [pad_ticker(str(t)) for t in tickers],
                "cycles": int(cycles),
                "interval_sec": float(interval_sec),
                "required_bundle_id": self._required_bundle_id,
                "track_id": self._track_id,
                "policy_hash": self._policy_hash,
                "execution_policy": {
                    "max_orders_per_cycle": self._max_orders_per_cycle,
                    "max_order_qty_per_order": self._max_order_qty_per_order,
                },
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

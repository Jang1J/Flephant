"""S1-6 Execution Gateway (C10 ExecutionFeedbackContract).

Sprint 1 MVP: mock 모드 실동작. paper/live 모드는 KIS 클라이언트 주입 시
주문 제출 경로까지 수행한다.

Mode 분기:
  - mock: 즉시 filled 시뮬레이션 (가격 = order price or last known)
  - paper: KIS 모의투자 서버 실 주문 (S4-6)
  - live: KIS 실계좌 주문 (S1-8 + Phase 2 안전장치 후, live_enabled=true 필수)

Kill Switch 연동: 활성 상태 → 전면 REJECTED.
Audit Log: 모든 execute 호출 → JSONL 기록.
"""
from __future__ import annotations

import inspect
import random
import time
from datetime import datetime, timezone
from typing import Any

from src.execution.kill_switch import KillSwitch
from src.ops.audit_logger import AuditLogger
from src.utils.config_loader import load as config_load
from src.utils.id_factory import generate_order_plan_id
from src.utils.logger import get_logger
from src.utils.safe_cast import safe_bool, safe_float, safe_lossless_int
from src.utils.ticker_utils import is_valid_ticker, pad_ticker

logger = get_logger("execution_gateway")


class ExecutionModeError(ValueError):
    """invalid execution_mode."""


class LiveNotEnabledError(PermissionError):
    """execution_mode=live인데 live_enabled=false."""


class ExecutionDependencyError(RuntimeError):
    """paper/live 실행에 필요한 broker client가 없음."""


class ExecutionGateway:
    """C10 Execution Gateway. final_decision → execution_report.

    mock 모드에서는 주문 미발송 + 즉시 filled 시뮬레이션.
    paper/live는 주입된 KIS client.submit_order 경유. Kill Switch 활성 시 전면 차단.
    """

    def __init__(
        self,
        kill_switch: KillSwitch | None = None,
        audit_logger: AuditLogger | None = None,
        kis_client: Any | None = None,
        mode_override: str | None = None,
        live_enabled_override: bool | None = None,
        live_approval_proof: dict[str, Any] | None = None,
    ) -> None:
        exec_cfg = config_load("risk_config.yaml", "execution")
        self._mode: str = str(mode_override or exec_cfg["mode"]).lower()
        self._live_enabled: bool = (
            safe_bool(exec_cfg.get("live_enabled", False), default=False)
            if live_enabled_override is None
            else safe_bool(live_enabled_override, default=False)
        )
        exec_cost_cfg = config_load("risk_config.yaml", "execution_cost_model") or {}
        exec_cost_components = exec_cost_cfg.get("components")
        if not isinstance(exec_cost_components, dict):
            raise ValueError("execution_cost_model.components 설정 누락")
        self._slippage_bps = safe_float(
            exec_cost_components.get("slippage_bps"),
            default=-1.0,
            min_value=0.0,
        )
        if self._slippage_bps < 0:
            raise ValueError("execution_cost_model.components.slippage_bps 설정 누락")

        allowed_order_types = exec_cfg.get("allowed_order_types", ["00"])
        if isinstance(allowed_order_types, (list, tuple, set)):
            order_type_values = allowed_order_types
        else:
            order_type_values = [allowed_order_types]
        self._allowed_order_types: set[str] = {
            str(value).strip().zfill(2)
            for value in order_type_values
            if str(value).strip()
        } or {"00"}

        self._kill_switch = kill_switch
        self._audit_logger = audit_logger
        self._kis_client = kis_client
        self._live_approval_proof: dict[str, Any] = dict(live_approval_proof or {})
        self._active_trade_universe: set[str] | None = None

        logger.info(
            "[execution_gateway] 초기화: mode=%s, live_enabled=%s, "
            "kill_switch=%s, audit=%s",
            self._mode, self._live_enabled,
            self._kill_switch is not None,
            self._audit_logger is not None,
        )

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def live_enabled(self) -> bool:
        return self._live_enabled

    # ================================================================== #
    # Public API
    # ================================================================== #

    def execute(self, final_decision: dict[str, Any]) -> dict[str, Any]:
        """C10 execution_report 생성.

        Args:
            final_decision: FDAAgent.decide 결과의 final_decision dict
                            (decision_id, approved, order_deltas, ...).

        Returns: {execution_report, feedback_record, final_decision_ref, latency_ms, ...}
        """
        t0 = time.perf_counter()
        order_plan_id = generate_order_plan_id()
        decision_id = str(final_decision.get("decision_id", ""))
        approved_raw = final_decision.get("approved")
        if not isinstance(approved_raw, bool):
            return self._rejected(
                order_plan_id,
                decision_id,
                "approved_must_be_bool",
                [],
                t0,
            )
        approved = approved_raw
        order_deltas, validation_errors = self._normalize_order_deltas(
            final_decision.get("order_deltas", [])
        )
        if validation_errors:
            return self._rejected(
                order_plan_id,
                decision_id,
                f"invalid_order_deltas: {validation_errors}",
                order_deltas,
                t0,
            )

        # 1. approved=False → no order
        if not approved:
            reason = f"veto ({final_decision.get('reason_code', 'UNKNOWN')})"
            return self._rejected(
                order_plan_id, decision_id, reason, order_deltas, t0,
            )

        if not order_deltas:
            return self._rejected(
                order_plan_id,
                decision_id,
                "no_order_deltas",
                order_deltas,
                t0,
            )

        active_universe_rejection = self._active_universe_rejection(order_deltas)
        if active_universe_rejection:
            return self._rejected(
                order_plan_id,
                decision_id,
                active_universe_rejection,
                order_deltas,
                t0,
            )

        # 2. Kill Switch
        if self._kill_switch and self._kill_switch.is_active():
            status = self._kill_switch.status
            reason = f"kill_switch_active: {status.get('reason', 'unknown')}"
            return self._rejected(
                order_plan_id, decision_id, reason, order_deltas, t0,
            )
        if self._mode == "live" and self._kill_switch is None:
            return self._rejected(
                order_plan_id,
                decision_id,
                "kill_switch_missing. 실계좌 주문 차단.",
                order_deltas,
                t0,
            )

        # 3. Mode 분기
        if self._mode == "mock":
            report = self._execute_mock(
                order_plan_id, decision_id, order_deltas, t0,
            )
        elif self._mode == "paper":
            mode_rejection = self._broker_mode_rejection(expected_mode="virtual")
            if mode_rejection:
                report = self._rejected(
                    order_plan_id,
                    decision_id,
                    mode_rejection,
                    order_deltas,
                    t0,
                )
            else:
                report = self._execute_broker(
                    order_plan_id, decision_id, order_deltas, t0,
                )
        elif self._mode == "live":
            if not self._live_enabled:
                report = self._rejected(
                    order_plan_id,
                    decision_id,
                    "live_enabled=false. 실계좌 주문 차단.",
                    order_deltas,
                    t0,
                )
            else:
                approval_rejection = self._live_approval_rejection()
                mode_rejection = (
                    approval_rejection
                    or self._broker_mode_rejection(expected_mode="real")
                )
                if mode_rejection:
                    report = self._rejected(
                        order_plan_id,
                        decision_id,
                        mode_rejection,
                        order_deltas,
                        t0,
                    )
                else:
                    report = self._execute_broker(
                        order_plan_id, decision_id, order_deltas, t0,
                    )
        else:
            raise ExecutionModeError(
                f"invalid execution_mode={self._mode}"
            )

        # 4. Audit log. Rejected reports already emit execution_rejected in _rejected().
        status = (report.get("execution_report") or {}).get("status")
        if status != "rejected":
            self._audit("execution_success", report)

        return report

    # ================================================================== #
    # Internal: Mock execution
    # ================================================================== #

    def _execute_mock(
        self,
        order_plan_id: str,
        decision_id: str,
        order_deltas: list[dict[str, Any]],
        t0: float,
    ) -> dict[str, Any]:
        """Mock: 즉시 filled. 가격은 order delta의 price 필드 사용 (PM이 제공).

        slippage 계산 (2026-04-21, C5 하드코딩 제거):
          - snapshot_vwap = fill_price ± 3bps 랜덤 noise (mock 기준)
          - realized_slippage = (fill_price - snapshot_vwap) / snapshot_vwap
          - 하드코딩 0.0 제거. C10 realized_slippage 필드 단위 유지 (fraction).
        """
        now = datetime.now(tz=timezone.utc).isoformat()
        fills: list[dict[str, Any]] = []
        estimated_cost = 0.0
        total_slippage_weighted = 0.0
        slippage_noise = self._slippage_bps / 10000.0

        for od in order_deltas:
            fill_price = safe_float(od.get("price", 0.0), default=0.0)
            qty = safe_lossless_int(od.get("qty", 0), default=0)

            # snapshot_vwap: yaml slippage_bps 기반 noise
            vwap_noise = random.uniform(-slippage_noise, slippage_noise)
            snapshot_vwap = fill_price * (1.0 + vwap_noise) if fill_price > 0 else 0.0

            # realized_slippage (fraction): (fill - vwap) / vwap
            if snapshot_vwap > 0:
                realized_slippage = (fill_price - snapshot_vwap) / snapshot_vwap
            else:
                realized_slippage = 0.0

            fills.append({
                "ticker": od.get("ticker", ""),
                "side": od.get("side", ""),
                "qty": qty,
                "avg_fill_price": fill_price,
                "fill_ts": now,
                "snapshot_vwap": round(snapshot_vwap, 4),        # C18 audit 용 신규 필드
                "realized_slippage": round(realized_slippage, 6),  # 하드코딩 0.0 제거
            })
            estimated_cost += qty * fill_price
            total_slippage_weighted += realized_slippage * (qty * fill_price)

        # portfolio-level realized_slippage: 비용 가중 평균
        total_cost = sum(
            safe_lossless_int(od.get("qty", 0), default=0)
            * safe_float(od.get("price", 0.0), default=0.0)
            for od in order_deltas
        )
        portfolio_slippage = (
            total_slippage_weighted / total_cost if total_cost > 0 else 0.0
        )

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "execution_report": {
                "order_plan_id": order_plan_id,
                "submitted_at": now,
                "status": "filled",
                "fills": fills,
                "estimated_cost": float(estimated_cost),
                "realized_slippage": round(portfolio_slippage, 6),  # 하드코딩 0.0 제거
                "execution_mode": "mock",
            },
            "feedback_record": {
                "kb_message_id": f"KB-{order_plan_id}",
                "pnl_contribution": 0.0,
                "execution_shortfall": 0.0,
                "lesson_stub": None,
            },
            "final_decision_ref": decision_id,
            "latency_ms": elapsed_ms,
            "n_fills": len(fills),
        }

    # ================================================================== #
    # Internal: Broker-backed execution
    # ================================================================== #

    def _broker_client_mode(self) -> str | None:
        """Return injected broker mode when the client exposes it."""
        if self._kis_client is None:
            return None
        mode = getattr(self._kis_client, "mode", None)
        if callable(mode):
            try:
                mode = mode()
            except Exception:
                mode = None
        if mode is None and hasattr(self._kis_client, "auth"):
            auth = getattr(self._kis_client, "auth")
            get_mode = getattr(auth, "get_mode", None)
            if callable(get_mode):
                try:
                    mode = get_mode()
                except Exception:
                    mode = None
        return str(mode).strip().lower() if mode is not None else None

    def _broker_mode_rejection(self, expected_mode: str) -> str | None:
        """Fail closed when paper/live execution is wired to the wrong KIS mode."""
        if self._kis_client is None:
            return (
                "broker_dependency_missing: "
                f"execution_mode={self._mode} requires kis_client.submit_order"
            )
        client_mode = self._broker_client_mode()
        if client_mode != expected_mode:
            return (
                f"{self._mode}_mode_requires_{expected_mode}_kis_client: "
                f"client_mode={client_mode or 'unknown'}"
            )
        return None

    @staticmethod
    def _proof_status_is_pass(value: Any) -> bool:
        """Accept plain PASS or nested {status: PASS} proof fields."""
        if isinstance(value, dict):
            value = (
                value.get("status")
                or value.get("overall_status")
                or value.get("gate_status")
            )
        return str(value or "").strip().upper() == "PASS"

    @staticmethod
    def _broker_evidence_is_live_approved(value: Any) -> bool:
        """실계좌 proof는 문자열 PASS가 아니라 외부 KIS order-history 증거여야 한다."""
        if not isinstance(value, dict):
            return False
        stage_statuses = value.get("stage_statuses")
        if not isinstance(stage_statuses, dict):
            stage_statuses = {}
        required_stages = (
            "paper_auto_cycle",
            "balance_reconciliation",
            "probe_order",
            "order_history_requery",
        )
        return (
            ExecutionGateway._proof_status_is_pass(value)
            and safe_bool(value.get("external_kis_api"), default=False)
            and safe_bool(value.get("bundle_match"), default=False)
            and safe_bool(value.get("paper_auto_cycle_history_matched"), default=False)
            and all(str(stage_statuses.get(stage, "")).upper() == "PASS" for stage in required_stages)
        )

    def _live_approval_rejection(self) -> str | None:
        """실계좌 주문은 C10 경계에서도 명시 proof 없으면 fail-closed."""
        proof = self._live_approval_proof
        missing: list[str] = []
        for field in ("prelive_gate", "deploy_quality"):
            if not self._proof_status_is_pass(proof.get(field)):
                missing.append(field)
        broker_evidence = proof.get("broker_evidence")
        if not isinstance(broker_evidence, dict):
            broker_evidence = proof.get("kis_broker_evidence")
        if not self._broker_evidence_is_live_approved(broker_evidence):
            missing.append("broker_evidence")

        registry = proof.get("production_registry")
        active_version = ""
        if isinstance(registry, dict):
            active_version = str(registry.get("active_version") or "").strip()
        if not active_version:
            missing.append("production_registry.active_version")

        if not safe_bool(proof.get("live_trading_allowed", False), default=False):
            missing.append("live_trading_allowed")

        if missing:
            return "live_approval_missing: " + ",".join(missing)
        return None

    def _active_universe_rejection(self, order_deltas: list[dict[str, Any]]) -> str | None:
        """paper/live broker 주문은 active trade universe 안에서만 허용한다."""
        if self._mode not in {"paper", "live"}:
            return None
        active_universe = self._get_active_trade_universe()
        if not active_universe:
            return "active_trade_universe_empty. paper/live 주문 차단."
        blocked_tickers = sorted({
            str(order_delta.get("ticker", "")).strip()
            for order_delta in order_deltas
            if str(order_delta.get("ticker", "")).strip() not in active_universe
        })
        if blocked_tickers:
            return (
                "order_ticker_not_active_universe: "
                + ",".join(blocked_tickers)
            )
        return None

    def _get_active_trade_universe(self) -> set[str]:
        if self._active_trade_universe is None:
            self._active_trade_universe = self._load_active_trade_universe()
        return self._active_trade_universe

    @staticmethod
    def _load_active_trade_universe() -> set[str]:
        """Load active-only order universe from universe_config.yaml."""
        try:
            universe_cfg = config_load("universe_config.yaml", None) or {}
        except TypeError:
            universe_cfg = config_load("universe_config.yaml") or {}
        except Exception as e:
            logger.warning("[execution_gateway] active universe 로드 실패: %s", e)
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
            for stock in sector.get("stocks") or []:
                if not isinstance(stock, dict):
                    continue
                if str(stock.get("status", "")).strip() != "active":
                    continue
                ticker = pad_ticker(str(stock.get("ticker", "")))
                if ticker != "000000" and is_valid_ticker(ticker):
                    active.add(ticker)
        return active

    def _execute_broker(
        self,
        order_plan_id: str,
        decision_id: str,
        order_deltas: list[dict[str, Any]],
        t0: float,
    ) -> dict[str, Any]:
        """paper/live: KIS client.submit_order 경유 주문 제출.

        실제 체결 조회와 정산은 broker reconciliation 단계가 담당한다. 이 메서드는
        C10 계약에 맞춰 제출 성공/실패를 execution_report로 정규화한다.
        """
        if self._kis_client is None:
            return self._rejected(
                order_plan_id,
                decision_id,
                f"broker_dependency_missing: execution_mode={self._mode} requires kis_client.submit_order",
                order_deltas,
                t0,
            )
        if not hasattr(self._kis_client, "submit_order"):
            return self._rejected(
                order_plan_id,
                decision_id,
                "broker_dependency_missing: kis_client must expose submit_order(ticker, side, qty)",
                order_deltas,
                t0,
            )
        audit_rejection = self._broker_audit_preflight_rejection(
            order_plan_id,
            decision_id,
            order_deltas,
            t0,
        )
        if audit_rejection is not None:
            return audit_rejection

        now = datetime.now(tz=timezone.utc).isoformat()
        fills: list[dict[str, Any]] = []
        rejections: list[dict[str, Any]] = []
        estimated_cost = 0.0

        for od in order_deltas:
            ticker = str(od.get("ticker", ""))
            side = str(od.get("side", "")).lower()
            qty = safe_lossless_int(od.get("qty", 0), default=0)
            price = safe_float(od.get("price", 0.0), default=0.0)
            order_type = str(od.get("order_type", "00") or "00")

            if not self._valid_order_delta(
                ticker,
                side,
                qty,
                price,
                order_type,
                self._allowed_order_types,
            ):
                rejections.append({
                    "ticker": ticker,
                    "side": side,
                    "qty": qty,
                    "reason": "invalid_order_delta",
                })
                continue

            try:
                broker_response = self._submit_broker_order(
                    ticker,
                    side,
                    qty,
                    price,
                    order_type,
                )
                if not isinstance(broker_response, dict):
                    broker_response = {"raw_response": repr(broker_response)}
                broker_order_id = self._broker_order_id(broker_response)
                broker_rejection = self._broker_response_rejection_reason(
                    broker_response,
                    broker_order_id=broker_order_id,
                )
                if broker_rejection:
                    rejections.append({
                        "ticker": ticker,
                        "side": side,
                        "qty": qty,
                        "reason": broker_rejection,
                        "broker_response": broker_response,
                    })
                    continue
                broker_status = str(
                    broker_response.get("status")
                    or "submitted"
                ).strip().lower()

                avg_fill_price = safe_float(
                    broker_response.get(
                        "avg_fill_price",
                        broker_response.get("price", price),
                    ),
                    default=price,
                )
                estimated_cost += qty * avg_fill_price
                fills.append({
                    "ticker": ticker,
                    "side": side,
                    "qty": qty,
                    "avg_fill_price": avg_fill_price,
                    "fill_ts": str(broker_response.get("fill_ts", now)),
                    "broker_status": broker_status,
                    "broker_order_id": broker_order_id,
                    "broker_response": broker_response,
                })
            except Exception as e:
                rejections.append({
                    "ticker": ticker,
                    "side": side,
                    "qty": qty,
                    "reason": "broker_submit_exception",
                    "error": str(e),
                })

        if not fills:
            status = "rejected"
        elif rejections:
            status = "partial_filled"
        elif all(
            fill.get("broker_status") in {"filled", "mock_accepted"}
            for fill in fills
        ):
            status = "filled"
        else:
            status = "submitted"

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        lesson_stub = (
            None if not rejections else f"{len(rejections)} broker submission(s) rejected"
        )
        return {
            "execution_report": {
                "order_plan_id": order_plan_id,
                "submitted_at": now,
                "status": status,
                "fills": fills,
                "estimated_cost": float(estimated_cost),
                "realized_slippage": 0.0,
                "execution_mode": self._mode,
                "rejections": rejections,
            },
            "feedback_record": {
                "kb_message_id": f"KB-{order_plan_id}",
                "pnl_contribution": 0.0,
                "execution_shortfall": 0.0,
                "lesson_stub": lesson_stub,
            },
            "final_decision_ref": decision_id,
            "latency_ms": elapsed_ms,
            "n_fills": len(fills),
        }

    def _normalize_order_deltas(
        self,
        raw: Any,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Validate and normalize C10 order_deltas before any execution mode."""
        if raw is None:
            return [], []
        if not isinstance(raw, list):
            return [], [{"reason": "order_deltas_must_be_list", "type": type(raw).__name__}]

        normalized: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for idx, od in enumerate(raw):
            if not isinstance(od, dict):
                errors.append({
                    "index": idx,
                    "reason": "order_delta_must_be_dict",
                    "type": type(od).__name__,
                })
                continue
            ticker = pad_ticker(str(od.get("ticker", "")))
            side = str(od.get("side", "")).lower()
            qty = safe_lossless_int(od.get("qty", 0), default=0)
            price = safe_float(od.get("price", 0.0), default=0.0)
            order_type = str(od.get("order_type", "00") or "00")
            if not self._valid_order_delta(
                ticker,
                side,
                qty,
                price,
                order_type,
                self._allowed_order_types,
            ):
                errors.append({
                    "index": idx,
                    "ticker": ticker,
                    "side": side,
                    "qty": qty,
                    "price": price,
                    "order_type": order_type,
                    "reason": "invalid_order_delta",
                })
                continue
            normalized_od = dict(od)
            normalized_od.update({
                "ticker": ticker,
                "side": side,
                "qty": qty,
                "price": price,
                "order_type": order_type,
            })
            normalized.append(normalized_od)
        return normalized, errors

    @staticmethod
    def _valid_order_delta(
        ticker: str,
        side: str,
        qty: int,
        price: float,
        order_type: str,
        allowed_order_types: set[str] | None = None,
    ) -> bool:
        allowed = allowed_order_types or {"00"}
        if ticker == "000000" or not is_valid_ticker(ticker):
            return False
        if side not in {"buy", "sell"}:
            return False
        if qty <= 0:
            return False
        if order_type not in allowed:
            return False
        if order_type != "01" and price <= 0:
            return False
        return True

    def _submit_broker_order(
        self,
        ticker: str,
        side: str,
        qty: int,
        price: float,
        order_type: str = "00",
    ) -> Any:
        submit_order = self._kis_client.submit_order
        signature = inspect.signature(submit_order)
        params = signature.parameters
        accepts_kwargs = any(
            param.kind == inspect.Parameter.VAR_KEYWORD
            for param in params.values()
        )
        kwargs: dict[str, Any] = {}
        if "price" in params or accepts_kwargs:
            kwargs["price"] = price
        if "order_type" in params or accepts_kwargs:
            kwargs["order_type"] = order_type
        if kwargs:
            return submit_order(ticker, side, qty, **kwargs)
        return submit_order(ticker, side, qty)

    @staticmethod
    def _broker_order_id(broker_response: dict[str, Any]) -> str:
        return str(
            broker_response.get("order_id")
            or broker_response.get("order_no")
            or broker_response.get("odno")
            or broker_response.get("ODNO")
            or ""
        ).strip()

    @staticmethod
    def _broker_response_rejection_reason(
        broker_response: dict[str, Any],
        *,
        broker_order_id: str,
    ) -> str | None:
        status_raw = broker_response.get("status")
        status = str(status_raw or "").strip().lower()
        rt_cd = str(broker_response.get("rt_cd") or "").strip()
        if rt_cd and rt_cd not in {"0", "0000"}:
            return f"broker_rt_cd_{rt_cd}"

        rejected_statuses = {"rejected", "failed", "fail", "error", "cancelled"}
        success_statuses = {
            "submitted",
            "accepted",
            "success",
            "filled",
            "partial_filled",
            "partial",
        }
        if status in rejected_statuses:
            return status
        if status and status not in success_statuses:
            return f"broker_status_unknown:{status}"
        if not status and rt_cd not in {"0", "0000"}:
            return "broker_status_missing"
        if not broker_order_id:
            return "broker_order_id_missing"
        return None

    def _broker_audit_preflight_rejection(
        self,
        order_plan_id: str,
        decision_id: str,
        order_deltas: list[dict[str, Any]],
        t0: float,
    ) -> dict[str, Any] | None:
        if self._audit_logger is None:
            return self._rejected(
                order_plan_id,
                decision_id,
                "audit_logger_missing. broker submit 차단.",
                order_deltas,
                t0,
            )
        try:
            self._audit_logger.log(
                "execution_broker_pre_submit",
                {
                    "order_plan_id": order_plan_id,
                    "decision_id": decision_id,
                    "execution_mode": self._mode,
                    "n_orders": len(order_deltas),
                },
            )
        except Exception as e:
            return self._rejected(
                order_plan_id,
                decision_id,
                f"audit_logger_unavailable: {e}",
                order_deltas,
                t0,
            )
        return None

    # ================================================================== #
    # Internal: REJECTED
    # ================================================================== #

    def _rejected(
        self,
        order_plan_id: str,
        decision_id: str,
        reason: str,
        order_deltas: list[dict[str, Any]],
        t0: float,
    ) -> dict[str, Any]:
        now = datetime.now(tz=timezone.utc).isoformat()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        report = {
            "execution_report": {
                "order_plan_id": order_plan_id,
                "submitted_at": now,
                "status": "rejected",
                "fills": [],
                "estimated_cost": 0.0,
                "realized_slippage": 0.0,
                "execution_mode": self._mode,
                "rejection_reason": reason,
            },
            "feedback_record": {
                "kb_message_id": f"KB-{order_plan_id}",
                "pnl_contribution": 0.0,
                "execution_shortfall": 0.0,
                "lesson_stub": f"rejected: {reason}",
            },
            "final_decision_ref": decision_id,
            "latency_ms": elapsed_ms,
            "n_fills": 0,
        }
        self._audit("execution_rejected", report)
        return report

    # ================================================================== #
    # Internal: Audit
    # ================================================================== #

    def _audit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._audit_logger is None:
            return
        try:
            self._audit_logger.log(event_type, payload)
        except Exception as e:
            logger.warning(
                "[execution_gateway] audit log 실패: %s", e
            )

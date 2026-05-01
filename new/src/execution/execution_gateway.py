"""S1-6 Execution Gateway (C10 ExecutionFeedbackContract).

Sprint 1 MVP: mock 모드 실동작. paper/live 모드는 NotImplementedError (Sprint 4+).

Mode 분기:
  - mock: 즉시 filled 시뮬레이션 (가격 = order price or last known)
  - paper: KIS 모의투자 서버 실 주문 (S4-6)
  - live: KIS 실계좌 주문 (S1-8 + Phase 2 안전장치 후, live_enabled=true 필수)

Kill Switch 연동: 활성 상태 → 전면 REJECTED.
Audit Log: 모든 execute 호출 → JSONL 기록.
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from typing import Any

from src.execution.kill_switch import KillSwitch
from src.ops.audit_logger import AuditLogger
from src.utils.config_loader import load as config_load
from src.utils.id_factory import generate_order_plan_id
from src.utils.logger import get_logger

logger = get_logger("execution_gateway")


class ExecutionModeError(ValueError):
    """invalid execution_mode."""


class LiveNotEnabledError(PermissionError):
    """execution_mode=live인데 live_enabled=false."""


class ExecutionGateway:
    """C10 Execution Gateway. final_decision → execution_report.

    mock 모드에서는 주문 미발송 + 즉시 filled 시뮬레이션.
    paper/live는 후속 sprint. Kill Switch 활성 시 전면 차단.
    """

    def __init__(
        self,
        kill_switch: KillSwitch | None = None,
        audit_logger: AuditLogger | None = None,
        kis_client: Any | None = None,
    ) -> None:
        exec_cfg = config_load("risk_config.yaml", "execution")
        self._mode: str = str(exec_cfg["mode"]).lower()
        self._live_enabled: bool = bool(exec_cfg["live_enabled"])
        exec_cost_cfg = config_load("risk_config.yaml", "execution_cost_model") or {}
        self._slippage_bps: float = float(exec_cost_cfg.get("slippage_bps", 10))

        self._kill_switch = kill_switch
        self._audit_logger = audit_logger
        self._kis_client = kis_client

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
        approved = bool(final_decision.get("approved", False))
        order_deltas = list(final_decision.get("order_deltas", []))

        # 1. approved=False → no order
        if not approved:
            reason = f"veto ({final_decision.get('reason_code', 'UNKNOWN')})"
            return self._rejected(
                order_plan_id, decision_id, reason, order_deltas, t0,
            )

        # 2. Kill Switch
        if self._kill_switch and self._kill_switch.is_active():
            status = self._kill_switch.status
            reason = f"kill_switch_active: {status.get('reason', 'unknown')}"
            return self._rejected(
                order_plan_id, decision_id, reason, order_deltas, t0,
            )

        # 3. Mode 분기
        if self._mode == "mock":
            report = self._execute_mock(
                order_plan_id, decision_id, order_deltas, t0,
            )
        elif self._mode == "paper":
            raise NotImplementedError(
                "Paper mode는 S4-6 Paper Trading에서 KIS 모의투자 서버 연동"
            )
        elif self._mode == "live":
            if not self._live_enabled:
                raise LiveNotEnabledError(
                    "live_enabled=false. 실계좌 주문 차단."
                )
            raise NotImplementedError(
                "Live mode는 S1-8 (KIS API) + Phase 2 안전장치 완성 후"
            )
        else:
            raise ExecutionModeError(
                f"invalid execution_mode={self._mode}"
            )

        # 4. Audit log
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
            fill_price = float(od.get("price", 0.0))
            qty = int(od.get("qty", 0))

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
            int(od.get("qty", 0)) * float(od.get("price", 0.0))
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

"""S1-7 KillSwitch. 일일 손실 한도 초과 또는 수동 트리거 시 주문 전면 차단.

불변 원칙: 임계값 하드코딩 금지 (risk_config.yaml kill_switch.daily_loss_threshold).
reset은 operator token 검증 필수 (자동 해제 금지, architecture.md §6.0.1 S-1).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger

logger = get_logger("kill_switch")


class KillSwitchAlreadyActiveError(RuntimeError):
    """이미 활성 상태에서 또 trigger 호출."""


class InvalidOperatorTokenError(PermissionError):
    """reset 시 operator_token 검증 실패."""


class KillSwitch:
    """S-1 Safety Guard. daily_loss_threshold 초과 시 전면 차단.

    활성 상태에서 ExecutionGateway는 모든 주문을 REJECTED.
    reset은 operator만 (수동 개입, 자동 해제 금지).
    """

    def __init__(self) -> None:
        cfg = config_load("risk_config.yaml", "kill_switch")
        self._threshold: float = float(cfg["daily_loss_threshold"])
        self._action: str = str(cfg.get("action", "emergency_halt"))
        self._operator_token: str = str(cfg["operator_reset_token"])

        self._active: bool = False
        self._trigger_reason: str | None = None
        self._trigger_ts: str | None = None

        logger.info(
            "[kill_switch] 초기화: threshold=%.4f, action=%s",
            self._threshold, self._action,
        )

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def action(self) -> str:
        return self._action

    def is_active(self) -> bool:
        return self._active

    @property
    def status(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "threshold": self._threshold,
            "action": self._action,
            "reason": self._trigger_reason,
            "triggered_at": self._trigger_ts,
        }

    def check_daily_pnl(self, daily_pnl: float) -> bool:
        """daily_pnl (비율, 음수=손실) 체크. 임계 초과 시 자동 trigger.

        Returns: True if (이미 또는 방금) 활성. False if 안전.
        """
        if self._active:
            return True
        # daily_pnl <= -threshold → trigger
        if daily_pnl <= -self._threshold:
            self.trigger(
                f"daily_pnl={daily_pnl:.4f} <= -threshold={-self._threshold:.4f}"
            )
            return True
        return False

    def trigger(self, reason: str) -> None:
        """수동/자동 trigger. 이미 활성이면 무시 (idempotent)."""
        if self._active:
            logger.warning(
                "[kill_switch] 이미 활성. 새 reason=%s 기록만", reason
            )
            return
        self._active = True
        self._trigger_reason = str(reason)
        self._trigger_ts = datetime.now(tz=timezone.utc).isoformat()
        logger.warning("[kill_switch] TRIGGERED: %s", reason)

    def reset(self, operator_token: str) -> None:
        """수동 해제. operator_token 검증 필수.

        Sprint 4 실거래 전 실 인증(RBAC) 연동 필요.
        """
        if not operator_token or operator_token != self._operator_token:
            raise InvalidOperatorTokenError(
                "operator_token 무효. KillSwitch 해제 거부."
            )
        if not self._active:
            logger.info("[kill_switch] 이미 비활성. reset no-op")
            return
        prev = self._trigger_reason
        self._active = False
        self._trigger_reason = None
        self._trigger_ts = None
        logger.info(
            "[kill_switch] RESET by operator (이전 reason=%s)", prev
        )

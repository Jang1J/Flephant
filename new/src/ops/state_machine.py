"""파이프라인 상태 기계. architecture.md §7.4 Runtime State Machine 반영.

상태 목록 (15개, 2026-04-20 Sprint 1 감사 반영):

  Mode A (장중, 7개):
    - BOOTSTRAP : 시스템 시작, 모델/config 로딩 중
    - HOT_RUNNING : 정상 매매 (Hot Path)
    - COLD_RUNNING : Cold Path 활성 (LLM 에이전트 동작 중)
    - DEGRADED : 일부 에이전트/커넥터 장애. 퀀트만으로 운영
    - EMERGENCY_HALT : 긴급 전량 청산 + 시스템 중단 (kill switch / manual_emergency_halt)
    - MANUAL_OVERRIDE : 수동 일시정지 (manual_pause). 신규 주문 중단, 기존 포지션 유지
    - RECOVERY : 긴급 중단 후 복구 중. 포지션 확인 + 잔고 대조

  Mode B (장마감, 6개):
    - MODE_B_IDLE : 15:30~18:00 대기 + 22:00~다음 날 09:00 휴식
    - MODE_B_EVOLVING : Alpha Factor Engine + Co-STEER (18:00~21:00)
    - MODE_B_BACKTEST : Backtest Agent 검증 (21:00~21:30)
    - MODE_B_OPERATOR_REVIEW : verdict=warn 시 human_approval 대기
    - MODE_B_DEPLOY : 22:00 배포 실행 (model_registry 교체)
    - MODE_B_BLOCKED : Backtest fail. baseline 유지

  Terminal / Recovery (2개):
    - SHUTDOWN : 정상 종료
    - ERROR : 예외 상태 (미처리 예외 등). BOOTSTRAP 복귀 가능

전이 그래프 (§7.4):
  BOOTSTRAP → HOT_RUNNING / DEGRADED / EMERGENCY_HALT / SHUTDOWN / ERROR
  HOT_RUNNING ⇄ COLD_RUNNING (이벤트 감지/처리)
  HOT_RUNNING → DEGRADED → HOT_RUNNING
  HOT_RUNNING → MODE_B_IDLE (15:30 장 마감)
  ANY → EMERGENCY_HALT → RECOVERY → HOT_RUNNING
  ANY → MANUAL_OVERRIDE → HOT_RUNNING
  RECOVERY → EMERGENCY_HALT (복구 실패)
  COLD_RUNNING → DEGRADED (에이전트 timeout, LLM 장애)
  MANUAL_OVERRIDE → RECOVERY (수동 해제 후 안전 검증)
  MODE_B_IDLE → MODE_B_EVOLVING (18:00)
  MODE_B_EVOLVING → MODE_B_BACKTEST (21:00)
  MODE_B_BACKTEST → MODE_B_DEPLOY / MODE_B_OPERATOR_REVIEW / MODE_B_BLOCKED
  MODE_B_OPERATOR_REVIEW → MODE_B_DEPLOY / MODE_B_BLOCKED
  MODE_B_DEPLOY / MODE_B_BLOCKED → MODE_B_IDLE
  MODE_B_IDLE → BOOTSTRAP (다음 날 09:00)
  ERROR → BOOTSTRAP / SHUTDOWN
  모든 상태 → SHUTDOWN / ERROR

v3 원칙: Mode B 상태는 Mode A와 물리적으로 분리 (Backtest Agent 장중 미개입 강제).
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from src.utils.logger import get_logger

logger = get_logger("state_machine")


class PipelineState(Enum):
    """파이프라인 운영 상태 (architecture.md §7.4, 15종)."""

    # Mode A 장중 (7)
    BOOTSTRAP = "BOOTSTRAP"
    HOT_RUNNING = "HOT_RUNNING"
    COLD_RUNNING = "COLD_RUNNING"
    DEGRADED = "DEGRADED"
    EMERGENCY_HALT = "EMERGENCY_HALT"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    RECOVERY = "RECOVERY"

    # Mode B 장마감 (6)
    MODE_B_IDLE = "MODE_B_IDLE"
    MODE_B_EVOLVING = "MODE_B_EVOLVING"
    MODE_B_BACKTEST = "MODE_B_BACKTEST"
    MODE_B_OPERATOR_REVIEW = "MODE_B_OPERATOR_REVIEW"
    MODE_B_DEPLOY = "MODE_B_DEPLOY"
    MODE_B_BLOCKED = "MODE_B_BLOCKED"

    # Terminal / Recovery (2)
    SHUTDOWN = "SHUTDOWN"
    ERROR = "ERROR"


class IllegalTransitionError(ValueError):
    """허용되지 않는 상태 전이."""


# 모든 상태에서 허용되는 기본 전이 (architecture.md §7.4 "ANY → ..." 규칙)
_ANY_STATE_TRANSITIONS: frozenset[PipelineState] = frozenset({
    PipelineState.EMERGENCY_HALT,     # kill switch / manual_emergency_halt
    PipelineState.MANUAL_OVERRIDE,    # manual_pause
    PipelineState.SHUTDOWN,           # 정상 종료
    PipelineState.ERROR,              # 미처리 예외
})


# 상태별 특수 허용 전이 (architecture.md §7.4 전이 그래프)
_SPECIAL_TRANSITIONS: dict[PipelineState, frozenset[PipelineState]] = {
    PipelineState.BOOTSTRAP: frozenset({
        PipelineState.HOT_RUNNING,
        PipelineState.DEGRADED,       # 모델/config 로딩 부분 실패
    }),
    PipelineState.HOT_RUNNING: frozenset({
        PipelineState.COLD_RUNNING,    # 이벤트 감지
        PipelineState.DEGRADED,        # 에이전트 timeout
        PipelineState.MODE_B_IDLE,     # 15:30 장 마감
    }),
    PipelineState.COLD_RUNNING: frozenset({
        PipelineState.HOT_RUNNING,     # 처리 완료
        PipelineState.DEGRADED,        # 에이전트 timeout / LLM 장애
    }),
    PipelineState.DEGRADED: frozenset({
        PipelineState.HOT_RUNNING,     # 장애 복구
    }),
    PipelineState.EMERGENCY_HALT: frozenset({
        PipelineState.RECOVERY,        # 복구 시작
    }),
    PipelineState.RECOVERY: frozenset({
        PipelineState.HOT_RUNNING,     # 검증 완료
        # EMERGENCY_HALT 재진입은 _ANY_STATE_TRANSITIONS로 커버
    }),
    PipelineState.MANUAL_OVERRIDE: frozenset({
        PipelineState.HOT_RUNNING,     # 수동 해제 후 바로 재개
        PipelineState.RECOVERY,        # 수동 해제 후 안전 검증 필요 시
    }),
    PipelineState.MODE_B_IDLE: frozenset({
        PipelineState.MODE_B_EVOLVING, # 18:00 Scheduler
        PipelineState.BOOTSTRAP,       # 다음 날 09:00 장 시작
    }),
    PipelineState.MODE_B_EVOLVING: frozenset({
        PipelineState.MODE_B_BACKTEST, # 21:00
    }),
    PipelineState.MODE_B_BACKTEST: frozenset({
        PipelineState.MODE_B_DEPLOY,           # verdict=pass
        PipelineState.MODE_B_OPERATOR_REVIEW,  # verdict=warn
        PipelineState.MODE_B_BLOCKED,          # verdict=fail
    }),
    PipelineState.MODE_B_OPERATOR_REVIEW: frozenset({
        PipelineState.MODE_B_DEPLOY,
        PipelineState.MODE_B_BLOCKED,
    }),
    PipelineState.MODE_B_DEPLOY: frozenset({
        PipelineState.MODE_B_IDLE,     # 완료
    }),
    PipelineState.MODE_B_BLOCKED: frozenset({
        PipelineState.MODE_B_IDLE,     # baseline 유지
    }),
    PipelineState.SHUTDOWN: frozenset(),                # terminal
    PipelineState.ERROR: frozenset({
        PipelineState.BOOTSTRAP,       # 재시작 복구
    }),
}


def _allowed_transitions(current: PipelineState) -> frozenset[PipelineState]:
    """현재 상태에서 허용되는 모든 전이 (특수 + ANY 공통).

    SHUTDOWN은 terminal이라 ANY 포함 안 함. 그 외 모든 상태는 EMERGENCY_HALT /
    MANUAL_OVERRIDE / SHUTDOWN / ERROR로 언제든 전이 가능.
    """
    specials = _SPECIAL_TRANSITIONS.get(current, frozenset())
    if current == PipelineState.SHUTDOWN:
        return specials
    return specials | _ANY_STATE_TRANSITIONS


class StateMachine:
    """파이프라인 상태 전이 관리자. architecture.md §7.4 전이 그래프 enforce."""

    def __init__(
        self,
        initial: PipelineState = PipelineState.BOOTSTRAP,
    ) -> None:
        self._state: PipelineState = initial
        self._history: list[dict[str, Any]] = [
            {"from": None, "to": initial.value, "ts": None}
        ]
        logger.info("[state_machine] 초기 상태: %s", initial.value)

    @property
    def state(self) -> PipelineState:
        return self._state

    @property
    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    def transition(
        self,
        target: PipelineState,
        ts: str | None = None,
    ) -> None:
        """상태 전이. 허용되지 않은 전이는 IllegalTransitionError.

        target이 현재 상태와 동일하면 no-op (idempotent).
        """
        if target == self._state:
            return

        allowed = _allowed_transitions(self._state)
        if target not in allowed:
            raise IllegalTransitionError(
                f"허용되지 않는 전이: {self._state.value} → {target.value}. "
                f"허용: {sorted(s.value for s in allowed)}"
            )

        prev = self._state
        self._state = target
        self._history.append({
            "from": prev.value,
            "to": target.value,
            "ts": ts,
        })
        logger.info(
            "[state_machine] 전이: %s → %s", prev.value, target.value
        )

    def allowed_next_states(self) -> list[str]:
        """현재 상태에서 가능한 다음 상태 목록."""
        return sorted(s.value for s in _allowed_transitions(self._state))

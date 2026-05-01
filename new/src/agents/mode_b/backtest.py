"""C12 Backtest Agent. Mode B 전용. 장중 경로 절대 미개입."""
from __future__ import annotations

from src.agents._base import AgentBase
from src.utils.mode_guard import mode_b_only


class BacktestAgent(AgentBase):
    """C12 BacktestAgentContract. Mode B 전용 에이전트.

    불변 원칙 3: 장중 경로(Mode A) 절대 미개입.
    CAN_RUN_IN_MODE_A = False.

    forbidden_permissions (6개):
      1. 장중 주문 실행 (ExecutionGateway.submit_order) 호출 금지
      2. HotRunner 루프 개입 금지
      3. MessagePool에 Hot Path 채널 publish 금지
      4. PortfolioManager.apply_patch() 직접 호출 금지
      5. FDAAgent.decide() 직접 호출 금지
      6. KISWebSocketClient 실시간 스트림 구독 금지

    Mode B 스케줄: 18:00~22:00 KST.
    22:00 배포 게이트 통과 후 다음 날 Hot Path에 반영.
    Sprint 3 구현 예정.
    """

    CAN_RUN_IN_MODE_A: bool = False  # 불변 원칙 3. 절대 True로 변경 금지.

    @mode_b_only
    def report(self, report_type: str, payload: dict) -> dict:
        """C5 에이전트 리포트 생성. Mode B 전용."""
        raise NotImplementedError("Sprint 3 구현 예정")

    @mode_b_only
    def run(self, bundle_id: str) -> dict:
        """번들 ID 기준 백테스트 실행. C12 BacktestResult 반환."""
        raise NotImplementedError("Sprint 3 구현 예정")

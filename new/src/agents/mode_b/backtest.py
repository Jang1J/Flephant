"""C12 Backtest Agent. Mode B 전용. 장중 경로 절대 미개입."""
from __future__ import annotations

from src.agents._base import AgentBase
from src.utils.mode_guard import mode_b_only


class BacktestAgent(AgentBase):
    """C12 BacktestAgentContract. Mode B 전용 에이전트.

    불변 원칙 3: 장중 경로(Mode A) 절대 미개입.
    CAN_RUN_IN_MODE_A = False.

    W4 설계 명확화: BacktestAgent.run()/report()는 의도적 stub.
      실제 백테스트 엔진은 new/src/ops/validation_tools.py BacktestEngine에 구현됨.
      ModeBScheduler.stage_6_backtest_validation()이 직접 BacktestEngine을 호출하는 구조.
      BacktestAgent는 장중 완전 격리를 위한 권한 제어 레이어이며,
      Sprint 3 S3-9에서 BacktestEngine 호출 wrapper로 실구현 예정.

    forbidden_permissions (6개, C12 SSOT):
      1. 장중 주문 실행 (ExecutionGateway.submit_order) 호출 금지
      2. HotRunner 루프 개입 금지
      3. MessagePool에 Hot Path 채널 publish 금지
      4. PortfolioManager.apply_patch() 직접 호출 금지
      5. FDAAgent.decide() 직접 호출 금지
      6. KISWebSocketClient 실시간 스트림 구독 금지

    C4 분리 참조: ModeBScheduler(C14)의 forbidden_permissions = 4개 (별도 계약서).
    BacktestAgent(C12)의 6개와 C14의 4개는 다른 집합. 혼용 금지.

    Mode B 스케줄: 18:00~22:00 KST.
    22:00 배포 게이트 통과 후 다음 날 Hot Path에 반영.
    Sprint 3 S3-9에서 BacktestEngine 연동 구현 예정.
    """

    CAN_RUN_IN_MODE_A: bool = False  # 불변 원칙 3. 절대 True로 변경 금지.

    @mode_b_only
    def report(self, report_type: str, payload: dict) -> dict:
        """C5 에이전트 리포트 생성. Mode B 전용.

        stub: Sprint 3 S3-9에서 validation_tools.BacktestEngine 연동 구현 예정.
        """
        raise NotImplementedError("Sprint 3 S3-9 구현 예정 (validation_tools.BacktestEngine 연동)")

    @mode_b_only
    def run(self, bundle_id: str) -> dict:
        """번들 ID 기준 백테스트 실행. C12 BacktestResult 반환.

        stub: Sprint 3 S3-9에서 validation_tools.BacktestEngine 연동 구현 예정.
        실제 엔진: new/src/ops/validation_tools.py BacktestEngine.
        """
        raise NotImplementedError("Sprint 3 S3-9 구현 예정 (validation_tools.BacktestEngine 연동)")

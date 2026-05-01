"""Alpha Factor Agent. LLM Idea/Factor/Eval 루프. GPT-4o 전용."""
from __future__ import annotations

from src.agents._base import AgentBase
from src.utils.mode_guard import mode_b_only


class AlphaFactorAgent(AgentBase):
    """Mode B Alpha Factor 자동 생성/평가 에이전트.

    RD-Agent(Q) Co-STEER 기반. GPT-4o 전용 (Mode B = GPT-4o 불변 원칙 4).
    루프: Idea 생성 → Factor 코드 생성 → Eval 백테스트 → 채택/기각.
    AlphaAgent AST 독창성 + 가설 정합 + 복잡도 3중 정규화 적용 예정.
    Sprint 3 구현 예정.
    """

    @mode_b_only
    def report(self, report_type: str, payload: dict) -> dict:
        """C5 에이전트 리포트 생성. Mode B 전용."""
        raise NotImplementedError("Sprint 3 구현 예정")

    @mode_b_only
    def generate_factor(self, hypothesis: str) -> dict:
        """가설 문자열로부터 alpha factor 코드 생성."""
        raise NotImplementedError("Sprint 3 구현 예정")

    @mode_b_only
    def evaluate_factor(self, factor_code: str, bundle_id: str) -> dict:
        """생성된 factor를 백테스트 번들에서 평가. IC/IR 반환."""
        raise NotImplementedError("Sprint 3 구현 예정")

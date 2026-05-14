"""Alpha Factor Agent. Mode B Idea/Factor/Eval 오케스트레이터.

이 파일은 S0 시점의 얇은 agent shell 이었고, 실제 구현은
``src.mode_b.alpha_factor`` 패키지의 IdeaAgent / FactorAgent / EvalAgent로
발전했다. 공개 import 경로를 깨지 않기 위해 shell을 제거하지 않고
실 구현체로 위임한다.
"""
from __future__ import annotations

import ast
import hashlib
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.agents._base import AgentBase
from src.mode_b.alpha_factor.eval_agent import EvalAgent
from src.mode_b.alpha_factor.factor_agent import FactorAgent, FactorCandidate
from src.mode_b.alpha_factor.idea_agent import Hypothesis, IdeaAgent
from src.utils.mode_guard import mode_b_only

_KST = ZoneInfo("Asia/Seoul")


class AlphaFactorAgent(AgentBase):
    """Mode B Alpha Factor 자동 생성/평가 facade.

    RD-Agent(Q) Co-STEER 기반. GPT-4o 전용 (Mode B = GPT-4o 불변 원칙 4).
    루프: Idea 생성 → Factor 코드 생성 → Eval 백테스트 → 채택/기각.
    AlphaAgent AST 독창성 + 가설 정합 + 복잡도 3중 정규화는 EvalAgent가 수행.
    """

    ALLOWED_PUBLISH_CHANNELS = frozenset({"theme_score"})

    def __init__(
        self,
        llm_router: Any = None,
        idea_agent: IdeaAgent | None = None,
        factor_agent: FactorAgent | None = None,
        eval_agent: EvalAgent | None = None,
    ) -> None:
        self._llm_router = llm_router
        self._idea_agent = idea_agent or IdeaAgent(llm_router=llm_router)
        self._factor_agent = factor_agent or FactorAgent(llm_router=llm_router)
        self._eval_agent = eval_agent or EvalAgent(llm_router=llm_router)

    @mode_b_only
    def report(self, report_type: str, payload: dict) -> dict:
        """C5 호환 리포트 생성. Mode B 전용."""
        if report_type not in {"theme_score", "factor_eval"}:
            raise ValueError(
                "AlphaFactorAgent.report: 지원하지 않는 report_type="
                f"{report_type!r}"
            )
        return {
            "agent": "alpha_factor",
            "report_type": report_type,
            "payload": dict(payload or {}),
            "ts": datetime.now(_KST).isoformat(),
        }

    @mode_b_only
    def generate_factor(self, hypothesis: str | Hypothesis) -> dict:
        """가설 문자열 또는 Hypothesis로부터 alpha factor 후보 생성."""
        hyp = hypothesis if isinstance(hypothesis, Hypothesis) else Hypothesis(
            observation=str(hypothesis),
            knowledge="사용자 제공 가설",
            justification="Mode B Alpha Factor 후보 생성",
            specification=str(hypothesis),
            created_at=datetime.now(_KST).isoformat(),
            hypothesis_id=self._new_hypothesis_id(),
        )
        candidate = self._factor_agent.implement(hyp)
        return candidate.to_dict()

    @mode_b_only
    def evaluate_factor(self, factor_code: str, bundle_id: str) -> dict:
        """생성된 factor 코드를 3중 정규화 + IC 기준으로 평가."""
        hyp = Hypothesis(
            observation=f"bundle={bundle_id}",
            knowledge="factor_code 직접 평가",
            justification="Mode B 후보 품질 검증",
            specification="direct_factor_code",
            created_at=datetime.now(_KST).isoformat(),
            hypothesis_id=self._new_hypothesis_id(),
        )
        candidate = self._candidate_from_code(factor_code, hyp)
        result = self._eval_agent.evaluate(candidate, hyp)
        out = result.to_dict()
        out["bundle_id"] = str(bundle_id)
        return out

    @staticmethod
    def _new_hypothesis_id() -> str:
        stamp = datetime.now(_KST).strftime("%Y%m%d")
        return f"HYP-{stamp}-{hashlib.sha256(str(datetime.now(_KST).timestamp()).encode()).hexdigest()[:8].upper()}"

    @staticmethod
    def _candidate_from_code(code: str, hypothesis: Hypothesis) -> FactorCandidate:
        tree = ast.parse(code)
        ast_dump = ast.dump(tree, annotate_fields=False)
        ast_hash = hashlib.sha256(ast_dump.encode()).hexdigest()[:16]
        node_count = sum(1 for _ in ast.walk(tree))
        stamp = datetime.now(_KST).strftime("%Y%m%d")
        candidate_id = f"FAC-{stamp}-{ast_hash[:8].upper()}"
        return FactorCandidate(
            candidate_id=candidate_id,
            hypothesis_id=hypothesis.hypothesis_id,
            code=code,
            ast_hash=ast_hash,
            ast_node_count=node_count,
            description=hypothesis.specification,
            status="active",
            attempt_count=1,
            created_at=datetime.now(_KST).isoformat(),
            error=None,
        )

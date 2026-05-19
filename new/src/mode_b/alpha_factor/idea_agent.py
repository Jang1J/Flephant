"""S3-1 Alpha Factor Engine: Idea Agent.

AlphaAgent 논문 §1 기반. GPT-4o가 4요소 가설을 생성.
evolving anchor: 이전 성공 가설을 anchor로 진화 루프.
caller='factor_hypothesis' (risk_config.yaml mode_b_allowed_callers 화이트리스트).

Mode B 전용. 장중 호출 금지 (mode_guard 없이도 LLMRouter가 mode_b 전용 강제).
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.mode_guard import mode_b_only

logger = get_logger("idea_agent")
_KST = ZoneInfo("Asia/Seoul")

# Operator Library (AlphaAgent §1 Operator Library)
OPERATOR_LIBRARY: list[str] = [
    "ts_mean", "ts_std", "ts_momentum", "ts_zscore", "ts_rank",
    "cs_rank", "cs_zscore", "cs_neutralize",
    "conditional", "correlation",
    "rank", "ts_argmax", "ts_argmin",
    "sector_mean", "sector_std",
]

_HYPOTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "observation": {"type": "string"},
        "knowledge": {"type": "string"},
        "justification": {"type": "string"},
        "specification": {"type": "string"},
    },
    "required": ["observation", "knowledge", "justification", "specification"],
}


def _llm_content(response: Any) -> str:
    """LLMRouter의 LLMCallResult 또는 테스트용 str mock을 문자열로 정규화."""
    if hasattr(response, "success") and hasattr(response, "content"):
        if not bool(getattr(response, "success")):
            raise RuntimeError(str(getattr(response, "error", "LLM_CALL_FAILED")))
        content = getattr(response, "content", None)
        if content is None:
            raise RuntimeError("LLM_EMPTY_CONTENT")
        return str(content)
    return str(response)


@dataclass
class Hypothesis:
    """AlphaAgent 4요소 가설.

    Fields:
        observation:    오늘 시장에서 관찰된 패턴
        knowledge:      관련 금융 이론 참조
        justification:  이 팩터가 수익률을 예측하는 근거
        specification:  Python 구현 제약 (Operator + 윈도우 힌트)
        anchor_id:      진화 기반 가설 ID (최초 생성 시 None)
        created_at:     생성 시각 (ISO format, KST)
        hypothesis_id:  고유 식별자 (HYP-{YYYYMMDD}-{UUID8})
    """

    observation: str
    knowledge: str
    justification: str
    specification: str
    anchor_id: str | None = None
    created_at: str = ""
    hypothesis_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IdeaAgent:
    """GPT-4o 기반 Alpha Factor 4요소 가설 생성 에이전트.

    Mode B 전용. LLMRouter caller='factor_hypothesis'.
    evolving anchor: 이전 가설 기반 진화.
    JSONL 저장: artifacts/alpha_factor/hypotheses.jsonl (risk_config.yaml alpha_factor.hypothesis_path).

    llm_router: LLMRouter 인스턴스 주입. None이면 fallback template 사용.
    """

    def __init__(self, llm_router: Any = None) -> None:
        cfg = config_load("risk_config.yaml", "alpha_factor") or {}
        hypothesis_path = cfg.get(
            "hypothesis_path", "artifacts/alpha_factor/hypotheses.jsonl"
        )
        self._hypothesis_path = Path(hypothesis_path)
        self._hypothesis_path.parent.mkdir(parents=True, exist_ok=True)
        self._llm_router = llm_router
        self._max_hypotheses_per_round: int = int(
            cfg.get("max_hypotheses_per_round", 3)
        )
        logger.info("[idea_agent] 초기화. path=%s", self._hypothesis_path)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    @mode_b_only
    def generate(
        self,
        market_context: dict[str, Any],
        anchor: Hypothesis | None = None,
    ) -> Hypothesis:
        """4요소 가설 1개 생성.

        Args:
            market_context: 오늘 시장 요약 dict.
                keys: date, news_summary, risk_level, sector_moves, macro_signals
            anchor: 이전 라운드 성공 가설 (evolving anchor, optional).

        Returns:
            Hypothesis: GPT-4o 생성 가설 또는 fallback 가설.
        """
        prompt = self._build_prompt(market_context, anchor)
        raw_response = ""

        if self._llm_router is not None:
            try:
                raw_response = _llm_content(
                    self._llm_router.call(
                        prompt,
                        mode="mode_b",
                        caller="factor_hypothesis",
                        structured_schema=_HYPOTHESIS_SCHEMA,
                    )
                )
            except Exception as e:
                logger.warning("[idea_agent] LLM 호출 실패: %s. fallback 사용", e)
                raw_response = self._fallback_hypothesis_text(market_context, anchor)
        else:
            raw_response = self._fallback_hypothesis_text(market_context, anchor)

        hypothesis = self._parse_response(raw_response, anchor)
        self._save_hypothesis(hypothesis)
        logger.info("[idea_agent] 가설 생성 완료: %s", hypothesis.hypothesis_id)
        return hypothesis

    @mode_b_only
    def generate_batch(
        self,
        market_context: dict[str, Any],
        anchors: list[Hypothesis | None] | None = None,
    ) -> list[Hypothesis]:
        """복수 가설 생성 (최대 max_hypotheses_per_round).

        Args:
            market_context: 오늘 시장 요약 dict.
            anchors: 각 가설 생성에 사용할 anchor 목록.
                None이면 모든 가설을 anchor 없이 생성.

        Returns:
            list[Hypothesis]: 생성된 가설 목록.
        """
        if anchors is None:
            anchors_list: list[Hypothesis | None] = [None] * self._max_hypotheses_per_round
        else:
            anchors_list = list(anchors)

        results: list[Hypothesis] = []
        limit = self._max_hypotheses_per_round
        for i, anchor in enumerate(anchors_list[:limit]):
            logger.info(
                "[idea_agent] 가설 %d/%d 생성 중", i + 1, min(len(anchors_list), limit)
            )
            h = self.generate(market_context, anchor=anchor)
            results.append(h)
        return results

    def load_latest_hypotheses(self, n: int = 5) -> list[Hypothesis]:
        """JSONL에서 최근 n개 가설 로드 (evolving anchor 용).

        Args:
            n: 로드할 최대 가설 수.

        Returns:
            list[Hypothesis]: 최근 n개 가설. 파일 없으면 빈 리스트.
        """
        if not self._hypothesis_path.exists():
            return []

        lines: list[str] = []
        with self._hypothesis_path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    lines.append(stripped)

        out: list[Hypothesis] = []
        for line in lines[-n:]:
            try:
                d = json.loads(line)
                h = Hypothesis(
                    observation=d.get("observation", ""),
                    knowledge=d.get("knowledge", ""),
                    justification=d.get("justification", ""),
                    specification=d.get("specification", ""),
                    anchor_id=d.get("anchor_id"),
                    created_at=d.get("created_at", ""),
                    hypothesis_id=d.get("hypothesis_id", ""),
                )
                out.append(h)
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("[idea_agent] 가설 파싱 실패: %s", e)
        return out

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _build_prompt(
        self,
        market_context: dict[str, Any],
        anchor: Hypothesis | None,
    ) -> str:
        """GPT-4o 프롬프트 빌드.

        anchor가 있으면 "Evolving Anchor" 섹션 포함. 없으면 생략.
        """
        ops_str = ", ".join(OPERATOR_LIBRARY)

        anchor_section = ""
        if anchor is not None:
            anchor_section = f"""
## Evolving Anchor (이전 검증 가설)
기존 가설을 발전시켜라. 이것을 anchor로 삼되 독창성을 유지하라.

observation: {anchor.observation}
knowledge: {anchor.knowledge}
justification: {anchor.justification}
specification: {anchor.specification}
"""

        return (
            f"당신은 KOSPI 퀀트 팩터 연구자다.\n"
            f"오늘 시장 정보를 바탕으로 AlphaAgent 4요소 가설을 생성하라.\n"
            f"\n"
            f"## 오늘 시장 Context\n"
            f"날짜: {market_context.get('date', 'unknown')}\n"
            f"뉴스 요약: {market_context.get('news_summary', '없음')}\n"
            f"리스크 레벨: {market_context.get('risk_level', 'medium')}\n"
            f"섹터 움직임: {market_context.get('sector_moves', '없음')}\n"
            f"거시 신호: {market_context.get('macro_signals', '없음')}\n"
            f"{anchor_section}\n"
            f"## Operator Library\n"
            f"사용 가능한 연산자: {ops_str}\n"
            f"\n"
            f"## 출력 형식 (반드시 JSON)\n"
            f"```json\n"
            f"{{\n"
            f'  "observation": "오늘 시장에서 관찰된 구체적 패턴 (1~2문장)",\n'
            f'  "knowledge": "관련 금융 이론 또는 선행 연구 (1~2문장)",\n'
            f'  "justification": "왜 이 팩터가 향후 1~5분 수익률을 예측하는지 논리적 근거 (2~3문장)",\n'
            f'  "specification": "Operator Library에서 2~3개 연산자를 사용한 수식 힌트 (1~2문장)"\n'
            f"}}\n"
            f"```\n"
            f"\n"
            f"JSON만 출력하라. 설명 없이."
        )

    def _parse_response(
        self,
        raw: str,
        anchor: Hypothesis | None,
    ) -> Hypothesis:
        """LLM 응답 파싱. JSON 파싱 실패 시 fallback 필드 사용."""
        hyp_id = (
            f"HYP-{datetime.now(_KST).strftime('%Y%m%d')}-"
            f"{uuid.uuid4().hex[:8].upper()}"
        )

        parsed: dict[str, Any] = {}
        try:
            # ```json ... ``` 블록 우선 추출
            match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
            if match:
                parsed = json.loads(match.group(1))
            else:
                parsed = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            logger.warning("[idea_agent] JSON 파싱 실패. fallback 필드 사용")
            parsed = {
                "observation": raw[:200] if raw else "파싱 실패",
                "knowledge": "fallback",
                "justification": "fallback",
                "specification": "ts_zscore(cs_rank, window=30)",
            }

        return Hypothesis(
            observation=str(parsed.get("observation", "")),
            knowledge=str(parsed.get("knowledge", "")),
            justification=str(parsed.get("justification", "")),
            specification=str(parsed.get("specification", "")),
            anchor_id=anchor.hypothesis_id if anchor is not None else None,
            created_at=datetime.now(_KST).isoformat(),
            hypothesis_id=hyp_id,
        )

    def _fallback_hypothesis_text(
        self,
        market_context: dict[str, Any],
        anchor: Hypothesis | None,
    ) -> str:
        """LLM 없을 때 template 기반 fallback JSON 문자열."""
        risk = market_context.get("risk_level", "medium")
        anchor_note = ""
        if anchor is not None:
            anchor_note = f" (anchor: {anchor.hypothesis_id}에서 진화)"
        return json.dumps(
            {
                "observation": f"리스크 레벨 {risk} 환경. 외국인 수급 변동 관찰.{anchor_note}",
                "knowledge": "외국인 수급은 단기 모멘텀을 선행하는 경향이 있다.",
                "justification": (
                    "외국인 순매수가 증가하면 기관 추종 매수로 단기 상승 모멘텀 발생."
                    " 반대로 순매도 급증 시 지지선 붕괴 확률 상승."
                ),
                "specification": (
                    "ts_zscore(investor_flow, window=20) with cs_rank neutralize(sector)"
                ),
            },
            ensure_ascii=False,
        )

    def _save_hypothesis(self, hypothesis: Hypothesis) -> None:
        """가설을 JSONL 파일에 append."""
        with self._hypothesis_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(hypothesis.to_dict(), ensure_ascii=False) + "\n")

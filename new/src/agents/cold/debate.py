"""C6 Debate Agent. pairwise CoT, Kanana-o, 충돌 시만 호출.

S2-9 실구현. Cold Path 에이전트 간 신호 충돌 시 호출.
pairwise 비교 최대 45회 (C(10,2) = 45 쌍). Kanana-o 100회/일 예산 내.
결과: debate_resolution / pairwise_ranking publish + FDA에 전달.

불변 원칙 준수:
  - mode='cold': Kanana-o 사용 (불변 원칙 4)
  - 임계값/설정: risk_config.yaml 경유 (불변 원칙 5)
  - weight 수정 불가: Debate 결과는 FDA 입력용, 비중 변경 금지 (불변 원칙 2)
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")

from src.agents._base import AgentBase
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger

logger = get_logger("debate")

_DEFAULT_MEMORY_ROOT = (
    Path(__file__).resolve().parents[4] / "artifacts" / "agent_memory"
)
_CALLER = "debate"
# max_pairwise SSOT: risk_config.yaml debate.max_pairwise (기본값 45 = C(10,2))


class DebateAgent(AgentBase):
    """Cold Path 에이전트 간 신호 충돌 해소 에이전트.

    C6 DebatePairwiseRankingContract 구현.
    충돌 감지 시에만 호출. 비충돌 시 skip (퀀트 점수 순 유지).
    """

    ALLOWED_PUBLISH_CHANNELS: frozenset[str] = frozenset(
        {"debate_resolution", "pairwise_ranking"}
    )

    # 충돌 패턴 (C6 conflict_criteria)
    _CONFLICT_PATTERNS = [
        ("quant_top10", "veto_recommendation"),
        ("quant_buy", "risk_high"),
        ("news_sell", "quant_top5"),
    ]

    def __init__(
        self,
        llm_router: Any,
        pubsub: Any | None = None,
        memory_root: Path | str | None = None,
    ) -> None:
        self._llm_router = llm_router
        self._pubsub = pubsub
        self._memory_root = (
            Path(memory_root) if memory_root else _DEFAULT_MEMORY_ROOT
        )
        self._max_pairwise = self._load_max_pairwise()
        self._uncertainty_threshold = self._load_uncertainty_threshold()

    def _load_max_pairwise(self) -> int:
        """risk_config.yaml debate.max_pairwise 로드 (불변 원칙 5).

        비상 기본값 45 = C(10,2). yaml 로드 실패 시만 사용.
        """
        try:
            cfg = config_load("risk_config.yaml", "debate") or {}
            return int(cfg.get("max_pairwise", 45))
        except Exception as e:
            logger.warning("[debate] max_pairwise 로드 실패: %s. 기본값 사용", e)
            return 45

    def _load_uncertainty_threshold(self) -> float:
        """risk_config.yaml debate.uncertainty_threshold 로드 (불변 원칙 5)."""
        try:
            cfg = config_load("risk_config.yaml", "debate") or {}
            return float(cfg.get("uncertainty_threshold", 0.7))
        except Exception as e:
            logger.warning("[debate] uncertainty_threshold 로드 실패: %s. 기본값 사용", e)
            return 0.7

    # ------------------------------------------------------------------
    # C5/C6 report 생성
    # ------------------------------------------------------------------

    def report(self, report_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """C5/C6 debate_resolution / pairwise_ranking 리포트 생성."""
        allowed = {"debate_resolution", "pairwise_ranking"}
        if report_type not in allowed:
            raise ValueError(
                f"[debate] report_type={report_type} 미지원. 허용: {sorted(allowed)}"
            )
        channel = report_type
        msg = self.publish(channel, payload)
        msg["report_type"] = report_type
        msg["ts"] = datetime.now(timezone.utc).isoformat()
        return msg

    # ------------------------------------------------------------------
    # 메인 진입점
    # ------------------------------------------------------------------

    def run_debate(
        self,
        signals: list[dict[str, Any]],
        candidates: list[str] | None = None,
    ) -> dict[str, Any]:
        """충돌 신호 목록에 대해 pairwise CoT 토론 실행. 합의 결과 반환.

        충돌 없으면 퀀트 점수 순 유지 (Debate 미호출).

        Args:
            signals: 에이전트 신호 리스트.
              각 항목: {agent, channel, payload, ts}
            candidates: 대상 ticker 목록. None이면 signals에서 추출.

        Returns:
            {
              conflict_detected: bool,
              debate_id: str,
              winner_view: str,
              ranked_tickers: list[str],
              uncertainty_delta: float,  # -1.0 ~ +1.0 (양수=불확실성 증가)
              comparison_count: int,
              debate_resolution_msg: dict | None,
              pairwise_msgs: list[dict],
              skipped_reason: str | None,
            }
        """
        conflict = self._detect_conflict(signals)

        if not conflict["detected"]:
            logger.info(
                "[debate] 충돌 없음. debate skip. skipped_reason=%s",
                conflict.get("reason", "no_conflict"),
            )
            return {
                "conflict_detected": False,
                "debate_id": None,
                "winner_view": None,
                "ranked_tickers": candidates or [],
                "uncertainty_delta": 0.0,
                "comparison_count": 0,
                "debate_resolution_msg": None,
                "pairwise_msgs": [],
                "skipped_reason": conflict.get("reason", "no_conflict"),
            }

        debate_id = f"DEB-{datetime.now(_KST).strftime('%Y%m%d%H%M%S')}"
        logger.info(
            "[debate] 충돌 감지. debate_id=%s patterns=%s",
            debate_id, conflict.get("patterns", []),
        )

        # pairwise CoT 비교
        pairs = self._build_pairs(signals)
        pairs = pairs[: self._max_pairwise]
        all_pair_results: list[dict[str, Any]] = []
        pairwise_msgs: list[dict[str, Any]] = []

        for s1, s2 in pairs:
            cmp_result = self._pairwise_compare(s1, s2, conflict)
            winner = cmp_result.get("winner", s1.get("agent", "quant"))
            all_pair_results.append({
                "winner": winner,
                "pair": [s1.get("agent"), s2.get("agent")],
                "reasoning": cmp_result.get("reasoning", ""),
            })

        # C6 pairwise_ranking: 집계 후 1회 publish
        win_counts: dict[str, int] = {}
        for pr in all_pair_results:
            winner = pr.get("winner")
            if winner:
                win_counts[winner] = win_counts.get(winner, 0) + 1

        ranked = sorted(win_counts, key=lambda t: win_counts[t], reverse=True)
        pr_msg = self.publish("pairwise_ranking", {
            "wins": [{"ticker": t, "win_count": win_counts[t]} for t in ranked],
            "ranked": ranked,
            "top_k": ranked[:3],
            "total_pairs": len(all_pair_results),
            "ts": datetime.now(_KST).isoformat(),
        })
        pairwise_msgs.append(pr_msg)

        wins = win_counts

        # 최종 합의
        if wins:
            winner_view = max(wins, key=wins.__getitem__)
        else:
            winner_view = "mixed"

        # uncertainty_delta: 의견 갈림 정도
        total = sum(wins.values())
        if total > 0:
            top_share = max(wins.values()) / total
            uncertainty_delta = round(1.0 - top_share, 2)  # 0.0 (완전합의) ~ 1.0 (완전불일치)
        else:
            uncertainty_delta = 0.5

        # ranked_tickers (candidates 기준, debate 결과로 재정렬 스텁)
        ranked = candidates or []

        # debate_resolution 리포트
        conflict_patterns = conflict.get("patterns", [])
        resolution_payload = {
            "conflict_id": debate_id,
            "winner_view": winner_view,
            "conflict_patterns": conflict_patterns,
            "wins": [{"agent": a, "win_count": c} for a, c in wins.items()],
            "comparison_count": len(pairs),
            "uncertainty_delta": uncertainty_delta,
            "ranked_tickers": ranked,
            "reasoning": f"winner_view={winner_view}, patterns={len(conflict_patterns)}건 해소",
        }
        resolution_msg = self.report("debate_resolution", resolution_payload)

        # memory 저장
        self._save_debate_history(debate_id, resolution_payload)

        return {
            "conflict_detected": True,
            "debate_id": debate_id,
            "winner_view": winner_view,
            "ranked_tickers": ranked,
            "uncertainty_delta": uncertainty_delta,
            "comparison_count": len(pairs),
            "debate_resolution_msg": resolution_msg,
            "pairwise_msgs": pairwise_msgs,
            "skipped_reason": None,
        }

    # ------------------------------------------------------------------
    # 충돌 감지
    # ------------------------------------------------------------------

    def _detect_conflict(self, signals: list[dict[str, Any]]) -> dict[str, Any]:
        """에이전트 신호에서 충돌 패턴 감지.

        C6 conflict_criteria 3개:
          - quant_top10 vs agent veto_recommendation
          - quant_buy vs risk_high
          - news_sell vs quant_top5
        """
        channels = {s.get("channel", ""): s for s in signals}
        payloads = {s.get("channel", ""): s.get("payload", {}) for s in signals}

        detected_patterns: list[str] = []

        # 패턴 1: quant_signal Top10이 있는데 risk_warning이 veto_recommendation
        if "quant_signal" in channels and "risk_warning" in channels:
            quant_top10 = payloads.get("quant_signal", {}).get("top10_candidates", [])
            risk_stance = payloads.get("risk_warning", {}).get("stance", "neutral")
            if quant_top10 and risk_stance == "veto_recommendation":
                detected_patterns.append("quant_top10 vs agent_veto_recommendation")

        # 패턴 2: quant_signal Top5 buy와 risk_warning risk_level=high 동시
        if "quant_signal" in channels and "risk_warning" in channels:
            top5 = payloads.get("quant_signal", {}).get("top10_candidates", [])[:5]
            risk_level = payloads.get("risk_warning", {}).get("risk_level", "low")
            if top5 and risk_level == "high":
                detected_patterns.append("quant_buy vs risk_high")

        # 패턴 3: news_signal sell과 quant top5 동시
        if "news_signal" in channels and "quant_signal" in channels:
            news_stance = payloads.get("news_signal", {}).get("stance", "neutral")
            quant_top5 = payloads.get("quant_signal", {}).get("top10_candidates", [])[:5]
            if news_stance == "sell" and quant_top5:
                detected_patterns.append("news_sell vs quant_top5")

        return {
            "detected": len(detected_patterns) > 0,
            "patterns": detected_patterns,
            "reason": "no_conflict" if not detected_patterns else "conflict",
        }

    # ------------------------------------------------------------------
    # Pairwise 비교
    # ------------------------------------------------------------------

    def _build_pairs(
        self, signals: list[dict[str, Any]]
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """신호 리스트에서 pairwise 쌍 생성."""
        pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for i in range(len(signals)):
            for j in range(i + 1, len(signals)):
                pairs.append((signals[i], signals[j]))
        return pairs

    def _pairwise_compare(
        self,
        s1: dict[str, Any],
        s2: dict[str, Any],
        conflict_ctx: dict[str, Any],
    ) -> dict[str, Any]:
        """두 신호 간 Kanana-o CoT 1:1 비교. 예산 부족 시 heuristic fallback."""
        prompt = (
            f"두 에이전트 신호를 비교하여 어느 쪽이 더 신뢰할 수 있는지 판단하세요.\n\n"
            f"신호 A ({s1.get('agent', '?')}, 채널: {s1.get('channel', '?')}):\n"
            f"{json.dumps(s1.get('payload', {}), ensure_ascii=False)}\n\n"
            f"신호 B ({s2.get('agent', '?')}, 채널: {s2.get('channel', '?')}):\n"
            f"{json.dumps(s2.get('payload', {}), ensure_ascii=False)}\n\n"
            f"충돌 맥락: {', '.join(conflict_ctx.get('patterns', []))}\n\n"
            "JSON으로 응답: "
            '{"winner": "A_agent|B_agent|tied", "confidence": 0.0~1.0, "reasoning": "한국어 1문장"}'
        )

        llm_result = self._llm_router.call(
            prompt, mode="cold", caller=_CALLER
        )

        if not llm_result.success:
            # heuristic: 리스크 신호 우선
            winner = s1.get("agent", "quant")
            if "risk" in s2.get("agent", "").lower():
                winner = s2.get("agent", winner)
            return {"winner": winner, "reasoning": "[LLM 실패, heuristic fallback]"}

        return self._parse_comparison(llm_result.content, s1, s2)

    def _parse_comparison(
        self,
        content: str,
        s1: dict[str, Any],
        s2: dict[str, Any],
    ) -> dict[str, Any]:
        """LLM 비교 응답 파싱."""
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
        try:
            parsed = json.loads(stripped)
            winner_raw = str(parsed.get("winner", "tied"))
            # A_agent → s1 agent 이름으로 해석
            if winner_raw.startswith("A") or winner_raw == s1.get("agent"):
                winner = s1.get("agent", "quant")
            elif winner_raw.startswith("B") or winner_raw == s2.get("agent"):
                winner = s2.get("agent", "risk")
            else:
                winner = "mixed"
            return {
                "winner": winner,
                "confidence": float(parsed.get("confidence", 0.5)),
                "reasoning": str(parsed.get("reasoning", "")),
            }
        except (json.JSONDecodeError, ValueError):
            return {
                "winner": s1.get("agent", "quant"),
                "confidence": 0.5,
                "reasoning": content[:100],
            }

    # ------------------------------------------------------------------
    # Memory 저장
    # ------------------------------------------------------------------

    def _save_debate_history(
        self, debate_id: str, result: dict[str, Any]
    ) -> None:
        """debate_history JSONL 저장."""
        today = datetime.now(_KST).strftime("%Y%m%d")
        path = self._memory_root / "debate_agent" / f"{today}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "debate_id": debate_id,
            "winner_view": result.get("winner_view"),
            "patterns": result.get("conflict_patterns", []),
            "comparison_count": result.get("comparison_count", 0),
            "uncertainty_delta": result.get("uncertainty_delta", 0.0),
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

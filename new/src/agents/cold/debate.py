"""C6 Debate Agent. pairwise CoT, Kanana-o, 충돌 시만 호출.

S2-9 실구현. Cold Path 에이전트 간 신호 충돌 시 호출.
pairwise 비교 최대 45쌍 (C(10,2))을 1회 batch 요청. Kanana-o 100회/일 예산 내.
결과: debate_resolution / pairwise_ranking publish + FDA에 전달.

불변 원칙 준수:
  - mode='cold': Kanana-o 사용 (불변 원칙 4)
  - 임계값/설정: risk_config.yaml 경유 (불변 원칙 5)
  - weight 수정 불가: Debate 결과는 FDA 입력용, 비중 변경 금지 (불변 원칙 2)
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")

from src.agents._base import AgentBase
from src.utils.config_loader import load as config_load
from src.utils.llm_parser import parse_llm_json
from src.utils.logger import get_logger
from src.utils.ticker_utils import pad_ticker

logger = get_logger("debate")

_DEFAULT_MEMORY_ROOT = (
    Path(__file__).resolve().parents[4] / "artifacts" / "agent_memory"
)
_CALLER = "debate_agent"
# max_pairwise SSOT: risk_config.yaml debate.max_pairwise (기본값 45 = C(10,2))


class DebateAgent(AgentBase):
    """Cold Path 에이전트 간 신호 충돌 해소 에이전트.

    C6 DebatePairwiseRankingContract 구현.
    충돌 감지 시에만 호출. 비충돌 시 skip (퀀트 점수 순 유지).
    """

    ALLOWED_PUBLISH_CHANNELS: frozenset[str] = frozenset(
        {"debate_resolution", "pairwise_ranking"}
    )

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
        self._max_runtime_sec = self._load_max_runtime_sec()
        self._uncertainty_threshold = self._load_uncertainty_threshold()
        self._conflict_criteria = self._load_conflict_criteria()

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

    def _load_max_runtime_sec(self) -> float:
        """risk_config.yaml debate.max_runtime_sec 로드."""
        try:
            debate_cfg = config_load("risk_config.yaml", "debate") or {}
            if "max_runtime_sec" in debate_cfg:
                return float(debate_cfg["max_runtime_sec"])
            bounded_cfg = config_load("risk_config.yaml", "bounded_execution") or {}
            return float(bounded_cfg.get("debate_agent_max_sec", 15))
        except Exception as e:
            logger.warning("[debate] max_runtime_sec 로드 실패: %s. 기본값 사용", e)
            return 15.0

    def _load_uncertainty_threshold(self) -> float:
        """risk_config.yaml debate.uncertainty_threshold 로드 (불변 원칙 5)."""
        try:
            cfg = config_load("risk_config.yaml", "debate") or {}
            return float(cfg.get("uncertainty_threshold", 0.7))
        except Exception as e:
            logger.warning("[debate] uncertainty_threshold 로드 실패: %s. 기본값 사용", e)
            return 0.7

    def _load_conflict_criteria(self) -> list[dict[str, str]]:
        """risk_config.yaml debate.conflict_criteria 로드 (불변 원칙 5).

        C6 SSOT: yaml에서만 정의. 코드 하드코딩 금지.

        Raises:
            KeyError: conflict_criteria 키가 yaml에 없는 경우 (설정 오류 명확화).
        """
        cfg = config_load("risk_config.yaml", "debate") or {}
        if "conflict_criteria" not in cfg:
            raise KeyError(
                "[debate] risk_config.yaml debate.conflict_criteria 섹션 누락. "
                "설정 오류. 시스템을 시작할 수 없음."
            )
        criteria = cfg["conflict_criteria"]
        if not isinstance(criteria, list):
            raise KeyError(
                f"[debate] debate.conflict_criteria 형식 오류: list 기대, got {type(criteria).__name__}"
            )
        return criteria

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

        ranked_base = self._resolve_candidates(candidates, signals)
        if not ranked_base:
            logger.warning("[debate] 충돌은 감지됐지만 후보 ticker 없음. debate skip.")
            return {
                "conflict_detected": True,
                "debate_id": debate_id,
                "winner_view": "mixed",
                "ranked_tickers": [],
                "uncertainty_delta": 0.5,
                "comparison_count": 0,
                "completed_comparisons": 0,
                "deadline_hit": False,
                "debate_resolution_msg": None,
                "pairwise_msgs": [],
                "skipped_reason": "no_candidates",
            }

        # C6 pairwise ranking: 후보 ticker끼리 비교해야 한다. LLM 호출은 1회 batch로 제한한다.
        pairs = self._build_ticker_pairs(ranked_base)[: self._max_pairwise]
        all_pair_results: list[dict[str, Any]] = []
        pairwise_msgs: list[dict[str, Any]] = []
        deadline = time.perf_counter() + self._max_runtime_sec
        deadline_hit = False

        if pairs:
            if time.perf_counter() >= deadline:
                deadline_hit = True
                all_pair_results = self._heuristic_fallback(pairs)
            else:
                all_pair_results = self._batch_pairwise_compare(pairs, signals, conflict)

        # C6 pairwise_ranking: 집계 후 1회 publish
        win_counts: dict[str, int] = {ticker: 0 for ticker in ranked_base}
        for pr in all_pair_results:
            winner = pr.get("winner")
            if winner in win_counts:
                win_counts[winner] = win_counts.get(winner, 0) + 1

        ranked = self._rank_tickers_by_win_count(ranked_base, win_counts)
        pr_msg = self.publish("pairwise_ranking", {
            "wins": [{"ticker": t, "win_count": win_counts[t]} for t in ranked],
            "ranked": ranked,
            "top_k": ranked[:3],
            "comparison_count": len(pairs),
            "completed_comparisons": len(all_pair_results),
            "total_pairs": len(pairs),  # legacy field, C6 clients should read comparison_count.
            "ts": datetime.now(_KST).isoformat(),
        })
        pairwise_msgs.append(pr_msg)

        wins = win_counts

        # C6 winner_view는 ticker가 아니라 관점 enum {"news","risk","quant","mixed"}만 허용.
        winner_view = self._infer_winner_view(signals)

        # uncertainty_delta: 의견 갈림 정도
        total = sum(wins.values())
        if total > 0:
            top_share = max(wins.values()) / total
            uncertainty_delta = round(1.0 - top_share, 2)  # 0.0 (완전합의) ~ 1.0 (완전불일치)
        else:
            uncertainty_delta = 0.5

        # debate_resolution 리포트
        conflict_patterns = conflict.get("patterns", [])
        resolution_payload = {
            "conflict_id": debate_id,
            "winner_view": winner_view,
            "conflict_patterns": conflict_patterns,
            "wins": [{"ticker": t, "win_count": wins.get(t, 0)} for t in ranked],
            "comparison_count": len(pairs),
            "completed_comparisons": len(all_pair_results),
            "deadline_hit": deadline_hit,
            "uncertainty_delta": uncertainty_delta,
            "ranked_tickers": ranked,
            "reasoning": (
                f"winner_view={winner_view}, patterns={len(conflict_patterns)}건 해소"
                + ("; deadline_hit=true" if deadline_hit else "")
            ),
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
            "completed_comparisons": len(all_pair_results),
            "deadline_hit": deadline_hit,
            "debate_resolution_msg": resolution_msg,
            "pairwise_msgs": pairwise_msgs,
            "skipped_reason": None,
        }

    # ------------------------------------------------------------------
    # 충돌 감지
    # ------------------------------------------------------------------

    def _detect_conflict(self, signals: list[dict[str, Any]]) -> dict[str, Any]:
        """에이전트 신호에서 충돌 패턴 감지.

        C6 conflict_criteria (risk_config.yaml debate.conflict_criteria SSOT):
          - quant_top10 vs veto_recommendation
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

    def _resolve_candidates(
        self,
        candidates: list[str] | None,
        signals: list[dict[str, Any]],
    ) -> list[str]:
        """명시 candidates 우선, 없으면 quant/news/risk payload에서 ticker 후보 추출."""
        raw_candidates: list[Any] = list(candidates or [])
        if not raw_candidates:
            raw_candidates = self._extract_candidates(signals)

        resolved: list[str] = []
        seen: set[str] = set()
        for raw in raw_candidates:
            ticker = pad_ticker(str(raw))
            if not ticker.isdigit() or ticker in seen:
                continue
            resolved.append(ticker)
            seen.add(ticker)
            if len(resolved) >= 10:
                break
        return resolved

    def _extract_candidates(self, signals: list[dict[str, Any]]) -> list[str]:
        """signals payload에서 C6 후보 ticker 목록 추출."""
        out: list[str] = []
        list_keys = ("top10_candidates", "candidates", "ranked", "top_k", "tickers")
        for signal in signals:
            payload = signal.get("payload", {})
            if not isinstance(payload, dict):
                continue
            for key in list_keys:
                values = payload.get(key)
                if isinstance(values, list):
                    out.extend(str(v) for v in values)
            ticker = payload.get("ticker")
            if ticker:
                out.append(str(ticker))
        return out

    def _build_ticker_pairs(self, candidates: list[str]) -> list[tuple[str, str]]:
        """후보 ticker 리스트에서 pairwise 쌍 생성."""
        pairs: list[tuple[str, str]] = []
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                pairs.append((candidates[i], candidates[j]))
        return pairs

    def _batch_pairwise_compare(
        self,
        ticker_pairs: list[tuple[str, str]],
        signals: list[dict[str, Any]],
        conflict_ctx: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """후보 ticker pair 전체를 Kanana-o 1회 호출로 비교. 실패 시 기존 랭킹 fallback."""
        prompt = (
            "다음은 KOSPI 후보 종목 간 pairwise 재랭킹 요청입니다.\n"
            "각 pair마다 더 신뢰할 수 있는 후보 ticker를 하나 고르세요. "
            "판단이 같으면 기존 순서를 보존하기 위해 첫 번째 ticker를 고르세요.\n\n"
            f"충돌 맥락: {', '.join(conflict_ctx.get('patterns', []))}\n"
            f"에이전트 신호: {json.dumps(signals, ensure_ascii=False)}\n"
            f"비교 pair 목록: {json.dumps(ticker_pairs, ensure_ascii=False)}\n\n"
            "반드시 JSON만 응답하세요: "
            '{"results":[{"pair":["005930","000660"],"winner":"005930",'
            '"confidence":0.0,"reasoning":"한국어 1문장"}]}'
        )

        try:
            llm_result = self._llm_router.call(prompt, mode="cold", caller=_CALLER)
        except Exception as e:
            logger.warning("[debate] batch pairwise 호출 예외. heuristic fallback: %s", e)
            return self._heuristic_fallback(ticker_pairs)

        if not llm_result.success:
            logger.warning(
                "[debate] batch pairwise LLM 실패. heuristic fallback: %s",
                getattr(llm_result, "error", None),
            )
            return self._heuristic_fallback(ticker_pairs)

        return self._parse_batch_result(llm_result.content, ticker_pairs)

    def _parse_batch_result(
        self,
        content: str,
        ticker_pairs: list[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        """LLM batch 응답을 C6 pairwise 결과로 파싱."""
        try:
            parsed = parse_llm_json(content)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("[debate] batch 응답 파싱 실패. heuristic fallback: %s", e)
            return self._heuristic_fallback(ticker_pairs)

        raw_results = parsed.get("results", [])
        if not isinstance(raw_results, list):
            return self._heuristic_fallback(ticker_pairs)

        parsed_by_pair: dict[frozenset[str], dict[str, Any]] = {}
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            raw_pair = item.get("pair", [])
            if not isinstance(raw_pair, list) or len(raw_pair) != 2:
                continue
            left = pad_ticker(str(raw_pair[0]))
            right = pad_ticker(str(raw_pair[1]))
            if not left.isdigit() or not right.isdigit():
                continue
            winner = pad_ticker(str(item.get("winner", "")))
            if winner not in {left, right}:
                winner = left
            parsed_by_pair[frozenset((left, right))] = {
                "pair": [left, right],
                "winner": winner,
                "confidence": float(item.get("confidence", 0.5) or 0.5),
                "reasoning": str(item.get("reasoning", "")),
            }

        results: list[dict[str, Any]] = []
        for left, right in ticker_pairs:
            found = parsed_by_pair.get(frozenset((left, right)))
            if found:
                found["pair"] = [left, right]
                results.append(found)
            else:
                results.append({
                    "pair": [left, right],
                    "winner": left,
                    "confidence": 0.5,
                    "reasoning": "[LLM 누락 pair, 기존 순서 fallback]",
                })
        return results

    def _heuristic_fallback(
        self,
        ticker_pairs: list[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        """LLM 실패 시 기존 Quant 후보 순서를 보존하는 deterministic fallback."""
        return [
            {
                "pair": [left, right],
                "winner": left,
                "confidence": 0.5,
                "reasoning": "[LLM 실패, 기존 순서 fallback]",
            }
            for left, right in ticker_pairs
        ]

    def _rank_tickers_by_win_count(
        self,
        candidates: list[str],
        win_counts: dict[str, int],
    ) -> list[str]:
        """win_count 우선, 동률은 기존 후보 순서 보존."""
        original_order = {ticker: idx for idx, ticker in enumerate(candidates)}
        return sorted(
            candidates,
            key=lambda ticker: (-win_counts.get(ticker, 0), original_order[ticker]),
        )

    def _infer_winner_view(self, signals: list[dict[str, Any]]) -> str:
        """C6 winner_view enum 산출. ticker reranking과 agent-view enum을 분리한다."""
        has_quant = False
        has_news = False
        for signal in signals:
            channel = str(signal.get("channel", ""))
            payload = signal.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}
            if channel == "risk_warning":
                stance = str(payload.get("stance", ""))
                risk_level = str(payload.get("risk_level", ""))
                if stance == "veto_recommendation" or risk_level == "high":
                    return "risk"
            if channel == "news_signal":
                has_news = True
            if channel == "quant_signal":
                has_quant = True
        if has_news and has_quant:
            return "mixed"
        if has_news:
            return "news"
        if has_quant:
            return "quant"
        return "mixed"

    def _pairwise_compare(
        self,
        s1: dict[str, Any],
        s2: dict[str, Any],
        conflict_ctx: dict[str, Any],
    ) -> dict[str, Any]:
        """두 신호 간 Kanana-o 1:1 비교. legacy tests/backward compatibility용."""
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
        try:
            parsed = parse_llm_json(content)
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

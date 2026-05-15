"""S2-9: DebateAgent + FDA Cold Path unit tests."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.agents.cold.debate import DebateAgent
from src.agents.fda import FDAAgent


# ------------------------------------------------------------------ #
# fixtures
# ------------------------------------------------------------------ #

_DEFAULT_DEBATE_BATCH = (
    '{"results":['
    '{"pair":["005930","000660"],"winner":"005930","confidence":0.8,"reasoning":"삼성전자 우위"},'
    '{"pair":["005930","035420"],"winner":"005930","confidence":0.7,"reasoning":"삼성전자 우위"},'
    '{"pair":["000660","035420"],"winner":"000660","confidence":0.6,"reasoning":"하이닉스 우위"}'
    ']}'
)


def _make_router(content: str = _DEFAULT_DEBATE_BATCH) -> MagicMock:
    mock = MagicMock()
    mock.call.return_value = MagicMock(
        success=True,
        model_used="kanana-o",
        content=content,
        latency_ms=1200.0,
        error=None,
    )
    return mock


def _make_debate() -> DebateAgent:
    return DebateAgent(llm_router=_make_router(), pubsub=None)


def _make_fda(with_router: bool = True) -> FDAAgent:
    router = _make_router('{"approved": true, "reason_code": "NORMAL_APPROVE", "veto_reason": null, "confidence": 0.85}') if with_router else None
    return FDAAgent(llm_router=router)


def _signal(agent: str, channel: str, stance: str = "neutral") -> dict:
    return {
        "agent": agent,
        "channel": channel,
        "payload": {"stance": stance, "risk_level": "low"},
        "ts": "2026-04-26T10:00:00+09:00",
    }


# ------------------------------------------------------------------ #
# DebateAgent 테스트
# ------------------------------------------------------------------ #

def test_debate_no_conflict_skip() -> None:
    """충돌 없으면 debate skip, conflict_detected=False."""
    debate = _make_debate()
    signals = [
        _signal("news_agent", "news_signal", "buy"),
        _signal("risk_slow", "risk_warning", "neutral"),
        _signal("quant", "quant_signal", "neutral"),
    ]
    result = debate.run_debate(signals, candidates=["005930", "000660"])
    assert not result["conflict_detected"]
    assert result["skipped_reason"] == "no_conflict"
    assert result["comparison_count"] == 0
    assert debate._llm_router.call.call_count == 0  # LLM 미호출


def test_debate_conflict_triggers_pairwise() -> None:
    """veto_recommendation vs quant_top10 충돌 → pairwise 실행."""
    debate = _make_debate()
    quant_sig = {
        "agent": "quant",
        "channel": "quant_signal",
        "payload": {
            "stance": "neutral",
            "top10_candidates": ["005930", "000660", "035420"],
        },
        "ts": "2026-04-26T10:00:00+09:00",
    }
    risk_sig = _signal("risk_slow", "risk_warning", "veto_recommendation")
    signals = [quant_sig, risk_sig]
    result = debate.run_debate(signals, candidates=["005930", "000660", "035420"])
    assert result["conflict_detected"]
    assert result["comparison_count"] == 3
    assert result["completed_comparisons"] == 3
    assert result["ranked_tickers"][0] == "005930"
    assert result["winner_view"] in {"news", "risk", "quant", "mixed"}
    assert debate._llm_router.call.call_count == 1
    payload = result["pairwise_msgs"][0]["payload"]
    assert payload["comparison_count"] == 3
    assert payload["wins"][0]["ticker"] == "005930"


def test_debate_conflict_criteria_respects_yaml_rules() -> None:
    """C6 conflict_criteria가 하드코딩 패턴이 아니라 로드된 규칙을 따른다."""
    debate = _make_debate()
    debate._conflict_criteria = [
        {
            "signal_a": "news_sell",
            "signal_b": "quant_top5",
            "reason": "test-only criterion",
        }
    ]
    quant_sig = {
        "agent": "quant",
        "channel": "quant_signal",
        "payload": {
            "stance": "neutral",
            "top10_candidates": ["005930", "000660", "035420"],
        },
        "ts": "2026-04-26T10:00:00+09:00",
    }
    risk_sig = _signal("risk_slow", "risk_warning", "veto_recommendation")
    news_sig = _signal("news_agent", "news_signal", "sell")

    no_match = debate.run_debate([quant_sig, risk_sig], candidates=["005930", "000660"])
    matched = debate.run_debate([quant_sig, news_sig], candidates=["005930", "000660"])

    assert no_match["conflict_detected"] is False
    assert matched["conflict_detected"] is True
    assert matched["debate_resolution_msg"]["payload"]["conflict_patterns"] == [
        "news_sell vs quant_top5"
    ]


def test_debate_conflict_detects_multiple_same_channel_signals() -> None:
    """같은 channel의 마지막 payload만 남겨 충돌을 놓치지 않는다."""
    debate = _make_debate()
    debate._conflict_criteria = [
        {"signal_a": "news_sell", "signal_b": "quant_top5"},
    ]
    quant_sig = {
        "agent": "quant",
        "channel": "quant_signal",
        "payload": {"top10_candidates": ["005930", "000660"]},
        "ts": "2026-04-26T10:00:00+09:00",
    }
    news_sell = _signal("news_agent", "news_signal", "sell")
    news_neutral = _signal("news_agent", "news_signal", "neutral")

    result = debate.run_debate(
        [quant_sig, news_sell, news_neutral],
        candidates=["005930", "000660"],
    )

    assert result["conflict_detected"] is True
    assert result["debate_resolution_msg"]["payload"]["conflict_patterns"] == [
        "news_sell vs quant_top5"
    ]


def test_debate_max_pairwise_respected() -> None:
    """C6 max_pairwise=45 초과 쌍은 truncate."""
    debate = _make_debate()
    candidates = [f"{i:06d}" for i in range(1, 12)]
    quant_sig = {
        "agent": "quant",
        "channel": "quant_signal",
        "payload": {"stance": "neutral", "top10_candidates": candidates},
        "ts": "2026-04-26T10:00:00+09:00",
    }
    risk_sig = _signal("risk_slow", "risk_warning", "veto_recommendation")
    result = debate.run_debate([quant_sig, risk_sig], candidates=candidates)
    assert result["comparison_count"] == 45
    assert result["completed_comparisons"] == 45
    assert debate._llm_router.call.call_count == 1


def test_debate_report_schema() -> None:
    """debate_resolution 리포트가 C6 payload 스키마 포함."""
    debate = _make_debate()
    quant_sig = {
        "agent": "quant",
        "channel": "quant_signal",
        "payload": {
            "stance": "neutral",
            "top10_candidates": ["005930", "000660"],
        },
        "ts": "2026-04-26T10:00:00+09:00",
    }
    risk_sig = _signal("risk_slow", "risk_warning", "veto_recommendation")
    result = debate.run_debate([quant_sig, risk_sig])
    assert result["conflict_detected"]
    rpt = result["debate_resolution_msg"]
    assert rpt["channel"] == "debate_resolution"
    assert rpt["report_type"] == "debate_resolution"
    assert "ts" in rpt
    payload = rpt["payload"]
    assert "conflict_id" in payload
    assert "winner_view" in payload
    assert "uncertainty_delta" in payload


def test_debate_llm_failure_heuristic_fallback() -> None:
    """LLM 실패 시 heuristic fallback으로 debate 완료."""
    mock = MagicMock()
    mock.call.return_value = MagicMock(
        success=False, model_used=None, content="", latency_ms=0, error="timeout"
    )
    debate = DebateAgent(llm_router=mock, pubsub=None)
    quant_sig = {
        "agent": "quant",
        "channel": "quant_signal",
        "payload": {"stance": "neutral", "top10_candidates": ["005930", "000660"]},
        "ts": "2026-04-26T10:00:00+09:00",
    }
    risk_sig = _signal("risk_slow", "risk_warning", "veto_recommendation")
    result = debate.run_debate([quant_sig, risk_sig])
    assert result["conflict_detected"]  # heuristic으로도 완료


def test_debate_batch_invalid_confidence_does_not_crash() -> None:
    """LLM batch confidence가 비수치여도 fallback 0.5로 정규화된다."""
    mock = _make_router(
        '{"results":[{"pair":["005930","000660"],"winner":"005930",'
        '"confidence":"high","reasoning":"비수치 confidence"}]}'
    )
    debate = DebateAgent(llm_router=mock, pubsub=None)
    quant_sig = {
        "agent": "quant",
        "channel": "quant_signal",
        "payload": {"stance": "neutral", "top10_candidates": ["005930", "000660"]},
        "ts": "2026-04-26T10:00:00+09:00",
    }
    risk_sig = _signal("risk_slow", "risk_warning", "veto_recommendation")

    result = debate.run_debate([quant_sig, risk_sig])

    assert result["conflict_detected"] is True
    assert result["completed_comparisons"] == 1


def test_debate_llm_json_array_falls_back_without_crash() -> None:
    """LLM이 JSON array를 반환해도 DebateAgent는 heuristic fallback으로 완료한다."""
    mock = _make_router("[]")
    debate = DebateAgent(llm_router=mock, pubsub=None)
    quant_sig = {
        "agent": "quant",
        "channel": "quant_signal",
        "payload": {"stance": "neutral", "top10_candidates": ["005930", "000660"]},
        "ts": "2026-04-26T10:00:00+09:00",
    }
    risk_sig = _signal("risk_slow", "risk_warning", "veto_recommendation")

    result = debate.run_debate([quant_sig, risk_sig])

    assert result["conflict_detected"] is True
    assert result["completed_comparisons"] >= 1


def test_debate_allowed_channels() -> None:
    """ALLOWED_PUBLISH_CHANNELS에 debate_resolution + pairwise_ranking 포함."""
    assert "debate_resolution" in DebateAgent.ALLOWED_PUBLISH_CHANNELS
    assert "pairwise_ranking" in DebateAgent.ALLOWED_PUBLISH_CHANNELS


def test_debate_wrong_report_type_raises() -> None:
    """debate_resolution / pairwise_ranking 외 → ValueError."""
    debate = _make_debate()
    with pytest.raises(ValueError):
        debate.report("risk_warning", {})


# ------------------------------------------------------------------ #
# FDA Cold Path 테스트
# ------------------------------------------------------------------ #

def _fd(result: dict) -> dict:
    """final_decision 내부 딕셔너리 추출 헬퍼."""
    return result["final_decision"]


def test_fda_cold_approve_no_conflict() -> None:
    """Cold Path: 충돌/veto 없으면 approve, NORMAL_APPROVE."""
    fda = _make_fda()
    result = fda.decide(
        portfolio_patch_ref="PP-20260426-001",
        target_weights={"005930": 0.1},
        mode="cold",
        risk_warnings=[],
        debate_result={"conflict_detected": False},
        agent_signals=[],
    )
    fd = _fd(result)
    assert fd["approved"] is True
    assert fd["reason_code"] is not None


def test_fda_cold_veto_debate_conflict() -> None:
    """Cold Path: debate uncertainty > 0.7 → DEBATE_CONFLICT veto."""
    fda = _make_fda(with_router=False)
    result = fda.decide(
        portfolio_patch_ref="PP-20260426-002",
        target_weights={"005930": 0.1},
        mode="cold",
        risk_warnings=[],
        debate_result={
            "conflict_detected": True,
            "uncertainty_delta": 0.85,
            "winner_view": "mixed",
        },
    )
    fd = _fd(result)
    assert fd["approved"] is False
    assert fd["reason_code"] == "DEBATE_CONFLICT"


def test_fda_cold_debate_uncertainty_string_no_crash() -> None:
    """Debate uncertainty_delta 문자열도 안전하게 float 변환한다."""
    fda = _make_fda(with_router=False)
    result = fda.decide(
        portfolio_patch_ref="PP-20260426-002",
        target_weights={"005930": 0.1},
        mode="cold",
        risk_warnings=[],
        debate_result={
            "conflict_detected": True,
            "uncertainty_delta": "0.91",
            "winner_view": "mixed",
        },
    )
    fd = _fd(result)
    assert fd["approved"] is False
    assert fd["reason_code"] == "DEBATE_CONFLICT"


def test_fda_cold_veto_risk_warning() -> None:
    """Cold Path: risk_warning veto_recommendation stance → RISK_FAST_TRIGGER veto."""
    fda = _make_fda(with_router=False)
    result = fda.decide(
        portfolio_patch_ref="PP-20260426-003",
        target_weights={"005930": 0.1},
        mode="cold",
        risk_warnings=[
            {"stance": "veto_recommendation", "risk_level": "high", "ticker": "005930"}
        ],
        debate_result={"conflict_detected": False},
    )
    fd = _fd(result)
    assert fd["approved"] is False
    assert fd["reason_code"] == "RISK_FAST_TRIGGER"


def test_fda_cold_missing_patch_ref() -> None:
    """Cold Path: portfolio_patch_ref 없음 → MISSING_PORTFOLIO_PATCH veto."""
    fda = _make_fda()
    result = fda.decide(
        portfolio_patch_ref=None,
        mode="cold",
    )
    fd = _fd(result)
    assert fd["approved"] is False
    assert fd["reason_code"] == "MISSING_PORTFOLIO_PATCH"


def test_fda_cold_llm_approve() -> None:
    """Cold Path: LLM approve 응답 → approved=True."""
    fda = _make_fda(with_router=True)
    result = fda.decide(
        portfolio_patch_ref="PP-20260426-004",
        target_weights={"005930": 0.05},
        mode="cold",
        risk_warnings=[],
        agent_signals=[_signal("news_agent", "news_signal", "buy")],
        debate_result={"conflict_detected": False},
    )
    fd = _fd(result)
    assert fd["approved"] is True
    assert fd["reason_code"] == "NORMAL_APPROVE"
    assert fd["confidence"] == pytest.approx(0.85)


def test_fda_cold_llm_string_false_vetoes() -> None:
    """LLM approved='false' 문자열은 Python truthy가 아니라 False로 해석한다."""
    router = _make_router(
        '{"approved":"false","reason_code":"NEWS_DIVERGENCE",'
        '"veto_reason":"뉴스 괴리","confidence":0.6}'
    )
    fda = FDAAgent(llm_router=router)
    result = fda.decide(
        portfolio_patch_ref="PP-20260426-005",
        target_weights={"005930": 0.05},
        mode="cold",
        risk_warnings=[],
        agent_signals=[_signal("news_agent", "news_signal", "sell")],
        debate_result={"conflict_detected": False},
    )
    fd = _fd(result)
    assert fd["approved"] is False
    assert fd["reason_code"] == "NEWS_DIVERGENCE"


def test_fda_cold_llm_json_array_falls_back_without_crash() -> None:
    """JSON array 응답은 crash 없이 heuristic fallback으로 처리된다."""
    fda = FDAAgent(llm_router=_make_router("[]"))
    result = fda.decide(
        portfolio_patch_ref="PP-20260426-006",
        target_weights={"005930": 0.05},
        mode="cold",
        risk_warnings=[],
        agent_signals=[],
        debate_result={"conflict_detected": False},
    )
    fd = _fd(result)
    assert fd["approved"] is True


def test_fda_cold_can_change_weight_still_false() -> None:
    """Cold Path에서도 CAN_CHANGE_WEIGHT=False 유지."""
    fda = _make_fda()
    assert fda.CAN_CHANGE_WEIGHT is False


def test_fda_reason_code_catalog_final() -> None:
    """reason_code_catalog status가 'final'이어야 함 (stub 제거 확인)."""
    from src.utils.config_loader import load as cfg  # noqa: PLC0415
    rc = cfg("risk_config.yaml", "reason_code_catalog")
    assert rc.get("status") == "final", f"status={rc.get('status')} — draft_sprint2면 S2-9 미완료"


def test_fda_cold_llm_calls_with_correct_mode() -> None:
    """Cold Path LLM 호출 시 mode='cold', caller='fda_cold_path'."""
    fda = _make_fda(with_router=True)
    fda.decide(
        portfolio_patch_ref="PP-TEST",
        mode="cold",
        risk_warnings=[],
        agent_signals=[],
        debate_result={"conflict_detected": False},
    )
    if fda._llm_router.call.called:
        call_args = fda._llm_router.call.call_args
        args = call_args.args if call_args.args else []
        kwargs = call_args.kwargs if call_args.kwargs else {}
        mode_val = kwargs.get("mode") or (args[1] if len(args) > 1 else None)
        caller_val = kwargs.get("caller") or (args[2] if len(args) > 2 else None)
        assert mode_val == "cold"
        assert caller_val == "fda_cold_path"

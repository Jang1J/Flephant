"""S2-8 Cold Path RiskAgentFast + RiskAgentSlow unit tests."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from src.agents.cold.risk_fast import RiskAgentFast
from src.agents.cold.risk_slow import RiskAgentSlow


# ------------------------------------------------------------------ #
# fixtures
# ------------------------------------------------------------------ #

def _make_fast() -> RiskAgentFast:
    return RiskAgentFast(pubsub=None)


def _make_slow() -> RiskAgentSlow:
    mock_router = MagicMock()
    mock_router.call.return_value = MagicMock(
        success=True,
        model_used="kanana-o",
        content='{"stance": "risk_reduce", "risk_level": "medium", '
                '"regime_signal": false, "affected_tickers": ["005930"], '
                '"narrative": "외국인 매도 증가로 단기 하락 위험"}',
        latency_ms=1500.0,
        error=None,
    )
    return RiskAgentSlow(llm_router=mock_router, pubsub=None)


def _make_event(event_type: str = "news", ticker: str = "005930") -> dict:
    return {
        "event_id": "EVT-TEST-001",
        "event_type": event_type,
        "ticker": ticker,
        "occurred_at": "2026-04-18T10:00:00+09:00",
        "payload": {
            "ticker": ticker,
            "priority": "normal",
            "title": "테스트 이벤트",
        },
    }


# ------------------------------------------------------------------ #
# RiskAgentFast (Cold) 테스트
# ------------------------------------------------------------------ #

def test_fast_no_trigger_returns_low() -> None:
    """컨텍스트 수치 모두 임계값 미달 → low risk, neutral."""
    fast = _make_fast()
    result = fast.evaluate(_make_event(), context={
        "comm_volume_zscore": 1.0,
        "comm_sentiment_delta": 0.1,
        "intraday_return_zscore": -1.0,
        "foreign_net_sell_krw": 1_000_000_000.0,  # 10B (< 100B)
        "news_comm_divergence": 0.1,
    })
    assert result["risk_level"] == "low"
    assert result["stance"] == "neutral"
    assert result["fast_rule_match"] is None
    assert result["triggered_rules"] == []


def test_fast_comm_volume_spike_triggers_medium() -> None:
    """커뮤니티 z-score > 2.5 → medium, risk_reduce."""
    fast = _make_fast()
    result = fast.evaluate(_make_event(), context={"comm_volume_zscore": 3.0})
    assert result["risk_level"] == "medium"
    assert "comm_volume_spike" in result["triggered_rules"]
    assert result["stance"] == "risk_reduce"


def test_fast_foreign_sell_critical_triggers_high() -> None:
    """외국인 순매도 -150B (음수 컨벤션) → high, veto_recommendation."""
    fast = _make_fast()
    result = fast.evaluate(_make_event(), context={
        "foreign_net_sell_krw": -150_000_000_000.0
    })
    assert result["risk_level"] == "high"
    assert result["stance"] == "veto_recommendation"
    assert "foreign_net_sell_critical" in result["triggered_rules"]


def test_fast_dart_critical_disclosure() -> None:
    """C2 event_type='dart' + priority='critical' → dart_critical_disclosure 트리거.

    'dart_alert'는 C4 publish_channel 이름이지 C2 event_type이 아님 (C2 enum 준수).
    """
    fast = _make_fast()
    event = {
        "event_id": "EVT-TEST-DART",
        "event_type": "dart",
        "ticker": "005930",
        "occurred_at": "2026-04-18T10:00:00+09:00",
        "priority": "critical",
        "payload": {"ticker": "005930", "title": "주요 공시"},
    }
    result = fast.evaluate(event, context={})
    assert "dart_critical_disclosure" in result["triggered_rules"]
    assert result["stance"] == "veto_recommendation"


def test_fast_multiple_triggers_highest_risk() -> None:
    """복수 트리거 → 가장 높은 risk_level 선택 (음수 컨벤션 적용)."""
    fast = _make_fast()
    result = fast.evaluate(_make_event(), context={
        "comm_volume_zscore": 3.5,               # medium
        "foreign_net_sell_krw": -200_000_000_000.0,  # high (음수)
    })
    assert result["risk_level"] == "high"
    assert result["stance"] == "veto_recommendation"
    assert len(result["triggered_rules"]) >= 2


def test_fast_latency_under_50ms() -> None:
    """evaluate() 실행 시간 < 50ms."""
    fast = _make_fast()
    ctx = {
        "comm_volume_zscore": 0.5,
        "comm_sentiment_delta": 0.1,
        "intraday_return_zscore": -0.5,
        "foreign_net_sell_krw": 1_000_000.0,
        "news_comm_divergence": 0.1,
    }
    start = time.perf_counter()
    fast.evaluate(_make_event(), context=ctx)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 50, f"latency {elapsed_ms:.1f}ms > 50ms"


def test_fast_report_payload_schema() -> None:
    """report() 반환 딕셔너리에 C5 필드 존재."""
    fast = _make_fast()
    payload = {
        "stance": "risk_reduce",
        "risk_level": "medium",
        "macro_note_ref": None,
        "micro_note_ref": None,
        "fast_rule_match": None,
    }
    rpt = fast.report("risk_warning", payload)
    assert rpt["channel"] == "risk_warning"
    assert rpt["report_type"] == "risk_warning"
    assert "ts" in rpt
    assert rpt["agent"] == "RiskAgentFast"


def test_fast_report_wrong_type_raises() -> None:
    """risk_warning 외 report_type → ValueError."""
    fast = _make_fast()
    with pytest.raises(ValueError, match="미지원"):
        fast.report("regime_change", {})


def test_fast_news_comm_divergence_trigger() -> None:
    """뉴스-커뮤니티 방향 불일치 > 0.5 → 트리거."""
    fast = _make_fast()
    result = fast.evaluate(_make_event(), context={
        "news_comm_divergence": 0.7
    })
    assert "news_comm_divergence_strong" in result["triggered_rules"]
    assert result["stance"] == "risk_reduce"


# ------------------------------------------------------------------ #
# RiskAgentSlow 테스트
# ------------------------------------------------------------------ #

def test_slow_analyze_success() -> None:
    """LLM 성공 → C5 risk_warning 리포트 반환."""
    slow = _make_slow()
    result = slow.analyze(_make_event())
    assert result is not None
    assert result["channel"] in {"risk_warning", "regime_change", "veto_recommendation"}
    assert result["report_type"] in {"risk_warning", "regime_change", "veto_recommendation"}
    assert "payload" in result


def test_slow_analyze_with_fast_eval() -> None:
    """fast_eval 컨텍스트 포함 → LLM 호출 시 포함됨."""
    slow = _make_slow()
    fast_eval = {
        "risk_level": "high",
        "stance": "veto_recommendation",
        "fast_rule_match": [{"rule_id": "foreign_net_sell_critical", "matched_at": "2026-04-18"}],
        "triggered_rules": ["foreign_net_sell_critical"],
    }
    result = slow.analyze(_make_event(), fast_eval=fast_eval)
    assert result is not None
    payload = result["payload"]
    assert payload.get("fast_rule_match") is not None


def test_slow_analyze_llm_failure_fallback() -> None:
    """LLM 실패 → fast_eval fallback 응답 반환."""
    mock_router = MagicMock()
    mock_router.call.return_value = MagicMock(
        success=False,
        model_used=None,
        content="",
        latency_ms=0.0,
        error="timeout",
    )
    slow = RiskAgentSlow(llm_router=mock_router, pubsub=None)
    fast_eval = {
        "risk_level": "medium",
        "stance": "risk_reduce",
        "fast_rule_match": None,
        "triggered_rules": [],
    }
    result = slow.analyze(_make_event(), fast_eval=fast_eval)
    assert result is not None
    assert result["payload"]["stance"] == "risk_reduce"


def test_slow_allowed_channels() -> None:
    """ALLOWED_PUBLISH_CHANNELS에 3개 채널 포함."""
    assert "risk_warning" in RiskAgentSlow.ALLOWED_PUBLISH_CHANNELS
    assert "regime_change" in RiskAgentSlow.ALLOWED_PUBLISH_CHANNELS
    assert "veto_recommendation" in RiskAgentSlow.ALLOWED_PUBLISH_CHANNELS


def test_slow_report_invalid_stance_raises() -> None:
    """risk_warning 리포트에 잘못된 stance → ValueError."""
    slow = _make_slow()
    with pytest.raises(ValueError):
        slow.report("risk_warning", {"stance": "INVALID_STANCE"})


def test_slow_report_invalid_type_raises() -> None:
    """report_type=regime_change → ValueError (C5 SSOT: risk_warning 고정)."""
    slow = _make_slow()
    with pytest.raises(ValueError, match="VALID_REPORT_TYPES"):
        slow.report("regime_change", {"stance": "neutral"})


def test_slow_llm_call_uses_cold_mode_and_correct_caller() -> None:
    """LLM 호출 시 mode='cold', caller='risk_agent' 확인 (불변 원칙 4 검증)."""
    slow = _make_slow()
    slow.analyze(_make_event())
    call_args = slow._llm_router.call.call_args
    args = call_args.args if call_args.args else []
    kwargs = call_args.kwargs if call_args.kwargs else {}
    mode_val = kwargs.get("mode") or (args[1] if len(args) > 1 else None)
    caller_val = kwargs.get("caller") or (args[2] if len(args) > 2 else None)
    assert mode_val == "cold", f"mode={mode_val!r}, 'cold' 이어야 함"
    assert caller_val == "risk_agent", f"caller={caller_val!r}, 'risk_agent' 이어야 함"


def test_fast_intraday_drop_anomaly_trigger() -> None:
    """분봉 수익률 z-score < -3.0 → intraday_drop_anomaly 트리거, veto_recommendation."""
    fast = _make_fast()
    result = fast.evaluate(_make_event(), context={"intraday_return_zscore": -4.5})
    assert "intraday_drop_anomaly" in result["triggered_rules"]
    assert result["stance"] == "veto_recommendation"
    assert result["risk_level"] == "high"


def test_fast_comm_sentiment_delta_trigger() -> None:
    """감성 점수 변화율 절대값 > 0.5 → comm_sentiment_delta 트리거 (음수 방향도 발동)."""
    fast = _make_fast()
    result = fast.evaluate(_make_event(), context={"comm_sentiment_delta": -0.8})
    assert "comm_sentiment_delta" in result["triggered_rules"]
    assert result["stance"] == "risk_reduce"


def test_fast_foreign_sell_negative_convention() -> None:
    """음수 컨벤션 (-150B KRW) 외국인 순매도 → foreign_net_sell_critical 발동."""
    fast = _make_fast()
    result = fast.evaluate(_make_event(), context={
        "foreign_net_sell_krw": -150_000_000_000.0
    })
    assert "foreign_net_sell_critical" in result["triggered_rules"]
    assert result["stance"] == "veto_recommendation"


def test_fast_foreign_sell_positive_no_trigger() -> None:
    """양수 값(순매수)은 foreign_net_sell_critical 미발동."""
    fast = _make_fast()
    result = fast.evaluate(_make_event(), context={
        "foreign_net_sell_krw": 200_000_000_000.0  # 양수 = 순매수, 트리거 안 됨
    })
    assert "foreign_net_sell_critical" not in result["triggered_rules"]

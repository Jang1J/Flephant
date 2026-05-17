"""S2-8 Cold Path RiskAgentFast + RiskAgentSlow unit tests."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from src.agents.cold.risk_fast import RiskAgentFast
from src.agents.cold.risk_slow import RiskAgentSlow
from src.utils.pit_guard import PITViolationError


# ------------------------------------------------------------------ #
# fixtures
# ------------------------------------------------------------------ #

@pytest.fixture(autouse=True)
def _isolate_default_memory_root(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agents.cold.risk_slow._DEFAULT_MEMORY_ROOT",
        tmp_path / "agent_memory",
    )


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
        "asof": "2026-04-18T10:01:00+09:00",
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
        "asof": "2026-04-18T10:01:00+09:00",
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
    assert result["uncertainty_score"] == pytest.approx(0.7)


def test_fast_uncertainty_signal_payload_has_score_and_trace() -> None:
    """uncertainty_signal은 FDA가 읽을 score + correlation trace를 포함한다."""
    pubsub = MagicMock()
    pubsub.publish.return_value = "MSG-UNC-1"
    fast = RiskAgentFast(pubsub=pubsub)
    event = _make_event()
    event["scope"] = "ticker:005930"
    event["asof"] = "2026-04-18T10:01:00+09:00"

    fast.evaluate(event, context={"news_comm_divergence": -0.8})

    pubsub.publish.assert_called()
    channel, message = pubsub.publish.call_args.args
    payload = message["payload"]
    assert channel == "uncertainty_signal"
    assert payload["uncertainty_score"] == pytest.approx(0.8)
    assert payload["confidence"] == pytest.approx(0.8)
    assert payload["scope"] == "ticker:005930"
    assert payload["event_id"] == "EVT-TEST-001"
    assert payload["occurred_at"] == "2026-04-18T10:00:00+09:00"
    assert payload["asof"] == "2026-04-18T10:01:00+09:00"


def test_fast_rejects_future_event_before_uncertainty_publish() -> None:
    """RiskFast도 occurred_at > asof 미래 이벤트를 publish 전에 차단한다."""
    pubsub = MagicMock()
    fast = RiskAgentFast(pubsub=pubsub)
    event = _make_event()
    event["occurred_at"] = "2026-04-18T10:02:00+09:00"
    event["asof"] = "2026-04-18T10:01:00+09:00"

    with pytest.raises(PITViolationError):
        fast.evaluate(event, context={"news_comm_divergence": 0.8})

    pubsub.publish.assert_not_called()


def test_fast_requires_asof_for_timestamped_event() -> None:
    """직접 호출 이벤트에 occurred_at이 있으면 asof 없이는 평가하지 않는다."""
    fast = _make_fast()
    event = _make_event()
    event.pop("asof")

    with pytest.raises(PITViolationError, match="asof_required"):
        fast.evaluate(event)


def test_fast_uncertainty_signal_normalizes_short_ticker_and_scope() -> None:
    """uncertainty_signal ticker/scope는 raw ticker가 아니라 6자리 코드로 발행된다."""
    pubsub = MagicMock()
    pubsub.publish.return_value = "MSG-UNC-2"
    fast = RiskAgentFast(pubsub=pubsub)
    event = _make_event(ticker="5930")
    event["asof"] = "2026-04-18T10:01:00+09:00"

    fast.evaluate(event, context={"news_comm_divergence": 0.8})

    payload = pubsub.publish.call_args.args[1]["payload"]
    assert payload["ticker"] == "005930"
    assert payload["scope"] == "ticker:005930"


def test_fast_context_numeric_strings_and_malformed_values_fail_closed() -> None:
    """외부 context 숫자는 문자열을 허용하되 NaN/오염값은 기본값으로 fail-closed."""
    fast = _make_fast()
    result = fast.evaluate(
        _make_event(),
        context={
            "comm_volume_zscore": "3.1",
            "comm_sentiment_delta": "bad",
            "intraday_return_zscore": "NaN",
            "foreign_net_sell_krw": "-150000000000",
            "news_comm_divergence": "inf",
        },
    )

    assert "comm_volume_spike" in result["triggered_rules"]
    assert "foreign_net_sell_critical" in result["triggered_rules"]
    assert "comm_sentiment_delta" not in result["triggered_rules"]
    assert "intraday_drop_anomaly" not in result["triggered_rules"]
    assert "news_comm_divergence_strong" not in result["triggered_rules"]


# ------------------------------------------------------------------ #
# RiskAgentSlow 테스트
# ------------------------------------------------------------------ #

def test_slow_analyze_success() -> None:
    """LLM 성공 → C5 risk_warning 리포트 반환."""
    slow = _make_slow()
    result = slow.analyze(_make_event())
    assert result is not None
    assert result["channel"] in {"risk_warning", "regime_change", "veto_recommendation"}
    assert result["report_type"] == "risk_warning"
    assert "payload" in result


def test_slow_memory_write_failure_does_not_fail_analysis(tmp_path) -> None:
    """memory_root가 쓰기 불가여도 RiskSlow 분석 결과는 반환된다."""
    blocked_root = tmp_path / "blocked_memory_root"
    blocked_root.write_text("not a directory", encoding="utf-8")
    mock_router = MagicMock()
    mock_router.call.return_value = MagicMock(
        success=True,
        model_used="kanana-o",
        content='{"stance":"risk_reduce","risk_level":"medium",'
        '"regime_signal":false,"affected_tickers":["005930"],"narrative":"위험"}',
        latency_ms=10.0,
        error=None,
    )
    slow = RiskAgentSlow(llm_router=mock_router, pubsub=None, memory_root=blocked_root)

    result = slow.analyze(_make_event())

    assert result is not None
    assert result["payload"]["stance"] == "risk_reduce"
    assert "memory_write_error" in result


def test_slow_analyze_rejects_future_event_before_llm() -> None:
    """직접 호출 경로도 occurred_at > asof 미래 이벤트를 LLM 전에 차단한다."""
    slow = _make_slow()
    event = _make_event()
    event["occurred_at"] = "2026-04-18T10:01:00+09:00"
    event["asof"] = "2026-04-18T10:00:00+09:00"

    with pytest.raises(PITViolationError):
        slow.analyze(event)

    slow._llm_router.call.assert_not_called()


def test_slow_requires_asof_for_timestamped_event() -> None:
    """직접 호출 이벤트에 occurred_at이 있으면 LLM 호출 전 asof 누락을 차단한다."""
    slow = _make_slow()
    event = _make_event()
    event.pop("asof")

    with pytest.raises(PITViolationError, match="asof_required"):
        slow.analyze(event)

    slow._llm_router.call.assert_not_called()


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


def test_slow_llm_failure_without_fast_eval_fails_closed() -> None:
    """LLM 실패 + fast_eval 없음 → neutral/low로 열지 않고 risk_reduce로 닫는다."""
    mock_router = MagicMock()
    mock_router.call.return_value = MagicMock(
        success=False,
        model_used=None,
        content="",
        latency_ms=0.0,
        error="timeout",
    )
    slow = RiskAgentSlow(llm_router=mock_router, pubsub=None)

    result = slow.analyze(_make_event())

    assert result is not None
    assert result["channel"] == "risk_warning"
    payload = result["payload"]
    assert payload["stance"] == "risk_reduce"
    assert payload["risk_level"] == "medium"
    assert "Fast Rule 없음" in payload["narrative"]


def test_slow_llm_failure_without_fast_eval_dart_vetoes() -> None:
    """DART 이벤트는 fast_eval 없이 LLM이 실패하면 high/veto로 fail-closed."""
    mock_router = MagicMock()
    mock_router.call.return_value = MagicMock(
        success=False,
        model_used=None,
        content="",
        latency_ms=0.0,
        error="timeout",
    )
    pubsub = MagicMock()
    pubsub.publish.return_value = "MSG-RISK-DART-FALLBACK"
    slow = RiskAgentSlow(llm_router=mock_router, pubsub=pubsub)

    result = slow.analyze(_make_event(event_type="dart"))

    assert result is not None
    assert result["channel"] == "veto_recommendation"
    pubsub.publish.assert_called_once()
    assert pubsub.publish.call_args.args[0] == "veto_recommendation"
    payload = result["payload"]
    assert payload["stance"] == "veto_recommendation"
    assert payload["risk_level"] == "high"
    assert payload["affected_tickers"] == ["005930"]


@pytest.mark.parametrize(
    "event_update,payload_update",
    [
        ({"priority": "urgent"}, {}),
        ({"priority": "critical"}, {}),
        ({"severity": "high"}, {}),
        ({"critical": True}, {}),
        ({}, {"priority": "urgent"}),
        ({}, {"severity": "critical"}),
        ({}, {"critical": "true"}),
    ],
)
def test_slow_llm_failure_without_fast_eval_high_risk_markers_veto(
    event_update: dict,
    payload_update: dict,
) -> None:
    """fast_eval 없이 LLM이 실패하면 urgent/critical marker를 high/veto로 닫는다."""
    mock_router = MagicMock()
    mock_router.call.return_value = MagicMock(
        success=False,
        model_used=None,
        content="",
        latency_ms=0.0,
        error="timeout",
    )
    slow = RiskAgentSlow(llm_router=mock_router, pubsub=None)
    event = _make_event()
    event.update(event_update)
    event["payload"].update(payload_update)

    result = slow.analyze(event)

    assert result is not None
    assert result["channel"] == "veto_recommendation"
    payload = result["payload"]
    assert payload["stance"] == "veto_recommendation"
    assert payload["risk_level"] == "high"


def test_slow_parse_string_bool_and_string_ticker() -> None:
    """LLM이 문자열 bool/ticker를 반환해도 regime/ticker를 안정적으로 파싱한다."""
    slow = _make_slow()
    parsed = slow._parse_llm_content(
        '{"stance":"risk_reduce","risk_level":"medium",'
        '"regime_signal":"false","affected_tickers":"005930","narrative":"위험"}'
    )
    assert parsed["regime_signal"] is False
    assert parsed["affected_tickers"] == ["005930"]


def test_slow_parse_rejects_ambiguous_numeric_bool_and_free_text_ticker() -> None:
    """숫자 2/자유 텍스트 연도는 regime/ticker로 오인하지 않는다."""
    slow = _make_slow()
    parsed = slow._parse_llm_content(
        '{"stance":"risk_reduce","risk_level":"medium",'
        '"regime_signal":2,"affected_tickers":"2026 outlook",'
        '"narrative":"위험"}'
    )
    assert parsed["regime_signal"] is False
    assert parsed["affected_tickers"] == []


def test_slow_parse_heuristic_tickers_reject_non_universe_numbers() -> None:
    """heuristic fallback도 날짜/임의 숫자를 affected_tickers로 오인하지 않는다."""
    slow = _make_slow()
    parsed = slow._parse_llm_content(
        "20260517 regime risk_reduce 공시번호 999999, 005930 위험"
    )
    assert parsed["regime_signal"] is True
    assert parsed["affected_tickers"] == ["005930"]


def test_slow_parse_llm_json_array_falls_back_without_crash() -> None:
    """JSON array 응답은 crash 없이 fallback으로 처리된다."""
    slow = _make_slow()
    parsed = slow._parse_llm_content("[]")
    assert parsed["stance"] == "neutral"
    assert parsed["affected_tickers"] == []


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


def test_slow_direct_call_publishes_to_pubsub() -> None:
    """RiskSlow 직접 호출 경로도 MessagePool로 C4 message를 흘린다."""
    mock_router = MagicMock()
    mock_router.call.return_value = MagicMock(
        success=True,
        model_used="kanana-o",
        content='{"stance": "risk_reduce", "risk_level": "medium", '
                '"regime_signal": false, "affected_tickers": "005930", '
                '"narrative": "외국인 매도 증가"}',
        latency_ms=10.0,
        error=None,
    )
    pubsub = MagicMock()
    pubsub.publish.return_value = "MSG-RISK-1"
    slow = RiskAgentSlow(llm_router=mock_router, pubsub=pubsub)

    result = slow.analyze(_make_event())

    pubsub.publish.assert_called_once()
    assert pubsub.publish.call_args.args[0] == result["channel"]
    assert result["message_id"] == "MSG-RISK-1"
    assert result["published_by_agent"] is True
    payload = result["payload"]
    assert payload["event_id"] == "EVT-TEST-001"
    assert payload["occurred_at"] == "2026-04-18T10:00:00+09:00"
    assert payload["ticker"] == "005930"
    assert payload["scope"] == "ticker:005930"


def test_slow_fallback_publishes_event_trace_to_pubsub() -> None:
    """LLM 실패 fallback도 MessagePool trace를 잃지 않는다."""
    mock_router = MagicMock()
    mock_router.call.return_value = MagicMock(
        success=False,
        model_used=None,
        content="",
        latency_ms=0.0,
        error="timeout",
    )
    pubsub = MagicMock()
    pubsub.publish.return_value = "MSG-RISK-FALLBACK"
    slow = RiskAgentSlow(llm_router=mock_router, pubsub=pubsub)
    event = _make_event(ticker="5930")
    event["asof"] = "2026-04-18T10:01:00+09:00"

    result = slow.analyze(event, fast_eval={"stance": "risk_reduce", "risk_level": "medium"})

    assert result is not None
    pubsub.publish.assert_called_once()
    payload = result["payload"]
    assert payload["event_id"] == "EVT-TEST-001"
    assert payload["occurred_at"] == "2026-04-18T10:00:00+09:00"
    assert payload["asof"] == "2026-04-18T10:01:00+09:00"
    assert payload["ticker"] == "005930"
    assert payload["scope"] == "ticker:005930"


def test_slow_fallback_preserves_fast_veto_channel() -> None:
    """LLM 실패 fallback도 fast veto 성격을 channel에 반영한다."""
    mock_router = MagicMock()
    mock_router.call.return_value = MagicMock(
        success=False,
        model_used=None,
        content="",
        latency_ms=0.0,
        error="timeout",
    )
    pubsub = MagicMock()
    pubsub.publish.return_value = "MSG-RISK-VETO"
    slow = RiskAgentSlow(llm_router=mock_router, pubsub=pubsub)

    result = slow.analyze(
        _make_event(),
        fast_eval={
            "stance": "veto_recommendation",
            "risk_level": "high",
            "fast_rule_match": [{"rule_id": "foreign_net_sell_critical"}],
            "triggered_rules": ["foreign_net_sell_critical"],
        },
    )

    assert result is not None
    assert result["channel"] == "veto_recommendation"
    pubsub.publish.assert_called_once()
    assert pubsub.publish.call_args.args[0] == "veto_recommendation"
    payload = result["payload"]
    assert payload["stance"] == "veto_recommendation"
    assert payload["risk_level"] == "high"
    assert payload["affected_tickers"] == ["005930"]


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

"""EventNormalizer unit tests. C2 EventNormalizeContract 실구현 검증.

6개 source happy path + 에러 케이스 + C2 필수 필드 검증.
kis_bar/kis_event는 C1 bypass (UnknownSourceError 검증 포함).
occurred_at은 현재(2026-04-19) 기준 과거 시간 사용 (PIT-Safety 통과).
"""
from __future__ import annotations

import pytest
from src.data.event_normalizer import (
    EventNormalizer,
    PITViolationError,
    UnknownSourceError,
    ValidationError,
)

# 과거 시간 (PIT-Safety 통과 보장)
_PAST_TS = "2026-04-19T09:00:00+09:00"
_PAST_DATE = "2026-04-19"

# 미래 시간 (PIT 위반 유도)
_FUTURE_TS = "2099-01-01T10:00:00+09:00"

C2_REQUIRED_FIELDS = (
    "event_id",
    "source",
    "event_type",
    "scope",
    "title",
    "summary",
    "occurred_at",
    "ingest_ts",
    "priority",
    "llm_required",
    "ttl",
    "expires_at",
    "supersedes",
    "payload",
    "pit_safe",
)


# ------------------------------------------------------------------ #
# 1. dart happy path
# ------------------------------------------------------------------ #

def test_normalize_dart_happy_path() -> None:
    n = EventNormalizer()
    raw = {
        "title": "삼성전자 분기보고서",
        "corp_name": "삼성전자",
        "disclosure_time": _PAST_TS,
        "summary": "2026년 1분기 실적 공시",
    }
    out = n.normalize(raw, source="dart")

    assert out["source"] == "dart"
    assert out["event_type"] == "dart"
    assert out["title"] == "삼성전자 분기보고서"
    assert out["summary"] == "2026년 1분기 실적 공시"
    assert out["priority"] == "urgent"
    assert out["llm_required"] is True
    assert out["pit_safe"] is True
    assert out["payload"]["corp_name"] == "삼성전자"
    assert "occurred_at" in out
    assert "ingest_ts" in out
    assert out["supersedes"] is None


# ------------------------------------------------------------------ #
# 2. krx_investor_flow happy path
# ------------------------------------------------------------------ #

def test_normalize_krx_investor_flow_happy_path() -> None:
    n = EventNormalizer()
    raw = {
        "ticker": "005930",
        "date": _PAST_DATE,
        "foreign_net_buy": 5000000,
        "institutional": -2000000,
        "retail": -3000000,
    }
    out = n.normalize(raw, source="krx_investor_flow")

    assert out["source"] == "krx_investor_flow"
    assert out["event_type"] == "investor_flow"
    assert out["scope"] == "ticker:005930"
    assert out["payload"]["ticker"] == "005930"
    assert out["payload"]["foreign_net_buy"] == 5000000
    assert out["pit_safe"] is True
    assert out["llm_required"] is False


# ------------------------------------------------------------------ #
# 3. naver_news happy path
# ------------------------------------------------------------------ #

def test_normalize_naver_news_happy_path() -> None:
    n = EventNormalizer()
    raw = {
        "title": "삼성전자 주가 급등",
        "summary": "외국인 대량 매수로 주가 급등",
        "published_at": _PAST_TS,
        "link": "https://news.naver.com/article/12345",
    }
    out = n.normalize(raw, source="naver_news")

    assert out["source"] == "naver_news"
    assert out["event_type"] == "news"
    assert out["title"] == "삼성전자 주가 급등"
    assert out["payload"]["link"] == "https://news.naver.com/article/12345"
    assert out["llm_required"] is True
    assert out["pit_safe"] is True


# ------------------------------------------------------------------ #
# 4. community happy path
# ------------------------------------------------------------------ #

def test_normalize_community_happy_path() -> None:
    n = EventNormalizer()
    raw = {
        "post_title": "삼전 오늘 터질 것 같음",
        "body": "외국인 계속 사고 있어서 곧 갈듯",
        "posted_at": _PAST_TS,
        "ticker_mentioned": "5930",
        "spam_flag": False,
    }
    out = n.normalize(raw, source="community")

    assert out["source"] == "community"
    assert out["event_type"] == "community"
    assert out["scope"] == "ticker:005930"
    assert out["payload"]["ticker_mentioned"] == "005930"
    assert out["payload"]["spam_flag"] is False
    assert out["priority"] == "low"
    assert out["pit_safe"] is True


# ------------------------------------------------------------------ #
# 5. ecos happy path
# ------------------------------------------------------------------ #

def test_normalize_ecos_happy_path() -> None:
    n = EventNormalizer()
    raw = {
        "indicator": "기준금리",
        "value": 3.5,
        "date": _PAST_DATE,
    }
    out = n.normalize(raw, source="ecos")

    assert out["source"] == "ecos"
    assert out["event_type"] == "macro"
    assert out["scope"] == "market"
    assert out["payload"]["indicator"] == "기준금리"
    assert out["payload"]["value"] == 3.5
    assert out["pit_safe"] is True
    assert out["llm_required"] is False


# ------------------------------------------------------------------ #
# 6. us_market happy path
# ------------------------------------------------------------------ #

def test_normalize_us_market_happy_path() -> None:
    n = EventNormalizer()
    raw = {
        "sp500_change": -0.45,
        "nasdaq_change": -0.78,
        "vix": 18.3,
        "soxx_change": -1.2,
        "close_time_utc": "2026-04-18T21:00:00+00:00",
    }
    out = n.normalize(raw, source="us_market")

    assert out["source"] == "us_market"
    assert out["event_type"] == "us_market"
    assert out["scope"] == "market"
    assert out["payload"]["vix"] == 18.3
    assert out["payload"]["sp500_change"] == -0.45
    assert out["pit_safe"] is True


# ------------------------------------------------------------------ #
# 7. unknown source raises UnknownSourceError
# ------------------------------------------------------------------ #

def test_unknown_source_raises() -> None:
    n = EventNormalizer()
    with pytest.raises(UnknownSourceError):
        n.normalize({"title": "테스트"}, source="unknown_source")


# ------------------------------------------------------------------ #
# 8. kis_bar → UnknownSourceError (C1 bypass: EventNormalizer 거치지 않음)
# ------------------------------------------------------------------ #

def test_kis_bar_rejected_as_unknown_source() -> None:
    """kis_bar는 C1 bypass. Quant Agent가 bar_buffer 직접 consume.
    EventNormalizer에 전달하면 UnknownSourceError 발생해야 한다.
    """
    n = EventNormalizer()
    with pytest.raises(UnknownSourceError) as exc_info:
        n.normalize(
            {"ticker": "005930", "timestamp": _PAST_TS, "ohlcv": {}},
            source="kis_bar",
        )
    assert "kis_bar" in str(exc_info.value)


# ------------------------------------------------------------------ #
# 9. kis_event → UnknownSourceError (C1 bypass)
# ------------------------------------------------------------------ #

def test_kis_event_rejected_as_unknown_source() -> None:
    """kis_event도 C1 bypass. EventNormalizer에 전달하면 UnknownSourceError."""
    n = EventNormalizer()
    with pytest.raises(UnknownSourceError) as exc_info:
        n.normalize(
            {"event_type": "체결", "timestamp": _PAST_TS},
            source="kis_event",
        )
    assert "kis_event" in str(exc_info.value)


# ------------------------------------------------------------------ #
# 11. missing required field raises ValidationError
# ------------------------------------------------------------------ #

def test_missing_required_field_raises() -> None:
    n = EventNormalizer()
    # dart는 title, corp_name, disclosure_time 필수
    raw = {
        "title": "공시 제목",
        # corp_name 누락
        "disclosure_time": _PAST_TS,
    }
    with pytest.raises(ValidationError) as exc_info:
        n.normalize(raw, source="dart")
    assert "corp_name" in str(exc_info.value)


def test_missing_required_field_krx_investor_flow_raises() -> None:
    n = EventNormalizer()
    # krx_investor_flow는 ticker, date 필수
    with pytest.raises(ValidationError) as exc_info:
        n.normalize({"ticker": "005930"}, source="krx_investor_flow")
    assert "date" in str(exc_info.value)


# ------------------------------------------------------------------ #
# 12. PIT violation: 미래 occurred_at → PITViolationError
# ------------------------------------------------------------------ #

def test_pit_violation_detected() -> None:
    n = EventNormalizer()
    raw = {
        "title": "미래 공시",
        "corp_name": "테스트",
        "disclosure_time": _FUTURE_TS,
        "summary": "미래 데이터",
    }
    with pytest.raises(PITViolationError):
        n.normalize(raw, source="dart")


def test_asof_rejects_same_day_future_event() -> None:
    """장중 asof 기준 이후 이벤트는 18:00 전이라도 PIT 위반이다."""
    n = EventNormalizer()
    raw = {
        "title": "장중 미래 공시",
        "corp_name": "테스트",
        "disclosure_time": "2026-04-19T10:01:00+09:00",
        "summary": "아직 알 수 없는 이벤트",
    }

    with pytest.raises(PITViolationError):
        n.normalize(raw, source="dart", asof="2026-04-19T10:00:00+09:00")


def test_asof_allows_prior_intraday_event() -> None:
    """장중 asof 이전 이벤트는 정상 처리된다."""
    n = EventNormalizer()
    raw = {
        "title": "장중 과거 공시",
        "corp_name": "테스트",
        "disclosure_time": "2026-04-19T09:59:00+09:00",
        "summary": "이미 발생한 이벤트",
    }

    out = n.normalize(raw, source="dart", asof="2026-04-19T10:00:00+09:00")

    assert out["pit_safe"] is True


# ------------------------------------------------------------------ #
# 13. event_id unique
# ------------------------------------------------------------------ #

def test_event_id_unique() -> None:
    n = EventNormalizer()
    raw = {
        "title": "공시 1",
        "corp_name": "삼성전자",
        "disclosure_time": _PAST_TS,
        "summary": "",
    }
    out1 = n.normalize(raw, source="dart")
    out2 = n.normalize(raw, source="dart")
    assert out1["event_id"] != out2["event_id"], "연속 호출 시 event_id가 달라야 한다"


# ------------------------------------------------------------------ #
# 14. C2 required fields 전부 포함
# ------------------------------------------------------------------ #

def test_output_contains_c2_required_fields() -> None:
    n = EventNormalizer()
    raw = {
        "title": "공시",
        "corp_name": "삼성전자",
        "disclosure_time": _PAST_TS,
        "summary": "테스트 요약",
    }
    out = n.normalize(raw, source="dart")
    for field in C2_REQUIRED_FIELDS:
        assert field in out, f"C2 필수 필드 '{field}' 누락"


# ------------------------------------------------------------------ #
# 15. scope 필드: ticker 있을 때 zero-padded
# ------------------------------------------------------------------ #

def test_krx_investor_flow_ticker_zero_padded() -> None:
    n = EventNormalizer()
    raw = {
        "ticker": 5930,    # 정수 입력
        "date": _PAST_DATE,
    }
    out = n.normalize(raw, source="krx_investor_flow")
    assert out["scope"] == "ticker:005930"
    assert out["payload"]["ticker"] == "005930"


def test_price_snapshot_preserves_snapshots_and_single_ticker_scope() -> None:
    """C16 price_snapshot → C15 admission bridge용 ticker/return_pct 보존."""
    n = EventNormalizer()
    raw = {
        "watch_snapshot_id": "WS-20260509-AABBCCDD",
        "ts": _PAST_TS,
        "snapshots": [
            {"ticker": "200", "day_change_pct": 0.061, "last_price": 12000},
        ],
    }
    out = n.normalize(raw, source="price_snapshot")

    assert out["event_type"] == "price_snapshot"
    assert out["scope"] == "ticker:000200"
    assert out["payload"]["ticker"] == "000200"
    assert out["payload"]["return_pct"] == pytest.approx(0.061)
    assert out["payload"]["snapshots"] == raw["snapshots"]


# ------------------------------------------------------------------ #
# 16. expires_at > occurred_at
# ------------------------------------------------------------------ #

def test_expires_at_after_occurred_at() -> None:
    from datetime import datetime
    n = EventNormalizer()
    raw = {
        "title": "공시",
        "corp_name": "삼성전자",
        "disclosure_time": _PAST_TS,
        "summary": "",
    }
    out = n.normalize(raw, source="dart")
    occurred = datetime.fromisoformat(out["occurred_at"])
    expires = datetime.fromisoformat(out["expires_at"])
    assert expires > occurred, "expires_at은 occurred_at보다 이후여야 한다"

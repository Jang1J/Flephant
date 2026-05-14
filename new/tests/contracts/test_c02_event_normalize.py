"""C2 EventNormalizeContract 계약 테스트. SSOT 기반 스키마 검증."""
from __future__ import annotations

import pytest
from src.data.event_normalizer import EventNormalizer, UnknownSourceError

_PAST_TS = "2026-04-19T09:00:00+09:00"


def test_c02_normalize_output_schema() -> None:
    """C2: output에 event_id, source, event_type, occurred_at, ingest_ts, payload, pit_safe 포함."""
    raw = {
        "title": "테스트 공시",
        "corp_name": "삼성전자",
        "disclosure_time": _PAST_TS,
        "summary": "...",
    }
    n = EventNormalizer()
    out = n.normalize(raw, source="dart")

    for field in ("event_id", "source", "event_type", "occurred_at", "ingest_ts", "payload", "pit_safe"):
        assert field in out, f"C2 필수 필드 '{field}' 누락"

    assert out["source"] == "dart"
    assert out["event_type"] == "dart"
    assert isinstance(out["payload"], dict)
    assert out["pit_safe"] is True


def test_c02_source_field_required() -> None:
    """C2: 지원하지 않는 source는 UnknownSourceError 발생."""
    n = EventNormalizer()
    with pytest.raises(UnknownSourceError):
        n.normalize({"title": "테스트"}, source="invalid_source")


def test_c02_event_id_format() -> None:
    """C2: event_id가 'EVT-' 접두사로 시작."""
    raw = {
        "title": "공시",
        "corp_name": "삼성전자",
        "disclosure_time": _PAST_TS,
        "summary": "",
    }
    n = EventNormalizer()
    out = n.normalize(raw, source="dart")
    assert out["event_id"].startswith("EVT-"), f"event_id 포맷 불일치: {out['event_id']}"


def test_c02_occurred_at_iso8601() -> None:
    """C2: occurred_at이 ISO 8601 형식이고 KST timezone 포함."""
    from datetime import datetime
    raw = {
        "title": "공시",
        "corp_name": "삼성전자",
        "disclosure_time": _PAST_TS,
        "summary": "",
    }
    n = EventNormalizer()
    out = n.normalize(raw, source="dart")
    # datetime.fromisoformat 파싱 성공 = 유효한 ISO 8601
    dt = datetime.fromisoformat(out["occurred_at"])
    assert dt.tzinfo is not None, "occurred_at에 timezone 정보 필요"


def test_c02_all_7_sources_supported() -> None:
    """C2: 7개 source 지원 (S5-1 v3.8 price_snapshot 추가).

    kis_bar/kis_event는 C1 bypass (EventNormalizer 제외).
    architect 판단 (2026-04-19 Critical 정리): 1분봉은 Quant Agent가
    bar_buffer 직접 consume. EventNormalizer를 거치지 않는다.

    price_snapshot 은 S5-1 (Sprint 5 Watch Universe) WatchSnapshotFetcher 가
    EventGateway 에 발행하는 source. AdmissionEngine 의 price_spike_admission rule 매칭에 사용.
    """
    expected = {
        "dart",
        "krx_investor_flow",
        "naver_news",
        "community",
        "ecos",
        "us_market",
        "price_snapshot",
    }
    assert expected == EventNormalizer.SUPPORTED_SOURCES


def test_c02_pit_safe_true_for_past_event() -> None:
    """C2: 과거 이벤트는 pit_safe=True."""
    n = EventNormalizer()
    raw = {
        "indicator": "기준금리",
        "value": 3.5,
        "date": "2026-04-19",
    }
    out = n.normalize(raw, source="ecos")
    assert out["pit_safe"] is True


def test_c02_supersedes_null_by_default() -> None:
    """C2: supersedes 기본값 null."""
    n = EventNormalizer()
    raw = {
        "title": "뉴스",
        "published_at": _PAST_TS,
    }
    out = n.normalize(raw, source="naver_news")
    assert out["supersedes"] is None

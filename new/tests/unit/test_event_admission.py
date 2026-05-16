"""EventAdmission unit tests. C11 EventAdmissionControlContract 검증.

3 필터 (dedupe / stale_drop / backlog_overflow) + dead_letter_log + comparator 정렬.
모든 임계값은 config dict 직접 주입 (yaml 파일 의존 최소화).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.data.event_admission import EventAdmission

_KST = ZoneInfo("Asia/Seoul")


@pytest.fixture
def _enable_stale_check(monkeypatch):
    """conftest.py 의 ELEPHANT_TEST_FRESHNESS_SKIP=1 을 이 테스트에서만 해제.

    stale_drop 동작을 직접 검증하는 테스트에 적용. STALE 필터가 실제로 동작해야
    테스트가 의미 있다.
    """
    monkeypatch.setenv("ELEPHANT_TEST_FRESHNESS_SKIP", "0")


def _cfg(overrides: dict | None = None) -> dict:
    """테스트용 최소 config dict."""
    base = {
        "event_admission": {
            "max_backlog": 3,
            "stale_drop": True,
            "max_cold_path_jobs_per_minute": 10,
            "comparator_sort_key": ["priority", "trigger_type", "scope", "recency"],
            "dedupe_ttl_sec": 5,   # 테스트에서 짧은 TTL 사용
            "dead_letter_path": "artifacts/dead_letter.jsonl",
        }
    }
    if overrides:
        base["event_admission"].update(overrides)
    return base


def _make_event(
    event_id: str = "EVT-TEST-001",
    priority: str = "normal",
    event_type: str = "news",
    scope: str = "market",
    occurred_at: str | None = None,
    expires_at: str | None = None,
    supersedes: str | None = None,
) -> dict:
    """테스트용 정규화 이벤트 dict 생성."""
    now = datetime.now(_KST)
    if occurred_at is None:
        occurred_at = (now - timedelta(minutes=5)).isoformat()
    if expires_at is None:
        expires_at = (now + timedelta(hours=1)).isoformat()
    return {
        "event_id": event_id,
        "source": "naver_news",
        "event_type": event_type,
        "scope": scope,
        "title": "테스트 이벤트",
        "summary": "테스트 요약",
        "occurred_at": occurred_at,
        "ingest_ts": now.isoformat(),
        "priority": priority,
        "llm_required": True,
        "ttl": 3600,
        "expires_at": expires_at,
        "supersedes": supersedes,
        "pit_safe": True,
        "payload": {},
    }


# ------------------------------------------------------------------
# 1. dedupe: 동일 event_id 차단
# ------------------------------------------------------------------

def test_dedupe_blocks_duplicate_id(tmp_path: Path) -> None:
    cfg = _cfg()
    ea = EventAdmission(config=cfg, dead_letter_path=tmp_path / "dl.jsonl")

    ev = _make_event(event_id="EVT-DUP-001")
    assert ea.admit(ev) is True
    assert ea.admit(ev) is False   # 동일 ID 재입력

    # dead_letter 기록 확인
    lines = (tmp_path / "dl.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["drop_reason"] == "DUPLICATE_EVENT_ID"
    assert entry["event_id"] == "EVT-DUP-001"


# ------------------------------------------------------------------
# 2. dedupe TTL 만료 후 재입력 허용
# ------------------------------------------------------------------

def test_dedupe_ttl_expiry_allows_readmit(tmp_path: Path) -> None:
    # 경계값 테스트: TTL=0 은 dedupe 사실상 비활성화 (만료 즉시 재입력 허용)
    # dedupe_ttl_sec=0 이면 즉시 만료 (time.time() + 0 <= time.time())
    # 직접 _seen을 조작하는 대신 dedupe_ttl_sec을 0으로 설정 후 clean 호출
    cfg = _cfg({"dedupe_ttl_sec": 0})
    ea = EventAdmission(config=cfg, dead_letter_path=tmp_path / "dl.jsonl")

    ev = _make_event(event_id="EVT-TTL-001")
    assert ea.admit(ev) is True
    # _seen 캐시가 TTL 0이므로 다음 admit 에서 clean 후 재입력 허용
    assert ea.admit(ev) is True


# ------------------------------------------------------------------
# 3. supersedes 체크: 이미 처리된 이벤트 참조 시 거부
# ------------------------------------------------------------------

def test_supersedes_blocks_reprocess(tmp_path: Path) -> None:
    cfg = _cfg()
    ea = EventAdmission(config=cfg, dead_letter_path=tmp_path / "dl.jsonl")

    # 원본 이벤트 수용 -> 원본 event_id 가 seen_supersedes 에 등록됨
    original = _make_event(event_id="EVT-ORIG-001")
    assert ea.admit(original) is True

    # supersedes=EVT-ORIG-001 인 후속 이벤트 시도 -> 거부 (원본이 이미 처리됨)
    later = _make_event(event_id="EVT-LATER-001", supersedes="EVT-ORIG-001")
    assert ea.admit(later) is False

    lines = (tmp_path / "dl.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert any(json.loads(l)["drop_reason"] == "SUPERSEDED" for l in lines)


# ------------------------------------------------------------------
# 4. stale_drop: expires_at < now
# ------------------------------------------------------------------

def test_stale_drop_expired_event(tmp_path: Path, _enable_stale_check) -> None:
    cfg = _cfg()
    ea = EventAdmission(config=cfg, dead_letter_path=tmp_path / "dl.jsonl")

    past_expires = (datetime.now(_KST) - timedelta(hours=1)).isoformat()
    ev = _make_event(event_id="EVT-STALE-001", expires_at=past_expires)
    assert ea.admit(ev) is False

    lines = (tmp_path / "dl.jsonl").read_text(encoding="utf-8").strip().splitlines()
    entry = json.loads(lines[0])
    assert entry["drop_reason"] == "STALE"


def test_leaked_test_freshness_skip_does_not_bypass_runtime_stale_drop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """테스트용 env 가 런타임으로 새도 stale live/cold 이벤트는 거부."""
    monkeypatch.setenv("ELEPHANT_TEST_FRESHNESS_SKIP", "1")
    cfg = _cfg()
    ea = EventAdmission(config=cfg, dead_letter_path=tmp_path / "dl.jsonl")

    past_expires = (datetime.now(_KST) - timedelta(hours=1)).isoformat()
    ev = _make_event(
        event_id="EVT-STALE-LEAKED-ENV",
        event_type="news",
        expires_at=past_expires,
    )
    assert ea.admit(ev) is False

    lines = (tmp_path / "dl.jsonl").read_text(encoding="utf-8").strip().splitlines()
    entry = json.loads(lines[0])
    assert entry["drop_reason"] == "STALE"
    assert entry["event_id"] == "EVT-STALE-LEAKED-ENV"


# ------------------------------------------------------------------
# 5. stale_drop=false 시 만료 이벤트도 허용
# ------------------------------------------------------------------

def test_stale_drop_false_disables_filter(tmp_path: Path) -> None:
    cfg = _cfg({"stale_drop": False})
    ea = EventAdmission(config=cfg, dead_letter_path=tmp_path / "dl.jsonl")

    past_expires = (datetime.now(_KST) - timedelta(hours=1)).isoformat()
    ev = _make_event(event_id="EVT-NOSTALE-001", expires_at=past_expires)
    assert ea.admit(ev) is True


def test_stale_drop_string_false_disables_filter(tmp_path: Path) -> None:
    """stale_drop='false' 문자열이 만료 이벤트 필터를 켜지 않도록 방어."""
    cfg = _cfg({"stale_drop": "false"})
    ea = EventAdmission(config=cfg, dead_letter_path=tmp_path / "dl.jsonl")

    past_expires = (datetime.now(_KST) - timedelta(hours=1)).isoformat()
    ev = _make_event(event_id="EVT-NOSTALE-STR-001", expires_at=past_expires)
    assert ea.admit(ev) is True


# ------------------------------------------------------------------
# 6. backlog overflow: max_backlog 초과 시 lowest priority drop
# ------------------------------------------------------------------

def test_backlog_overflow_drops_lowest_priority(tmp_path: Path) -> None:
    cfg = _cfg({"max_backlog": 2})
    ea = EventAdmission(config=cfg, dead_letter_path=tmp_path / "dl.jsonl")

    ev_low1 = _make_event(event_id="EVT-LOW-001", priority="low", event_type="community")
    ev_low2 = _make_event(event_id="EVT-LOW-002", priority="low", event_type="community")
    ev_urgent = _make_event(event_id="EVT-URGENT-001", priority="urgent", event_type="dart")

    assert ea.admit(ev_low1) is True
    assert ea.admit(ev_low2) is True
    assert ea.backlog_size() == 2

    # urgent 이벤트 삽입 -> low 하나 drop
    assert ea.admit(ev_urgent) is True
    assert ea.backlog_size() == 2

    lines = (tmp_path / "dl.jsonl").read_text(encoding="utf-8").strip().splitlines()
    dropped_reasons = [json.loads(l)["drop_reason"] for l in lines]
    assert "BACKLOG_OVERFLOW" in dropped_reasons


# ------------------------------------------------------------------
# 7. jobs_per_minute cap 강제
# ------------------------------------------------------------------

def test_jobs_per_minute_cap_enforced(tmp_path: Path) -> None:
    cfg = _cfg({"max_cold_path_jobs_per_minute": 3})
    ea = EventAdmission(config=cfg, dead_letter_path=tmp_path / "dl.jsonl")

    for i in range(3):
        ev = _make_event(event_id=f"EVT-CAP-{i:03d}")
        assert ea.admit(ev) is True

    # 4번째는 cap 도달로 거부
    ev_over = _make_event(event_id="EVT-CAP-999")
    assert ea.admit(ev_over) is False

    lines = (tmp_path / "dl.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert any(json.loads(l)["drop_reason"] == "JOBS_PER_MINUTE_CAP" for l in lines)


# ------------------------------------------------------------------
# 8. comparator: priority 정렬 (urgent > normal > low)
# ------------------------------------------------------------------

def test_comparator_priority_order(tmp_path: Path) -> None:
    cfg = _cfg({"max_backlog": 5, "max_cold_path_jobs_per_minute": 100})
    ea = EventAdmission(config=cfg, dead_letter_path=tmp_path / "dl.jsonl")

    ev_low = _make_event(event_id="EVT-PRI-LOW", priority="low")
    ev_normal = _make_event(event_id="EVT-PRI-NORMAL", priority="normal")
    ev_urgent = _make_event(event_id="EVT-PRI-URGENT", priority="urgent")

    ea.admit(ev_low)
    ea.admit(ev_normal)
    ea.admit(ev_urgent)

    # pop 순서: urgent -> normal -> low
    first = ea.pop_next()
    second = ea.pop_next()
    third = ea.pop_next()

    assert first["event_id"] == "EVT-PRI-URGENT"
    assert second["event_id"] == "EVT-PRI-NORMAL"
    assert third["event_id"] == "EVT-PRI-LOW"


# ------------------------------------------------------------------
# 9. comparator: trigger_type 정렬 (dart_alert > news_detected)
# ------------------------------------------------------------------

def test_comparator_trigger_order(tmp_path: Path) -> None:
    cfg = _cfg({"max_backlog": 5, "max_cold_path_jobs_per_minute": 100})
    ea = EventAdmission(config=cfg, dead_letter_path=tmp_path / "dl.jsonl")

    # 동일 priority=normal 에서 trigger_type 차이
    ev_news = _make_event(event_id="EVT-TRIG-NEWS", priority="normal", event_type="news")
    ev_dart = _make_event(event_id="EVT-TRIG-DART", priority="normal", event_type="dart")

    ea.admit(ev_news)
    ea.admit(ev_dart)

    first = ea.pop_next()
    second = ea.pop_next()

    # dart_alert(1) < news_detected(2) -> dart 먼저
    assert first["event_id"] == "EVT-TRIG-DART"
    assert second["event_id"] == "EVT-TRIG-NEWS"


# ------------------------------------------------------------------
# 10. dead_letter_log JSONL 포맷 (C11 필드 4개)
# ------------------------------------------------------------------

def test_dead_letter_log_jsonl_format(tmp_path: Path, _enable_stale_check) -> None:
    cfg = _cfg()
    ea = EventAdmission(config=cfg, dead_letter_path=tmp_path / "dl.jsonl")

    past_expires = (datetime.now(_KST) - timedelta(hours=1)).isoformat()
    ev = _make_event(event_id="EVT-FMT-001", expires_at=past_expires)
    ea.admit(ev)

    lines = (tmp_path / "dl.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])

    # C11 dead_letter_log fields: [timestamp, event_id, drop_reason, original_event_ref]
    assert "timestamp" in entry
    assert "event_id" in entry
    assert "drop_reason" in entry
    assert "original_event_ref" in entry

    # timestamp 파싱 가능 여부 확인
    datetime.fromisoformat(entry["timestamp"])


# ------------------------------------------------------------------
# 11. config YAML 연동 (실제 risk_config.yaml 로드)
# ------------------------------------------------------------------

def test_config_loads_from_yaml(tmp_path: Path) -> None:
    """risk_config.yaml 의 event_admission 섹션이 정상 로드되는지 확인."""
    from src.utils.config_loader import load as config_load

    cfg = config_load("risk_config.yaml")
    ea_cfg = cfg.get("event_admission", {})

    assert ea_cfg.get("max_backlog") == 3
    assert ea_cfg.get("stale_drop") is True
    assert ea_cfg.get("max_cold_path_jobs_per_minute") == 10
    assert ea_cfg.get("dedupe_ttl_sec") == 300
    assert ea_cfg.get("dead_letter_path") == "artifacts/dead_letter.jsonl"

    # EventAdmission 실제 인스턴스화
    ea = EventAdmission(dead_letter_path=tmp_path / "dl.jsonl")
    assert ea._max_backlog == 3
    assert ea._dedupe_ttl_sec == 300


# ------------------------------------------------------------------
# 12. backlog 단순 pop (backlog 빔 시 None)
# ------------------------------------------------------------------

def test_pop_next_empty_returns_none(tmp_path: Path) -> None:
    cfg = _cfg()
    ea = EventAdmission(config=cfg, dead_letter_path=tmp_path / "dl.jsonl")
    assert ea.pop_next() is None


# ------------------------------------------------------------------
# 13. backlog_size 반영
# ------------------------------------------------------------------

def test_backlog_size_reflects_admission(tmp_path: Path) -> None:
    cfg = _cfg({"max_cold_path_jobs_per_minute": 100})
    ea = EventAdmission(config=cfg, dead_letter_path=tmp_path / "dl.jsonl")

    assert ea.backlog_size() == 0
    ea.admit(_make_event(event_id="EVT-SZ-001"))
    assert ea.backlog_size() == 1
    ea.admit(_make_event(event_id="EVT-SZ-002"))
    assert ea.backlog_size() == 2
    ea.pop_next()
    assert ea.backlog_size() == 1

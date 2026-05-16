"""C11 EventAdmissionControlContract contract tests."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.data.event_admission import EventAdmission

_KST = ZoneInfo("Asia/Seoul")


def _cfg(dead_letter_path: Path, **overrides) -> dict:
    cfg = {
        "event_admission": {
            "max_backlog": 3,
            "stale_drop": True,
            "max_cold_path_jobs_per_minute": 10,
            "jobs_per_minute_window_sec": 60,
            "comparator_sort_key": ["priority", "trigger_type", "scope", "recency"],
            "dedupe_ttl_sec": 300,
            "dead_letter_path": str(dead_letter_path),
            "dead_letter_retention_days": 30,
        }
    }
    cfg["event_admission"].update(overrides)
    return cfg


def _event(event_id: str, **overrides) -> dict:
    now = datetime.now(_KST)
    payload = {
        "event_id": event_id,
        "source": "contract_test",
        "event_type": "news",
        "scope": "market",
        "occurred_at": now.isoformat(),
        "ingest_ts": now.isoformat(),
        "priority": "normal",
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "pit_safe": True,
    }
    payload.update(overrides)
    return payload


def _dead_letter_entries(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_c11_dedupe_by_event_id(tmp_path: Path) -> None:
    """C11: 중복 event_id dedupe."""
    dead_letter_path = tmp_path / "dead_letter.jsonl"
    admission = EventAdmission(
        config=_cfg(dead_letter_path),
        dead_letter_path=dead_letter_path,
    )
    event = _event("EVT-C11-DUPE")

    assert admission.admit(event) is True
    assert admission.admit(event) is False

    entries = _dead_letter_entries(dead_letter_path)
    assert entries[-1]["event_id"] == "EVT-C11-DUPE"
    assert entries[-1]["drop_reason"] == "DUPLICATE_EVENT_ID"


def test_c11_stale_drop(tmp_path: Path) -> None:
    """C11: stale_threshold 초과 이벤트 drop."""
    dead_letter_path = tmp_path / "dead_letter.jsonl"
    admission = EventAdmission(
        config=_cfg(dead_letter_path),
        dead_letter_path=dead_letter_path,
    )
    expired = _event(
        "EVT-C11-STALE",
        expires_at=(datetime.now(_KST) - timedelta(minutes=1)).isoformat(),
    )

    assert admission.admit(expired) is False

    entries = _dead_letter_entries(dead_letter_path)
    assert entries[-1]["drop_reason"] == "STALE"


def test_c11_dead_letter_log(tmp_path: Path) -> None:
    """C11: 거부된 이벤트는 dead_letter_log 기록."""
    dead_letter_path = tmp_path / "dead_letter.jsonl"
    admission = EventAdmission(
        config=_cfg(dead_letter_path),
        dead_letter_path=dead_letter_path,
    )

    assert admission.admit(_event("EVT-C11-OK")) is True
    assert admission.admit(_event("EVT-C11-SUPERSEDED", supersedes="EVT-C11-OK")) is False

    entries = _dead_letter_entries(dead_letter_path)
    assert entries[-1].keys() >= {
        "timestamp",
        "event_id",
        "drop_reason",
        "original_event_ref",
    }
    assert entries[-1]["event_id"] == "EVT-C11-SUPERSEDED"
    assert entries[-1]["drop_reason"] == "SUPERSEDED"
    assert entries[-1]["original_event_ref"] == "EVT-C11-SUPERSEDED"

"""id_factory Sprint 5 함수 포맷 검증.

generate_admission_event_id, generate_exit_event_id,
generate_watch_snapshot_id, generate_promotion_id 4개 함수가
architecture.md §14 ID Convention (PREFIX-YYYYMMDD-UUID8) 을 준수하는지 검증.

_uuid8() = uuid4().hex[:8].upper() → [0-9A-F]{8} 대문자.
"""
from __future__ import annotations

import re

from src.utils import id_factory


def test_generate_admission_event_id_format():
    """ADM-YYYYMMDD-UUID8 (대문자 hex) 형식 검증."""
    aid = id_factory.generate_admission_event_id()
    assert re.match(r"^ADM-\d{8}-[0-9A-F]{8}$", aid), f"형식 불일치: {aid!r}"


def test_generate_exit_event_id_format():
    """EXT-YYYYMMDD-UUID8 (대문자 hex) 형식 검증."""
    eid = id_factory.generate_exit_event_id()
    assert re.match(r"^EXT-\d{8}-[0-9A-F]{8}$", eid), f"형식 불일치: {eid!r}"


def test_generate_watch_snapshot_id_format():
    """WS-YYYYMMDD-UUID8 (대문자 hex) 형식 검증."""
    wid = id_factory.generate_watch_snapshot_id()
    assert re.match(r"^WS-\d{8}-[0-9A-F]{8}$", wid), f"형식 불일치: {wid!r}"


def test_generate_promotion_id_format():
    """PRM-YYYYMMDD-UUID8 (대문자 hex) 형식 검증."""
    pid = id_factory.generate_promotion_id()
    assert re.match(r"^PRM-\d{8}-[0-9A-F]{8}$", pid), f"형식 불일치: {pid!r}"

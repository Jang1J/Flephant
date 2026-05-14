"""AuditLogger C18 20 필드 (2026-05-09 P1 fix: backfill 메타 2개 추가) + PIT-Safety + backfill 테스트.

coverage:
  - AuditLogEntry 20 필드 완전성 (C18 schema, 2026-05-09 backfilled_at + backfill_source 추가)
  - log_entry: 정상 기록 (장중 label None)
  - log_entry: 장중 label_t5_ret 기록 시도 → RuntimeError (PIT-Safety)
  - log_entry: 장중 price_t5_snapshot 기록 시도 → RuntimeError (PIT-Safety)
  - backfill_label: 18:00 이후 정상 backfill
  - backfill_label: 18:00 이전 시도 → RuntimeError (PIT-Safety)
  - read_entries: 전체 항목 반환
  - 레거시 API (log / read_recent / count / clear) 하위 호환 유지
"""
from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

_KST = ZoneInfo("Asia/Seoul")


# ------------------------------------------------------------------ #
# helper: now_kst 를 특정 시각으로 freeze
# ------------------------------------------------------------------ #


def _kst(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 4, 21, hour, minute, tzinfo=_KST)


# ------------------------------------------------------------------ #
# C18 schema 검증
# ------------------------------------------------------------------ #


def test_audit_entry_20_fields_c18_schema():
    """AuditLogEntry 가 C18 20 필드 전부 지원 (2026-05-09: backfill 메타 2개 추가).

    P1 fix (2026-05-09): label_backfilled_at + label_backfill_source 추가.
    cause_attribution._is_label_pit_safe() 가드의 SSOT 데이터 원천.
    """
    from src.ops.audit_logger import AuditLogEntry

    entry_fields = {f.name for f in fields(AuditLogEntry)}
    expected = {
        "ts", "decision_id", "agent", "event_type", "ticker",
        "reason_code", "signal_score", "anomaly_flag",
        "target_weight", "actual_weight",
        "fill_price", "snapshot_vwap", "slippage_bps",
        "sector", "llm_called", "llm_model",
        "label_t5_ret", "price_t5_snapshot",
        # 2026-05-09 P1 fix
        "label_backfilled_at", "label_backfill_source",
    }
    missing = expected - entry_fields
    extra = entry_fields - expected
    assert not missing, f"C18 누락 필드: {missing}"
    assert not extra, f"C18 예상 외 필드: {extra}"
    assert len(entry_fields) == 20, f"필드 개수 {len(entry_fields)} != 20"


def test_audit_entry_to_dict_all_20_keys():
    """AuditLogEntry.to_dict() 결과가 20 키 포함 (P1 fix 2026-05-09)."""
    from src.ops.audit_logger import AuditLogEntry

    entry = AuditLogEntry(
        ts="2026-04-21T10:00:00+09:00",
        decision_id="DEC-20260421-ABCD1234",
        agent="quant",
        event_type="signal",
        ticker="005930",
        signal_score=0.72,
    )
    d = entry.to_dict()
    assert len(d) == 20
    assert d["ticker"] == "005930"
    assert d["signal_score"] == pytest.approx(0.72)
    assert d["label_t5_ret"] is None
    assert d["price_t5_snapshot"] is None
    # 2026-05-09 P1 fix: backfill 메타 default None
    assert d["label_backfilled_at"] is None
    assert d["label_backfill_source"] is None


# ------------------------------------------------------------------ #
# log_entry 정상 케이스
# ------------------------------------------------------------------ #


def test_log_entry_writes_jsonl(tmp_path: Path):
    """log_entry: JSONL 파일에 20 필드 entry 기록 (P1 fix 2026-05-09)."""
    from src.ops.audit_logger import AuditLogger, AuditLogEntry

    log = AuditLogger(log_path=tmp_path / "audit.jsonl")
    entry = AuditLogEntry(
        ts="2026-04-21T10:00:00+09:00",
        decision_id="DEC-20260421-ABCD1234",
        agent="quant",
        event_type="signal",
        ticker="005930",
        signal_score=0.72,
        anomaly_flag=False,
    )

    with patch("src.ops.audit_logger.now_kst", return_value=_kst(10)):
        log.log_entry(entry)

    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["agent"] == "quant"
    assert rec["ticker"] == "005930"
    assert rec["signal_score"] == pytest.approx(0.72)
    assert rec["label_t5_ret"] is None


# ------------------------------------------------------------------ #
# PIT-Safety: 장중 label 금지
# ------------------------------------------------------------------ #


def test_log_entry_pit_safety_blocks_intraday_label(tmp_path: Path):
    """장중(14:00 KST) label_t5_ret 기록 시도 → RuntimeError."""
    from src.ops.audit_logger import AuditLogger, AuditLogEntry

    log = AuditLogger(log_path=tmp_path / "audit.jsonl")
    entry = AuditLogEntry(
        ts="2026-04-21T14:00:00+09:00",
        decision_id="DEC-20260421-ABCD1234",
        agent="quant",
        event_type="signal",
        ticker="005930",
        label_t5_ret=0.0125,     # 장중에 label 채움 → PIT-Safety 위반
    )

    with patch("src.ops.audit_logger.now_kst", return_value=_kst(14)):
        with pytest.raises(RuntimeError, match="PIT_SAFETY_VIOLATION"):
            log.log_entry(entry)


def test_log_entry_pit_safety_blocks_intraday_price_snapshot(tmp_path: Path):
    """장중(15:00 KST) price_t5_snapshot 기록 시도 → RuntimeError."""
    from src.ops.audit_logger import AuditLogger, AuditLogEntry

    log = AuditLogger(log_path=tmp_path / "audit.jsonl")
    entry = AuditLogEntry(
        ts="2026-04-21T15:00:00+09:00",
        decision_id="DEC-20260421-BCDE2345",
        agent="pm",
        event_type="order",
        ticker="000660",
        price_t5_snapshot=70800.0,    # 장중에 price 채움 → 위반
    )

    with patch("src.ops.audit_logger.now_kst", return_value=_kst(15)):
        with pytest.raises(RuntimeError, match="PIT_SAFETY_VIOLATION"):
            log.log_entry(entry)


def test_log_entry_after_1800_allows_label(tmp_path: Path):
    """18:00 이후 label_t5_ret 기록 시 C18 backfill metadata 자동 보강."""
    from src.ops.audit_logger import AuditLogger, AuditLogEntry

    log = AuditLogger(log_path=tmp_path / "audit.jsonl")
    entry = AuditLogEntry(
        ts="2026-04-21T19:00:00+09:00",
        decision_id="DEC-20260421-ABCD9999",
        agent="mode_b",
        event_type="backfill",
        ticker="005930",
        label_t5_ret=0.02,
        price_t5_snapshot=71000.0,
    )

    with patch("src.ops.audit_logger.now_kst", return_value=_kst(19)):
        log.log_entry(entry)  # 예외 없어야 함

    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    rec = json.loads(lines[0])
    assert rec["label_t5_ret"] == pytest.approx(0.02)
    assert rec["label_backfilled_at"] == "2026-04-21T19:00:00+09:00"
    assert rec["label_backfill_source"] == "manual"


# ------------------------------------------------------------------ #
# backfill_label
# ------------------------------------------------------------------ #


def test_backfill_label_after_1800_kst(tmp_path: Path):
    """18:00 이후 backfill 정상 동작 + read_entries 검증."""
    from src.ops.audit_logger import AuditLogger, AuditLogEntry

    log = AuditLogger(log_path=tmp_path / "audit.jsonl")

    # 두 entry 기록 (장중, label None)
    for i in range(2):
        entry = AuditLogEntry(
            ts=f"2026-04-21T1{i}:00:00+09:00",
            decision_id=f"DEC-20260421-000{i}",
            agent="quant",
            event_type="signal",
            ticker="005930",
        )
        with patch("src.ops.audit_logger.now_kst", return_value=_kst(10 + i)):
            log.log_entry(entry)

    # 18:00 이후 backfill
    with patch("src.ops.audit_logger.now_kst", return_value=_kst(19)):
        count = log.backfill_label("DEC-20260421-0001", 0.015, 71500.0)

    assert count == 1

    entries = log.read_entries()
    assert len(entries) == 2
    target = next(e for e in entries if e["decision_id"] == "DEC-20260421-0001")
    assert target["label_t5_ret"] == pytest.approx(0.015)
    assert target["price_t5_snapshot"] == pytest.approx(71500.0)
    # 2026-05-09 P1 fix (Critical MA-1): backfill 메타 SSOT 검증.
    # cause_attribution._is_label_pit_safe() 가 이 두 필드로 PIT-Safety 판정.
    assert target["label_backfilled_at"] is not None
    assert target["label_backfilled_at"].startswith("2026-")
    assert target["label_backfill_source"] == "mode_b_stage_1_rollup"  # default


def test_backfill_label_with_custom_source(tmp_path: Path):
    """2026-05-09 P1 fix: backfill source 파라미터 전달 검증."""
    from src.ops.audit_logger import AuditLogger, AuditLogEntry

    log = AuditLogger(log_path=tmp_path / "audit.jsonl")

    entry = AuditLogEntry(
        ts="2026-04-21T10:00:00+09:00",
        decision_id="DEC-20260421-0001",
        agent="quant",
        event_type="signal",
        ticker="005930",
    )
    with patch("src.ops.audit_logger.now_kst", return_value=_kst(10)):
        log.log_entry(entry)

    with patch("src.ops.audit_logger.now_kst", return_value=_kst(19)):
        log.backfill_label("DEC-20260421-0001", 0.01, 70000.0, source="manual")

    entries = log.read_entries()
    target = entries[0]
    assert target["label_backfill_source"] == "manual"


def test_backfill_label_before_1800_raises(tmp_path: Path):
    """18:00 이전 backfill 시도 → RuntimeError."""
    from src.ops.audit_logger import AuditLogger

    log = AuditLogger(log_path=tmp_path / "audit.jsonl")

    with patch("src.ops.audit_logger.now_kst", return_value=_kst(15)):
        with pytest.raises(RuntimeError, match="PIT_SAFETY_VIOLATION"):
            log.backfill_label("DEC-20260421-XXXX", 0.01, 70000.0)


def test_backfill_label_no_file_returns_zero(tmp_path: Path):
    """파일 없을 때 backfill → 0 반환."""
    from src.ops.audit_logger import AuditLogger

    log = AuditLogger(log_path=tmp_path / "nonexistent.jsonl")

    with patch("src.ops.audit_logger.now_kst", return_value=_kst(19)):
        count = log.backfill_label("DEC-20260421-XXXX", 0.01, 70000.0)

    assert count == 0


# ------------------------------------------------------------------ #
# 레거시 API 하위 호환
# ------------------------------------------------------------------ #


def test_legacy_log_api_still_works(tmp_path: Path):
    """레거시 log(event_type, payload) API 하위 호환 유지."""
    from src.ops.audit_logger import AuditLogger

    log = AuditLogger(log_path=tmp_path / "legacy.jsonl")
    log.log("test_event", {"a": 1})
    log.log("another_event", {"x": 42})

    assert log.count() == 2
    lines = (tmp_path / "legacy.jsonl").read_text().splitlines()
    rec = json.loads(lines[0])
    assert rec["event_type"] == "test_event"
    assert rec["payload"]["a"] == 1


def test_legacy_clear_api(tmp_path: Path):
    """레거시 clear() API 하위 호환."""
    from src.ops.audit_logger import AuditLogger

    log = AuditLogger(log_path=tmp_path / "legacy.jsonl")
    log.log("evt", {"x": 1})
    assert log.count_from_file() == 1
    log.clear()
    assert log.count_from_file() == 0

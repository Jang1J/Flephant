"""C18 post-hoc label metadata PIT-Safety validator.

장중 decision log의 `label_t5_ret` / `price_t5_snapshot`은 18:00 KST 이후
backfill metadata가 있을 때만 사후 지표로 쓸 수 있다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.config_loader import load as config_load

_KST = ZoneInfo("Asia/Seoul")
_ALLOWED_BACKFILL_SOURCES = frozenset({
    "mode_b_stage_1_rollup",
    "synth_audit_log",
    "manual",
})


def backfill_source_default() -> str:
    """수동 직접 label 기록 시 사용할 C18 source 기본값."""
    return "manual"


def normalize_kst(ts: Any) -> datetime:
    """ISO8601-like timestamp를 KST aware datetime으로 정규화."""
    dt = datetime.fromisoformat(str(ts))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_KST)
    return dt.astimezone(_KST)


def is_label_backfill_pit_safe(entry: dict[str, Any]) -> bool:
    """C18 label metadata가 PIT-safe인지 검증.

    True 조건:
      - `label_backfilled_at`과 `label_backfill_source` 존재
      - source가 허용된 backfill source
      - event `ts`와 backfill 시각이 같은 KST 날짜
      - event 시각 <= backfill 시각
      - backfill 시각 >= risk_config.yaml pit_safety.snapshot_hour
    """
    backfilled_at = entry.get("label_backfilled_at")
    source = entry.get("label_backfill_source")
    event_ts = entry.get("ts")
    if not backfilled_at or not source or not event_ts:
        return False
    if str(source) not in _ALLOWED_BACKFILL_SOURCES:
        return False

    try:
        cfg = config_load("risk_config.yaml", "pit_safety") or {}
        snapshot_hour = int(cfg.get("snapshot_hour", 18))

        bf_kst = normalize_kst(backfilled_at)
        ev_kst = normalize_kst(event_ts)
    except (TypeError, ValueError):
        return False

    if ev_kst > bf_kst:
        return False
    if ev_kst.date() != bf_kst.date():
        return False

    threshold = bf_kst.replace(hour=snapshot_hour, minute=0, second=0, microsecond=0)
    return bf_kst >= threshold

"""PIT-Safety 집행 모듈. 불변 원칙 1: 미래 데이터 접근 차단."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from src.utils.config_loader import load as config_load

_KST = ZoneInfo("Asia/Seoul")


class PITViolationError(ValueError):
    """미래 데이터 접근 시도. 불변 원칙 1 위반."""


def _parse_ts(ts: str | datetime) -> datetime:
    """ISO 8601 string 또는 datetime → aware datetime(KST)."""
    if isinstance(ts, str):
        dt = datetime.fromisoformat(ts)
    else:
        dt = ts
    if dt.tzinfo is None:
        # naive → KST 가정
        dt = dt.replace(tzinfo=_KST)
    return dt


def _default_snapshot() -> datetime:
    """오늘 18:00 KST. 인수 미지정 시 기본값.

    snapshot_hour는 risk_config.yaml pit_safety.snapshot_hour에서 로드 (불변 원칙 5).
    """
    cfg = config_load("risk_config.yaml", "pit_safety")
    snapshot_hour: int = int(cfg["snapshot_hour"])
    today = datetime.now(_KST).date()
    return datetime.combine(today, time(snapshot_hour, 0, 0), tzinfo=_KST)


def is_pit_safe(
    data_ts: str | datetime,
    snapshot_ts: str | datetime | None = None,
) -> bool:
    """data_ts가 snapshot 이전이면 True.

    snapshot_ts 미지정 시 오늘 18:00 KST 사용.
    PIT-Safety 통과 기준: data_ts <= snapshot_ts.
    """
    dt_data = _parse_ts(data_ts)
    dt_snap = _parse_ts(snapshot_ts) if snapshot_ts is not None else _default_snapshot()
    return dt_data <= dt_snap


def assert_pit_safe(
    data_ts: str | datetime,
    snapshot_ts: str | datetime | None = None,
) -> None:
    """PIT-Safety 위반 시 ValueError 발생.

    사용 예:
        assert_pit_safe(bar.ts, snapshot_ts=run_start)
    """
    if not is_pit_safe(data_ts, snapshot_ts):
        dt_data = _parse_ts(data_ts)
        dt_snap = _parse_ts(snapshot_ts) if snapshot_ts is not None else _default_snapshot()
        raise ValueError(
            f"PIT-Safety 위반: data_ts={dt_data.isoformat()} > snapshot_ts={dt_snap.isoformat()}"
        )

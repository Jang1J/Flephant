"""KST/UTC 변환 + 18:00 KST snapshot cutoff. PIT-Safety 보조."""
from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")
_UTC = timezone.utc
_SNAPSHOT_HOUR = 18


def utc_to_kst(dt: datetime) -> datetime:
    """UTC → KST 변환. naive datetime은 UTC 가정."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt.astimezone(_KST)


def kst_to_utc(dt: datetime) -> datetime:
    """KST → UTC 변환. naive datetime은 KST 가정."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_KST)
    return dt.astimezone(_UTC)


def now_kst() -> datetime:
    """현재 KST 시각 반환."""
    return datetime.now(_KST)


def snapshot_cutoff(date: str = "today") -> datetime:
    """해당 날짜의 18:00 KST datetime 반환.

    인수:
        date: 'today' 또는 'YYYY-MM-DD' 형식.
    반환:
        18:00:00 KST aware datetime.
    """
    if date == "today":
        base_date = datetime.now(_KST).date()
    else:
        base_date = datetime.fromisoformat(date).date()
    return datetime.combine(base_date, time(_SNAPSHOT_HOUR, 0, 0), tzinfo=_KST)

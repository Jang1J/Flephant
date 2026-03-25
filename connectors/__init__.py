# Elephant Lab - Data Connectors
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    """현재 KST aware datetime 반환"""
    return datetime.now(KST)


def now_kst_iso() -> str:
    """현재 KST ISO 8601 문자열 반환"""
    return datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")


def make_snapshot_dt(date_str: str, hour: int = 18) -> str:
    """snapshot datetime 생성 (기본 18:00 KST)"""
    return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}T{hour:02d}:00:00+09:00"

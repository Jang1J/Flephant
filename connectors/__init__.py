# Elephant Lab - Data Connectors
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def now_kst() -> datetime:
    """현재 KST aware datetime 반환"""
    return datetime.now(KST)


def now_kst_iso() -> str:
    """현재 KST 시각을 ISO 8601 문자열로 반환 (ZoneInfo 기반, 서버 TZ 무관)"""
    return datetime.now(KST).isoformat(timespec="seconds")


def make_snapshot_dt(date_str: str, hour: int = 18) -> str:
    """YYYYMMDD → YYYY-MM-DDT{hour}:00:00+09:00 (장마감 스냅샷)"""
    return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}T{hour:02d}:00:00+09:00"

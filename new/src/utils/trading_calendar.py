"""KRX 거래일 캘린더 유틸리티.

주말 + risk_config.yaml dqr.kospi_holidays_{year}를 단일 기준으로 사용한다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.utils.config_loader import load as config_load

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")


def load_kospi_holidays(year: int) -> set[date]:
    """risk_config.yaml dqr 섹션의 KRX 평일 휴장일을 date set으로 반환."""
    try:
        dqr_cfg = config_load("risk_config.yaml", "dqr")
    except Exception as e:
        logger.warning("[trading_calendar] risk_config.yaml dqr 로드 실패: %s", e)
        return set()

    if not dqr_cfg.get("skip_on_holiday", True):
        return set()

    key = f"kospi_holidays_{year}"
    if key not in dqr_cfg:
        logger.warning(
            "[trading_calendar] risk_config.yaml dqr.%s 미정의. "
            "주말만 제외하는 임시 캘린더를 사용합니다.",
            key,
        )
    raw_dates = dqr_cfg.get(key, []) or []
    holidays: set[date] = set()
    for raw in raw_dates:
        try:
            holidays.add(date.fromisoformat(str(raw)))
        except ValueError as e:
            logger.warning(
                "[trading_calendar] KRX 휴장일 파싱 실패: value=%s error=%s",
                raw,
                e,
            )
    return holidays


def is_kospi_trading_day(value: date) -> bool:
    """KRX 거래일 여부. 토/일과 risk_config 휴장일은 제외."""
    return value.weekday() < 5 and value not in load_kospi_holidays(value.year)


def is_business_day(value: date) -> bool:
    """Backward-compatible alias for KRX trading-day checks."""
    return is_kospi_trading_day(value)


def previous_kospi_trading_day(today: date | None = None) -> date:
    """today 기준 직전 KRX 거래일."""
    cur = (today or datetime.now(_KST).date()) - timedelta(days=1)
    while not is_kospi_trading_day(cur):
        cur -= timedelta(days=1)
    return cur


def kospi_trading_start_date(end: date, trading_days: int) -> date:
    """end를 포함한 최근 N KRX 거래일의 시작일."""
    if trading_days < 1:
        raise ValueError("trading_days must be >= 1")

    cur = end
    while not is_kospi_trading_day(cur):
        cur -= timedelta(days=1)

    remaining = trading_days - 1
    while remaining > 0:
        cur -= timedelta(days=1)
        if is_kospi_trading_day(cur):
            remaining -= 1
    return cur


def kospi_trading_dates_between(start: date, end: date) -> list[str]:
    """start~end 사이 KRX 거래일을 YYYYMMDD 문자열로 반환."""
    out: list[str] = []
    cur = start
    while cur <= end:
        if is_kospi_trading_day(cur):
            out.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return out


def business_days_between(start: date, end: date) -> list[str]:
    """Backward-compatible alias for KRX trading-date ranges."""
    return kospi_trading_dates_between(start, end)

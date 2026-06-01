"""KRX trading calendar tests."""
from __future__ import annotations

from datetime import date

from src.utils.trading_calendar import (
    business_days_between,
    is_business_day,
    is_kospi_trading_day,
    kospi_trading_dates_between,
    kospi_trading_start_date,
    previous_kospi_trading_day,
)


def test_kospi_calendar_skips_2026_market_holidays():
    assert is_kospi_trading_day(date(2026, 2, 13)) is True
    assert is_kospi_trading_day(date(2026, 2, 16)) is False
    assert is_kospi_trading_day(date(2026, 2, 17)) is False
    assert is_kospi_trading_day(date(2026, 2, 18)) is False
    assert is_kospi_trading_day(date(2026, 3, 2)) is False
    assert is_kospi_trading_day(date(2026, 5, 1)) is False
    assert is_kospi_trading_day(date(2026, 5, 5)) is False
    assert is_kospi_trading_day(date(2026, 5, 6)) is True
    assert is_kospi_trading_day(date(2026, 6, 3)) is False
    assert is_kospi_trading_day(date(2026, 7, 17)) is False
    assert is_kospi_trading_day(date(2026, 8, 17)) is False
    assert is_kospi_trading_day(date(2026, 9, 23)) is False
    assert is_kospi_trading_day(date(2026, 9, 24)) is False
    assert is_kospi_trading_day(date(2026, 9, 25)) is False
    assert is_kospi_trading_day(date(2026, 10, 5)) is False


def test_kospi_trading_window_matches_pre_live_80_days():
    start = kospi_trading_start_date(date(2026, 5, 8), 80)
    dates = kospi_trading_dates_between(start, date(2026, 5, 8))

    assert start == date(2026, 1, 9)
    assert len(dates) == 80
    assert "20260216" not in dates
    assert "20260501" not in dates
    assert dates[-1] == "20260508"


def test_previous_trading_day_skips_holiday_and_weekend():
    assert previous_kospi_trading_day(date(2026, 5, 6)) == date(2026, 5, 4)
    assert previous_kospi_trading_day(date(2026, 5, 11)) == date(2026, 5, 8)


def test_backward_compatible_aliases():
    assert is_business_day(date(2026, 5, 4)) is True
    assert is_business_day(date(2026, 5, 5)) is False
    assert business_days_between(date(2026, 5, 4), date(2026, 5, 6)) == [
        "20260504",
        "20260506",
    ]

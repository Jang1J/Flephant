"""BarBuffer unit tests. Sprint 1-0 S1-0 Batch A."""
from __future__ import annotations

import pytest


def _make_bar(ticker: str = "005930", price: int = 70000, vol: int = 1000) -> dict:
    return {
        "ticker": ticker,
        "ts_close": "2026-04-17T09:01:00+09:00",
        "open": price,
        "high": price + 100,
        "low": price - 100,
        "close": price + 50,
        "volume": vol,
    }


# ------------------------------------------------------------------ #
# test_push_and_get_latest
# ------------------------------------------------------------------ #


def test_push_and_get_latest():
    """push 후 get_latest 반환 확인."""
    from src.data.bar_buffer import BarBuffer

    bb = BarBuffer(max_bars=10)
    for i in range(5):
        bar = _make_bar(price=70000 + i * 100)
        bb.push(bar)

    result = bb.get_latest("005930", n=5)
    assert len(result) == 5
    # 마지막 push가 마지막 원소
    assert result[-1]["open"] == 70400


# ------------------------------------------------------------------ #
# test_maxlen_deque_behavior
# ------------------------------------------------------------------ #


def test_maxlen_deque_behavior():
    """maxlen 초과 시 오래된 것이 밀려나는지 확인."""
    from src.data.bar_buffer import BarBuffer

    bb = BarBuffer(max_bars=3)
    for i in range(5):
        bar = _make_bar(price=70000 + i * 100)
        bb.push(bar)

    result = bb.get_latest("005930", n=10)
    assert len(result) == 3
    # 가장 오래된 bar는 i=2 (70200), 최신은 i=4 (70400)
    assert result[0]["open"] == 70200
    assert result[-1]["open"] == 70400


# ------------------------------------------------------------------ #
# test_get_batch_multiple_tickers
# ------------------------------------------------------------------ #


def test_get_batch_multiple_tickers():
    """여러 ticker 한번에 get_batch 검증."""
    from src.data.bar_buffer import BarBuffer

    bb = BarBuffer(max_bars=10)
    tickers = ["005930", "000660", "042700"]
    for t in tickers:
        for i in range(3):
            bb.push(_make_bar(ticker=t, price=70000 + i * 100))

    batch = bb.get_batch(tickers, n_bars=3)
    assert set(batch.keys()) == {"005930", "000660", "042700"}
    for t in tickers:
        assert len(batch[t]) == 3


# ------------------------------------------------------------------ #
# test_missing_check_reports_short
# ------------------------------------------------------------------ #


def test_missing_check_reports_short():
    """expected보다 적은 buffer 보고 확인."""
    from src.data.bar_buffer import BarBuffer

    bb = BarBuffer(max_bars=60)
    # 005930: 30개, 000660: 0개
    for i in range(30):
        bb.push(_make_bar(ticker="005930", price=70000 + i))

    result = bb.missing_check(["005930", "000660"], expected_n=60)
    assert result["005930"] == 30
    assert result["000660"] == 0


# ------------------------------------------------------------------ #
# test_push_validation_required_fields
# ------------------------------------------------------------------ #


def test_push_validation_required_fields():
    """필수 필드 누락 → ValueError."""
    from src.data.bar_buffer import BarBuffer

    bb = BarBuffer(max_bars=10)
    incomplete = {
        "ticker": "005930",
        "ts_close": "2026-04-17T09:01:00+09:00",
        "open": 70000,
        # close, high, low, volume 누락
    }
    with pytest.raises(ValueError):
        bb.push(incomplete)


# ------------------------------------------------------------------ #
# test_clear_single_ticker
# ------------------------------------------------------------------ #


def test_clear_single_ticker():
    """ticker 지정 clear 후 해당 ticker만 비워지는지 확인."""
    from src.data.bar_buffer import BarBuffer

    bb = BarBuffer(max_bars=10)
    bb.push(_make_bar(ticker="005930"))
    bb.push(_make_bar(ticker="000660"))

    bb.clear("005930")
    assert bb.get_latest("005930") == []
    assert len(bb.get_latest("000660")) == 1


# ------------------------------------------------------------------ #
# test_tickers_property
# ------------------------------------------------------------------ #


def test_tickers_property():
    """tickers 프로퍼티가 buffer에 있는 ticker 반환."""
    from src.data.bar_buffer import BarBuffer

    bb = BarBuffer(max_bars=10)
    assert bb.tickers == []

    bb.push(_make_bar(ticker="005930"))
    bb.push(_make_bar(ticker="000660"))
    assert set(bb.tickers) == {"005930", "000660"}


# ------------------------------------------------------------------ #
# test_get_latest_returns_time_ordered
# ------------------------------------------------------------------ #


def test_get_latest_returns_time_ordered():
    """push 순서대로 시간순 반환 확인."""
    from src.data.bar_buffer import BarBuffer

    bb = BarBuffer(max_bars=10)
    prices = [70000, 70100, 70200, 70300, 70400]
    for p in prices:
        bb.push(_make_bar(price=p))

    result = bb.get_latest("005930", n=5)
    result_prices = [r["open"] for r in result]
    assert result_prices == prices

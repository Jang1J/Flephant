"""ticker_utils normalization regression tests."""
from __future__ import annotations

from src.utils.ticker_utils import normalize_ticker


def test_normalize_ticker_preserves_explicit_inputs() -> None:
    """Explicit ticker fields keep existing zfill and market suffix behavior."""
    assert normalize_ticker(5930) == "005930"
    assert normalize_ticker("5930") == "005930"
    assert normalize_ticker("999999") == "999999"
    assert normalize_ticker("005930.KS") == "005930"
    assert normalize_ticker("A005930") == "005930"
    assert normalize_ticker("ticker:5930") == "005930"
    assert normalize_ticker("종목코드=5930") == "005930"


def test_normalize_ticker_rejects_free_text_digit_fragments() -> None:
    """Free text must not synthesize tickers from 1-5 digit fragments."""
    assert normalize_ticker("2026 outlook") == ""
    assert normalize_ticker("매출 5000억원") == ""
    assert normalize_ticker("FY2026 0059") == ""
    assert normalize_ticker("삼성전자 5930 전망") == ""


def test_normalize_ticker_allows_known_six_digit_code_in_text() -> None:
    """A standalone 6-digit active/watch ticker in text remains accepted."""
    assert normalize_ticker("삼성전자 005930 outlook") == "005930"
    assert normalize_ticker("report 991231 outlook") == ""


def test_normalize_ticker_rejects_ambiguous_multiple_text_codes() -> None:
    """Multiple standalone text codes are ambiguous and should not pick one."""
    assert normalize_ticker("005930 vs 000660") == ""


def test_normalize_ticker_honors_explicit_allowed_tickers_for_text() -> None:
    """Callers may opt into a tighter universe for free-text 6-digit matches."""
    assert (
        normalize_ticker("candidate 123456 watch", allowed_tickers=["123456"])
        == "123456"
    )
    assert normalize_ticker("candidate 654321 watch", allowed_tickers=["123456"]) == ""
    assert normalize_ticker("candidate 5930 watch", allowed_tickers=["005930"]) == ""

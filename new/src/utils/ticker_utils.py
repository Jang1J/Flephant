"""종목코드 유틸. CLAUDE.md 표준: str(ticker).zfill(6) 6자리 zero-padded."""
from __future__ import annotations

import re


def pad_ticker(ticker: str | int) -> str:
    """6자리 zero-padded 문자열. CLAUDE.md 표준.

    사용 예:
        pad_ticker(5930)   → "005930"
        pad_ticker("035")  → "000035"
    """
    return str(ticker).zfill(6)


def is_valid_ticker(ticker: str) -> bool:
    """6자리 숫자 문자열인지 검증."""
    return len(ticker) == 6 and ticker.isdigit()


def normalize_ticker(value: str | int | None) -> str:
    """Ticker-like 입력에서 6자리 종목코드를 안전하게 추출한다.

    Examples:
        "5930" -> "005930"
        "005930.KS" -> "005930"
        None / invalid -> ""
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    exact = text.zfill(6)
    if is_valid_ticker(exact):
        return exact
    match = re.search(r"\d{1,6}", text)
    if not match:
        return ""
    ticker = match.group(0).zfill(6)
    return ticker if is_valid_ticker(ticker) and ticker != "000000" else ""

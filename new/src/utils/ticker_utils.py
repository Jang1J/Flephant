"""종목코드 유틸. CLAUDE.md 표준: str(ticker).zfill(6) 6자리 zero-padded."""
from __future__ import annotations


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

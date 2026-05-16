"""종목코드 유틸. CLAUDE.md 표준: str(ticker).zfill(6) 6자리 zero-padded."""
from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
import logging
import re
from typing import Any

from src.utils.config_loader import load as config_load

logger = logging.getLogger(__name__)

_EXPLICIT_TICKER_RE = re.compile(
    r"^(?:A)?(?P<ticker>\d{1,6})(?:\.(?:KS|KQ|KOSPI|KOSDAQ))?$",
    re.IGNORECASE,
)
_LABELED_TICKER_RE = re.compile(
    r"^(?:ticker|symbol|code|종목|종목코드|ticker_mentioned)\s*[:=]\s*"
    r"(?:A)?(?P<ticker>\d{1,6})(?:\.(?:KS|KQ|KOSPI|KOSDAQ))?$",
    re.IGNORECASE,
)
_STANDALONE_SIX_DIGIT_RE = re.compile(r"(?<!\d)(?P<ticker>\d{6})(?!\d)")


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


def _coerce_ticker(value: Any) -> str:
    """단일 ticker 값을 6자리로 변환하고 검증한다."""
    ticker = pad_ticker(value)
    return ticker if is_valid_ticker(ticker) else ""


def _normalize_allowed_tickers(allowed_tickers: Iterable[str | int] | None) -> set[str]:
    """호출자가 명시한 universe를 6자리 ticker set으로 정규화한다."""
    if allowed_tickers is None:
        return set()
    normalized: set[str] = set()
    for value in allowed_tickers:
        ticker = _coerce_ticker(value)
        if ticker:
            normalized.add(ticker)
    return normalized


def _collect_stock_tickers(items: Any, *, active_only: bool) -> set[str]:
    """config list/dict 구조에서 ticker 필드만 추출한다."""
    tickers: set[str] = set()
    if not isinstance(items, list):
        return tickers
    for item in items:
        if not isinstance(item, dict):
            continue
        if active_only and item.get("status") != "active":
            continue
        ticker = _coerce_ticker(item.get("ticker"))
        if ticker:
            tickers.add(ticker)
    return tickers


@lru_cache(maxsize=1)
def _known_active_watch_tickers() -> frozenset[str]:
    """active trade universe + watch universe ticker set을 lazy-load 한다."""
    tickers: set[str] = set()

    try:
        universe_cfg = config_load("universe_config.yaml") or {}
        sectors = universe_cfg.get("sectors") or {}
        if isinstance(sectors, dict):
            for sector in sectors.values():
                if isinstance(sector, dict):
                    tickers.update(
                        _collect_stock_tickers(sector.get("stocks"), active_only=True)
                    )
    except Exception as e:
        logger.warning("[ticker_utils] active universe 로드 실패: %s", e)

    try:
        watch_cfg = config_load("watch_universe_kospi200.yaml") or {}
        tickers.update(
            _collect_stock_tickers(watch_cfg.get("tickers"), active_only=False)
        )
    except Exception as e:
        logger.warning("[ticker_utils] watch universe 로드 실패: %s", e)

    return frozenset(tickers)


def normalize_ticker(
    value: str | int | None,
    *,
    allowed_tickers: Iterable[str | int] | None = None,
) -> str:
    """Ticker-like 입력에서 6자리 종목코드를 안전하게 추출한다.

    Examples:
        "5930" -> "005930"
        "005930.KS" -> "005930"
        None / invalid -> ""

    자유 텍스트에서는 1~5자리 숫자를 zfill 하지 않는다. 텍스트 안의 6자리
    코드는 호출자가 allowed_tickers를 넘기거나 active/watch universe에 있는
    경우에만 ticker로 인정한다.
    """
    if value is None:
        return ""
    if isinstance(value, int):
        return _coerce_ticker(value)

    text = str(value).strip()
    if not text:
        return ""

    exact_match = _EXPLICIT_TICKER_RE.fullmatch(text)
    if exact_match:
        return _coerce_ticker(exact_match.group("ticker"))

    labeled_match = _LABELED_TICKER_RE.fullmatch(text)
    if labeled_match:
        return _coerce_ticker(labeled_match.group("ticker"))

    matches = {
        match.group("ticker") for match in _STANDALONE_SIX_DIGIT_RE.finditer(text)
    }
    if len(matches) != 1:
        return ""

    ticker = next(iter(matches))
    known_tickers = (
        _normalize_allowed_tickers(allowed_tickers)
        if allowed_tickers is not None
        else set(_known_active_watch_tickers())
    )
    return ticker if ticker in known_tickers else ""

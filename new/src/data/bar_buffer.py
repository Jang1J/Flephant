"""C1 MinuteBar ring buffer. C1→C3 연결 고리.

Sprint 1-0 실구현: ticker별 deque ring buffer.
Hot Path Quant Agent(C3)가 consume하는 완충층.

maxlen: risk_config.yaml bar_buffer.max_bars_per_ticker 로드 (하드코딩 금지).
ticker: 6자리 zero-padded (pad_ticker 경유).
"""
from __future__ import annotations

from collections import deque
from typing import Any

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.ticker_utils import pad_ticker

logger = get_logger("bar_buffer")

# C1 MinuteBarContract 필수 필드
_REQUIRED_FIELDS = frozenset(
    {"ticker", "ts_close", "open", "high", "low", "close", "volume"}
)


def _load_max_bars() -> int:
    """risk_config.yaml bar_buffer.max_bars_per_ticker 로드.

    불변 원칙 5 엄격 적용: yaml 로드 실패 시 KeyError 전파 (fallback 금지).
    섹션 누락은 설정 오류이므로 silent fallback 대신 명시적 실패가 올바르다.
    """
    cfg = config_load("risk_config.yaml", "bar_buffer")
    return int(cfg["max_bars_per_ticker"])


class BarBuffer:
    """Ticker별 in-memory ring buffer. ticker → deque(maxlen=N).

    C1 MinuteBarContract 생산자(KISWebSocketClient)와
    C3 QuantFeatureContract 소비자(QuantAgent) 사이의 완충층.

    maxlen은 risk_config.yaml bar_buffer.max_bars_per_ticker 로드.
    push 시 필수 필드(C1 기준) 검증 수행.
    """

    def __init__(self, max_bars: int | None = None) -> None:
        """max_bars 미지정 시 risk_config.yaml 로드."""
        self._max_bars: int = max_bars if max_bars is not None else _load_max_bars()
        self._buffers: dict[str, deque[dict]] = {}
        logger.info("BarBuffer 초기화: max_bars_per_ticker=%d", self._max_bars)

    def _get_or_create(self, ticker: str) -> deque[dict]:
        """ticker deque 없으면 생성."""
        if ticker not in self._buffers:
            self._buffers[ticker] = deque(maxlen=self._max_bars)
        return self._buffers[ticker]

    def push(self, bar: dict) -> None:
        """1분봉 dict 저장. bar["ticker"]로 키 구분.

        필수 필드 검증: ticker, ts_close, open, high, low, close, volume.
        누락 시 ValueError.
        """
        missing = _REQUIRED_FIELDS - bar.keys()
        if missing:
            raise ValueError(
                f"BarBuffer.push: 필수 필드 누락 {sorted(missing)}. bar keys={list(bar.keys())}"
            )

        ticker = pad_ticker(str(bar["ticker"]))
        normalized = dict(bar)
        normalized["ticker"] = ticker

        buf = self._get_or_create(ticker)
        buf.append(normalized)

    def get_latest(self, ticker: str, n: int = 60) -> list[dict]:
        """ticker의 최근 n개 bar 반환 (시간순 오름차순).

        n > 현재 buffer 크기면 가용한 것만 반환.
        """
        ticker = pad_ticker(str(ticker))
        buf = self._buffers.get(ticker)
        if buf is None:
            return []
        items = list(buf)
        if n >= len(items):
            return items
        return items[-n:]

    def get_batch(
        self, tickers: list[str], n_bars: int = 60
    ) -> dict[str, list[dict]]:
        """여러 ticker 한 번에 최근 n_bars 반환.

        Cross-sectional 처리 전제. 없는 ticker는 빈 리스트로 포함.
        """
        return {pad_ticker(str(t)): self.get_latest(t, n_bars) for t in tickers}

    def missing_check(
        self, tickers: list[str], expected_n: int = 60
    ) -> dict[str, int]:
        """ticker별 buffer 크기 확인.

        expected_n 미달 ticker 포함 전체 실제 크기 반환.
        Returns: {ticker: actual_n_bars}
        """
        result: dict[str, int] = {}
        for ticker in tickers:
            padded = pad_ticker(str(ticker))
            buf = self._buffers.get(padded)
            actual = len(buf) if buf is not None else 0
            result[padded] = actual
            if actual < expected_n:
                logger.warning(
                    "[bar_buffer] %s buffer 부족: actual=%d / expected=%d",
                    padded, actual, expected_n,
                )
        return result

    def clear(self, ticker: str | None = None) -> None:
        """ticker 지정 시 해당만 clear. None이면 전체 clear."""
        if ticker is None:
            self._buffers.clear()
            logger.info("[bar_buffer] 전체 buffer clear")
        else:
            padded = pad_ticker(str(ticker))
            if padded in self._buffers:
                self._buffers[padded].clear()
                logger.info("[bar_buffer] %s buffer clear", padded)

    @property
    def tickers(self) -> list[str]:
        """현재 buffer에 있는 ticker 리스트 (오름차순)."""
        return sorted(self._buffers.keys())

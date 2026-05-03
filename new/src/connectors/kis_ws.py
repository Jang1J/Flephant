"""KIS WebSocket 커넥터. 1분봉 실시간 스트림. Hot Path 전용.

모드별 동작:
  mock    : sync iterator로 fake 1분봉 스트림. Sprint 0 S0-2 Hot Path 테스트용.
  virtual : 모의투자 WebSocket. Sprint 1 구현 예정.
  real    : 실계좌 WebSocket. Sprint 1+ 구현 예정.

Hot Path 불변 원칙:
  - 동기 LLM 호출 금지
  - C1 MinuteBarContract 생산자
  - 인증: AuthManager 경유 (.env 직접 접근 금지)
"""
from __future__ import annotations

import os
import random
import time
from datetime import datetime
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from src.utils.auth import AuthManager
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.rate_limiter import RateLimiter
from src.utils.ticker_utils import pad_ticker

logger = get_logger("kis_ws")

_KST = ZoneInfo("Asia/Seoul")


class KISWebSocketClient:
    """KIS 실시간 1분봉 WebSocket. Mock 모드 Sprint 0 S0-2.

    Mock 모드:
        KIS_MODE=mock에서 sync iterator로 1분봉 스트림 생성.
        Sprint 1 Hot Path 테스트용. real/virtual은 Sprint 1 구현.

    사용 예:
        ws = KISWebSocketClient(tickers=["005930", "000660"])
        for bar in ws.subscribe(n_bars=10):
            ...  # 1분봉 dict 처리
    """

    def __init__(
        self,
        tickers: list[str] | None = None,
        auth: AuthManager | None = None,
        poll_interval_sec: float = 0.0,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        """
        Args:
            tickers: 구독 종목 코드 리스트. None이면 005930 단독.
            auth: AuthManager 인스턴스. 미지정 시 자동 생성.
            poll_interval_sec: Mock 스트림 yield 사이 대기 시간(초). 기본 0 (즉시).
            rate_limiter: RateLimiter 인스턴스. 미지정 시 rate_limits.kis_ws 로드.
        """
        self.auth = auth or AuthManager()
        self.mode = self.auth.get_mode()
        self.tickers = [pad_ticker(t) for t in (tickers or ["005930"])]
        self.poll_interval_sec = poll_interval_sec

        # RateLimiter 연동 (2026-04-21 감사 반영). WebSocket은 구독 기반이라 선택적.
        # rate_limits.kis_ws 섹션이 있으면 사용, 없으면 None (연결 기반 WS는 선택적).
        rate_cfg = config_load("risk_config.yaml", "rate_limits")
        if "kis_ws" in rate_cfg:
            self._rate_limiter = rate_limiter or RateLimiter("kis_ws")
        else:
            self._rate_limiter = rate_limiter

        # connector_mock 파라미터 로드 (불변 원칙 5: 하드코딩 금지)
        mock_cfg = config_load("risk_config.yaml", "connector_mock")
        kis_mock = mock_cfg.get("kis", {})
        self._base_price: int = int(kis_mock.get("base_price", 50000))
        self._price_modulo: int = int(kis_mock.get("price_modulo", 100000))
        self._mock_volume_min: int = int(kis_mock.get("volume_min", 1000))
        self._mock_volume_max: int = int(kis_mock.get("volume_max", 100000))
        self._mock_change_min: int = int(kis_mock.get("change_min", 0))
        self._mock_change_max: int = int(kis_mock.get("change_max", 100))
        self._mock_bid_size_min: int = int(kis_mock.get("bid_size_min", 500))
        self._mock_bid_size_max: int = int(kis_mock.get("bid_size_max", 10000))

        if self.mode == "mock":
            seed_str = os.getenv("KIS_MOCK_SEED", "42")
            self._rng = random.Random(int(seed_str))
            logger.info(
                "Mock 모드. tickers=%d개, seed=%s",
                len(self.tickers),
                seed_str,
            )

    def subscribe(self, n_bars: int = 10) -> Iterator[dict[str, Any]]:
        """1분봉 스트림 iterator.

        Mock 모드: n_bars만큼 순차 yield. 종목 순서 유지.
                   poll_interval_sec > 0이면 yield 사이 sleep.
        실 모드: Sprint 1 WebSocket 연결 구현.

        Yields:
            C1 MinuteBarContract 호환 dict:
            {"ticker", "open", "high", "low", "close", "volume",
             "ts_close": ISO 8601 KST, "_mode": "mock"}
        """
        if self.mode != "mock":
            raise NotImplementedError(
                f"KIS_MODE={self.mode}: Sprint 1 WebSocket 구현 예정."
            )

        base_prices = {
            t: self._base_price + (int(t) % self._price_modulo)
            for t in self.tickers
        }
        # C1 required_features: vwap/turnover/change/ingest_ts/completeness 포함
        for _ in range(n_bars):
            for ticker in self.tickers:
                base = base_prices[ticker]
                delta = self._rng.randint(-200, 200)
                close_p = base + delta
                high_p = max(base, close_p) + self._rng.randint(
                    self._mock_change_min, self._mock_change_max
                )
                low_p = min(base, close_p) - self._rng.randint(
                    self._mock_change_min, self._mock_change_max
                )
                volume = self._rng.randint(self._mock_bid_size_min, self._mock_bid_size_max)
                vwap = (base + high_p + low_p + close_p) / 4.0
                turnover = float(vwap * volume)
                change = float(close_p - base)
                base_prices[ticker] = close_p
                now = datetime.now(_KST).replace(second=0, microsecond=0)
                yield {
                    "ticker": ticker,
                    "open": base,
                    "high": high_p,
                    "low": low_p,
                    "close": close_p,
                    "volume": volume,
                    "vwap": vwap,
                    "turnover": turnover,
                    "change": change,
                    "ts_close": now.isoformat(),
                    "ingest_ts": now.isoformat(),
                    "completeness": "full",
                    "_mode": "mock",
                }
            if self.poll_interval_sec > 0:
                time.sleep(self.poll_interval_sec)

    def connect(self) -> None:
        """WebSocket 연결 시작. Sprint 1 구현."""
        if self.mode == "mock":
            logger.info("Mock 모드: connect() 스킵.")
            return
        raise NotImplementedError("Sprint 1 구현 예정")

    def disconnect(self) -> None:
        """연결 종료. Sprint 1 구현."""
        if self.mode == "mock":
            logger.info("Mock 모드: disconnect() 스킵.")
            return
        raise NotImplementedError("Sprint 1 구현 예정")

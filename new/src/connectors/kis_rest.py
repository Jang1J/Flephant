"""KIS REST 커넥터. 주문/잔고/분봉 조회.

모드별 동작:
  mock    : 로컬 fake OHLCV 생성. 실 API 호출 없음. Sprint 0 S0-2 완료용.
  virtual : 모의투자. Sprint 1 구현 예정.
  real    : 실계좌. Sprint 1+ 구현 예정.

불변 원칙 준수:
  - 인증: AuthManager 경유 (.env 직접 접근 금지)
  - Rate: RateLimiter("kis_rest") 경유
  - 하드코딩 금지: magic number 없음
  - 종목코드: pad_ticker() 경유 6자리 zero-padded
"""
from __future__ import annotations

import os
import random
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.connectors.base import BaseConnector
from src.utils.auth import AuthManager
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.pit_guard import PITViolationError, is_pit_safe
from src.utils.rate_limiter import RateLimiter
from src.utils.ticker_utils import pad_ticker

logger = get_logger("kis_rest")

_KST = ZoneInfo("Asia/Seoul")


class KISAPIError(Exception):
    """KIS API 오류."""


class KISRestClient(BaseConnector):
    """KIS 한국투자증권 REST. 모의투자 / 실계좌 / Mock 3 모드.

    Mock 모드 (S0-2 Sprint 0 완료용):
        KIS_MODE=mock 환경변수로 활성화. 실 API 호출 없이 fake OHLCV 생성.
        재현성을 위해 seed 사용 (KIS_MOCK_SEED 환경변수, 기본 42).

    Sprint 1에서 virtual/real 모드 실 구현 추가 예정.
    """

    def __init__(
        self,
        auth: AuthManager | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        super().__init__()  # BaseConnector: timeout_sec / max_retries / backoff_base 로드
        # 언더스코어 별칭 유지 (내부 코드 하위 호환)
        self._timeout_sec = self.timeout_sec
        self._max_retries = self.max_retries
        self._backoff_base = self.backoff_base

        self.auth = auth or AuthManager()
        self.rate_limiter = rate_limiter or RateLimiter("kis_rest")
        self.mode = self.auth.get_mode()

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
            logger.info("Mock 모드 활성. seed=%s", seed_str)

    def inquire_price(self, ticker: str) -> dict[str, Any]:
        """현재가 조회. Mock 모드면 fake 가격. 실 모드는 Sprint 1 구현.

        Returns:
            {"ticker": "005930", "current_price": int, "volume": int,
             "ts_close": ISO 8601 KST string}
        """
        ticker = pad_ticker(ticker)
        self.rate_limiter.wait_and_acquire()

        if self.mode == "mock":
            return self._mock_inquire_price(ticker)
        raise NotImplementedError(
            f"KIS_MODE={self.mode}: Sprint 1에서 실 API 구현 예정. "
            "임시로는 KIS_MODE=mock 사용."
        )

    def get_price_snapshot(self, tickers: list[str]) -> list[dict[str, Any]]:
        """KOSPI200 watch universe N종목 일괄 현재가 조회 (S5-1, C16).

        KIS REST는 단건 inquire_price 만 공식 제공. bulk endpoint 부재 시 N회 sequential 호출.
        rate_limits.kis_rest = 20/sec, burst 50. KOSPI200 200종목 = 평균 3.3 req/s 사용 (안전).

        Args:
            tickers: 종목코드 리스트 (6자리 zero-padded 자동 적용).

        Returns:
            list of dict, each:
                {ticker, ts (ISO8601 KST), last_price, day_change_pct, volume, turnover}
        """
        if self.mode == "mock":
            return self._mock_get_price_snapshot(tickers)
        raise NotImplementedError(
            "KIS 키 발급 후 S1-8/S5-1 fix 단계에서 활성화. "
            "임시로는 KIS_MODE=mock 사용."
        )

    def _mock_get_price_snapshot(self, tickers: list[str]) -> list[dict[str, Any]]:
        """mock 모드: ticker별 fake 현재가 스냅샷 생성.

        200종목 × {last_price=10000~50000 random, day_change_pct=±2% random,
        volume=random, turnover=last_price*volume}.
        """
        results: list[dict[str, Any]] = []
        now_str = datetime.now(_KST).isoformat()
        for ticker in tickers:
            padded = pad_ticker(ticker)
            base_price = self._base_price + (int(padded) % self._price_modulo)
            last_price = base_price + self._rng.randint(-500, 500)
            # ±2% 범위 변동률
            day_change_pct = round(self._rng.uniform(-0.02, 0.02), 6)
            volume = self._rng.randint(self._mock_volume_min, self._mock_volume_max)
            turnover = float(last_price * volume)
            results.append({
                "ticker": padded,
                "ts": now_str,
                "last_price": last_price,
                "day_change_pct": day_change_pct,
                "volume": volume,
                "turnover": turnover,
                "_mode": "mock",
            })
        return results

    def inquire_minute_bar(
        self, ticker: str, n_bars: int = 60, date: str | None = None
    ) -> list[dict[str, Any]]:
        """최근 n분봉 OHLCV 조회.

        Mock 모드: n_bars 개수만큼 fake OHLCV 생성. 가격은 random walk.
        실 모드: Sprint 1 구현.

        Args:
            ticker: 종목 코드 (6자리 zero-padded 자동 적용).
            n_bars: 조회할 분봉 수 (기본 60).
            date: 기준 날짜 YYYYMMDD. None이면 당일. 실 API 경로에서 PIT-Safety guard 적용.

        Returns:
            list of OHLCV dict. ts_close 오름차순.
        """
        ticker = pad_ticker(ticker)
        self.rate_limiter.wait_and_acquire()

        if self.mode == "mock":
            return self._mock_inquire_minute_bar(ticker, n_bars)

        # PIT-Safety guard: 미래 날짜 요청 차단 (불변 원칙 1)
        if date and not is_pit_safe(
            f"{date[:4]}-{date[4:6]}-{date[6:8]}T23:59:59+09:00"
        ):
            raise PITViolationError(
                f"[kis_rest] 미래 날짜 요청 차단: date={date}"
            )

        raise NotImplementedError(
            f"KIS_MODE={self.mode}: Sprint 1에서 실 API 구현 예정."
        )

    def get_balance(self) -> dict[str, Any]:
        """현재 잔고/포지션 조회. Sprint 1 구현."""
        if self.mode == "mock":
            return {"balance": 0, "positions": [], "_mode": "mock"}
        raise NotImplementedError("Sprint 1 구현 예정")

    def submit_order(self, ticker: str, side: str, qty: int) -> dict[str, Any]:
        """주문 제출. ticker = 6자리 zero-padded. Sprint 1 구현."""
        ticker = pad_ticker(ticker)
        if self.mode == "mock":
            return {"ticker": ticker, "side": side, "qty": qty,
                    "status": "mock_accepted", "_mode": "mock"}
        raise NotImplementedError("Sprint 1 구현 예정")

    # --------------------------------------------------------------------- #
    # Mock 데이터 생성
    # --------------------------------------------------------------------- #

    def _mock_inquire_price(self, ticker: str) -> dict[str, Any]:
        base_price = self._base_price + (int(ticker) % self._price_modulo)
        delta = self._rng.randint(-500, 500)
        now_str = datetime.now(_KST).isoformat()
        return {
            "ticker": ticker,
            "current_price": base_price + delta,
            "volume": self._rng.randint(self._mock_volume_min, self._mock_volume_max),
            "ts_close": now_str,
            "ingest_ts": now_str,
            "completeness": "full",
            "_mode": "mock",
        }

    def _mock_inquire_minute_bar(
        self, ticker: str, n_bars: int
    ) -> list[dict[str, Any]]:
        base = self._base_price + (int(ticker) % self._price_modulo)
        now = datetime.now(_KST).replace(second=0, microsecond=0)
        bars: list[dict[str, Any]] = []
        price = base
        prev_close = base
        for i in range(n_bars):
            ts = now - timedelta(minutes=n_bars - 1 - i)
            open_p = price
            close_p = price + self._rng.randint(-200, 200)
            high_p = max(open_p, close_p) + self._rng.randint(
                self._mock_change_min, self._mock_change_max
            )
            low_p = min(open_p, close_p) - self._rng.randint(
                self._mock_change_min, self._mock_change_max
            )
            volume = self._rng.randint(self._mock_bid_size_min, self._mock_bid_size_max)
            # C1 required_features: vwap / turnover / change / ingest_ts / completeness
            vwap = (open_p + high_p + low_p + close_p) / 4.0
            turnover = float(vwap * volume)
            change = float(close_p - prev_close)
            ingest_ts = now.isoformat()
            bars.append({
                "ticker": ticker,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "volume": volume,
                "vwap": vwap,
                "turnover": turnover,
                "change": change,
                "ts_close": ts.isoformat(),
                "ingest_ts": ingest_ts,
                "completeness": "full",
                "_mode": "mock",
            })
            price = close_p
            prev_close = close_p
        return bars

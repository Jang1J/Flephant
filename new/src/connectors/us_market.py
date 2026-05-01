"""US Market 야간 데이터 커넥터 v3 Sprint 2 S2-5 (2026-04-21).

미국 야간 지수 수집 (SPX/NDX/VIX/SOXX). 08:30 KST 배치 (한국 장 개장 전 1회).

## 사용 패턴
    client = USMarketClient()
    idx = client.get_indices(as_of="2026-04-21")
    # USMarketIndices(us_sp500_change=-0.003, us_nasdaq_change=-0.005,
    #                 us_vix=18.5, us_soxx_change=-0.011, ...)

## Data Source
1차: yfinance (설치 시). SPX: ^GSPC, NDX: ^IXIC, VIX: ^VIX, SOXX: SOXX
2차 Mock: yfinance 미설치 또는 US_MARKET_MOCK=1 시 자동 분기.

## PIT-Safety
d-1 US close 데이터만 사용. 오늘 미국 장중 데이터 참조 금지.

SSOT: api_contracts.md v3.5 C3 us_overnight 4 피처.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import time

from src.connectors.base import BaseConnector
from src.data.event_normalizer import EventNormalizer
from src.utils.logger import get_logger
from src.utils.pit_guard import PITViolationError, is_pit_safe
from src.utils.rate_limiter import RateLimiter

logger = get_logger("us_market")

_KST = ZoneInfo("Asia/Seoul")

# yfinance ticker → feature명 매핑
_SYMBOLS: dict[str, str] = {
    "us_sp500": "^GSPC",
    "us_nasdaq": "^IXIC",
    "us_vix": "^VIX",
    "us_soxx": "SOXX",
}

# yfinance 선택적 import (설치 안 된 환경 대비)
try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    yf = None  # type: ignore[assignment]
    _YF_AVAILABLE = False


@dataclass
class USMarketIndices:
    """C3 us_overnight 4 피처.

    단위: change 필드는 소수 비율 (-0.003 = -0.3%).
    us_vix 는 absolute level.
    """

    us_sp500_change: float    # d-1 close 대비 % change (소수)
    us_nasdaq_change: float
    us_vix: float              # absolute level (예: 18.5)
    us_soxx_change: float
    as_of_date: str            # 'YYYY-MM-DD' (d-1 US close 기준일)
    source: str                # 'yfinance' | 'mock'


class USMarketClient(BaseConnector):
    """US Market 야간 지수 클라이언트. 08:30 KST 배치 1회 호출.

    DART/Naver/ECOS 패턴 통일: rate_limiter/config YAML 경유.
    PIT-Safety: d-1 US close 만 사용. 오늘 미국 장중 데이터 금지.
    """

    def __init__(
        self,
        rate_limiter: RateLimiter | None = None,
        normalizer: EventNormalizer | None = None,
    ) -> None:
        super().__init__()  # BaseConnector: timeout_sec / max_retries / backoff_base 로드
        self._timeout_sec = self.timeout_sec

        self._rate_limiter = rate_limiter or RateLimiter("us_market")
        self._normalizer = normalizer or EventNormalizer()

        # Mock 분기: yfinance 미설치 or 환경변수 강제
        self._is_mock = not _YF_AVAILABLE or os.environ.get("US_MARKET_MOCK") == "1"
        if self._is_mock:
            reason = "yfinance 미설치" if not _YF_AVAILABLE else "US_MARKET_MOCK=1"
            logger.info("[us_market] Mock 모드 (%s)", reason)

    def get_indices(self, as_of: str | None = None) -> USMarketIndices:
        """08:30 KST 배치. d-1 US close 지수 조회.

        PIT-Safety: as_of 당일 KST 기준 d-1 US 날짜를 계산해 이전 close 만 수집.
        오늘 미국 장중 데이터는 절대 참조하지 않음.

        Args:
            as_of: 한국 날짜 'YYYY-MM-DD' (미지정 시 오늘). 내부에서 d-1 US 날짜 계산.

        Returns:
            USMarketIndices (4 필드 + date + source)
        """
        if as_of is None:
            as_of = datetime.now(_KST).strftime("%Y-%m-%d")

        # PIT-Safety guard: as_of가 미래 날짜인 경우 즉시 차단 (불변 원칙 1)
        if not is_pit_safe(as_of):
            raise PITViolationError(
                f"[us_market] PIT violation: as_of={as_of}는 미래 날짜"
            )

        # PIT-Safety: KST 당일 → d-1 US 날짜 (KST는 US 대비 +14~+15시간)
        kst_date = datetime.strptime(as_of, "%Y-%m-%d").replace(tzinfo=_KST)
        us_date = (kst_date - timedelta(days=1)).strftime("%Y-%m-%d")

        if self._is_mock:
            return self._mock_indices(us_date)

        self._rate_limiter.wait_and_acquire()
        return self._fetch_yfinance(us_date)

    def to_normalizer_input(self, indices: USMarketIndices) -> dict:
        """USMarketIndices → EventNormalizer raw dict 변환.

        필수 필드:
            close_time_utc  (미 동부 마감 UTC, as_of_date d-1 기준)
        선택 필드:
            sp500_change, nasdaq_change, vix, soxx_change, source_tag
        """
        # as_of_date (US d-1 기준) → 미국 동부 마감 UTC 20:00 (DST 적용 시 21:00Z)
        # 간략화: 평균 20:00 UTC 사용 (DST 여부 관계없이 normalizer 처리 가능한 형태)
        us_close_utc = f"{indices.as_of_date}T20:00:00+00:00"

        return {
            "close_time_utc": us_close_utc,                # 필수 (normalizer 요구)
            "sp500_change": indices.us_sp500_change,
            "nasdaq_change": indices.us_nasdaq_change,
            "vix": indices.us_vix,
            "soxx_change": indices.us_soxx_change,
            "source_tag": indices.source,                  # 'yfinance' | 'mock'
        }

    def poll_and_normalize(self, as_of: str | None = None) -> list[dict]:
        """US Market 수집 + EventNormalizer 경유 C2 event 반환.

        **경고**: EventGateway bypass (편의 메서드).
        Cold Path 운영은 get_indices() + to_normalizer_input() +
        EventGateway.ingest() 조합을 사용할 것.
        """
        indices = self.get_indices(as_of=as_of)
        raw = self.to_normalizer_input(indices)
        try:
            event = self._normalizer.normalize(raw, source="us_market")
            return [event]
        except Exception as e:
            logger.warning("[us_market] 정규화 실패: as_of=%s err=%s", as_of, e)
            return []

    # ------------------------------------------------------------------ #
    # 내부 helper
    # ------------------------------------------------------------------ #

    def _fetch_yfinance(self, us_date: str) -> USMarketIndices:
        """yfinance 경유 4 지수 수집.

        주말/휴일 대비 5 영업일 범위 조회 후 가장 최근 close 사용.
        max_retries / backoff_base 는 BaseConnector (risk_config.yaml connector_defaults) 경유.
        """
        end = datetime.strptime(us_date, "%Y-%m-%d") + timedelta(days=1)
        start = end - timedelta(days=7)  # 주말 2일 + 공휴일 여유 포함

        def _fetch_single(symbol: str):
            ticker = yf.Ticker(symbol)
            return ticker.history(
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                interval="1d",
            )

        def pct_change_from_hist(hist) -> float:
            closes = hist.get("Close")
            if closes is None or len(closes) < 2:
                return 0.0
            return float((closes.iloc[-1] - closes.iloc[-2]) / closes.iloc[-2])

        def vix_from_hist(hist) -> float:
            closes = hist.get("Close")
            if closes is None or len(closes) == 0:
                return 20.0
            return float(closes.iloc[-1])

        for attempt in range(self.max_retries):
            try:
                hist_sp500 = _fetch_single("^GSPC")
                hist_ndx = _fetch_single("^IXIC")
                hist_vix = _fetch_single("^VIX")
                hist_soxx = _fetch_single("SOXX")

                return USMarketIndices(
                    us_sp500_change=pct_change_from_hist(hist_sp500),
                    us_nasdaq_change=pct_change_from_hist(hist_ndx),
                    us_vix=vix_from_hist(hist_vix),
                    us_soxx_change=pct_change_from_hist(hist_soxx),
                    as_of_date=us_date,
                    source="yfinance",
                )
            except Exception as e:
                logger.warning(
                    "[us_market] yfinance 시도 %d/%d 실패: %s",
                    attempt + 1,
                    self.max_retries,
                    e,
                )
                if attempt < self.max_retries - 1:
                    time.sleep(self.backoff_base ** attempt)

        logger.warning("[us_market] yfinance 전체 실패, mock fallback 사용")
        return self._mock_indices(us_date)

    def _mock_indices(self, us_date: str) -> USMarketIndices:
        """Mock 응답. Sprint 2 + yfinance 실패 시 fallback."""
        return USMarketIndices(
            us_sp500_change=-0.003,
            us_nasdaq_change=-0.005,
            us_vix=18.5,
            us_soxx_change=-0.011,
            as_of_date=us_date,
            source="mock",
        )

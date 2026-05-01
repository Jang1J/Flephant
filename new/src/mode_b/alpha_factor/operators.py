"""Alpha Factor Operator Library (AlphaAgent §2).

Factor code에서 import 없이 사용 가능한 연산자 모음.
factor(df) 함수 실행 환경(exec namespace)에 주입됨.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def rolling_mean(series: pd.Series, window: int) -> pd.Series:
    """시계열 단순 이동평균 (SMA)."""
    return series.rolling(window, min_periods=1).mean()


def rolling_std(series: pd.Series, window: int) -> pd.Series:
    """시계열 롤링 표준편차."""
    return series.rolling(window, min_periods=1).std()


def rolling_min(series: pd.Series, window: int) -> pd.Series:
    """시계열 롤링 최솟값."""
    return series.rolling(window, min_periods=1).min()


def rolling_max(series: pd.Series, window: int) -> pd.Series:
    """시계열 롤링 최댓값."""
    return series.rolling(window, min_periods=1).max()


def cs_rank(series: pd.Series) -> pd.Series:
    """Cross-sectional rank (백분위, 0~1)."""
    return series.rank(pct=True)


def ts_rank(series: pd.Series, window: int) -> pd.Series:
    """시계열 롤링 rank percentile (각 시점에서 window 내 순위)."""
    return series.rolling(window, min_periods=1).apply(
        lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False
    )


def ts_zscore(series: pd.Series, window: int) -> pd.Series:
    """시계열 롤링 z-score 정규화."""
    m = series.rolling(window, min_periods=1).mean()
    s = series.rolling(window, min_periods=1).std()
    return (series - m) / (s + 1e-8)


def ts_momentum(series: pd.Series, window: int) -> pd.Series:
    """시계열 모멘텀: series / series.shift(window) - 1."""
    return series / (series.shift(window) + 1e-8) - 1.0


def correlation(a: pd.Series, b: pd.Series, window: int) -> pd.Series:
    """두 시계열의 롤링 피어슨 상관계수."""
    return a.rolling(window, min_periods=1).corr(b)


def ema(series: pd.Series, span: int) -> pd.Series:
    """지수 이동평균 (Exponential Moving Average)."""
    return series.ewm(span=span, adjust=False).mean()


def volume_ratio(volume: pd.Series, window: int) -> pd.Series:
    """현재 거래량 / 롤링 평균 거래량 (상대 거래량)."""
    avg_vol = rolling_mean(volume, window)
    return volume / (avg_vol + 1e-8)


def price_range(high: pd.Series, low: pd.Series) -> pd.Series:
    """(고가 - 저가) / 저가. 변동폭 비율."""
    return (high - low) / (low + 1e-8)


def vwap_deviation(close: pd.Series, vwap: pd.Series) -> pd.Series:
    """(종가 - VWAP) / VWAP. 가격의 VWAP 대비 편차."""
    return (close - vwap) / (vwap + 1e-8)


def turnover_rate(turnover: pd.Series, window: int) -> pd.Series:
    """거래대금 시계열 z-score (상대적 회전율)."""
    return ts_zscore(turnover, window)


def rank(series: pd.Series) -> pd.Series:
    """백분위 순위 (cs_rank 별칭)."""
    return series.rank(pct=True)


def ts_argmax(series: pd.Series, window: int) -> pd.Series:
    """롤링 window 내 최댓값의 상대적 위치 인덱스."""
    return series.rolling(window, min_periods=1).apply(
        lambda x: float(np.argmax(x)), raw=True
    )


def ts_argmin(series: pd.Series, window: int) -> pd.Series:
    """롤링 window 내 최솟값의 상대적 위치 인덱스."""
    return series.rolling(window, min_periods=1).apply(
        lambda x: float(np.argmin(x)), raw=True
    )


def rsi(series: pd.Series, window: int) -> pd.Series:
    """Relative Strength Index (RSI). 0~100 스케일."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(window, min_periods=1).mean()
    avg_loss = loss.rolling(window, min_periods=1).mean()
    rs = avg_gain / (avg_loss + 1e-8)
    return 100.0 - 100.0 / (1.0 + rs)


# ------------------------------------------------------------------ #
# Factor 실행 환경(namespace)에 주입되는 dict
# factor(df) exec 시 locals/globals로 전달.
# ------------------------------------------------------------------ #
OPERATOR_NAMESPACE: dict = {
    "pd": pd,
    "np": np,
    "rolling_mean": rolling_mean,
    "rolling_std": rolling_std,
    "rolling_min": rolling_min,
    "rolling_max": rolling_max,
    "cs_rank": cs_rank,
    "ts_rank": ts_rank,
    "ts_zscore": ts_zscore,
    "ts_momentum": ts_momentum,
    "correlation": correlation,
    "ema": ema,
    "volume_ratio": volume_ratio,
    "price_range": price_range,
    "vwap_deviation": vwap_deviation,
    "turnover_rate": turnover_rate,
    "rank": rank,
    "ts_argmax": ts_argmax,
    "ts_argmin": ts_argmin,
    "rsi": rsi,
}

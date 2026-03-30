"""
Momentum Strategy FeatureFactory — Train/Serve 공통 피처 생성 모듈
Train(lgbm_ranker.py)과 Serve(build_strategy_card_momentum.py) 양쪽에서
이 모듈만 import하여 피처를 생성한다. Train-Serve Skew 방지.

공개 인터페이스:
    extract_single_ticker_features(
        dmp_market_data, ticker, close_history, date_idx,
        sector_map, universe_tickers, macro,
    ) -> dict

    build_cross_sectional_raw(dmp_market_data, universe_tickers, close_history, date_idx)
        -> dict[str, dict[str, float]]  (각 피처 이름 → {ticker: raw_value})

    pct_rank(raw_dict, ticker) -> float
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ──────────────────────────────────────────────────────────────────────────────
# 기술적 계산 헬퍼 (내부 전용)
# ──────────────────────────────────────────────────────────────────────────────

def _compute_macd(
    close_series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[float, float]:
    """MACD 및 Signal line 계산. 짧은 시리즈는 NaN 반환."""
    if len(close_series) < slow + signal:
        return np.nan, np.nan
    ema_fast = close_series.ewm(span=fast, adjust=False).mean()
    ema_slow = close_series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line.iloc[-1], signal_line.iloc[-1]


def _compute_sma60_ratio(close_series: pd.Series) -> float:
    """close / sma_60 - 1. 60일치 미만이면 NaN."""
    if len(close_series) < 60:
        return np.nan
    sma60 = close_series.iloc[-60:].mean()
    if sma60 == 0:
        return np.nan
    return close_series.iloc[-1] / sma60 - 1.0


def _compute_return_60d(close_series: pd.Series) -> float:
    """60거래일 수익률 (%). 60일치 미만이면 NaN."""
    if len(close_series) < 61:
        return np.nan
    p0 = close_series.iloc[-61]
    p1 = close_series.iloc[-1]
    if p0 == 0:
        return np.nan
    return (p1 / p0 - 1.0) * 100.0


def _safe_float(val, fallback: float = np.nan) -> float:
    """None/NaN 안전 float 변환."""
    if val is None:
        return fallback
    try:
        f = float(val)
        return f
    except (TypeError, ValueError):
        return fallback


# ──────────────────────────────────────────────────────────────────────────────
# Cross-sectional 원시값 수집
# ──────────────────────────────────────────────────────────────────────────────

def build_cross_sectional_raw(
    dmp_market_data: dict,
    universe_tickers: list[str],
    close_history: dict[str, list[float]] | None = None,
    date_idx: int | None = None,
) -> dict[str, dict[str, float]]:
    """
    날짜별 cross-sectional percentile rank 계산을 위한 원시값 수집.

    Args:
        dmp_market_data: DMP["market_data"] dict.
        universe_tickers: 유니버스 종목코드 리스트.
        close_history: {ticker: [close, ...]} 누적 시계열. None이면 sma 기반 계산 건너뜀.
        date_idx: close_history에서 현재 날짜 인덱스. None이면 close_history 전체 사용.

    Returns:
        {피처명: {ticker: raw_value}} 형태의 dict.
        피처명: ret_5d, ret_20d, rsi_14, volume_ratio_20,
                sma5_ratio, sma20_ratio, sma60_ratio, return_60d
    """
    cs_raw: dict[str, dict[str, float]] = {
        "ret_5d": {},
        "ret_20d": {},
        "rsi_14": {},
        "volume_ratio_20": {},
        "sma5_ratio": {},
        "sma20_ratio": {},
        "sma60_ratio": {},
        "return_60d": {},
    }

    for _t in universe_tickers:
        _td = dmp_market_data.get(_t, {})
        if not _td:
            continue
        _tech = _td.get("tech_features", {})
        _cl_raw = _td.get("ohlcv", {}).get("close")
        _cl = float(_cl_raw) if (_cl_raw is not None and _cl_raw != 0) else None
        _sma5 = _tech.get("sma_5")
        _sma20 = _tech.get("sma_20")

        # 누적 종가 시계열 (있을 때만)
        if close_history is not None and _t in close_history:
            _end = (date_idx + 1) if date_idx is not None else len(close_history[_t])
            _hist = pd.Series(close_history[_t][:_end]).dropna()
        else:
            _hist = pd.Series(dtype=float)

        _r5 = _tech.get("return_5d")
        _r20 = _tech.get("return_20d")
        _rsi = _tech.get("rsi_14")
        _vol = _tech.get("volume_ratio_20")
        _s5r = (_cl / float(_sma5) - 1.0) if (_cl is not None and _sma5 and _sma5 != 0) else None
        _s20r = (_cl / float(_sma20) - 1.0) if (_cl is not None and _sma20 and _sma20 != 0) else None
        _s60r = _compute_sma60_ratio(_hist) if len(_hist) > 0 else np.nan
        _r60 = _compute_return_60d(_hist) if len(_hist) > 0 else np.nan

        if _r5 is not None:
            cs_raw["ret_5d"][_t] = float(_r5)
        if _r20 is not None:
            cs_raw["ret_20d"][_t] = float(_r20)
        if _rsi is not None:
            cs_raw["rsi_14"][_t] = float(_rsi)
        if _vol is not None:
            cs_raw["volume_ratio_20"][_t] = float(_vol)
        if _s5r is not None:
            cs_raw["sma5_ratio"][_t] = float(_s5r)
        if _s20r is not None:
            cs_raw["sma20_ratio"][_t] = float(_s20r)
        if not (isinstance(_s60r, float) and np.isnan(_s60r)):
            cs_raw["sma60_ratio"][_t] = float(_s60r)
        if not (isinstance(_r60, float) and np.isnan(_r60)):
            cs_raw["return_60d"][_t] = float(_r60)

    return cs_raw


def pct_rank(raw_dict: dict[str, float], ticker: str) -> float:
    """종목의 percentile rank (0~1). 종목 없거나 값 1개 이하면 0.5 반환."""
    vals = list(raw_dict.values())
    if ticker not in raw_dict or len(vals) < 2:
        return 0.5
    val = raw_dict[ticker]
    rank = sum(1 for v in vals if v < val)
    return rank / (len(vals) - 1)


# ──────────────────────────────────────────────────────────────────────────────
# 단일 종목 피처 추출 — 핵심 공개 인터페이스
# ──────────────────────────────────────────────────────────────────────────────

def extract_single_ticker_features(
    dmp_market_data: dict,
    ticker: str,
    close_history: dict[str, list[float]] | None,
    date_idx: int | None,
    sector_map: dict[str, str],
    universe_tickers: list[str],
    macro: dict,
    cs_raw: dict[str, dict[str, float]] | None = None,
    sector_mean_ratio: dict[str, float] | None = None,
    universe_mean_ret5: float | None = None,
    prev_close_val: float | None = None,
) -> dict | None:
    """
    단일 종목의 28개 피처를 계산하여 dict로 반환한다.

    Train(lgbm_ranker.py)과 Serve(build_strategy_card_momentum.py) 모두 이 함수를 호출하여
    피처 계산 로직의 단일 소스를 유지한다.

    Args:
        dmp_market_data: DMP["market_data"] dict.
        ticker: 종목코드 (6자리 zero-padded).
        close_history: {ticker: [close, ...]} 누적 시계열.
            None이면 sma60_ratio/return_60d/macd/overnight_gap을 NaN/0으로 처리.
        date_idx: close_history에서 현재 날짜 인덱스 (0-based).
            None이면 전체 시계열 사용.
        sector_map: {ticker: sector} 매핑.
        universe_tickers: 유니버스 전체 종목코드 리스트.
        macro: DMP["macro_snapshot"] dict.
        cs_raw: build_cross_sectional_raw() 결과. None이면 내부에서 계산.
            Serve 경로에서 단일 DMP만 가질 때, DMP 전체 종목 데이터로 계산 가능.
        sector_mean_ratio: {sector: mean(close/sma20 - 1)} 사전 계산값.
            None이면 내부에서 계산.
        universe_mean_ret5: 유니버스 평균 return_5d. None이면 내부에서 계산.
        prev_close_val: 전일 종가 (overnight_gap 계산용). None이면 0.0 처리.

    Returns:
        피처 dict (28개 키). 종목 데이터 없거나 close=0이면 None 반환.

    PIT-Safety:
        close_history[ticker][:date_idx+1] 슬라이싱으로 미래 데이터 차단.
        macro, cs_raw 모두 동일 날짜 DMP에서 추출한 값만 사용.
    """
    tdata = dmp_market_data.get(ticker, {})
    if not tdata:
        return None

    ohlcv = tdata.get("ohlcv", {})
    tech = tdata.get("tech_features", {})

    close_raw = ohlcv.get("close")
    if close_raw is None or close_raw == 0:
        return None
    close = float(close_raw)

    # ── 누적 종가 시계열 구성 ──────────────────────────────────────────────────
    if close_history is not None and ticker in close_history:
        _end = (date_idx + 1) if date_idx is not None else len(close_history[ticker])
        hist_close = pd.Series(close_history[ticker][:_end]).dropna()
    else:
        hist_close = pd.Series(dtype=float)

    # ── Manual factors ────────────────────────────────────────────────────────
    return_5d = _safe_float(tech.get("return_5d"))
    return_20d = _safe_float(tech.get("return_20d"))
    rsi_14 = _safe_float(tech.get("rsi_14"))
    volume_ratio_20 = _safe_float(tech.get("volume_ratio_20"))
    atr_14 = _safe_float(tech.get("atr_14"))

    sma_5 = tech.get("sma_5")
    sma_20 = tech.get("sma_20")
    sma5_ratio = (close / float(sma_5) - 1.0) if sma_5 and float(sma_5) != 0 else np.nan
    sma20_ratio = (close / float(sma_20) - 1.0) if sma_20 and float(sma_20) != 0 else np.nan

    if len(hist_close) > 0:
        sma60_ratio = _compute_sma60_ratio(hist_close)
        macd_val, macd_signal_val = _compute_macd(hist_close)
        return_60d = _compute_return_60d(hist_close)
    else:
        # Serve 경로 fallback: DMP tech_features에서 직접 읽기
        sma_60 = tech.get("sma_60")
        sma60_ratio = (close / float(sma_60) - 1.0) if sma_60 and float(sma_60) != 0 else np.nan
        macd_val = _safe_float(tech.get("macd"), fallback=0.0)
        macd_signal_val = _safe_float(tech.get("macd_signal"), fallback=0.0)
        return_60d_raw = tech.get("return_60d")
        if return_60d_raw is not None:
            sma_60_ref = tech.get("sma_60")
            return_60d = float(return_60d_raw) if return_60d_raw is not None else (
                (close / float(sma_60_ref) - 1) * 100 if sma_60_ref and float(sma_60_ref) != 0 else np.nan
            )
        else:
            return_60d = np.nan

    # ── MLF-lite ──────────────────────────────────────────────────────────────
    # period_agreement_score: 5d, 20d, 60d 수익률 부호 일치 비율
    signs = []
    for r in [return_5d, return_20d, return_60d]:
        if r is not None and not (isinstance(r, float) and np.isnan(r)):
            signs.append(np.sign(float(r)))
    if len(signs) >= 2:
        dominant_sign = np.sign(np.sum(signs))
        agree_count = sum(1 for s in signs if s == dominant_sign)
        period_agreement_score = agree_count / len(signs)
    else:
        period_agreement_score = np.nan

    # ── UMI-lite ──────────────────────────────────────────────────────────────
    # 유니버스 평균 return_5d (미리 계산되지 않으면 내부 계산)
    if universe_mean_ret5 is None:
        _urs = []
        for _t in universe_tickers:
            _r5 = dmp_market_data.get(_t, {}).get("tech_features", {}).get("return_5d")
            if _r5 is not None:
                _urs.append(float(_r5))
        universe_mean_ret5 = float(np.nanmean(_urs)) if _urs else np.nan

    # 매크로
    market_breadth = _safe_float(macro.get("market_breadth"))

    if (
        return_5d is not None
        and not np.isnan(float(return_5d))
        and not np.isnan(universe_mean_ret5)
    ):
        stock_sync_score = 1.0 if np.sign(float(return_5d)) == np.sign(universe_mean_ret5) else 0.0
    else:
        stock_sync_score = np.nan

    if not np.isnan(universe_mean_ret5):
        breadth_w = float(market_breadth) if not np.isnan(market_breadth) else 0.5
        market_synchronism = breadth_w * universe_mean_ret5
    else:
        market_synchronism = np.nan

    # rational_price_gap: 섹터 평균 close/sma20 대비 해당 종목 편차
    if sector_mean_ratio is None:
        # 내부 계산
        _scs: dict[str, list[float]] = {}
        for _t in universe_tickers:
            _td2 = dmp_market_data.get(_t, {})
            _cl2 = _td2.get("ohlcv", {}).get("close")
            _s20_2 = _td2.get("tech_features", {}).get("sma_20")
            _sec2 = sector_map.get(_t, "Unknown")
            if _cl2 is not None and _s20_2 and float(_s20_2) != 0:
                _scs.setdefault(_sec2, []).append(float(_cl2) / float(_s20_2) - 1.0)
        sector_mean_ratio = {
            sec: float(np.mean(vals)) for sec, vals in _scs.items() if vals
        }

    sec = sector_map.get(ticker, "Unknown")
    tick_ratio = (close / float(sma_20) - 1.0) if sma_20 and float(sma_20) != 0 else np.nan
    sec_mean = sector_mean_ratio.get(sec, np.nan)
    if not (isinstance(tick_ratio, float) and np.isnan(tick_ratio)) and not np.isnan(sec_mean):
        rational_price_gap = tick_ratio - sec_mean
    else:
        rational_price_gap = np.nan

    # ── Cross-sectional percentile rank ───────────────────────────────────────
    if cs_raw is None:
        cs_raw = build_cross_sectional_raw(
            dmp_market_data, universe_tickers, close_history, date_idx
        )

    ret_5d_pct = pct_rank(cs_raw.get("ret_5d", {}), ticker)
    ret_20d_pct = pct_rank(cs_raw.get("ret_20d", {}), ticker)
    rsi_14_pct = pct_rank(cs_raw.get("rsi_14", {}), ticker)
    volume_ratio_20_pct = pct_rank(cs_raw.get("volume_ratio_20", {}), ticker)
    sma5_ratio_pct = pct_rank(cs_raw.get("sma5_ratio", {}), ticker)
    sma20_ratio_pct = pct_rank(cs_raw.get("sma20_ratio", {}), ticker)
    sma60_ratio_pct = pct_rank(cs_raw.get("sma60_ratio", {}), ticker)
    return_60d_pct = pct_rank(cs_raw.get("return_60d", {}), ticker)

    # ── M3: overnight/intraday OHLCV micro features ───────────────────────────
    open_val = ohlcv.get("open")
    high_val = ohlcv.get("high")
    low_val = ohlcv.get("low")

    # overnight_gap: (today_open - prev_close) / prev_close
    if prev_close_val is not None and open_val is not None:
        if float(prev_close_val) != 0 and not np.isnan(float(prev_close_val)):
            overnight_gap = (float(open_val) - float(prev_close_val)) / float(prev_close_val)
        else:
            overnight_gap = 0.0
    elif close_history is not None and ticker in close_history and date_idx is not None:
        _prev_idx = date_idx - 1
        if _prev_idx >= 0 and open_val is not None:
            _prev_c = close_history[ticker][_prev_idx]
            if _prev_c and not np.isnan(float(_prev_c)) and float(_prev_c) != 0:
                overnight_gap = (float(open_val) - float(_prev_c)) / float(_prev_c)
            else:
                overnight_gap = 0.0
        else:
            overnight_gap = 0.0
    else:
        overnight_gap = 0.0

    if (high_val is not None and low_val is not None
            and open_val is not None):
        _h = float(high_val)
        _l = float(low_val)
        _o = float(open_val)
        _c = close
        _hl = _h - _l + 1e-8
        intraday_range = (_h - _l) / _c if _c != 0 else 0.0
        upper_shadow_ratio = (_h - max(_o, _c)) / _hl
        lower_shadow_ratio = (min(_o, _c) - _l) / _hl
        body_ratio = abs(_c - _o) / _hl
    else:
        intraday_range = 0.0
        upper_shadow_ratio = 0.0
        lower_shadow_ratio = 0.0
        body_ratio = 0.0

    # ── 매크로 보조 피처 ──────────────────────────────────────────────────────
    vix_proxy = _safe_float(macro.get("vix_proxy"))
    base_rate = _safe_float(macro.get("base_rate"))
    usd_krw = _safe_float(macro.get("usd_krw"))

    return {
        # Manual (9개)
        "return_5d":           float(return_5d) if return_5d is not None and not np.isnan(return_5d) else np.nan,
        "return_20d":          float(return_20d) if return_20d is not None and not np.isnan(return_20d) else np.nan,
        "rsi_14":              float(rsi_14) if rsi_14 is not None and not np.isnan(rsi_14) else np.nan,
        "volume_ratio_20":     float(volume_ratio_20) if volume_ratio_20 is not None and not np.isnan(volume_ratio_20) else np.nan,
        "macd":                float(macd_val) if not (isinstance(macd_val, float) and np.isnan(macd_val)) else np.nan,
        "macd_signal":         float(macd_signal_val) if not (isinstance(macd_signal_val, float) and np.isnan(macd_signal_val)) else np.nan,
        "atr_14":              float(atr_14) if atr_14 is not None and not np.isnan(atr_14) else np.nan,
        "sma5_ratio":          float(sma5_ratio) if sma5_ratio is not None and not np.isnan(sma5_ratio) else np.nan,
        "sma20_ratio":         float(sma20_ratio) if sma20_ratio is not None and not np.isnan(sma20_ratio) else np.nan,
        "sma60_ratio":         float(sma60_ratio),
        # MLF-lite (2개)
        "return_60d":          float(return_60d),
        "period_agreement_score": float(period_agreement_score) if not (isinstance(period_agreement_score, float) and np.isnan(period_agreement_score)) else np.nan,
        # UMI-lite (3개)
        "stock_sync_score":    float(stock_sync_score) if not (isinstance(stock_sync_score, float) and np.isnan(stock_sync_score)) else np.nan,
        "market_synchronism":  float(market_synchronism) if not (isinstance(market_synchronism, float) and np.isnan(market_synchronism)) else np.nan,
        "rational_price_gap":  float(rational_price_gap) if not (isinstance(rational_price_gap, float) and np.isnan(rational_price_gap)) else np.nan,
        # Cross-sectional pct rank (8개)
        "ret_5d_pct":          ret_5d_pct,
        "ret_20d_pct":         ret_20d_pct,
        "rsi_14_pct":          rsi_14_pct,
        "volume_ratio_20_pct": volume_ratio_20_pct,
        "sma5_ratio_pct":      sma5_ratio_pct,
        "sma20_ratio_pct":     sma20_ratio_pct,
        "sma60_ratio_pct":     sma60_ratio_pct,
        "return_60d_pct":      return_60d_pct,
        # OHLCV micro (5개)
        "overnight_gap":       overnight_gap,
        "intraday_range":      intraday_range,
        "upper_shadow_ratio":  upper_shadow_ratio,
        "lower_shadow_ratio":  lower_shadow_ratio,
        "body_ratio":          body_ratio,
        # 매크로 보조 (4개)
        "vix_proxy":           float(vix_proxy),
        "market_breadth":      float(market_breadth),
        "base_rate":           float(base_rate),
        "usd_krw":             float(usd_krw),
    }

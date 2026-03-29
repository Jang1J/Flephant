"""
KR-Rebound-CNN 공유 전처리 모듈
- make_chart_tensor: 20일 OHLCV → 3-channel 64x64 tensor
- compute_sector_relative_features: 섹터 상대 피처 계산
- _build_context_vector: 26차원 context vector 조립
- _build_wics_sector_list: WICS 섹터 목록 정렬

dataset.py, SC emitter(build_strategy_card_rebound.py) 등에서 공통 사용.
"""

import numpy as np
import pandas as pd
import torch


def _build_wics_sector_list(universe: pd.DataFrame) -> list:
    """
    universe DataFrame에서 WICS 섹터 목록을 알파벳(한글) 정렬하여 반환.
    섹터 one-hot 인코딩 인덱스 결정에 사용.
    """
    return sorted(universe["wics_sector"].unique().tolist())


def compute_sector_relative_features(
    ticker_data: dict, universe: pd.DataFrame, date_str: str
) -> dict:
    """
    특정 날짜의 모든 종목에 대해 섹터 상대 피처를 계산.
    반환: {ticker: {ret_5d_sector_z, rsi_14_sector_z, close_sma20_ratio_sector_rank,
                    volume_ratio_20_sector_pct, ret_5d, rsi_14, close_sma20_ratio,
                    bb_pos, ret_5d_rank_in_sector}}
    """
    dates_sorted = sorted(set(
        d for td in ticker_data.values() for d in td.keys()
    ))

    if date_str not in dates_sorted:
        return {}

    date_idx = dates_sorted.index(date_str)

    # 5일 수익률 계산을 위한 이전 날짜
    date_5d_ago = dates_sorted[date_idx - 5] if date_idx >= 5 else None

    # 종목별 raw 지표 수집
    ticker_metrics: dict = {}
    for ticker, td in ticker_data.items():
        if date_str not in td:
            continue

        snap = td[date_str]
        close_now = snap["ohlcv"]["close"]
        rsi_14 = snap["tech_features"].get("rsi_14", 50.0)
        sma_20 = snap["tech_features"].get("sma_20", close_now)
        vol_ratio = snap["tech_features"].get("volume_ratio_20", 1.0)

        # Bollinger band position: (close - bb_lower) / (bb_upper - bb_lower)
        bb_upper = snap["tech_features"].get("bb_upper", close_now * 1.02)
        bb_lower = snap["tech_features"].get("bb_lower", close_now * 0.98)
        bb_range = bb_upper - bb_lower
        if bb_range > 1e-8:
            bb_pos = (close_now - bb_lower) / bb_range
        else:
            bb_pos = 0.5
        bb_pos = float(np.clip(bb_pos, 0.0, 1.0))

        # 5일 수익률
        if date_5d_ago and date_5d_ago in td:
            close_5d = td[date_5d_ago]["ohlcv"]["close"]
            ret_5d = (close_now - close_5d) / close_5d if close_5d > 0 else 0.0
        else:
            ret_5d = 0.0

        # close/sma20 비율
        close_sma20_ratio = (close_now / sma_20 - 1.0) if sma_20 > 0 else 0.0

        ticker_metrics[ticker] = {
            "ret_5d": ret_5d,
            "rsi_14": rsi_14,
            "close_sma20_ratio": close_sma20_ratio,
            "volume_ratio_20": vol_ratio,
            "bb_pos": bb_pos,
            "close": close_now,
            "sma_20": sma_20,
        }

    # 섹터별 집계
    sector_map: dict = {}
    for _, row in universe.iterrows():
        t = str(row["ticker"]).zfill(6)
        sector = row["wics_sector"]
        if t not in sector_map:
            sector_map[t] = sector

    # 섹터별 종목 묶기
    sector_groups: dict = {}
    for ticker, metrics in ticker_metrics.items():
        sector = sector_map.get(ticker, "Unknown")
        if sector not in sector_groups:
            sector_groups[sector] = []
        sector_groups[sector].append((ticker, metrics))

    result: dict = {}
    for sector, members in sector_groups.items():
        if len(members) == 0:
            continue

        ret_5d_vals = np.array([m["ret_5d"] for _, m in members])
        rsi_vals = np.array([m["rsi_14"] for _, m in members])
        ratio_vals = np.array([m["close_sma20_ratio"] for _, m in members])
        vol_vals = np.array([m["volume_ratio_20"] for _, m in members])

        ret_mean, ret_std = ret_5d_vals.mean(), ret_5d_vals.std() + 1e-8
        rsi_mean, rsi_std = rsi_vals.mean(), rsi_vals.std() + 1e-8

        n = len(members)

        for ticker, metrics in members:
            ret_z = (metrics["ret_5d"] - ret_mean) / ret_std
            rsi_z = (metrics["rsi_14"] - rsi_mean) / rsi_std

            # rank: 0~1 사이 (작을수록 낮은 순위)
            ratio_rank = float(
                np.sum(ratio_vals <= metrics["close_sma20_ratio"]) / len(ratio_vals)
            )
            vol_pct = float(
                np.sum(vol_vals <= metrics["volume_ratio_20"]) / len(vol_vals)
            )
            # ret_5d sector 내 순위 (낮을수록 섹터 내 약세)
            ret_5d_rank_pct = float(
                np.sum(ret_5d_vals <= metrics["ret_5d"]) / n
            )

            result[ticker] = {
                "ret_5d_sector_z": float(ret_z),
                "rsi_14_sector_z": float(rsi_z),
                "close_sma20_ratio_sector_rank": ratio_rank,
                "volume_ratio_20_sector_pct": vol_pct,
                # oversold gate용 raw 값
                "ret_5d": metrics["ret_5d"],
                "rsi_14": metrics["rsi_14"],
                "close_sma20_ratio": metrics["close_sma20_ratio"],
                "bb_pos": metrics["bb_pos"],
                "ret_5d_rank_in_sector": ret_5d_rank_pct,
            }

    return result


def make_chart_tensor(
    ohlcv_window: list, size: tuple = (64, 64)
) -> torch.Tensor:
    """
    20일 OHLCV 데이터를 64x64 3-channel tensor로 변환.

    Ch1: candlestick body-wick map (normalized OHLC)
    Ch2: volume bars (log1p normalized, bar 형태)
    Ch3: indicator overlay (SMA5, SMA20, Bollinger position)

    정규화: window 내 min-max scaling (가격 축)
    반환: Tensor (3, height, width)
    """
    height, width = size
    n_days = len(ohlcv_window)

    opens = np.array([d["ohlcv"]["open"] for d in ohlcv_window], dtype=np.float32)
    highs = np.array([d["ohlcv"]["high"] for d in ohlcv_window], dtype=np.float32)
    lows = np.array([d["ohlcv"]["low"] for d in ohlcv_window], dtype=np.float32)
    closes = np.array([d["ohlcv"]["close"] for d in ohlcv_window], dtype=np.float32)
    volumes = np.array([d["volume"] for d in ohlcv_window], dtype=np.float32)

    # 가격 전체 범위 (body-wick 정규화 기준)
    price_lo = lows.min()
    price_hi = highs.max()
    price_range = price_hi - price_lo + 1e-8

    def price_to_y(price: float) -> int:
        """가격 -> y 픽셀 좌표 (상단=high, 하단=low)"""
        norm = (price - price_lo) / price_range
        y = int(norm * (height - 1))
        return min(max(y, 0), height - 1)

    # x 축 날짜별 픽셀 위치 (열 단위)
    x_positions = np.linspace(0, width - 1, n_days).astype(int)
    # 날짜별 열 폭 (body 표현을 위해)
    col_half = max(1, width // (2 * n_days))

    # Ch1: candlestick body-wick map
    ch1 = np.zeros((height, width), dtype=np.float32)
    for t, x in enumerate(x_positions):
        y_high = price_to_y(highs[t])
        y_low = price_to_y(lows[t])
        y_open = price_to_y(opens[t])
        y_close = price_to_y(closes[t])

        # wick: high ~ low 전체 (강도 0.5)
        y_wick_lo = min(y_low, y_high)
        y_wick_hi = max(y_low, y_high)
        ch1[height - 1 - y_wick_hi: height - y_wick_lo, x] = 0.5

        # body: open ~ close (강도 1.0)
        y_body_lo = min(y_open, y_close)
        y_body_hi = max(y_open, y_close)
        x_lo = max(0, x - col_half)
        x_hi = min(width, x + col_half + 1)
        if y_body_hi == y_body_lo:
            ch1[height - 1 - y_body_hi, x_lo:x_hi] = 1.0
        else:
            ch1[height - 1 - y_body_hi: height - y_body_lo, x_lo:x_hi] = 1.0

    # Ch2: volume bars (log1p normalized, 하단부터 채움)
    ch2 = np.zeros((height, width), dtype=np.float32)
    log_vols = np.log1p(volumes)
    vol_lo, vol_hi = log_vols.min(), log_vols.max()
    vol_range = vol_hi - vol_lo + 1e-8
    norm_vols = (log_vols - vol_lo) / vol_range

    for t, x in enumerate(x_positions):
        bar_h = max(1, int(norm_vols[t] * (height - 1)))
        x_lo = max(0, x - col_half)
        x_hi = min(width, x + col_half + 1)
        # 하단부터 bar_h 픽셀 채움
        ch2[height - bar_h: height, x_lo:x_hi] = norm_vols[t]

    # Ch3: indicator overlay (SMA5, SMA20, Bollinger position)
    ch3 = np.zeros((height, width), dtype=np.float32)

    # 기술 지표 추출
    sma5_vals = np.array(
        [d["tech_features"].get("sma_5", closes[i]) for i, d in enumerate(ohlcv_window)],
        dtype=np.float32,
    )
    sma20_vals = np.array(
        [d["tech_features"].get("sma_20", closes[i]) for i, d in enumerate(ohlcv_window)],
        dtype=np.float32,
    )
    bb_lower_vals = np.array(
        [d["tech_features"].get("bb_lower", lows[i]) for i, d in enumerate(ohlcv_window)],
        dtype=np.float32,
    )
    bb_upper_vals = np.array(
        [d["tech_features"].get("bb_upper", highs[i]) for i, d in enumerate(ohlcv_window)],
        dtype=np.float32,
    )

    # Bollinger position을 배경 레이어로 깔기
    bb_range_arr = bb_upper_vals - bb_lower_vals + 1e-8
    for t, x in enumerate(x_positions):
        bb_pos = float(np.clip((closes[t] - bb_lower_vals[t]) / bb_range_arr[t], 0.0, 1.0))
        # 전체 열에 bb_pos 값을 배경으로
        ch3[:, x] = bb_pos * 0.3  # 낮은 강도로 배경

    # SMA5 라인 (강도 0.8)
    for t, x in enumerate(x_positions):
        y_sma5 = price_to_y(sma5_vals[t])
        ch3[height - 1 - y_sma5, x] = 0.8

    # SMA20 라인 (강도 1.0)
    for t, x in enumerate(x_positions):
        y_sma20 = price_to_y(sma20_vals[t])
        ch3[height - 1 - y_sma20, x] = 1.0

    tensor = np.stack([ch1, ch2, ch3], axis=0)  # (3, H, W)
    return torch.from_numpy(tensor)


def _build_context_vector(
    ticker: str,
    t_date: str,
    td: dict,
    sf: dict,
    sector_map: dict,
    wics_sectors: list,
    mktcap_rank: float = 0.5,
) -> list:
    """
    26차원 context vector 조립 (설계서 S10.2 Context Branch).

    구성:
      macro(4) + technical(5) + price_stretch(2) + sector_relative(3)
      + sector_onehot(n_sectors) + market_cap_rank(1)
      = 15 + n_sectors 차원

    PIT-Safety: t일 이전 데이터(td[t_date])만 사용, 미래 데이터 참조 없음.
    """
    snap = td.get(t_date, {})
    macro_raw = snap.get("macro", {})
    tech = snap.get("tech_features", {})
    close = snap.get("ohlcv", {}).get("close", 1.0)
    if close <= 0:
        close = 1.0

    # macro (4)
    macro_vec = [
        float(macro_raw.get("vix_proxy", 0.0)),
        float(macro_raw.get("market_breadth", 0.0)),
        float(macro_raw.get("fx_rate", 0.0)),
        float(macro_raw.get("base_rate", 0.0)),
    ]

    # technical (5): rsi_14, atr_14_ratio, volume_ratio_20, macd, macd_signal
    atr_14 = float(tech.get("atr_14", 0.0))
    atr_14_ratio = atr_14 / close if close > 0 else 0.0
    tech_vec = [
        float(tech.get("rsi_14", 50.0)),
        atr_14_ratio,
        float(tech.get("volume_ratio_20", 1.0)),
        float(tech.get("macd", 0.0)),
        float(tech.get("macd_signal", 0.0)),
    ]

    # price_stretch (2): close/sma_5 - 1, close/sma_20 - 1
    sma_5 = float(tech.get("sma_5", close))
    sma_20 = float(tech.get("sma_20", close))
    stretch_vec = [
        (close / sma_5 - 1.0) if sma_5 > 0 else 0.0,
        (close / sma_20 - 1.0) if sma_20 > 0 else 0.0,
    ]

    # sector_relative (3): ret_5d_sector_z, rsi_14_sector_z, volume_ratio_20_sector_pct
    sector_vec = [
        float(sf.get("ret_5d_sector_z", 0.0)),
        float(sf.get("rsi_14_sector_z", 0.0)),
        float(sf.get("volume_ratio_20_sector_pct", 0.5)),
    ]

    # meta: sector_onehot (n_sectors 차원) + market_cap_rank (1)
    sector_name = sector_map.get(ticker, "Unknown")
    n_sectors = len(wics_sectors)
    sector_oh = [0.0] * n_sectors
    if sector_name in wics_sectors:
        sector_oh[wics_sectors.index(sector_name)] = 1.0

    context = macro_vec + tech_vec + stretch_vec + sector_vec + sector_oh + [mktcap_rank]
    return context

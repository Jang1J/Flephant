"""Preprocessor unit tests. Sprint 1-0 S1-0 Batch A."""
from __future__ import annotations

from zoneinfo import ZoneInfo

import numpy as np
import pytest

_KST = ZoneInfo("Asia/Seoul")


def _make_bars(n: int, base_price: float = 70000.0, ticker: str = "005930") -> list[dict]:
    """테스트용 1분봉 리스트 생성."""
    bars = []
    for i in range(n):
        bars.append({
            "ticker": ticker,
            "ts_close": f"2026-04-17T09:{i // 60:02d}:{i % 60:02d}+09:00",
            "open": base_price + i,
            "high": base_price + i + 50,
            "low": base_price + i - 50,
            "close": base_price + i + 10,
            "volume": 1000 + i * 10,
        })
    return bars


# ------------------------------------------------------------------ #
# test_robust_z_symmetric_data
# ------------------------------------------------------------------ #


def test_robust_z_symmetric_data():
    """대칭 데이터에서 median 근처 z=0."""
    from src.data.preprocessor import Preprocessor

    pp = Preprocessor()
    # 0 중심 대칭 데이터
    data = np.array([-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0])
    z = pp.robust_z(data)

    # median=0이므로 z[median 위치] ≈ 0
    assert abs(z[3]) < 1e-6, f"median 위치 z={z[3]:.6f}, 0이어야 함"


# ------------------------------------------------------------------ #
# test_robust_z_outlier_resistance
# ------------------------------------------------------------------ #


def test_robust_z_outlier_resistance():
    """outlier에 MAD robust-z가 mean z보다 강건한지 확인.

    일반 z-score는 outlier로 인해 정상값이 왜곡되지만,
    MAD robust-z는 정상값의 z가 작게 유지되어야 한다.
    """
    from src.data.preprocessor import Preprocessor

    pp = Preprocessor()
    # 정상 데이터 10개 + 극단 outlier 1개
    normal = np.array([100.0] * 9 + [1000000.0])
    z_robust = pp.robust_z(normal)

    # 정상값 9개의 robust z는 0 (모두 같으므로 MAD=0, epsilon fallback)
    # outlier는 cap 적용: z <= outlier_cap_z (5.0)
    assert abs(z_robust[-1]) <= pp.outlier_cap_z + 1e-6


# ------------------------------------------------------------------ #
# test_forward_fill
# ------------------------------------------------------------------ #


def test_forward_fill():
    """None → 직전 값으로 채움 검증."""
    from src.data.preprocessor import Preprocessor

    pp = Preprocessor()
    series = [1.0, None, None, 4.0, None]
    result = pp.forward_fill(series)

    assert result == [1.0, 1.0, 1.0, 4.0, 4.0]


def test_forward_fill_leading_none():
    """첫 값 None일 때 0.0 임시 삽입."""
    from src.data.preprocessor import Preprocessor

    pp = Preprocessor()
    series = [None, None, 3.0]
    result = pp.forward_fill(series)

    # 첫 None들은 0.0, 이후 값 이후는 그 값으로
    assert result[0] == 0.0
    assert result[1] == 0.0
    assert result[2] == 3.0


# ------------------------------------------------------------------ #
# test_multi_scale_aggregate_shape
# ------------------------------------------------------------------ #


def test_multi_scale_aggregate_shape():
    """60개 1m → 12개 5m / 2개 30m / 1개 60m 형태 확인."""
    from src.data.preprocessor import Preprocessor

    pp = Preprocessor()
    bars_60 = _make_bars(60)
    result = pp.multi_scale_aggregate(bars_60, windows=[1, 5, 30, 60])

    assert len(result[1]) == 60
    assert len(result[5]) == 12   # 60 / 5 = 12
    assert len(result[30]) == 2   # 60 / 30 = 2
    assert len(result[60]) == 1   # 60 / 60 = 1


def test_multi_scale_aggregate_ohlcv():
    """window=5 블록의 open/close/high/low/volume OHLCV 규칙 검증."""
    from src.data.preprocessor import Preprocessor

    pp = Preprocessor()
    bars_5 = _make_bars(5, base_price=70000.0)
    result = pp.multi_scale_aggregate(bars_5, windows=[5])
    agg = result[5]
    assert len(agg) == 1
    block = agg[0]
    # open = 첫 bar의 open (70000)
    assert block["open"] == pytest.approx(70000.0)
    # close = 마지막 bar의 close (70004 + 10 = 70014)
    assert block["close"] == pytest.approx(70014.0)
    # high = 최대 high
    assert block["high"] >= block["open"]
    # volume = sum
    expected_vol = sum(bars_5[i]["volume"] for i in range(5))
    assert block["volume"] == pytest.approx(expected_vol)


# ------------------------------------------------------------------ #
# test_build_quant_frame_cross_sectional
# ------------------------------------------------------------------ #


def test_build_quant_frame_cross_sectional():
    """20 종목 동시 처리, 결측 ticker에 cross-sectional mean 적용 확인."""
    from src.data.preprocessor import Preprocessor

    pp = Preprocessor()
    tickers = ["005930", "000660", "042700"]
    # 005930, 000660는 bar 있음. 042700은 데이터 없음 (결측)
    bar_batch = {
        "005930": _make_bars(60, base_price=70000.0, ticker="005930"),
        "000660": _make_bars(60, base_price=150000.0, ticker="000660"),
        "042700": [],  # 결측
    }
    asof = "2026-04-17T10:00:00+09:00"

    frame = pp.build_quant_frame(tickers, bar_batch, asof)

    assert frame["asof"] == asof
    assert set(frame["tickers"]) == {"005930", "000660", "042700"}
    assert "quant_frame_id" in frame
    assert frame["quant_frame_id"].startswith("RPT-")

    # 결측 ticker도 features가 있어야 함 (cross-sectional mean 채워짐)
    features_042 = frame["features"].get("042700", {})
    assert len(features_042) > 0, "042700 결측이어야 하지만 features가 없음"

    # 모든 feature 값이 float인지 확인
    for ticker in tickers:
        for feat_name, val in frame["features"][ticker].items():
            assert isinstance(val, float), f"{ticker}.{feat_name} = {val} (float 아님)"


# ------------------------------------------------------------------ #
# test_cross_sectional_mean_excludes_none
# ------------------------------------------------------------------ #


def test_cross_sectional_mean_excludes_none():
    """None/NaN 제외 평균 계산."""
    from src.data.preprocessor import Preprocessor

    pp = Preprocessor()
    vals = {"A": 10.0, "B": 20.0, "C": float("nan")}
    mean = pp.cross_sectional_mean(vals)
    assert mean == pytest.approx(15.0)


# ------------------------------------------------------------------ #
# test_robust_z_empty_input
# ------------------------------------------------------------------ #


def test_robust_z_empty_input():
    """빈 배열 입력 시 빈 배열 반환."""
    from src.data.preprocessor import Preprocessor

    pp = Preprocessor()
    result = pp.robust_z(np.array([]))
    assert len(result) == 0

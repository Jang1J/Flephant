"""compute_recency_weights + make_lgbm_dataset sample_weight test.

정책 검증:
- train fold만 weighted, validation은 unweighted 유지.
- primary grid (off + 15/30/45/60/90/120) + sensitivity grid (180/365) 처리.
- candidate evidence only (champion/active 승격 X — 호출자 책임).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.models.ranking_loss import compute_recency_weights, make_lgbm_dataset


def _panel(dates: list[str], tickers: list[str]) -> pd.DataFrame:
    """MultiIndex (ticker, ts_close) panel 생성. relevance 0, feat 1.0 default."""
    rows = []
    for d in dates:
        for t in tickers:
            rows.append({
                "ticker": t,
                "ts_close": d,
                "relevance": 0,
                "feat": 1.0,
            })
    return pd.DataFrame(rows).set_index(["ticker", "ts_close"]).sort_index()


def test_half_life_none_returns_none():
    """primary grid 첫 항목 (off) 처리: half_life=None → None (unweighted)."""
    panel = _panel(["2026-06-01", "2026-06-02"], ["005930"])
    assert compute_recency_weights(panel, half_life_days=None) is None


def test_half_life_zero_returns_none():
    """half_life_days=0 → None (가중치 없이)."""
    panel = _panel(["2026-06-01"], ["005930"])
    assert compute_recency_weights(panel, half_life_days=0) is None


def test_half_life_negative_returns_none():
    """half_life_days<0 → None (잘못된 입력은 fail-closed)."""
    panel = _panel(["2026-06-01"], ["005930"])
    assert compute_recency_weights(panel, half_life_days=-30) is None


def test_latest_row_weight_is_one():
    """가장 최근 row의 weight가 정확히 1.0."""
    panel = _panel(["2026-06-01", "2026-06-02"], ["005930"])
    weights = compute_recency_weights(panel, half_life_days=30)
    assert weights is not None
    assert weights[-1] == pytest.approx(1.0, abs=1e-9)


def test_half_life_30_days_decay():
    """half_life=30일에서 30일 차이 row weight=0.5 (수학적 정확)."""
    panel = _panel(["2026-05-03", "2026-06-02"], ["005930"])  # 정확히 30일 차이
    weights = compute_recency_weights(panel, half_life_days=30)
    assert weights is not None
    assert weights[0] == pytest.approx(0.5, abs=1e-6)
    assert weights[-1] == pytest.approx(1.0, abs=1e-6)


def test_weight_length_matches_panel():
    """weights 길이가 panel 길이와 일치."""
    panel = _panel(["2026-06-01", "2026-06-02"], ["005930", "000660"])
    weights = compute_recency_weights(panel, half_life_days=60)
    assert weights is not None
    assert len(weights) == len(panel) == 4


@pytest.mark.parametrize("half_life", [15, 30, 45, 60, 90, 120])
def test_primary_grid_values_accepted(half_life):
    """primary grid 6개 weighted 값 모두 처리 가능 + 가중치 범위 (0, 1]."""
    panel = _panel(["2026-05-01", "2026-06-01"], ["005930"])
    weights = compute_recency_weights(panel, half_life_days=half_life)
    assert weights is not None
    assert all(0.0 < w <= 1.0 for w in weights)


@pytest.mark.parametrize("half_life", [180, 365])
def test_sensitivity_grid_values_accepted(half_life):
    """sensitivity grid (180/365) 처리 가능."""
    panel = _panel(["2026-05-01", "2026-06-01"], ["005930"])
    weights = compute_recency_weights(panel, half_life_days=half_life)
    assert weights is not None


def test_ts_col_from_index_works():
    """MultiIndex의 ts_close level에서 정상 추출."""
    panel = _panel(["2026-06-01", "2026-06-02"], ["005930"])
    assert "ts_close" in panel.index.names
    weights = compute_recency_weights(panel, half_life_days=30)
    assert weights is not None


def test_ts_col_from_column_works():
    """ts_close가 일반 column (MultiIndex 아님)으로 있는 panel도 처리.

    DatetimeIndex 강제 변환으로 Series/Index 양쪽 안전 처리 회귀 보장.
    """
    panel = pd.DataFrame([
        {"ticker": "005930", "ts_close": "2026-05-03", "relevance": 0, "feat": 1.0},
        {"ticker": "005930", "ts_close": "2026-06-02", "relevance": 1, "feat": 1.0},
    ])
    weights = compute_recency_weights(panel, half_life_days=30)
    assert weights is not None
    assert weights[-1] == pytest.approx(1.0, abs=1e-6)
    assert weights[0] == pytest.approx(0.5, abs=1e-6)


def test_ts_col_missing_raises():
    """ts_col이 panel column 또는 index에 없으면 KeyError."""
    panel = _panel(["2026-06-01"], ["005930"])
    with pytest.raises(KeyError, match="ts_col"):
        compute_recency_weights(panel, half_life_days=30, ts_col="nonexistent")


def test_make_lgbm_dataset_with_weight():
    """make_lgbm_dataset이 sample_weight 인자 정상 처리."""
    panel = _panel(["2026-06-01", "2026-06-02"], ["005930", "000660"])
    weights = np.array([1.0, 0.5, 1.0, 0.5])
    ds = make_lgbm_dataset(
        panel,
        feature_cols=["feat"],
        sample_weight=weights,
    )
    assert ds is not None
    # lgb.Dataset weight 직접 접근 (free_raw_data=False라 access 가능)
    assert ds.weight is not None
    np.testing.assert_allclose(ds.weight, weights)


def test_make_lgbm_dataset_without_weight():
    """make_lgbm_dataset이 sample_weight=None (default)으로 unweighted Dataset 생성."""
    panel = _panel(["2026-06-01", "2026-06-02"], ["005930", "000660"])
    ds = make_lgbm_dataset(panel, feature_cols=["feat"])
    assert ds is not None
    assert ds.weight is None


def test_sample_weight_length_mismatch_raises():
    """sample_weight 길이가 panel과 다르면 ValueError."""
    panel = _panel(["2026-06-01", "2026-06-02"], ["005930", "000660"])  # 4 rows
    wrong_weights = np.array([1.0, 1.0])  # 2개만
    with pytest.raises(ValueError, match="sample_weight 길이 mismatch"):
        make_lgbm_dataset(
            panel,
            feature_cols=["feat"],
            sample_weight=wrong_weights,
        )


def test_end_to_end_recency_then_dataset():
    """compute_recency_weights 출력을 make_lgbm_dataset에 그대로 전달 가능."""
    panel = _panel(
        ["2026-05-03", "2026-05-18", "2026-06-02"],
        ["005930", "000660"],
    )
    weights = compute_recency_weights(panel, half_life_days=30)
    ds = make_lgbm_dataset(
        panel,
        feature_cols=["feat"],
        sample_weight=weights,
    )
    assert ds.weight is not None
    assert len(ds.weight) == len(panel) == 6


def test_returns_numpy_ndarray_not_pandas_index():
    """반환 타입이 numpy.ndarray 1D (lgb.Dataset 호환). pandas Index가 아님."""
    panel = _panel(["2026-06-01", "2026-06-02"], ["005930"])
    weights = compute_recency_weights(panel, half_life_days=30)
    assert isinstance(weights, np.ndarray)
    assert weights.ndim == 1


@pytest.mark.parametrize(
    "bad_input",
    ["false", "", "abc", "null"],
)
def test_string_input_returns_none(bad_input):
    """문자열 입력(yaml mis-cast 등)은 safe_int로 0 fallback → None 반환.

    int(str) 직접 호출이면 ValueError로 학습 중단되던 영역 가드.
    """
    panel = _panel(["2026-06-01"], ["005930"])
    assert compute_recency_weights(panel, half_life_days=bad_input) is None


def test_float_with_decimals_truncated_then_processed():
    """float 입력 (예: 30.5)은 safe_int로 30 정수화 후 정상 처리."""
    panel = _panel(["2026-05-03", "2026-06-02"], ["005930"])
    weights = compute_recency_weights(panel, half_life_days=30.5)
    # 30.5 → 30으로 정수화 → 30일 차이 row weight≈0.5
    assert weights is not None
    assert weights[0] == pytest.approx(0.5, abs=1e-6)

"""S1-0 Batch B metrics.py unit tests."""
from __future__ import annotations

import numpy as np
import pytest

from src.models.metrics import (
    MetricsBundle,
    annualized_return,
    ic,
    icir,
    information_ratio,
    max_drawdown,
    normalize_performance_vector,
    panel_ic,
    panel_rank_ic,
    rank_ic,
    rank_transform,
    sharpe_ratio,
)


# ====================================================================== #
# ic / rank_ic
# ====================================================================== #


def test_ic_perfect_correlation() -> None:
    a = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    b = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    assert ic(a, b) == pytest.approx(1.0, abs=1e-6)


def test_ic_negative_correlation() -> None:
    a = np.array([1.0, 2.0, 3.0, 4.0])
    b = np.array([4.0, 3.0, 2.0, 1.0])
    assert ic(a, b) == pytest.approx(-1.0, abs=1e-6)


def test_ic_zero_variance() -> None:
    a = np.array([1.0, 1.0, 1.0])
    b = np.array([1.0, 2.0, 3.0])
    assert ic(a, b) == 0.0


def test_ic_empty() -> None:
    assert ic(np.array([]), np.array([])) == 0.0


def test_rank_ic_monotonic() -> None:
    a = np.array([10.0, 20.0, 30.0, 40.0])
    b = np.array([100.0, 200.0, 300.0, 500.0])
    assert rank_ic(a, b) == pytest.approx(1.0, abs=1e-6)


def test_rank_ic_reversed() -> None:
    a = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    b = np.array([5.0, 4.0, 3.0, 2.0, 1.0])
    assert rank_ic(a, b) == pytest.approx(-1.0, abs=1e-6)


# ====================================================================== #
# panel_ic / panel_rank_ic / icir
# ====================================================================== #


def test_panel_ic_average() -> None:
    y_true = {
        "t1": np.array([1.0, 2.0, 3.0, 4.0]),
        "t2": np.array([4.0, 3.0, 2.0, 1.0]),
    }
    y_pred = {
        "t1": np.array([1.0, 2.0, 3.0, 4.0]),
        "t2": np.array([1.0, 2.0, 3.0, 4.0]),
    }
    arr = panel_ic(y_true, y_pred)
    assert len(arr) == 2
    assert float(np.mean(arr)) == pytest.approx(0.0, abs=1e-6)


def test_panel_rank_ic_average() -> None:
    y_true = {
        "t1": np.array([1.0, 2.0, 3.0]),
        "t2": np.array([3.0, 1.0, 2.0]),
    }
    y_pred = {
        "t1": np.array([1.0, 2.0, 3.0]),
        "t2": np.array([3.0, 1.0, 2.0]),
    }
    arr = panel_rank_ic(y_true, y_pred)
    assert float(np.mean(arr)) == pytest.approx(1.0, abs=1e-6)


def test_icir_constant_series() -> None:
    arr = np.array([0.05, 0.05, 0.05, 0.05])
    assert icir(arr) == 0.0


def test_icir_normal() -> None:
    arr = np.array([0.05, 0.10, 0.15, 0.20])
    v = icir(arr)
    assert v > 0.0
    # ddof=1 표본 표준편차 (2026-04-20 Phase 2 정렬)
    assert v == pytest.approx(0.125 / float(np.std(arr, ddof=1)), abs=1e-6)


# ====================================================================== #
# annualized_return
# ====================================================================== #


def test_annualized_return_zero() -> None:
    r = np.zeros(252)
    assert annualized_return(r, annualization_factor=252) == pytest.approx(0.0, abs=1e-8)


def test_annualized_return_positive() -> None:
    r = np.ones(252) * 0.001
    v = annualized_return(r, annualization_factor=252)
    assert v == pytest.approx(1.001 ** 252 - 1, abs=1e-4)


def test_annualized_return_total_loss() -> None:
    r = np.array([-1.0, 0.0, 0.0])
    assert annualized_return(r) == -1.0


# ====================================================================== #
# IR / SR
# ====================================================================== #


def test_information_ratio_zero_std() -> None:
    r = np.ones(100) * 0.001
    assert information_ratio(r, min_std=1e-8) == 0.0


def test_information_ratio_vs_sharpe_no_benchmark() -> None:
    rng = np.random.default_rng(123)
    r = rng.normal(0.001, 0.01, size=250)
    v_ir = information_ratio(r, benchmark=None, annualization_factor=252)
    v_sr = sharpe_ratio(r, annualization_factor=252)
    assert v_ir == pytest.approx(v_sr, abs=1e-10)


def test_information_ratio_benchmark_size_mismatch() -> None:
    r = np.ones(10) * 0.001
    b = np.ones(5) * 0.0005
    with pytest.raises(ValueError):
        information_ratio(r, benchmark=b)


# ====================================================================== #
# MDD
# ====================================================================== #


def test_max_drawdown_no_drawdown() -> None:
    r = np.array([0.01, 0.01, 0.01, 0.01])
    assert max_drawdown(r, daily_input=True) == pytest.approx(0.0, abs=1e-6)


def test_max_drawdown_simple() -> None:
    r = np.array([0.1, -0.2, 1 / 0.88 - 1])
    v = max_drawdown(r, daily_input=True)
    assert v == pytest.approx(-0.2, abs=1e-4)


def test_max_drawdown_negative_sign() -> None:
    r = np.array([0.05, -0.10, 0.02])
    v = max_drawdown(r, daily_input=True)
    assert v <= 0.0


def test_max_drawdown_cum_input() -> None:
    cum = np.array([1.0, 1.2, 0.9, 1.1])
    v = max_drawdown(cum, daily_input=False)
    assert v == pytest.approx(-0.25, abs=1e-6)


# ====================================================================== #
# MetricsBundle (C12 7종 compute)
# ====================================================================== #


def test_metrics_bundle_compute_all_fields() -> None:
    y_true = {
        "t1": np.array([0.01, 0.02, 0.03, -0.01]),
        "t2": np.array([0.005, 0.015, -0.005, 0.025]),
    }
    y_pred = {
        "t1": np.array([0.008, 0.018, 0.025, -0.008]),
        "t2": np.array([0.004, 0.012, -0.003, 0.022]),
    }
    daily_pnl = np.array([0.001, 0.002, -0.0015, 0.0025, 0.0])
    bundle = MetricsBundle.compute(y_true, y_pred, daily_pnl)
    d = bundle.to_dict()
    for k in ("ic", "icir", "rank_ic", "arr", "ir", "mdd", "sr"):
        assert k in d, f"missing {k}"
    assert bundle.mdd <= 0.0


def test_metrics_bundle_regime_breakdown() -> None:
    y_true = {"t1": np.array([0.01, 0.02, 0.03])}
    y_pred = {"t1": np.array([0.01, 0.02, 0.03])}
    daily_pnl = np.array([0.001, 0.002, 0.003])
    bundle = MetricsBundle.compute(y_true, y_pred, daily_pnl)

    regime_pnl = {
        "bull": np.array([0.01, 0.02, 0.005]),
        "bear": np.array([-0.02, -0.01, -0.005]),
    }
    out = bundle.regime_breakdown_fill(regime_pnl)
    assert len(out) == 2
    regimes = {r["regime"] for r in out}
    assert regimes == {"bull", "bear"}
    for r in out:
        assert "sharpe" in r
        assert "mdd" in r
        assert "n_days" in r


def test_metrics_bundle_regime_breakdown_all_four_labels() -> None:
    """C13 regime_labels 4종 (bull/bear/sideways/volatile) 전수 커버 (Part C C-3)."""
    y_true = {"t1": np.array([0.01, 0.02, 0.03])}
    y_pred = {"t1": np.array([0.01, 0.02, 0.03])}
    daily_pnl = np.array([0.001, 0.002, 0.003])
    bundle = MetricsBundle.compute(y_true, y_pred, daily_pnl)

    regime_pnl = {
        "bull": np.array([0.015, 0.02, 0.01, 0.018]),
        "bear": np.array([-0.02, -0.015, -0.01]),
        "sideways": np.array([0.001, -0.001, 0.002, -0.0005]),
        "volatile": np.array([0.03, -0.025, 0.02, -0.018, 0.015]),
    }
    out = bundle.regime_breakdown_fill(regime_pnl)
    assert len(out) == 4
    regimes = {r["regime"] for r in out}
    assert regimes == {"bull", "bear", "sideways", "volatile"}
    # 각 regime의 n_days 입력 크기와 일치
    n_days_map = {r["regime"]: r["n_days"] for r in out}
    assert n_days_map["bull"] == 4
    assert n_days_map["bear"] == 3
    assert n_days_map["sideways"] == 4
    assert n_days_map["volatile"] == 5
    # bear MDD는 음수 (전부 손실)
    bear = next(r for r in out if r["regime"] == "bear")
    assert bear["mdd"] <= 0.0


# ====================================================================== #
# rank_transform (public helper)
# ====================================================================== #


def test_rank_transform_range_0_to_1() -> None:
    """rank_transform 반환값은 항상 (0, 1] 범위."""
    rng = np.random.default_rng(42)
    x = rng.normal(size=50)
    out = rank_transform(x)
    assert out.shape == (50,)
    # min > 0 (최솟값은 1/n), max == 1.0
    assert float(out.min()) > 0.0
    assert float(out.max()) == pytest.approx(1.0, abs=1e-9)


def test_rank_transform_ties_average() -> None:
    """동점 처리: average method로 동일한 rank 부여."""
    x = np.array([1.0, 1.0, 2.0])  # 1.0이 2개 → rank 평균 = (1+2)/2 = 1.5
    out = rank_transform(x, method="average")
    # rank = [1.5, 1.5, 3.0] → / 3 = [0.5, 0.5, 1.0]
    assert out[0] == pytest.approx(0.5, abs=1e-9)
    assert out[1] == pytest.approx(0.5, abs=1e-9)
    assert out[2] == pytest.approx(1.0, abs=1e-9)


def test_rank_transform_length_one() -> None:
    """길이 1 배열 → 단일 값 반환 (1/1 = 1.0)."""
    result = rank_transform(np.array([42.0]))
    assert result.shape == (1,)
    assert np.isclose(result[0], 1.0), f"len=1 → rank=1/1=1.0, got {result[0]}"


def test_rank_transform_all_ties() -> None:
    """모든 값 동일 → tied average 반환 (전부 동일 값)."""
    x = np.array([5.0, 5.0, 5.0, 5.0])
    result = rank_transform(x)
    assert result.shape == (4,)
    assert np.allclose(result, result[0]), f"all ties → same value, got {result}"


def test_rank_transform_empty_array() -> None:
    """빈 배열 → 빈 배열 반환 (에러 없음)."""
    result = rank_transform(np.array([], dtype=float))
    assert result.shape == (0,)


# ====================================================================== #
# MetricsBundle.to_performance_vector()
# ====================================================================== #


def _make_bundle(
    ic_val: float = 0.05,
    icir_val: float = 1.2,
    arr_val: float = 0.15,
    ir_val: float = 1.1,
    mdd_val: float = -0.08,
    sr_val: float = 1.3,
    rank_ic_val: float = 0.06,
) -> MetricsBundle:
    return MetricsBundle(
        ic=ic_val,
        icir=icir_val,
        rank_ic=rank_ic_val,
        arr=arr_val,
        ir=ir_val,
        mdd=mdd_val,
        sr=sr_val,
    )


def test_to_performance_vector_shape_8() -> None:
    """to_performance_vector()는 shape (8,) float64를 반환."""
    bundle = _make_bundle()
    vec = bundle.to_performance_vector()
    assert vec.shape == (8,)
    assert vec.dtype == np.float64


def test_to_performance_vector_mdd_sign_inverted() -> None:
    """-MDD: bundle.mdd=-0.08 → vec[6] = +0.08 (부호 반전)."""
    bundle = _make_bundle(mdd_val=-0.08)
    vec = bundle.to_performance_vector()
    # vec[6] = -mdd = -(-0.08) = 0.08
    assert vec[6] == pytest.approx(0.08, abs=1e-9)
    # bundle.mdd 자체는 변경 없음
    assert bundle.mdd == pytest.approx(-0.08, abs=1e-9)


# ====================================================================== #
# normalize_performance_vector (C18 clip [0.01, 0.99] 준수)
# ====================================================================== #


def test_normalize_performance_vector_clip_range() -> None:
    """정상 history 입력 → 결과 전부 [0.01, 0.99] 범위."""
    vec = np.array([0.5, 0.4, 0.6, 0.5, 0.7, 0.8, 0.9, 0.3])
    # history: 5일치 각 8-dim 벡터
    history = np.random.RandomState(42).rand(5, 8)

    result = normalize_performance_vector(vec, history)

    assert result.shape == (8,)
    assert np.all(result >= 0.01 - 1e-9), f"violates lower clip: {result}"
    assert np.all(result <= 0.99 + 1e-9), f"violates upper clip: {result}"


def test_normalize_performance_vector_empty_history() -> None:
    """history 비어있으면 전부 0.5 (neutral fallback)."""
    vec = np.array([0.5, 0.4, 0.6, 0.5, 0.7, 0.8, 0.9, 0.3])
    history = np.empty((0, 8))

    result = normalize_performance_vector(vec, history)

    assert result.shape == (8,)
    assert np.allclose(result, 0.5), f"expected neutral 0.5, got {result}"


def test_normalize_performance_vector_extreme_values_clipped() -> None:
    """극단값 (max) → 0.99, 극단값 (min) → 0.01."""
    # history 최소: 0.1, 최대: 0.9
    history = np.array([
        [0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1],
        [0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9],
    ])

    # vec = max 경계 → clip 0.99
    vec_max = np.array([0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9])
    assert np.all(np.isclose(
        normalize_performance_vector(vec_max, history), 0.99, atol=1e-6
    ))

    # vec = min 경계 → clip 0.01
    vec_min = np.array([0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1])
    assert np.all(np.isclose(
        normalize_performance_vector(vec_min, history), 0.01, atol=1e-6
    ))

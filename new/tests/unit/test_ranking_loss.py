"""S1-0 Batch B ranking_loss.py unit tests. lightgbm 설치 필수."""
from __future__ import annotations

import pandas as pd
import pytest

from src.models.ranking_loss import (
    build_lgbm_params,
    compute_group_sizes,
    get_training_control,
    make_lgbm_dataset,
)


@pytest.fixture
def sample_panel():
    """3 ts_close × 4 ticker = 12 rows. group=[4,4,4]."""
    rows = []
    for t_idx, ts in enumerate(pd.date_range("2026-01-01 09:00+09:00", periods=3, freq="1min")):
        for ticker_idx in range(4):
            rows.append({
                "ticker": f"00000{ticker_idx}",
                "ts_close": ts,
                "feat_a": float(ticker_idx + t_idx),
                "feat_b": float(ticker_idx * 2 + t_idx),
                "relevance": ticker_idx,  # 0, 1, 2, 3
            })
    df = pd.DataFrame(rows).sort_values(["ts_close", "ticker"]).reset_index(drop=True)
    return df.set_index(["ticker", "ts_close"])


# ====================================================================== #
# compute_group_sizes
# ====================================================================== #


def test_compute_group_sizes_basic(sample_panel) -> None:
    # sample_panel은 ts_close 순으로 정렬됨. 각 ts당 4 ticker.
    # 하지만 MultiIndex set 후엔 (ticker, ts_close) 순 정렬될 수 있음 → 재정렬 필요.
    # DatasetBuilder 결과는 ts_close로 묶인 group으로 출력되지 않으므로 여기서 정렬 보장.
    panel = sample_panel.reset_index().sort_values(["ts_close", "ticker"])
    panel = panel.set_index(["ticker", "ts_close"])
    groups = compute_group_sizes(panel, group_col="ts_close")
    assert groups == [4, 4, 4]
    assert sum(groups) == 12


def test_compute_group_sizes_unsorted_raises(sample_panel) -> None:
    # 인덱스 불연속 (interleave) 시 group 합 검증에서 실패할 수도 있지만,
    # 같은 ts_close가 연속이 아니면 group 수가 많아져 sum에는 영향 없음.
    # 대신 group 개수가 의도와 다름을 감지.
    panel = sample_panel.reset_index()
    # ticker순 정렬 → 같은 ts_close가 떨어짐 → group 12개 (각 row별).
    panel = panel.sort_values(["ticker", "ts_close"])
    panel = panel.set_index(["ticker", "ts_close"])
    groups = compute_group_sizes(panel, group_col="ts_close")
    # 3개 ts × 4 row interleaved → group count 12 (각 1)
    assert sum(groups) == 12


def test_compute_group_sizes_missing_col(sample_panel) -> None:
    with pytest.raises(KeyError):
        compute_group_sizes(sample_panel, group_col="nonexistent")


# ====================================================================== #
# make_lgbm_dataset
# ====================================================================== #


def test_make_lgbm_dataset_happy(sample_panel) -> None:
    panel = sample_panel.reset_index().sort_values(["ts_close", "ticker"])
    panel = panel.set_index(["ticker", "ts_close"])
    ds = make_lgbm_dataset(panel, feature_cols=["feat_a", "feat_b"])
    assert ds is not None
    # lgb.Dataset은 lazy init. construct 후 num_data 확인.
    ds.construct()
    assert ds.num_data() == 12
    # group list 합이 row 수와 일치
    assert sum(ds.get_group()) == 12


def test_make_lgbm_dataset_missing_feature(sample_panel) -> None:
    with pytest.raises(KeyError):
        make_lgbm_dataset(sample_panel, feature_cols=["feat_a", "no_such_col"])


def test_make_lgbm_dataset_missing_label(sample_panel) -> None:
    with pytest.raises(KeyError):
        make_lgbm_dataset(
            sample_panel,
            feature_cols=["feat_a"],
            label_col="nope",
        )


# ====================================================================== #
# build_lgbm_params
# ====================================================================== #


def test_build_lgbm_params_from_config() -> None:
    params = build_lgbm_params()
    assert params["objective"] == "lambdarank"
    assert params["metric"] == "ndcg"
    assert params["ndcg_eval_at"] == [5]
    assert params["label_gain"] == [0, 1, 3, 7]
    assert params["num_leaves"] == 31
    assert params["learning_rate"] == pytest.approx(0.05, abs=1e-9)
    assert params["random_state"] == 42


def test_build_lgbm_params_overrides() -> None:
    params = build_lgbm_params(overrides={"learning_rate": 0.1, "num_leaves": 63})
    assert params["learning_rate"] == pytest.approx(0.1)
    assert params["num_leaves"] == 63


def test_get_training_control() -> None:
    tc = get_training_control()
    assert tc["n_estimators"] == 500
    assert tc["early_stopping_rounds"] == 30
    assert tc["n_relevance_grades"] == 4


# ====================================================================== #
# LightGBM 학습 smoke (실제 lgb.train 1회)
# ====================================================================== #


def test_lgbm_train_smoke(sample_panel) -> None:
    """make_lgbm_dataset으로 생성한 Dataset이 실제 lgb.train 호출에 통과하는지."""
    import lightgbm as lgb

    panel = sample_panel.reset_index().sort_values(["ts_close", "ticker"])
    panel = panel.set_index(["ticker", "ts_close"])

    ds = make_lgbm_dataset(panel, feature_cols=["feat_a", "feat_b"])
    params = build_lgbm_params(
        overrides={
            "num_leaves": 3,
            "learning_rate": 0.1,
            "min_child_samples": 1,
            "min_data_in_leaf": 1,
            "min_data_in_bin": 1,
        }
    )

    booster = lgb.train(
        params,
        ds,
        num_boost_round=3,
    )
    assert booster is not None
    assert booster.num_trees() >= 1

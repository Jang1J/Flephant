from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _load_script():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "rolling_window_ic_experiment.py"
    )
    spec = importlib.util.spec_from_file_location("rolling_window_ic_experiment", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_build_folds_uses_trading_dates_not_pooled_rows():
    mod = _load_script()
    dates = [f"202601{i:02d}" for i in range(1, 31)]
    folds = mod._build_folds(
        dates,
        window_days=10,
        n_folds=3,
        min_validation_days=5,
        embargo_days=1,
    )

    assert len(folds) == 3
    assert folds[0].mode == "rolling"
    assert folds[0].train_start == "20260101"
    assert folds[0].train_end == "20260110"
    assert folds[0].val_start == "20260112"
    assert folds[0].val_end == "20260117"
    assert folds[1].train_start == "20260107"


def test_build_expanding_folds_keeps_anchor_and_expands_train_window():
    mod = _load_script()
    dates = [f"202601{i:02d}" for i in range(1, 31)]
    folds = mod._build_expanding_folds(
        dates,
        initial_window_days=10,
        n_folds=3,
        min_validation_days=5,
        embargo_days=1,
    )

    assert len(folds) == 3
    assert folds[0].mode == "expanding"
    assert folds[0].train_start == "20260101"
    assert folds[0].train_end == "20260110"
    assert folds[0].val_start == "20260112"
    assert folds[0].val_end == "20260117"
    assert folds[1].train_start == "20260101"
    assert folds[1].train_end == "20260116"


def test_feature_groups_are_ssot_config_driven():
    mod = _load_script()
    groups = mod._feature_groups(
        {
            "groups": {
                "OHLCV": {
                    "include_base": True,
                    "include_exogenous": False,
                    "dual_source_cols": [],
                },
                "OHLCV+News_DS": {
                    "include_base": True,
                    "include_exogenous": False,
                    "dual_source_cols": ["news_score_t"],
                },
            }
        }
    )

    assert "feat_5m_ret" in groups["OHLCV"]
    assert "news_score_t" not in groups["OHLCV"]
    assert "news_score_t" in groups["OHLCV+News_DS"]


def test_sort_for_grouping_makes_ts_blocks_contiguous():
    import pandas as pd

    mod = _load_script()
    df = pd.DataFrame(
        {
            "ticker": ["000002", "000001", "000002", "000001"],
            "ts_close": pd.to_datetime(
                [
                    "2026-01-02 09:01",
                    "2026-01-02 09:00",
                    "2026-01-02 09:00",
                    "2026-01-02 09:01",
                ]
            ),
            "relevance": [1, 2, 3, 4],
        }
    ).set_index(["ticker", "ts_close"])

    out = mod._sort_for_grouping(df).reset_index()
    assert out["ts_close"].tolist() == sorted(out["ts_close"].tolist())
    assert out.loc[0, "ticker"] == "000001"

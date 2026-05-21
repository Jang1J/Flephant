from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _load_script():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "lgbm_hyperparam_sweep.py"
    )
    spec = importlib.util.spec_from_file_location("lgbm_hyperparam_sweep", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_compact_grid_contains_baseline_and_regularized_candidates():
    mod = _load_script()

    grid = mod._param_grid("compact")

    names = {candidate.name for candidate in grid}
    assert "baseline_lr005_l31_c20" in names
    assert "regularized_lr003_l31_c50" in names
    assert all(candidate.training_control["n_estimators"] > 0 for candidate in grid)


def test_registry_root_rejects_production_lgbm_path():
    import pytest

    mod = _load_script()

    with pytest.raises(ValueError, match="must not write under artifacts/lgbm"):
        mod._resolve_research_registry_root("artifacts/lgbm")


def test_target_col_must_be_deploy_allowlisted():
    import pytest

    mod = _load_script()

    assert mod._validate_target_col("label_195m_net_ret") == "label_195m_net_ret"
    with pytest.raises(ValueError, match="deploy_target_cols"):
        mod._validate_target_col("label_future_leaky_ret")


def test_fold_stability_computes_positive_rate_and_dispersion():
    mod = _load_script()

    stability = mod._fold_stability(
        [
            {"rank_ic": 0.10, "ic": 0.08},
            {"rank_ic": 0.05, "ic": 0.04},
            {"rank_ic": -0.01, "ic": 0.02},
        ]
    )

    assert stability["fold_count"] == 3
    assert stability["rank_ic_positive_fold_rate"] == 2 / 3
    assert stability["rank_ic_std"] > 0


def test_overfit_assessment_blocks_unstable_negative_candidate():
    mod = _load_script()

    stability = {
        "rank_ic_std": 0.12,
        "rank_ic_positive_fold_rate": 0.33,
    }
    assessment = mod._overfit_assessment(
        metrics={"rank_ic": -0.01, "ic": 0.02, "sr": 1.0},
        fold_metrics=[{"rank_ic": -0.01}, {"rank_ic": 0.02}, {"rank_ic": -0.02}],
        stability=stability,
        params={"num_leaves": 31, "min_child_samples": 50},
        training_control={"n_estimators": 500},
        final_num_boost_round=120,
    )

    assert assessment["status"] == "FAIL"
    assert "non_positive_ic_or_rank_ic" in assessment["blockers"]
    assert "rank_ic_positive_fold_rate_below_0_6" in assessment["blockers"]


def test_overfit_assessment_warns_when_early_stopping_does_not_reduce_rounds():
    mod = _load_script()

    assessment = mod._overfit_assessment(
        metrics={"rank_ic": 0.06, "ic": 0.05, "sr": 2.0},
        fold_metrics=[{"rank_ic": 0.05}, {"rank_ic": 0.07}, {"rank_ic": 0.06}],
        stability={"rank_ic_std": 0.01, "rank_ic_positive_fold_rate": 1.0},
        params={"num_leaves": 31, "min_child_samples": 50},
        training_control={"n_estimators": 100},
        final_num_boost_round=98,
    )

    assert assessment["status"] == "WARN"
    assert "early_stopping_did_not_reduce_rounds" in assessment["warnings"]

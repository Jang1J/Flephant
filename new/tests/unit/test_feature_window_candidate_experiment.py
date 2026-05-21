from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _load_script():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "feature_window_candidate_experiment.py"
    )
    spec = importlib.util.spec_from_file_location(
        "feature_window_candidate_experiment",
        path,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_feature_groups_cover_requested_news_ds_shapes():
    mod = _load_script()
    groups = mod._feature_groups()

    assert groups["OHLCV"].feature_cols
    assert "news_score_t" not in groups["OHLCV"].feature_cols
    assert "news_score_t" not in groups["OHLCV+Exog"].feature_cols
    assert "news_score_t" in groups["OHLCV+News_DS"].feature_cols
    assert "news_score_t" in groups["OHLCV+Exog+News_DS"].feature_cols
    assert groups["OHLCV+News_DS"].include_dual_source is True
    assert groups["OHLCV+News_DS"].include_exogenous is False
    assert groups["OHLCV+Exog+News_DS"].include_exogenous is True


def test_safe_slug_keeps_version_registry_safe():
    mod = _load_script()

    assert mod._safe_slug("OHLCV+Exog+News_DS") == "ohlcv-exog-news-ds"
    assert mod._safe_slug("  ") == "candidate"


def test_metric_value_handles_missing_and_invalid_values():
    mod = _load_script()

    assert mod._metric_value({"rank_ic": "0.12"}, "rank_ic") == 0.12
    assert mod._metric_value({"rank_ic": None}, "rank_ic") == 0.0
    assert mod._metric_value({}, "rank_ic") == 0.0

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest


def _load_script(name: str):
    script_path = Path(__file__).resolve().parents[2] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_label_horizon_scan_uses_final_gate_pending_data_universe(monkeypatch):
    mod = _load_script("cost_aware_label_horizon_scan")

    def fake_config_load(file: str = "risk_config.yaml", key: str | None = None):
        if file == "universe_config.yaml":
            return {
                "sectors": {
                    "semis": {
                        "status": "confirmed",
                        "stocks": [
                            {"ticker": "005930", "status": "active"},
                        ],
                    },
                    "banks": {
                        "status": "confirmed_pending_data",
                        "stocks": [
                            {"ticker": "105560", "status": "pending_data"},
                        ],
                    },
                    "ignored_sector": {
                        "status": "candidate",
                        "stocks": [
                            {"ticker": "000001", "status": "pending_data"},
                        ],
                    },
                },
            }
        if key == "backtest_agent":
            return {
                "deploy_decision_gate": {
                    "final_dataset_gate": {
                        "include_pending_data_tickers": True,
                        "allowed_stock_statuses": ["active", "pending_data"],
                        "allowed_sector_statuses": ["confirmed", "confirmed_pending_data"],
                    },
                },
            }
        return {}

    monkeypatch.setattr(mod, "config_load", fake_config_load)

    assert mod._active_tickers() == ["005930", "105560"]
    assert mod._universe_hash(["105560", "005930"]) == mod._universe_hash([
        "005930",
        "105560",
    ])


def test_label_horizon_scan_thresholds_loaded_from_risk_config(monkeypatch):
    mod = _load_script("cost_aware_label_horizon_scan")

    def fake_config_load(file: str = "risk_config.yaml", key: str | None = None):
        if key == "cost_aware_retraining":
            return {
                "label_horizon_gate": {
                    "min_mean_net_bps": "7.5",
                    "min_positive_net_rate": "0.61",
                    "allow_warn_for_research_only": "false",
                }
            }
        return {}

    monkeypatch.setattr(mod, "config_load", fake_config_load)

    assert mod._diagnostic_thresholds(2.5) == {
        "min_mean_net_bps": 7.5,
        "min_positive_net_rate": 0.61,
        "allow_warn_for_research_only": False,
    }


def test_default_horizons_include_service_policy_min_holding(monkeypatch):
    mod = _load_script("cost_aware_label_horizon_scan")

    def fake_config_load(file: str = "risk_config.yaml", key: str | None = None):
        if key == "label":
            return {"horizon_bars": 5}
        if key == "preprocessor":
            return {"multi_scale_windows": [5, 30, 60]}
        if key == "service_policy_replay":
            return {"min_holding_bars": 195}
        return {}

    monkeypatch.setattr(mod, "config_load", fake_config_load)

    assert mod._default_horizons() == ["5", "30", "60", "195", "session_close"]


def test_horizon_return_series_uses_dataset_builder_drop_last_n_policy():
    mod = _load_script("cost_aware_label_horizon_scan")
    panel = pd.DataFrame(
        {
            "ticker": ["005930"] * 10,
            "ts_close": pd.date_range(
                "2026-05-01 09:00:00+09:00",
                periods=10,
                freq="min",
            ),
            "close": [100.0 + i for i in range(10)],
        }
    )

    series = mod._horizon_return_series(
        panel,
        "2",
        drop_last_n_bars=5,
        active_horizon="2",
    )

    assert int(series.notna().sum()) == 5
    assert series.iloc[0] == pytest.approx(102.0 / 100.0 - 1.0)
    assert series.iloc[4] == pytest.approx(106.0 / 104.0 - 1.0)
    assert series.iloc[5:].isna().all()


def test_session_close_return_series_uses_active_horizon_drop_policy():
    mod = _load_script("cost_aware_label_horizon_scan")
    panel = pd.DataFrame(
        {
            "ticker": ["005930"] * 10,
            "ts_close": pd.date_range(
                "2026-05-01 09:00:00+09:00",
                periods=10,
                freq="min",
            ),
            "close": [100.0 + i for i in range(10)],
        }
    )

    series = mod._horizon_return_series(
        panel,
        "session_close",
        drop_last_n_bars=5,
        active_horizon="2",
    )

    assert int(series.notna().sum()) == 5
    assert series.iloc[0] == pytest.approx(109.0 / 100.0 - 1.0)
    assert series.iloc[4] == pytest.approx(109.0 / 104.0 - 1.0)
    assert series.iloc[5:].isna().all()


def test_best_horizon_selection_uses_label_topk_before_mean_net():
    mod = _load_script("cost_aware_label_horizon_scan")

    best = mod._select_best_horizon_report(
        [
            {
                "horizon": "5",
                "status": "PASS",
                "valid_rows": 100,
                "mean_net_bps": 12.0,
                "positive_net_rate": 0.62,
                "label_topk": {
                    "mean_net_bps": 13.0,
                    "positive_net_rate": 0.70,
                },
            },
            {
                "horizon": "30",
                "status": "PASS",
                "valid_rows": 100,
                "mean_net_bps": 8.0,
                "positive_net_rate": 0.59,
                "label_topk": {
                    "mean_net_bps": 25.0,
                    "positive_net_rate": 0.82,
                },
            },
        ]
    )

    assert best["horizon"] == "30"


def test_label_scan_marks_research_trainable_from_topk_even_when_mean_warn(
    monkeypatch,
    tmp_path,
) -> None:
    mod = _load_script("cost_aware_label_horizon_scan")

    panel = pd.DataFrame(
        {
            "ticker": ["005930", "000660", "005930", "000660"],
            "ts_close": pd.to_datetime([
                "2026-05-01 09:00:00+09:00",
                "2026-05-01 09:00:00+09:00",
                "2026-05-01 09:01:00+09:00",
                "2026-05-01 09:01:00+09:00",
            ]),
            "close": [100.0, 100.0, 101.0, 99.0],
        }
    )

    monkeypatch.setattr(mod, "_active_tickers", lambda: ["005930", "000660"])
    monkeypatch.setattr(mod, "_infer_date_range", lambda *_args: ("20260501", "20260501", []))
    monkeypatch.setattr(mod, "_load_raw_panel", lambda **_kwargs: (panel, []))
    monkeypatch.setattr(mod, "_cost_bps", lambda: 0.0)
    monkeypatch.setattr(
        mod,
        "_diagnostic_thresholds",
        lambda _cost: {
            "min_mean_net_bps": 1.0,
            "min_positive_net_rate": 0.5,
            "allow_warn_for_research_only": False,
        },
    )
    monkeypatch.setattr(
        mod,
        "_label_generation_settings",
        lambda: {"active_horizon_bars": 1, "drop_last_n_bars": 0},
    )
    monkeypatch.setattr(mod, "config_load", lambda *_args, **_kwargs: {"top_k_fraction": 0.5})

    args = type(
        "Args",
        (),
        {
            "artifacts_dir": str(tmp_path),
            "tickers": "005930,000660",
            "start_date": "20260501",
            "end_date": "20260501",
            "horizons": "1",
        },
    )()

    report = mod.build_report(args)

    assert report["status"] == "WARN"
    assert report["deployable_label_recommendation"] is False
    assert report["research_trainable_label_recommendation"] is True


def test_horizon_summary_reports_selection_impact_for_active_noop():
    mod = _load_script("cost_aware_label_horizon_scan")
    panel = pd.DataFrame(
        {
            "ticker": ["005930", "000660", "005930", "000660"],
            "ts_close": pd.to_datetime([
                "2026-05-01 09:00:00+09:00",
                "2026-05-01 09:00:00+09:00",
                "2026-05-01 09:01:00+09:00",
                "2026-05-01 09:01:00+09:00",
            ]),
            "close": [100.0, 100.0, 102.0, 101.0],
        }
    )

    summary = mod._summarize_horizon(
        panel=panel,
        horizon="1",
        active_horizon="1",
        total_cost_bps=1.0,
        thresholds={"min_mean_net_bps": 0.0, "min_positive_net_rate": 0.0},
        top_k_fraction=0.5,
    )

    assert summary["selection_impact"] == {
        "active_horizon": "1",
        "topk_overlap_rate": 1.0,
        "rank_correlation": 1.0,
        "selection_changes": 0,
        "groups_compared": 0,
        "rank_equivalent_to_active_horizon": True,
    }
    assert summary["label_topk"]["method"] == "candidate_label_topk_net_bps"
    assert summary["label_topk"]["rows"] == 1


def test_horizon_summary_reports_selection_changes():
    mod = _load_script("cost_aware_label_horizon_scan")
    panel = pd.DataFrame(
        {
            "ticker": ["005930", "000660", "005930", "000660", "005930", "000660"],
            "ts_close": pd.to_datetime([
                "2026-05-01 09:00:00+09:00",
                "2026-05-01 09:00:00+09:00",
                "2026-05-01 09:01:00+09:00",
                "2026-05-01 09:01:00+09:00",
                "2026-05-01 09:02:00+09:00",
                "2026-05-01 09:02:00+09:00",
            ]),
            "close": [100.0, 100.0, 103.0, 101.0, 104.0, 106.0],
        }
    )

    summary = mod._summarize_horizon(
        panel=panel,
        horizon="2",
        active_horizon="1",
        total_cost_bps=1.0,
        thresholds={"min_mean_net_bps": 0.0, "min_positive_net_rate": 0.0},
        top_k_fraction=0.5,
    )

    impact = summary["selection_impact"]
    assert impact["selection_changes"] == 1
    assert impact["topk_overlap_rate"] == pytest.approx(0.0)
    assert impact["rank_equivalent_to_active_horizon"] is False

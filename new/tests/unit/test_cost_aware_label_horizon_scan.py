from __future__ import annotations

import importlib.util
from pathlib import Path


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

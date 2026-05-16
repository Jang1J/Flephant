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


def _write_jsonl(path: Path, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join('{"close": 1}\n' for _ in range(rows)), encoding="utf-8")


def test_select_dates_requires_valid_bar_artifacts_for_all_tickers(tmp_path):
    mod = _load_script("phase2_feature_backfill")
    artifacts = tmp_path / "data"
    tickers = ["005930", "105560"]

    _write_jsonl(artifacts / "005930" / "bars_1m_20260514.jsonl", 2)
    _write_jsonl(artifacts / "105560" / "bars_1m_20260514.jsonl", 3)
    _write_jsonl(artifacts / "005930" / "bars_1m_20260515.jsonl", 3)
    _write_jsonl(artifacts / "105560" / "bars_1m_20260515.jsonl", 3)
    _write_jsonl(artifacts / "105560" / "unrelated_20260513.jsonl", 3)

    assert mod._select_dates(
        artifacts,
        tickers,
        end_date="20260515",
        business_days=10,
        min_rows=3,
    ) == ["20260515"]


def test_active_tickers_uses_final_gate_pending_data_universe(monkeypatch):
    mod = _load_script("phase2_feature_backfill")

    def fake_config_load(file: str = "risk_config.yaml", key: str | None = None):
        if file == "universe_config.yaml":
            return {
                "sectors": {
                    "semis": {
                        "status": "confirmed",
                        "stocks": [{"ticker": "005930", "status": "active"}],
                    },
                    "banks": {
                        "status": "confirmed_pending_data",
                        "stocks": [{"ticker": "105560", "status": "pending_data"}],
                    },
                    "ignored": {
                        "status": "candidate",
                        "stocks": [{"ticker": "000001", "status": "pending_data"}],
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


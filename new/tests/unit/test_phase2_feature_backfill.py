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
    date_key = path.name.removeprefix("bars_1m_").split(".", 1)[0]
    ticker = path.parent.name
    lines = []
    for idx in range(rows):
        lines.append(
            (
                "{"
                f'"ticker": "{ticker}", '
                f'"ts_close": "{date_key[:4]}-{date_key[4:6]}-{date_key[6:]}T09:{idx:02d}:00+09:00", '
                '"close": 1'
                "}\n"
            )
        )
    path.write_text("".join(lines), encoding="utf-8")


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


def test_select_dates_rejects_timestamp_ticker_and_duplicate_defects(tmp_path):
    mod = _load_script("phase2_feature_backfill")
    artifacts = tmp_path / "data"
    tickers = ["005930", "105560"]

    _write_jsonl(artifacts / "005930" / "bars_1m_20260515.jsonl", 3)
    _write_jsonl(artifacts / "105560" / "bars_1m_20260515.jsonl", 3)
    bad = artifacts / "105560" / "bars_1m_20260515.jsonl"
    bad.write_text(
        (
            '{"ticker": "105560", "ts_close": "2026-05-14T09:00:00+09:00", "close": 1}\n'
            '{"ticker": "105560", "ts_close": "2026-05-14T09:00:00+09:00", "close": 1}\n'
            '{"ticker": "999999", "ts_close": "2026-05-14T09:02:00+09:00", "close": 1}\n'
        ),
        encoding="utf-8",
    )

    assert mod._select_dates(
        artifacts,
        tickers,
        end_date="20260515",
        business_days=10,
        min_rows=3,
    ) == []


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

from __future__ import annotations

import importlib.util
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def _load_script(name: str):
    script_path = Path(__file__).resolve().parents[2] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_jsonl(
    path: Path,
    rows: int,
    *,
    gap_after: int | None = None,
    gap_minutes: int = 0,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.name.startswith("bars_1m_"):
        date_key = path.name.removeprefix("bars_1m_").split(".", 1)[0]
    else:
        match = re.search(r"(20\d{6})", path.name)
        assert match
        date_key = match.group(1)
    ticker = path.parent.name
    start = datetime(
        int(date_key[:4]),
        int(date_key[4:6]),
        int(date_key[6:]),
        9,
        0,
        tzinfo=ZoneInfo("Asia/Seoul"),
    )
    lines = []
    for idx in range(rows):
        offset = idx
        if gap_after is not None and idx > gap_after:
            offset += gap_minutes
        ts = start + timedelta(minutes=offset)
        lines.append(
            (
                "{"
                f'"ticker": "{ticker}", '
                f'"ts_close": "{ts.isoformat()}", '
                '"close": 1'
                "}\n"
            )
        )
    path.write_text("".join(lines), encoding="utf-8")


def test_select_dates_requires_valid_bar_artifacts_for_all_tickers(tmp_path):
    mod = _load_script("phase2_feature_backfill")
    artifacts = tmp_path / "data"
    tickers = ["005930", "105560"]

    _write_jsonl(artifacts / "005930" / "bars_1m_20260514.jsonl", 299)
    _write_jsonl(artifacts / "105560" / "bars_1m_20260514.jsonl", 301)
    _write_jsonl(artifacts / "005930" / "bars_1m_20260515.jsonl", 301)
    _write_jsonl(artifacts / "105560" / "bars_1m_20260515.jsonl", 301)
    _write_jsonl(artifacts / "105560" / "unrelated_20260513.jsonl", 301)

    assert mod._select_dates(
        artifacts,
        tickers,
        end_date="20260515",
        business_days=10,
        min_rows=300,
    ) == ["20260515"]


def test_select_dates_rejects_timestamp_ticker_and_duplicate_defects(tmp_path):
    mod = _load_script("phase2_feature_backfill")
    artifacts = tmp_path / "data"
    tickers = ["005930", "105560"]

    _write_jsonl(artifacts / "005930" / "bars_1m_20260515.jsonl", 301)
    _write_jsonl(artifacts / "105560" / "bars_1m_20260515.jsonl", 301)
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
        min_rows=300,
    ) == []


def test_select_dates_rejects_large_intraday_gap(tmp_path):
    mod = _load_script("phase2_feature_backfill")
    artifacts = tmp_path / "data"
    tickers = ["005930", "105560"]

    _write_jsonl(artifacts / "005930" / "bars_1m_20260515.jsonl", 301)
    _write_jsonl(
        artifacts / "105560" / "bars_1m_20260515.jsonl",
        301,
        gap_after=150,
        gap_minutes=20,
    )

    assert mod._select_dates(
        artifacts,
        tickers,
        end_date="20260515",
        business_days=10,
        min_rows=300,
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


def test_phase2_blocks_when_requested_artifact_dates_are_missing(
    tmp_path,
    monkeypatch,
):
    mod = _load_script("phase2_feature_backfill")
    artifacts = tmp_path / "data"
    tickers = ["005930", "105560"]

    for ticker in tickers:
        _write_jsonl(artifacts / ticker / "bars_1m_20260515.jsonl", 301)

    monkeypatch.setattr(mod, "_active_tickers", lambda: tickers)
    monkeypatch.setattr(
        mod,
        "_expected_artifact_dates",
        lambda *, end_date, business_days: ["20260514", "20260515"],
    )

    def fake_config_load(file: str = "risk_config.yaml", key: str | None = None):
        if key == "phase2_feature_backfill":
            return {
                "min_rows_per_day": 300,
                "min_dual_source_non_neutral_date_coverage": 0.0,
                "min_exogenous_non_neutral_date_coverage": 0.0,
            }
        if key == "live_data_readiness":
            return {"train_min_rows_per_day": 300}
        if key == "exogenous_features":
            return {"neutral_defaults": {}}
        return {}

    monkeypatch.setattr(mod, "config_load", fake_config_load)
    monkeypatch.setattr(
        mod,
        "load_latest_scores",
        lambda date_key: [{"ticker": "005930", "news_score_t": 0.1}],
    )
    monkeypatch.setattr(
        mod,
        "load_exogenous_scores",
        lambda date_key, feature_cols, defaults: (
            {"005930": {"macro": 1.0}},
            {"status": "found", "record_count": 1},
        ),
    )

    report = mod.run_phase2_feature_backfill(
        end_date="20260515",
        business_days=2,
        write_neutral_placeholders=False,
        artifacts_dir=artifacts,
        output_dir=tmp_path / "reports",
    )

    assert report["status"] == "BLOCKED"
    assert "kis_1m_artifact_date_coverage_below_threshold" in report["blockers"]
    assert report["date_count"] == 1
    assert report["artifact_date_coverage"]["expected_date_count"] == 2
    assert report["artifact_date_coverage"]["missing_date_count"] == 1
    assert report["artifact_date_coverage"]["missing_dates_sample"] == ["20260514"]

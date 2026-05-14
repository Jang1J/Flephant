from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _load_script(name: str):
    script_path = Path(__file__).resolve().parents[2] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_c12_recheck_blocks_run_outside_mode_b(monkeypatch, tmp_path: Path):
    mod = _load_script("c12_recheck_runner")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.delenv("ELEPHANT_MODE", raising=False)

    report = mod.run_c12_recheck(
        bundle_id="BUNDLE-TEST",
        run=True,
        write_report=False,
        now=datetime(2026, 5, 13, 3, 0, tzinfo=ZoneInfo("Asia/Seoul")),
    )

    assert report["status"] == "BLOCKED"
    assert report["executed"] is False
    assert "mode_b_window_or_env_not_satisfied" in report["blockers"]


def test_c12_recheck_detects_current_embedded_schema(monkeypatch, tmp_path: Path):
    mod = _load_script("c12_recheck_runner")
    bundle_id = "BUNDLE-TEST"
    report_path = tmp_path / f"artifacts/reports/backtest/backtest_{bundle_id}_20260513.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps({
            "bundle_id": bundle_id,
            "verdict": "warn",
            "regression_risk": {"flagged": True, "severity": "high"},
            "minute_bar_leakage_check": {"verdict": "pass"},
            "feature_quality": {},
            "service_policy_replay": {"status": "BLOCKED"},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "ROOT", tmp_path)

    report = mod.run_c12_recheck(bundle_id=bundle_id, run=False, write_report=False)

    assert report["latest_backtest"]["schema_current"] is True
    assert report["latest_backtest"]["deployable"] is False
    assert "c12_not_deployable" in report["blockers"]

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


def test_phase2_input_readiness_reports_missing_inputs(monkeypatch, tmp_path: Path):
    mod = _load_script("phase2_input_readiness")
    monkeypatch.setattr(mod.dual_source_history, "_business_dates", lambda end_date, business_days: ["20260508"])

    class DummyUS:
        _is_mock = False

    class DummyECOS:
        _is_mock = True

    class DummyKRX:
        def _has_kis_investor_provider(self):
            return False

    monkeypatch.setattr(mod.exogenous_history, "USMarketClient", lambda: DummyUS())
    monkeypatch.setattr(mod.exogenous_history, "ECOSRestClient", lambda: DummyECOS())
    monkeypatch.setattr(mod.exogenous_history, "KRXRestClient", lambda: DummyKRX())

    report = mod.check_phase2_input_readiness(
        end_date="20260508",
        business_days=1,
        raw_events_dir=tmp_path / "missing_raw",
        write_report=False,
    )

    assert report["status"] == "BLOCKED"
    assert "dual_source_raw_archive_coverage_below_threshold" in report["blockers"]
    assert "exogenous_required_provider_unavailable" in report["blockers"]
    assert report["dual_source_raw"]["missing_date_count"] == 1

"""post_close_data_update_scheduler tests."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _load_scheduler_module():
    script_dir = Path(__file__).resolve().parents[2] / "scripts"
    script_path = script_dir / "post_close_data_update_scheduler.py"
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    spec = importlib.util.spec_from_file_location("post_close_data_update_scheduler", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fake_config():
    return {
        "pit_safety": {"snapshot_hour": 18, "snapshot_minute": 0},
        "backtest_agent": {
            "deploy_decision_gate": {
                "final_dataset_gate": {
                    "expected_end_date": "20260515",
                    "min_business_days": 249,
                    "min_tickers": 30,
                }
            }
        },
        "post_close_data_update": {
            "scheduler": {
                "enabled": True,
                "run_time_kst": "18:05",
                "poll_interval_sec": 1,
                "retry_failed_after_sec": 1800,
                "db": {"enabled": False},
                "run_prelive": True,
                "dry_run": False,
                "bundle_id_template": "BUNDLE-{target_end_date}-POSTCLOSE",
            }
        },
    }


def test_tick_waits_before_configured_time(monkeypatch):
    mod = _load_scheduler_module()
    monkeypatch.setattr(mod.updater, "config_load", lambda *args, **kwargs: _fake_config())
    kst = ZoneInfo("Asia/Seoul")

    decision = mod.evaluate_tick(datetime(2026, 5, 20, 18, 4, tzinfo=kst), {})

    assert decision["should_run"] is False
    assert decision["reason"] == "before_configured_run_time"


def test_tick_runs_after_configured_time(monkeypatch):
    mod = _load_scheduler_module()
    monkeypatch.setattr(mod.updater, "config_load", lambda *args, **kwargs: _fake_config())
    kst = ZoneInfo("Asia/Seoul")

    decision = mod.evaluate_tick(datetime(2026, 5, 20, 18, 5, tzinfo=kst), {})

    assert decision["should_run"] is True
    assert decision["reason"] == "due"
    assert decision["target_end_date"] == "20260520"


def test_tick_skips_already_passed_target(monkeypatch):
    mod = _load_scheduler_module()
    monkeypatch.setattr(mod.updater, "config_load", lambda *args, **kwargs: _fake_config())
    kst = ZoneInfo("Asia/Seoul")

    decision = mod.evaluate_tick(
        datetime(2026, 5, 20, 19, 0, tzinfo=kst),
        {"last_target_end_date": "20260520", "last_status": "PASS"},
    )

    assert decision["should_run"] is False
    assert decision["reason"] == "target_already_passed"


def test_tick_reads_latest_pass_from_db_when_no_state(monkeypatch):
    mod = _load_scheduler_module()
    cfg = _fake_config()
    cfg["post_close_data_update"]["scheduler"]["db"]["enabled"] = True
    monkeypatch.setattr(mod.updater, "config_load", lambda *args, **kwargs: cfg)
    kst = ZoneInfo("Asia/Seoul")

    class FakeStore:
        db_path = Path("/tmp/fake.sqlite3")

        def latest_run(self, target_end_date):
            return {
                "target_end_date": target_end_date,
                "status": "PASS",
                "started_at": "2026-05-20T18:05:00+09:00",
                "bundle_id": "BUNDLE-20260520-POSTCLOSE",
                "report_path": "artifacts/reports/post_close_data_update/test.json",
            }

        def close(self):
            return None

    monkeypatch.setattr(mod, "PostCloseJobStore", FakeStore)

    decision = mod.evaluate_tick(datetime(2026, 5, 20, 19, 0, tzinfo=kst), None)

    assert decision["should_run"] is False
    assert decision["reason"] == "target_already_passed"
    assert decision["state_source"] == "sqlite"


def test_tick_waits_retry_interval_after_blocked(monkeypatch):
    mod = _load_scheduler_module()
    monkeypatch.setattr(mod.updater, "config_load", lambda *args, **kwargs: _fake_config())
    kst = ZoneInfo("Asia/Seoul")

    decision = mod.evaluate_tick(
        datetime(2026, 5, 20, 19, 10, tzinfo=kst),
        {
            "last_target_end_date": "20260520",
            "last_status": "BLOCKED",
            "last_attempted_at": "2026-05-20T18:50:00+09:00",
        },
    )

    assert decision["should_run"] is False
    assert decision["reason"] == "target_recently_failed_wait_retry"


def test_run_once_force_writes_state(monkeypatch, tmp_path):
    mod = _load_scheduler_module()
    monkeypatch.setattr(mod.updater, "config_load", lambda *args, **kwargs: _fake_config())
    monkeypatch.setattr(
        mod.updater,
        "run_update",
        lambda **kwargs: {
            "status": "PASS",
            "target_end_date": kwargs["end_date"],
            "report_path_relative": "artifacts/reports/post_close_data_update/test.json",
        },
    )
    monkeypatch.setattr(mod.updater, "_write_report", lambda report: tmp_path / "report.json")
    state_path = tmp_path / "state.json"

    report = mod.run_once(dry_run=False, force=True, state_path=state_path)

    assert report["status"] == "PASS"
    assert state_path.exists()
    assert "last_target_end_date" in state_path.read_text(encoding="utf-8")

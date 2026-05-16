from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from src.jobs.run_e2e_scenario import _summary_failures as e2e_summary_failures
from src.jobs.run_final_demo import (
    _parse_args,
    _summary_failures as demo_summary_failures,
    run_demo_mode_b,
)
from src.runner.e2e_scenario_runner import E2EScenarioRunner


def test_e2e_summary_treats_string_false_sla_as_failure() -> None:
    summary = {
        "pit_violations": 0,
        "fda_missing_reason_code": 0,
        "total_errors": 0,
        "hot_path_sla": {"sla_ok": "false"},
        "mode_b_verdicts": ["pass"],
    }

    assert e2e_summary_failures(summary, skip_mode_b=False) == ["hot_path_sla"]


def test_final_demo_summary_treats_string_false_sla_as_failure() -> None:
    summary = {
        "pit_violations": 0,
        "fda_missing_reason_code": 0,
        "total_errors": 0,
        "hot_path_sla": {"sla_ok": "false"},
    }

    assert demo_summary_failures(summary, require_sla=True) == ["hot_path_sla"]


def test_summary_failures_fail_closed_on_malformed_counters() -> None:
    summary = {
        "pit_violations": "many",
        "fda_missing_reason_code": "",
        "total_errors": None,
        "hot_path_sla": {"sla_ok": True},
        "mode_b_verdicts": ["pass"],
    }

    assert e2e_summary_failures(summary, skip_mode_b=False) == ["pit_violations"]
    assert demo_summary_failures(dict(summary)) == ["pit_violations"]


def test_mode_b_demo_uses_read_only_evidence(monkeypatch, tmp_path) -> None:
    def fake_status(*, bundle_id: str):
        return {
            "status": "PASS",
            "deploy_quality": "PASS",
            "broker_evidence": "PASS",
            "registry_mutated": False,
            "live_trading_allowed": False,
            "c12_backtest": {
                "verdict": "pass",
                "deployable": "true",
                "report_path": "artifacts/reports/backtest/backtest_BUNDLE-TEST.json",
                "selection": "latest_deployable",
                "metrics": {"sr": 1.0},
            },
            "production_registry": {"active_version": None},
            "paper_registry": {"active_version": "paper_model"},
        }

    monkeypatch.setattr("src.jobs.run_final_demo.build_service_status", fake_status)
    monkeypatch.setattr(
        "src.jobs.run_final_demo._save_summary",
        lambda _demo_id, _summary: tmp_path / "demo_mode_b.json",
    )

    summary = run_demo_mode_b("week1_basic.yaml", bundle_id="BUNDLE-TEST")

    assert summary["status"] == "PASS"
    assert summary["mode_b_demo_mode"] == "read_only_evidence"
    assert summary["mode_b_verdicts"] == ["pass"]
    assert summary["production_active_version"] is None
    assert summary["paper_active_version"] == "paper_model"


def test_final_demo_requires_bundle_id_for_mode_b(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_final_demo.py", "--demo", "mode_b"])

    with pytest.raises(SystemExit) as exc_info:
        _parse_args()

    assert exc_info.value.code == 2


def test_final_demo_requires_bundle_id_for_all(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_final_demo.py", "--demo", "all"])

    with pytest.raises(SystemExit) as exc_info:
        _parse_args()

    assert exc_info.value.code == 2


def test_final_demo_allows_hot_without_bundle_id(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_final_demo.py", "--demo", "hot"])

    args = _parse_args()

    assert args.demo == "hot"
    assert args.bundle_id is None


def test_final_demo_accepts_explicit_bundle_id(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_final_demo.py", "--demo", "mode_b", "--bundle-id", "BUNDLE-TEST"],
    )

    args = _parse_args()

    assert args.demo == "mode_b"
    assert args.bundle_id == "BUNDLE-TEST"


def test_final_demo_accepts_no_write_summary(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_final_demo.py", "--demo", "hot", "--no-write-summary"],
    )

    args = _parse_args()

    assert args.demo == "hot"
    assert args.no_write_summary is True


def test_mode_b_demo_no_write_summary_skips_artifact(monkeypatch) -> None:
    def fake_status(*, bundle_id: str):
        return {
            "status": "PASS",
            "deploy_quality": "PASS",
            "broker_evidence": "PASS",
            "registry_mutated": False,
            "live_trading_allowed": False,
            "c12_backtest": {"verdict": "pass", "deployable": True},
            "production_registry": {"active_version": None},
            "paper_registry": {"active_version": "paper_model"},
        }

    monkeypatch.setattr("src.jobs.run_final_demo.build_service_status", fake_status)

    def fail_save(*_args, **_kwargs):
        raise AssertionError("_save_summary should not be called")

    monkeypatch.setattr("src.jobs.run_final_demo._save_summary", fail_save)

    summary = run_demo_mode_b(
        "week1_basic.yaml",
        bundle_id="BUNDLE-TEST",
        write_summary=False,
    )

    assert summary["status"] == "PASS"


def test_e2e_no_write_redirects_dead_letter_to_devnull(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeAdmission:
        def __init__(self, *args, dead_letter_path=None, **kwargs):
            captured["dead_letter_path"] = dead_letter_path

    class FakeGateway:
        def __init__(self, admission=None):
            self.admission = admission
            captured["admission"] = admission

    monkeypatch.setattr("src.runner.e2e_scenario_runner.EventAdmission", FakeAdmission)
    monkeypatch.setattr("src.runner.e2e_scenario_runner.EventGateway", FakeGateway)

    runner = E2EScenarioRunner(write_audit=False)
    gateway = runner._event_gateway_for_run()

    assert gateway.admission is captured["admission"]
    assert captured["dead_letter_path"] == Path(os.devnull)
    assert runner._event_injector_audit_path() == Path(os.devnull)


def test_mode_b_stage_blocking_treats_string_true_critical_alert_as_blocking() -> None:
    assert E2EScenarioRunner._is_mode_b_stage_blocking(
        {"status": "PASS", "critical_alert": "true"}
    )

"""Unit tests for paper bake-off helper scripts."""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "new" / "scripts"


def _load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tickers() -> list[str]:
    return [f"{idx:06d}" for idx in range(1, 31)]


def _cycle(index: int, *, policy: str, fetch_n: int, started_at: datetime) -> dict[str, Any]:
    tickers = {
        ticker: {
            "fetch_policy": policy,
            "fetch_n": fetch_n,
            "reason": policy,
        }
        for ticker in _tickers()
    }
    scores = {ticker: float(pos) for pos, ticker in enumerate(_tickers(), start=1)}
    return {
        "cycle_index": index,
        "status": "PASS",
        "started_at": started_at.isoformat(),
        "hot_path_bar_readiness": {
            "status": "PASS",
            "bar_warmup_topup": {
                "minute_bar_window_cache": {
                    "status": "PASS",
                    "reason": policy,
                    "tickers": tickers,
                    "failed_tickers": {},
                },
            },
        },
        "hot_result": {
            "quant_output": {"scores": scores},
            "final_decision": {"order_deltas": []},
        },
        "execution": {
            "status": "NOT_SUBMITTED_SHADOW",
            "execution_report": {
                "status": "NOT_SUBMITTED_SHADOW",
                "fills": [],
                "rejections": [],
            },
        },
    }


def _paper_report(bundle_id: str) -> dict[str, Any]:
    started = datetime.fromisoformat("2026-06-04T09:10:00+09:00")
    return {
        "status": "PASS",
        "generated_at": "2026-06-04T09:12:00+09:00",
        "runtime": {
            "kis_mode": "virtual",
            "live_enabled": False,
            "broker_submit_enabled": False,
            "shadow_only": True,
        },
        "params": {"required_bundle_id": bundle_id},
        "stages": {
            "cycles": {
                "items": [
                    _cycle(0, policy="cold", fetch_n=61, started_at=started),
                    _cycle(
                        1,
                        policy="incremental",
                        fetch_n=6,
                        started_at=started + timedelta(seconds=60),
                    ),
                ],
            },
        },
    }


def test_manifest_marks_missing_custom_registry_blocked() -> None:
    module = _load_module("write_paper_bakeoff_manifest")

    manifest = module.build_manifest(
        date="20260604",
        bundle_specs=["test_role:BUNDLE-NOT-EXIST:TRACK-NOT-EXIST"],
    )

    assert manifest["status"] == "BLOCKED"
    assert manifest["candidates"][0]["precheck_reason"] == "paper_candidate_registry_missing"
    assert manifest["safety_invariants"]["live_trading_allowed"] is False


def test_runner_dry_run_uses_manifest_without_external_call(tmp_path: Path) -> None:
    module = _load_module("run_paper_bakeoff_20260604")
    manifest = {
        "date": "20260604",
        "candidates": [
            {
                "bundle_id": "BUNDLE-TEST",
                "track_id": "TRACK-TEST",
                "registry_exists": True,
                "registry_dir": "artifacts/lgbm_paper_candidate/BUNDLE-TEST",
                "report_dir": str(tmp_path / "reports"),
                "stdout_stderr_path": str(tmp_path / "reports" / "stdout_stderr.log"),
                "args": {
                    "bundle_id": "BUNDLE-TEST",
                    "registry_dir": "artifacts/lgbm_paper_candidate/BUNDLE-TEST",
                    "end_date": "20260602",
                    "business_days": 260,
                    "max_tickers": 30,
                    "cycles": 10,
                    "interval_sec": 60,
                    "prelive_scope": "paper-rehearsal",
                    "report_dir": str(tmp_path / "reports"),
                    "track_id": "TRACK-TEST",
                },
            },
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = module.run_bakeoff(
        manifest_path=manifest_path,
        dry_run=True,
        python="/opt/anaconda3/envs/elephant/bin/python",
    )

    assert report["status"] == "DRY_RUN"
    assert report["results"][0]["status"] == "DRY_RUN"
    assert report["safety"]["env_read"] is False


def test_runner_marks_candidate_blocked_when_subprocess_setup_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module("run_paper_bakeoff_20260604")
    manifest = {
        "date": "20260604",
        "candidates": [
            {
                "bundle_id": "BUNDLE-TEST",
                "track_id": "TRACK-TEST",
                "registry_exists": True,
                "registry_dir": "artifacts/lgbm_paper_candidate/BUNDLE-TEST",
                "report_dir": str(tmp_path / "reports"),
                "stdout_stderr_path": str(tmp_path / "reports" / "stdout_stderr.log"),
                "args": {
                    "bundle_id": "BUNDLE-TEST",
                    "registry_dir": "artifacts/lgbm_paper_candidate/BUNDLE-TEST",
                    "report_dir": str(tmp_path / "reports"),
                    "track_id": "TRACK-TEST",
                },
            },
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def raise_os_error(*_args, **_kwargs):
        raise OSError("exec denied")

    monkeypatch.setattr(module.subprocess, "run", raise_os_error)

    report = module.run_bakeoff(
        manifest_path=manifest_path,
        dry_run=False,
        python="/missing/python",
    )

    assert report["status"] == "BLOCKED"
    assert report["results"][0]["status"] == "BLOCKED"
    assert report["results"][0]["reason"] == "runner_exception:OSError"
    assert report["results"][0]["error_type"] == "OSError"
    assert report["safety"]["env_read"] is False


def test_validate_bakeoff_accepts_strict_shadow_report(tmp_path: Path, monkeypatch) -> None:
    module = _load_module("validate_paper_bakeoff_report")
    monkeypatch.setitem(module.build_report.__globals__, "REPO_ROOT", tmp_path)
    bundle_id = "BUNDLE-TEST"
    report_dir = tmp_path / "paper_reports"
    report_dir.mkdir()
    (report_dir / "paper_auto_trade_20260604_091000.json").write_text(
        json.dumps(_paper_report(bundle_id)),
        encoding="utf-8",
    )
    manifest = {
        "date": "20260604",
        "candidates": [
            {
                "bundle_id": bundle_id,
                "registry_exists": True,
                "report_dir": str(report_dir),
            },
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = module.validate_bakeoff(
        manifest_path=manifest_path,
        generated_date="20260604",
        min_cycles=2,
    )

    assert report["status"] == "PASS"
    assert report["candidate_reports"][0]["status"] == "PASS"
    assert report["safety"]["external_api_called"] is False


def test_summarize_bakeoff_outputs_row_from_validation(tmp_path: Path, monkeypatch) -> None:
    validate_module = _load_module("validate_paper_bakeoff_report")
    summarize_module = _load_module("summarize_paper_bakeoff")
    monkeypatch.setitem(validate_module.build_report.__globals__, "REPO_ROOT", tmp_path)
    bundle_id = "BUNDLE-TEST"
    report_dir = tmp_path / "paper_reports"
    report_dir.mkdir()
    (report_dir / "paper_auto_trade_20260604_091000.json").write_text(
        json.dumps(_paper_report(bundle_id)),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({
            "date": "20260604",
            "candidates": [
                {
                    "bundle_id": bundle_id,
                    "registry_exists": True,
                    "report_dir": str(report_dir),
                },
            ],
        }),
        encoding="utf-8",
    )
    validation = validate_module.validate_bakeoff(
        manifest_path=manifest_path,
        generated_date="20260604",
        min_cycles=2,
    )

    summary = summarize_module.build_summary(validation)

    assert summary["status"] == "PASS"
    assert summary["rows"][0]["bundle"] == bundle_id
    assert summary["rows"][0]["warm_incremental_rate"] == 1.0
    assert summary["rows"][0]["broker_submits"] == 0


def test_summarize_bakeoff_reads_nested_failure_blockers() -> None:
    module = _load_module("summarize_paper_bakeoff")
    validation = {
        "status": "BLOCKED",
        "date": "20260604",
        "candidate_reports": [
            {
                "bundle_id": "BUNDLE-BLOCKED",
                "status": "BLOCKED",
                "failures": [
                    {
                        "path": "artifacts/reports/paper.json",
                        "reason": "strict_cadence_failed",
                        "blockers": ["missing_cycle", "gap_exceeded"],
                    },
                    {
                        "error_type": "ValueError",
                        "error": "bad report",
                    },
                ],
            },
        ],
    }

    summary = module.build_summary(validation)

    blockers = summary["rows"][0]["blockers"]
    assert "strict_cadence_failed" in blockers
    assert "missing_cycle" in blockers
    assert "gap_exceeded" in blockers
    assert "ValueError:bad report" in blockers

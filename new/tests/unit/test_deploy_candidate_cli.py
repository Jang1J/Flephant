from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import deploy_candidate  # noqa: E402


def _service_policy_evidence(tmp_path: Path, bundle_id: str) -> dict[str, object]:
    report = {
        "status": "PASS",
        "bundle_id": bundle_id,
        "gate": {"status": "PASS"},
        "policy_checks": {
            "deploy_candidate_by_service_policy": True,
            "no_naked_short_exposure": True,
            "order_caps_respected": True,
            "cash_guard_respected": True,
        },
        "order_stats": {"naked_short_attempts": 0},
    }
    report_path = (
        tmp_path
        / "artifacts"
        / "reports"
        / "service_policy_replay"
        / f"service_policy_replay_{bundle_id}.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
    report_path.write_bytes(raw)
    evidence = dict(report)
    evidence["service_policy_report_path"] = str(report_path)
    evidence["service_policy_report_sha256"] = hashlib.sha256(raw).hexdigest()
    return evidence


def test_deploy_candidate_dry_run_blocks_without_deployable_backtest(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    latest_path = tmp_path / "backtest_BUNDLE-TEST_latest.json"
    latest_payload = {
        "bundle_id": "BUNDLE-TEST",
        "verdict": "warn",
        "metrics": {"sr": -1.0},
        "regression_risk": {"flagged": True, "severity": "high"},
        "minute_bar_leakage_check": {"verdict": "pass"},
    }
    monkeypatch.setattr(deploy_candidate, "_latest_deployable_backtest", lambda _: (None, None))
    monkeypatch.setattr(
        deploy_candidate,
        "_latest_any_backtest",
        lambda _: (latest_path, latest_payload),
    )

    rc = deploy_candidate.main([
        "--bundle-id",
        "BUNDLE-TEST",
        "--dry-run",
        "--no-write-report",
    ])

    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["status"] == "BLOCKED"
    assert out["dry_run"] is True
    assert out["deployability"]["deployable"] is False
    assert out["deployability"]["registry_mutated"] is False
    assert out["deployability"]["latest_backtest"]["verdict"] == "warn"


def test_deploy_candidate_dry_run_passes_when_deployable_backtest_exists(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    backtest_path = tmp_path / "backtest_BUNDLE-TEST_pass.json"
    backtest_payload = {
        "bundle_id": "BUNDLE-TEST",
        "verdict": "pass",
        "metrics": {"sr": 1.0},
        "regression_risk": {"flagged": False, "severity": "low"},
        "minute_bar_leakage_check": {"verdict": "pass"},
        "feature_quality": {
            "dual_source_rows": 100,
            "dual_source_non_neutral_rows": 90,
            "exogenous_rows": 100,
            "exogenous_non_neutral_rows": 90,
        },
        "service_policy_replay": _service_policy_evidence(tmp_path, "BUNDLE-TEST"),
    }
    monkeypatch.setattr(
        deploy_candidate,
        "_latest_deployable_backtest",
        lambda _: (backtest_path, backtest_payload),
    )

    rc = deploy_candidate.main([
        "--bundle-id",
        "BUNDLE-TEST",
        "--dry-run",
        "--no-write-report",
    ])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["status"] == "PASS"
    assert out["deployability"]["deployable"] is True
    assert out["deployability"]["feature_quality_gate_pass"] is True
    assert out["deployability"]["service_policy_gate_pass"] is True
    assert out["deployability"]["registry_mutated"] is False

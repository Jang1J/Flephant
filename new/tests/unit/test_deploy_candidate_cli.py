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
    universe = sorted(_final_dataset_metadata()["requested_tickers"])
    universe_hash = hashlib.sha256(
        json.dumps(universe, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
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
        "universe": universe,
        "universe_count": len(universe),
        "universe_hash": universe_hash,
        "universe_policy": "final_dataset_gate",
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


def _final_dataset_metadata() -> dict[str, object]:
    tickers = [
        "005930", "000660", "042700", "403870", "058470",
        "329180", "042660", "010140", "009540", "267250",
        "006400", "051910", "373220", "096770", "247540",
        "012450", "047810", "079550", "298040", "272210",
        "105560", "055550", "086790", "024110", "000810",
        "005380", "000270", "012330", "011210", "086280",
    ]
    return {
        "train_start": "2025-05-09",
        "train_end": "2026-05-15",
        "data_source": "artifact_bars",
        "synthetic_fallback": False,
        "requested_tickers": tickers,
        "loaded_tickers": tickers,
        "missing_tickers": [],
        "n_tickers": len(tickers),
    }


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
    assert out["deployable"] is False
    assert out["service_policy_gate_pass"] is False
    assert out["registry_mutated"] is False
    assert out["live_trading_allowed"] is False
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
        "candidate_model_metadata": _final_dataset_metadata(),
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
    assert out["deployable"] is True
    assert out["service_policy_gate_pass"] is True
    assert out["registry_mutated"] is False
    assert out["live_trading_allowed"] is False
    assert out["deployability"]["deployable"] is True
    assert out["deployability"]["feature_quality_gate_pass"] is True
    assert out["deployability"]["service_policy_gate_pass"] is True
    assert out["deployability"]["final_dataset_gate_pass"] is True
    assert out["deployability"]["registry_mutated"] is False


def test_deploy_candidate_treats_string_false_regression_flag_as_false(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    backtest_path = tmp_path / "backtest_BUNDLE-TEST_pass.json"
    backtest_payload = {
        "bundle_id": "BUNDLE-TEST",
        "verdict": "pass",
        "metrics": {"sr": 1.0},
        "regression_risk": {"flagged": "false", "severity": "low", "evidence": []},
        "minute_bar_leakage_check": {"verdict": "pass"},
        "feature_quality": {
            "dual_source_rows": 100,
            "dual_source_non_neutral_rows": 90,
            "exogenous_rows": 100,
            "exogenous_non_neutral_rows": 90,
        },
        "service_policy_replay": _service_policy_evidence(tmp_path, "BUNDLE-TEST"),
        "candidate_model_metadata": _final_dataset_metadata(),
    }
    monkeypatch.setattr(
        deploy_candidate,
        "_latest_deployable_backtest",
        lambda _: (backtest_path, backtest_payload),
    )

    captured = {}

    class _FakeDeployer:
        def deploy(self, **kwargs):
            captured["regression_risk"] = kwargs["regression_risk"]
            return {
                "status": "deployed",
                "deploy_status": "deployed",
                "bundle_id": kwargs["bundle_id"],
            }

    monkeypatch.setattr(deploy_candidate, "ModeBDeployer", _FakeDeployer)

    rc = deploy_candidate.main([
        "--bundle-id",
        "BUNDLE-TEST",
        "--confirm-deploy",
        "DEPLOY_CANDIDATE_OK",
        "--no-write-report",
    ])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["status"] == "PASS"
    assert captured["regression_risk"].flagged is False


def test_deploy_candidate_blocks_non_dry_run_without_confirm_phrase(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    backtest_path = tmp_path / "backtest_BUNDLE-TEST_pass.json"
    backtest_payload = {
        "bundle_id": "BUNDLE-TEST",
        "verdict": "pass",
        "metrics": {"sr": 1.0},
        "regression_risk": {"flagged": False, "severity": "low", "evidence": []},
        "minute_bar_leakage_check": {"verdict": "pass"},
        "feature_quality": {
            "dual_source_rows": 100,
            "dual_source_non_neutral_rows": 90,
            "exogenous_rows": 100,
            "exogenous_non_neutral_rows": 90,
        },
        "service_policy_replay": _service_policy_evidence(tmp_path, "BUNDLE-TEST"),
        "candidate_model_metadata": _final_dataset_metadata(),
    }
    monkeypatch.setattr(
        deploy_candidate,
        "_latest_deployable_backtest",
        lambda _: (backtest_path, backtest_payload),
    )

    class _FailIfCalled:
        def deploy(self, **kwargs):
            raise AssertionError("deploy should not be called without confirm phrase")

    monkeypatch.setattr(deploy_candidate, "ModeBDeployer", _FailIfCalled)

    rc = deploy_candidate.main([
        "--bundle-id",
        "BUNDLE-TEST",
        "--no-write-report",
    ])

    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["status"] == "BLOCKED"
    assert out["reason"] == "production_deploy_confirmation_required"
    assert out["registry_mutated"] is False
    assert out["live_trading_allowed"] is False

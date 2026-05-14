from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path


def _load_script(name: str):
    script_path = Path(__file__).resolve().parents[2] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cost_aware_retraining_plan_is_blocked_without_evidence(tmp_path, monkeypatch):
    mod = _load_script("cost_aware_retraining_plan")
    monkeypatch.setattr(mod, "ROOT", tmp_path)

    plan = mod.build_retraining_plan(
        bundle_id="BUNDLE-TEST",
        write_report=False,
    )

    assert plan["status"] == "BLOCKED"
    assert "phase2_feature_backfill_not_pass" in plan["blockers"]
    assert "service_policy_replay_not_pass" in plan["blockers"]
    assert plan["read_only"] is True
    assert plan["registry_mutated"] is False


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_cost_aware_plan_uses_newer_phase2_backfill_over_stale_input(
    tmp_path,
    monkeypatch,
):
    mod = _load_script("cost_aware_retraining_plan")
    monkeypatch.setattr(mod, "ROOT", tmp_path)

    phase2 = (
        tmp_path
        / "artifacts"
        / "reports"
        / "phase2_feature_backfill"
        / "phase2_feature_backfill_new.json"
    )
    stale_input = (
        tmp_path
        / "artifacts"
        / "reports"
        / "phase2_input_readiness"
        / "phase2_input_readiness_old.json"
    )
    service = (
        tmp_path
        / "artifacts"
        / "reports"
        / "service_policy_replay"
        / "service_policy_replay_BUNDLE-TEST_new.json"
    )
    label_scan = (
        tmp_path
        / "artifacts"
        / "reports"
        / "label_horizon_scan"
        / "cost_aware_label_horizon_scan_new.json"
    )

    _write_json(phase2, {"status": "PASS", "coverage": {"dual_source_file_coverage": 1.0}})
    _write_json(
        stale_input,
        {
            "status": "BLOCKED",
            "blockers": ["dual_source_raw_archive_coverage_below_threshold"],
        },
    )
    _write_json(
        service,
        {
            "status": "PASS",
            "metrics": {"sr": 1.0, "mdd": -0.01},
            "gate": {"blockers": []},
        },
    )
    _write_json(
        label_scan,
        {
            "status": "WARN",
            "best_horizon": "session_close",
            "deployable_label_recommendation": False,
            "horizons": [
                {"horizon": "5", "mean_net_bps": -14.0, "positive_net_rate": 0.25},
                {
                    "horizon": "session_close",
                    "mean_net_bps": 15.5,
                    "positive_net_rate": 0.51,
                },
            ],
        },
    )
    os.utime(stale_input, (1_000_000_000, 1_000_000_000))
    os.utime(phase2, (1_000_000_100, 1_000_000_100))

    plan = mod.build_retraining_plan(bundle_id="BUNDLE-TEST", write_report=False)

    assert plan["status"] == "READY"
    assert "phase2_input_readiness_not_pass" not in plan["blockers"]
    assert plan["evidence"]["phase2_input_readiness"]["blocking"] is False
    assert (
        plan["evidence"]["phase2_input_readiness"][
            "superseded_by_phase2_feature_backfill"
        ]
        is True
    )
    assert plan["recommended_experiment"]["target_horizon"] == "session_close"
    assert (
        plan["recommended_experiment"]["target_col_override"]
        == "label_session_close_net_ret"
    )
    assert plan["recommended_experiment"]["active_horizon_mean_net_bps"] == -14.0

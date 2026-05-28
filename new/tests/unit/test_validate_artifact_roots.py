from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_script(name: str):
    script_path = Path(__file__).resolve().parents[2] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base_policy() -> dict:
    return {
        "production_model_registry": "artifacts/lgbm",
        "paper_model_registry": "artifacts/lgbm_paper",
        "paper_candidate_registry_root": "artifacts/lgbm_paper_candidate",
        "candidate_bundle_root": "artifacts/bundles",
        "research_model_registry_roots": ["artifacts/lgbm_research"],
        "experiment_report_roots": ["artifacts/reports/rolling_window_ic"],
        "experiment_entrypoints": [],
        "paper_entrypoints": [],
    }


def test_root_separation_rejects_nested_research_under_production(tmp_path: Path) -> None:
    mod = _load_script("validate_artifact_roots")
    policy = _base_policy()
    policy["research_model_registry_roots"] = ["artifacts/lgbm/research"]

    blockers, warnings, _ = mod.check_root_separation(policy, root=tmp_path)

    assert warnings == []
    assert any(item["reason"] == "model_roots_overlap" for item in blockers)


def test_experiment_entrypoint_rejects_production_registry_default(tmp_path: Path) -> None:
    mod = _load_script("validate_artifact_roots")
    script = tmp_path / "new/scripts/bad_experiment.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "parser.add_argument('--registry-root', default='artifacts/lgbm')\n",
        encoding="utf-8",
    )
    policy = _base_policy()
    policy["experiment_entrypoints"] = ["new/scripts/bad_experiment.py"]

    blockers, warnings = mod.scan_experiment_entrypoints(policy, root=tmp_path)

    assert warnings == []
    assert blockers == [{
        "check": "experiment_entrypoint",
        "reason": "registry_default_points_to_deploy_root",
        "path": "new/scripts/bad_experiment.py",
        "default": "artifacts/lgbm",
    }]


def test_paper_entrypoint_rejects_production_registry_default(tmp_path: Path) -> None:
    mod = _load_script("validate_artifact_roots")
    script = tmp_path / "new/scripts/bad_paper.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "parser.add_argument('--registry-dir', default='artifacts/lgbm')\n",
        encoding="utf-8",
    )
    policy = _base_policy()
    policy["paper_entrypoints"] = ["new/scripts/bad_paper.py"]

    blockers, warnings = mod.scan_paper_entrypoints(policy, root=tmp_path)

    assert warnings == []
    assert blockers == [{
        "check": "paper_entrypoint",
        "reason": "paper_registry_default_points_to_production",
        "path": "new/scripts/bad_paper.py",
        "default": "artifacts/lgbm",
    }]


def test_production_registry_warns_on_inactive_candidate_missing_artifacts(tmp_path: Path) -> None:
    mod = _load_script("validate_artifact_roots")
    registry_dir = tmp_path / "artifacts" / "lgbm"
    registry_dir.mkdir(parents=True)
    (registry_dir / "registry.json").write_text(
        json.dumps(
            {
                "active_version": None,
                "versions": [
                    {
                        "version": "live_20260527",
                        "status": "candidate",
                        "model_path": "artifacts/lgbm/live_20260527.pkl",
                        "metadata_path": "artifacts/lgbm/live_20260527_metadata.json",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    blockers, warnings, state = mod.check_production_registry_state(
        _base_policy(),
        root=tmp_path,
    )

    assert blockers == []
    assert state["inactive_candidate_drift_count"] == 1
    assert state["inactive_candidate_missing_artifact_count"] == 2
    assert state["inactive_candidate_missing_artifacts"][0]["reason"] == (
        "inactive_candidate_artifact_missing"
    )
    assert any(
        item["reason"] == "inactive_candidate_artifacts_missing"
        for item in warnings
    )


def test_production_registry_does_not_warn_for_complete_inactive_candidate(tmp_path: Path) -> None:
    mod = _load_script("validate_artifact_roots")
    registry_dir = tmp_path / "artifacts" / "lgbm"
    registry_dir.mkdir(parents=True)
    (registry_dir / "complete.pkl").write_text("model", encoding="utf-8")
    (registry_dir / "complete_metadata.json").write_text("{}", encoding="utf-8")
    (registry_dir / "registry.json").write_text(
        json.dumps(
            {
                "active_version": None,
                "versions": [
                    {
                        "version": "complete",
                        "status": "candidate",
                        "model_path": "artifacts/lgbm/complete.pkl",
                        "metadata_path": "artifacts/lgbm/complete_metadata.json",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    blockers, warnings, state = mod.check_production_registry_state(
        _base_policy(),
        root=tmp_path,
    )

    assert blockers == []
    assert warnings == []
    assert state["inactive_candidate_drift_count"] == 1
    assert "inactive_candidate_missing_artifact_count" not in state

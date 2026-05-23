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

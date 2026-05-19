"""stage_lgbm_candidate_bundle CLI helper tests."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_script_module():
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "stage_lgbm_candidate_bundle.py"
    )
    spec = importlib.util.spec_from_file_location("stage_lgbm_candidate_bundle", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stage_existing_lgbm_candidate_bundle_patches_bundle_metadata(
    monkeypatch,
    tmp_path: Path,
):
    mod = _load_script_module()
    monkeypatch.setattr(
        mod.prelive_gate,
        "_final_dataset_gate_result",
        lambda payload: {"status": "PASS", "blockers": []},
    )

    lgbm_dir = tmp_path / "artifacts" / "lgbm"
    lgbm_dir.mkdir(parents=True)
    model_path = lgbm_dir / "live_20260515.pkl"
    model_path.write_bytes(b"model")
    registry = {
        "active_version": None,
        "versions": [
            {
                "version": "live_20260515",
                "status": "candidate",
                "bundle_id": None,
                "model_path": "artifacts/lgbm/live_20260515.pkl",
                "metadata_path": "artifacts/lgbm/live_20260515_metadata.json",
                "created_at": "2026-05-18T00:14:31+09:00",
                "synthetic_fallback": False,
                "data_source": "artifact_bars",
                "feature_cols": ["f1", "f2"],
                "target_col": "label_5m_ret",
            }
        ],
    }
    (lgbm_dir / "registry.json").write_text(
        json.dumps(registry, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    report = mod.stage_lgbm_candidate_bundle(
        bundle_id="BUNDLE-20260518-TEST0001",
        version="live_20260515",
        repo_root=tmp_path,
        write_report=False,
    )

    assert report["status"] == "PASS"
    assert report["registry_mutated"] is False
    staged_meta_path = (
        tmp_path
        / "artifacts"
        / "bundles"
        / "BUNDLE-20260518-TEST0001"
        / "lgbm"
        / "latest_model_metadata.json"
    )
    staged_meta = json.loads(staged_meta_path.read_text(encoding="utf-8"))
    assert staged_meta["bundle_id"] == "BUNDLE-20260518-TEST0001"
    assert staged_meta["staged_from_existing_version"] == "live_20260515"
    assert (
        tmp_path
        / "artifacts"
        / "bundles"
        / "BUNDLE-20260518-TEST0001"
        / "lgbm"
        / "latest_model.pkl"
    ).read_bytes() == b"model"

    registry_after = json.loads((lgbm_dir / "registry.json").read_text(encoding="utf-8"))
    assert registry_after["versions"][0]["bundle_id"] is None


def test_stage_existing_lgbm_candidate_blocks_non_candidate(
    monkeypatch,
    tmp_path: Path,
):
    mod = _load_script_module()
    monkeypatch.setattr(
        mod.prelive_gate,
        "_final_dataset_gate_result",
        lambda payload: {"status": "PASS", "blockers": []},
    )

    lgbm_dir = tmp_path / "artifacts" / "lgbm"
    lgbm_dir.mkdir(parents=True)
    model_path = lgbm_dir / "active.pkl"
    model_path.write_bytes(b"model")
    (lgbm_dir / "registry.json").write_text(
        json.dumps(
            {
                "active_version": "active",
                "versions": [
                    {
                        "version": "active",
                        "status": "active",
                        "model_path": "artifacts/lgbm/active.pkl",
                        "synthetic_fallback": False,
                        "data_source": "artifact_bars",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    report = mod.stage_lgbm_candidate_bundle(
        bundle_id="BUNDLE-20260518-TEST0002",
        version="active",
        repo_root=tmp_path,
        write_report=False,
    )

    assert report["status"] == "BLOCKED"
    assert "not_candidate_status" in report["blockers"]


def test_stage_existing_lgbm_candidate_from_research_registry(
    monkeypatch,
    tmp_path: Path,
):
    mod = _load_script_module()
    monkeypatch.setattr(
        mod.prelive_gate,
        "_final_dataset_gate_result",
        lambda payload: {"status": "PASS", "blockers": []},
    )

    research_dir = tmp_path / "artifacts" / "lgbm_research" / "BUNDLE-TEST"
    research_dir.mkdir(parents=True)
    model_path = research_dir / "cost-aware.pkl"
    model_path.write_bytes(b"research-model")
    (research_dir / "registry.json").write_text(
        json.dumps(
            {
                "active_version": None,
                "versions": [
                    {
                        "version": "cost-aware",
                        "status": "candidate",
                        "bundle_id": "BUNDLE-TEST",
                        "model_path": "artifacts/lgbm_research/BUNDLE-TEST/cost-aware.pkl",
                        "metadata_path": "artifacts/lgbm_research/BUNDLE-TEST/cost-aware_metadata.json",
                        "created_at": "2026-05-18T03:17:00+09:00",
                        "synthetic_fallback": False,
                        "data_source": "artifact_bars",
                        "feature_cols": ["f1", "f2"],
                        "target_col": "label_195m_net_ret",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    report = mod.stage_lgbm_candidate_bundle(
        bundle_id="BUNDLE-20260518-RESEARCH",
        version="cost-aware",
        registry_dir="artifacts/lgbm_research/BUNDLE-TEST",
        repo_root=tmp_path,
        write_report=False,
    )

    assert report["status"] == "PASS"
    assert report["source_registry_dir"] == "artifacts/lgbm_research/BUNDLE-TEST"
    staged_model = (
        tmp_path
        / "artifacts"
        / "bundles"
        / "BUNDLE-20260518-RESEARCH"
        / "lgbm"
        / "latest_model.pkl"
    )
    assert staged_model.read_bytes() == b"research-model"

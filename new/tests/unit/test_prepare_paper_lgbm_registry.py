"""prepare_paper_lgbm_registry helper tests."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_script_module():
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "prepare_paper_lgbm_registry.py"
    )
    spec = importlib.util.spec_from_file_location("prepare_paper_lgbm_registry", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _metadata(version: str = "candidate_v1") -> dict:
    return {
        "version": version,
        "bundle_id": "BUNDLE-20260518-TEST0001",
        "status": "candidate",
        "train_start": "2025-05-09",
        "train_end": "2026-05-15",
        "feature_cols": ["f1", "f2"],
        "metrics": {"ic": 0.1, "sr": 1.2},
        "commit_hash": "abc123",
        "data_version": "artifact_bars_249d",
        "created_at": "2026-05-18T04:00:00+09:00",
        "label_horizon_bars": 195,
        "target_col": "label_195m_net_ret",
        "label_generation_version": "session_local_v2",
        "label_session_scope": "ticker_trading_day",
        "model_path": "artifacts/bundles/BUNDLE-20260518-TEST0001/lgbm/latest_model.pkl",
        "metadata_path": (
            "artifacts/bundles/BUNDLE-20260518-TEST0001/lgbm/latest_model_metadata.json"
        ),
    }


def test_prepare_paper_registry_from_staged_bundle_lgbm(tmp_path: Path):
    mod = _load_script_module()
    source = (
        tmp_path
        / "artifacts"
        / "bundles"
        / "BUNDLE-20260518-TEST0001"
        / "lgbm"
    )
    source.mkdir(parents=True)
    (source / "latest_model.pkl").write_bytes(b"model")
    (source / "latest_model_metadata.json").write_text(
        json.dumps(_metadata(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    target = tmp_path / "artifacts" / "lgbm_paper_candidate"

    report = mod.prepare(
        candidate_version="candidate_v1",
        source_dir=str(source),
        target_dir=str(target),
        force=True,
        confirm_phrase="PREPARE_PAPER_LGBM_OK",
    )

    assert report["status"] == "PASS"
    assert report["source_kind"] == "bundle_lgbm"
    assert report["active_version"] == "candidate_v1"
    assert report["paper_only_activation"] is True
    assert report["live_trading_allowed"] is False
    assert report["production_registry_mutated"] is False

    registry = json.loads((target / "registry.json").read_text(encoding="utf-8"))
    assert registry["active_version"] == "candidate_v1"
    assert registry["paper_only_registry"] is True
    assert registry["versions"][0]["bundle_id"] == "BUNDLE-20260518-TEST0001"
    assert registry["versions"][0]["status"] == "active"

    metadata = json.loads((target / "candidate_v1_metadata.json").read_text(encoding="utf-8"))
    assert metadata["paper_only_activation"] is True
    assert metadata["model_path"].endswith("artifacts/lgbm_paper_candidate/candidate_v1.pkl")
    assert (target / "latest_model.pkl").exists()

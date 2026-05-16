from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import model_registry_readiness  # noqa: E402


class _AbiMismatchRegistry:
    base_dir = Path("artifacts/lgbm_paper")

    def __init__(self, artifacts_dir=None):
        if artifacts_dir is not None:
            self.base_dir = Path(artifacts_dir)

    def _read_registry_index(self):
        return {
            "active_version": "v-active",
            "versions": [{"version": "v-active"}],
        }

    def load_version(self, version):
        raise ValueError(
            "numpy.dtype size changed, may indicate binary incompatibility"
        )


def test_model_registry_readiness_reports_runtime_hint_on_abi_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(model_registry_readiness, "ModelRegistry", _AbiMismatchRegistry)

    report = model_registry_readiness.build_report(
        registry_dir="artifacts/lgbm_paper",
        require_active=True,
    )

    assert report["status"] == "BLOCKED"
    assert report["blockers"] == ["pickle_load_failed"]
    pickle_load = report["checks"]["pickle_load"]
    assert pickle_load["runtime_mismatch_suspected"] is True
    assert pickle_load["runtime"]["python_executable"]
    assert "/opt/anaconda3/envs/elephant/bin/python" in pickle_load["operator_hint"]


class _StringFalseDeployQualityRegistry:
    def __init__(self, artifacts_dir=None):
        self.base_dir = Path(artifacts_dir or "artifacts/lgbm_paper")

    def _read_registry_index(self):
        return {
            "active_version": "v-active",
            "versions": [{"version": "v-active"}],
        }

    def load_version(self, version):
        model_path = self.base_dir / "model.pkl"
        metadata_path = self.base_dir / "metadata.json"
        model_path.write_bytes(b"fake")
        metadata_path.write_text("{}", encoding="utf-8")
        metadata = {
            "version": version,
            "train_start": "2026-05-01",
            "train_end": "2026-05-08",
            "feature_cols": ["ret_1m", "news_score_t"],
            "metrics": {"deploy_quality": "false", "metric_scope": "paper"},
            "commit_hash": "abc123",
            "data_version": "test",
            "created_at": "2026-05-15T00:00:00+09:00",
            "label_horizon_bars": 5,
            "label_generation_version": "test",
            "label_session_scope": "regular",
            "model_path": str(model_path.name),
            "metadata_path": str(metadata_path.name),
            "feature_manifest": {
                "feature_count": 2,
                "requires_exogenous": True,
            },
        }
        return object(), metadata


def test_model_registry_readiness_treats_string_false_deploy_quality_as_warn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        model_registry_readiness,
        "ModelRegistry",
        _StringFalseDeployQualityRegistry,
    )

    report = model_registry_readiness.build_report(
        registry_dir=str(tmp_path),
        require_active=True,
    )

    assert report["status"] == "WARN"
    assert report["blockers"] == []
    assert "candidate_not_marked_deploy_quality" in report["warnings"]
    assert report["checks"]["deploy_quality"]["status"] == "WARN"
    assert report["checks"]["deploy_quality"]["value"] is False

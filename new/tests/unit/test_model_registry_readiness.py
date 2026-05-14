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

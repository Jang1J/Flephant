from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "new" / "scripts" / "build_sanitized_release.py"


def _load_release_module():
    spec = importlib.util.spec_from_file_location("build_sanitized_release", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_root_operational_scripts_are_release_sources() -> None:
    module = _load_release_module()

    for rel_path in (".gitignore", "init.sh", "demo.sh", "eval.sh", "smoke.sh"):
        assert module._is_source_candidate(rel_path), rel_path


def test_sanitized_release_includes_root_scripts_and_excludes_forbidden(tmp_path: Path) -> None:
    module = _load_release_module()
    zip_path = tmp_path / "release.zip"
    manifest_path = tmp_path / "release.manifest.json"

    manifest = module.build_release(zip_path, manifest_path)

    assert manifest["status"] == "PASS"
    assert manifest["forbidden_entries"] == []
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())

    for rel_path in (".gitignore", "init.sh", "demo.sh", "eval.sh", "smoke.sh"):
        assert rel_path in names
    assert any(
        name.startswith("artifacts/reports/paper_trading/")
        and name.endswith(".json")
        for name in names
    )
    assert ".env" not in names
    assert not any(name.startswith(".git/") for name in names)

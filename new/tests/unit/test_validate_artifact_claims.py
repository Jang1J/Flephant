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


def _write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def test_scan_json_report_flags_pass_missing_path(tmp_path: Path):
    mod = _load_script("validate_artifact_claims")
    report = tmp_path / "artifacts/reports/current.json"
    _write_json(
        report,
        {
            "status": "PASS",
            "report_path": "artifacts/reports/missing_child.json",
        },
    )

    checked, issues = mod.scan_json_report(report, root=tmp_path)

    assert checked == 1
    assert issues == [{
        "report": str(report),
        "key": "report_path",
        "value": "artifacts/reports/missing_child.json",
    }]


def test_scan_json_report_ignores_deleted_path_records(tmp_path: Path):
    mod = _load_script("validate_artifact_claims")
    report = tmp_path / "artifacts/reports/cleanup.json"
    _write_json(
        report,
        {
            "status": "PASS",
            "deleted": [
                {"path": "artifacts/reports/already_deleted.json"},
            ],
        },
    )

    checked, issues = mod.scan_json_report(report, root=tmp_path)

    assert checked == 0
    assert issues == []


def test_scan_json_report_accepts_existing_claimed_path(tmp_path: Path):
    mod = _load_script("validate_artifact_claims")
    child = tmp_path / "artifacts/reports/child.json"
    _write_json(child, {"status": "PASS"})
    report = tmp_path / "artifacts/reports/current.json"
    _write_json(
        report,
        {
            "status": "PASS",
            "report_path": "artifacts/reports/child.json",
        },
    )

    checked, issues = mod.scan_json_report(report, root=tmp_path)

    assert checked == 1
    assert issues == []

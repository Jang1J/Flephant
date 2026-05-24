from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path


def _load_validator_module():
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "validate_artifact_claims.py"
    )
    spec = importlib.util.spec_from_file_location("validate_artifact_claims", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_artifact_claims_detects_missing_path(tmp_path, monkeypatch) -> None:
    validator = _load_validator_module()
    repo_root = tmp_path
    source_dir = repo_root / "new" / "docs" / "meetings"
    source_dir.mkdir(parents=True)
    (source_dir / "report.md").write_text(
        "missing: artifacts/reports/nope.json\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(validator, "REPO_ROOT", repo_root)

    report = validator.build_report(bundle_id="BUNDLE-TEST")

    assert report["status"] == "BLOCKED"
    assert report["missing_count"] == 1
    assert report["missing_claims"][0]["artifact_path"] == "artifacts/reports/nope.json"


def test_artifact_claims_passes_existing_path(tmp_path, monkeypatch) -> None:
    validator = _load_validator_module()
    repo_root = tmp_path
    artifact = repo_root / "artifacts" / "reports" / "ok.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    source_dir = repo_root / "new" / "docs" / "meetings"
    source_dir.mkdir(parents=True)
    (source_dir / "report.md").write_text(
        "exists: artifacts/reports/ok.json\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(validator, "REPO_ROOT", repo_root)

    report = validator.build_report(bundle_id="BUNDLE-TEST")

    assert report["status"] == "PASS"
    assert report["claim_count"] == 1
    assert report["missing_count"] == 0


def test_artifact_claims_validates_dist_validation_zips(tmp_path, monkeypatch) -> None:
    validator = _load_validator_module()
    repo_root = tmp_path
    artifact = repo_root / "artifacts" / "reports" / "ok.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    source_dir = repo_root / "new" / "docs" / "meetings"
    source_dir.mkdir(parents=True)
    (source_dir / "report.md").write_text(
        "exists: artifacts/reports/ok.json\n",
        encoding="utf-8",
    )
    dist_dir = repo_root / "dist"
    dist_dir.mkdir(parents=True)
    zip_path = dist_dir / "validation_BUNDLE-TEST.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("BUNDLE-TEST/report.json", "{}")
    monkeypatch.setattr(validator, "REPO_ROOT", repo_root)

    report = validator.build_report(
        bundle_id="BUNDLE-TEST",
        include_validation_zip=True,
    )

    assert report["status"] == "PASS"
    assert report["low_signal"] is False
    assert len(report["zip_checks"]) == 1
    assert report["zip_checks"][0]["path"] == "dist/validation_BUNDLE-TEST.zip"
    assert report["zip_checks"][0]["contains_bundle_id"] is True
    assert report["zip_checks"][0]["forbidden_env_entries"] == []


def test_artifact_claims_package_root_validates_inside_package(tmp_path) -> None:
    validator = _load_validator_module()
    package_root = tmp_path / "review_package"
    artifact = package_root / "artifacts" / "reports" / "ok.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    source_dir = package_root / "new" / "docs" / "meetings"
    source_dir.mkdir(parents=True)
    (source_dir / "report.md").write_text(
        "exists: artifacts/reports/ok.json\n",
        encoding="utf-8",
    )

    report = validator.build_report(
        bundle_id="BUNDLE-TEST",
        package_root=package_root,
    )

    assert report["status"] == "PASS"
    assert report["validation_scope"] == "package_root"
    assert report["reproducible_from_this_package"] is True
    assert report["missing_count"] == 0


def test_artifact_claims_package_root_blocks_missing_inside_package(tmp_path) -> None:
    validator = _load_validator_module()
    package_root = tmp_path / "review_package"
    source_dir = package_root / "new" / "docs" / "meetings"
    source_dir.mkdir(parents=True)
    (source_dir / "report.md").write_text(
        "missing: artifacts/reports/nope.json\n",
        encoding="utf-8",
    )

    report = validator.build_report(
        bundle_id="BUNDLE-TEST",
        package_root=package_root,
    )

    assert report["status"] == "BLOCKED"
    assert report["validation_scope"] == "package_root"
    assert report["missing_count"] == 1


def test_artifact_claims_warns_when_no_sources_found(tmp_path) -> None:
    validator = _load_validator_module()
    package_root = tmp_path / "empty_review_package"
    package_root.mkdir()

    report = validator.build_report(
        bundle_id="BUNDLE-TEST",
        package_root=package_root,
    )

    assert report["status"] == "WARN"
    assert report["source_count"] == 0
    assert report["claim_count"] == 0
    assert report["low_signal"] is True
    assert report["warnings"] == [{
        "reason": "no_sources_scanned",
        "source_count": 0,
        "claim_count": 0,
    }]


def test_artifact_claims_warns_when_sources_have_no_claims(tmp_path) -> None:
    validator = _load_validator_module()
    package_root = tmp_path / "review_package"
    source_dir = package_root / "new" / "docs" / "meetings"
    source_dir.mkdir(parents=True)
    (source_dir / "report.md").write_text(
        "no artifact path in this source\n",
        encoding="utf-8",
    )

    report = validator.build_report(
        bundle_id="BUNDLE-TEST",
        package_root=package_root,
    )

    assert report["status"] == "WARN"
    assert report["source_count"] == 1
    assert report["claim_count"] == 0
    assert report["low_signal"] is True
    assert report["warnings"] == [{
        "reason": "no_artifact_claims_found",
        "source_count": 1,
        "claim_count": 0,
    }]

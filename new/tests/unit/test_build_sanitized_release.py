from __future__ import annotations

import json
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


def test_sanitized_release_includes_root_scripts_and_excludes_forbidden(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_release_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)

    for rel_path in (".gitignore", "init.sh", "demo.sh", "eval.sh", "smoke.sh"):
        path = tmp_path / rel_path
        path.write_text("# test\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=never-package\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("SECRET=also-never-package\n", encoding="utf-8")
    report_dir = tmp_path / "artifacts" / "reports" / "paper_trading"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.json").write_text(
        json.dumps({"status": "PASS"}),
        encoding="utf-8",
    )
    build_news_dir = tmp_path / "artifacts" / "reports" / "build_news_dart_archive"
    build_news_dir.mkdir(parents=True, exist_ok=True)
    (build_news_dir / "build_news_dart_archive_report.json").write_text(
        json.dumps({"status": "PASS", "total_events": 1}),
        encoding="utf-8",
    )
    readiness_dir = tmp_path / "artifacts" / "reports" / "data_readiness"
    readiness_dir.mkdir(parents=True, exist_ok=True)
    (readiness_dir / "data_readiness_report.json").write_text(
        json.dumps({"status": "PASS", "runtime": {"secret_presence": {"NAVER_CLIENT_SECRET": True}}}),
        encoding="utf-8",
    )
    llm_report_dir = tmp_path / "artifacts" / "reports" / "llm_real_api_smoke"
    llm_report_dir.mkdir(parents=True, exist_ok=True)
    (llm_report_dir / "llm_report.json").write_text(
        json.dumps({"status": "PASS"}),
        encoding="utf-8",
    )
    threshold_dir = tmp_path / "artifacts" / "reports" / "research_threshold_sweep"
    threshold_dir.mkdir(parents=True, exist_ok=True)
    (threshold_dir / "threshold_sweep.json").write_text(
        json.dumps({"status": "PASS", "read_only": True}),
        encoding="utf-8",
    )
    perf_opt_dir = tmp_path / "artifacts" / "reports" / "performance_optimization"
    perf_opt_dir.mkdir(parents=True, exist_ok=True)
    (perf_opt_dir / "service_policy_optimization.json").write_text(
        json.dumps({"status": "DONE_WITH_CONCERNS", "registry_mutated": False}),
        encoding="utf-8",
    )
    (perf_opt_dir / "service_policy_optimization.md").write_text(
        "# optimization\n",
        encoding="utf-8",
    )
    cleanup_dir = tmp_path / "artifacts" / "reports" / "cleanup"
    cleanup_dir.mkdir(parents=True, exist_ok=True)
    (cleanup_dir / "cleanup_report.json").write_text(
        json.dumps({"status": "PASS", "destructive_delete": False}),
        encoding="utf-8",
    )
    gpt_audit_dir = tmp_path / "artifacts" / "reports" / "gpt_pro_audit"
    gpt_audit_dir.mkdir(parents=True, exist_ok=True)
    (gpt_audit_dir / "gpt_pro_audit.json").write_text(
        json.dumps({"status": "PASS"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        module,
        "_git_file_candidates",
        lambda: [
            ".gitignore",
            "init.sh",
            "demo.sh",
            "eval.sh",
            "smoke.sh",
            ".env",
            ".env.example",
        ],
    )

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
    assert any(
        name.startswith("artifacts/reports/build_news_dart_archive/")
        and name.endswith(".json")
        for name in names
    )
    assert any(
        name.startswith("artifacts/reports/data_readiness/")
        and name.endswith(".json")
        for name in names
    )
    assert any(
        name.startswith("artifacts/reports/llm_real_api_smoke/")
        and name.endswith(".json")
        for name in names
    )
    assert any(
        name.startswith("artifacts/reports/research_threshold_sweep/")
        and name.endswith(".json")
        for name in names
    )
    assert any(
        name.startswith("artifacts/reports/performance_optimization/")
        and name.endswith(".json")
        for name in names
    )
    assert any(
        name.startswith("artifacts/reports/performance_optimization/")
        and name.endswith(".md")
        for name in names
    )
    assert any(
        name.startswith("artifacts/reports/cleanup/")
        and name.endswith(".json")
        for name in names
    )
    assert any(
        name.startswith("artifacts/reports/gpt_pro_audit/")
        and name.endswith(".json")
        for name in names
    )
    assert ".env" not in names
    assert ".env.example" not in names
    assert not any(name.startswith(".git/") for name in names)


def test_sanitized_release_fails_on_invalid_json_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_release_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "_git_file_candidates", lambda: [])

    invalid_report = (
        tmp_path
        / "artifacts"
        / "reports"
        / "prelive_gate"
        / "prelive_gate_invalid.json"
    )
    invalid_report.parent.mkdir(parents=True, exist_ok=True)
    invalid_report.write_text("{invalid-json", encoding="utf-8")

    zip_path = tmp_path / "release.zip"
    manifest_path = tmp_path / "release.manifest.json"

    manifest = module.build_release(zip_path, manifest_path)

    assert manifest["status"] == "FAIL"
    assert "invalid_json_evidence" in manifest["blockers"]
    assert manifest["skipped_invalid_json_artifacts"] == [
        "artifacts/reports/prelive_gate/prelive_gate_invalid.json"
    ]
    with zipfile.ZipFile(zip_path) as zf:
        assert "artifacts/reports/prelive_gate/prelive_gate_invalid.json" not in zf.namelist()

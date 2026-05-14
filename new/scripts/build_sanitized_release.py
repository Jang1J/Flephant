#!/usr/bin/env python
"""Build a sanitized service release ZIP and manifest.

The archive excludes secret sources, git history, caches, bulky runtime data,
and binary model/data artifacts. It includes source/config/docs/tests plus a
small allowlist of JSON metadata reports that are useful for service evidence.
The script never opens .env.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
_KST = ZoneInfo("Asia/Seoul")

_DENY_PREFIXES = (
    ".git/",
    ".pytest_cache/",
    ".mypy_cache/",
    "__MACOSX/",
    "artifacts/data/",
    "artifacts/bundles/",
    "artifacts/cache/",
    "artifacts/logs/",
    "new/artifacts/",
    "dist/",
)
_DENY_NAMES = {".env", ".DS_Store"}
_DENY_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".pkl",
    ".pickle",
    ".parquet",
    ".feather",
    ".zip",
    ".tar",
    ".gz",
)
_SOURCE_PREFIXES = (
    ".Codex/",
    ".agents/",
    "new/",
    "scripts/",
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    ".env.example",
    "eval.sh",
    "requirements.txt",
    "smoke.sh",
    "feature_list.json",
)
_MINIMAL_ARTIFACT_PATTERNS = (
    "artifacts/lgbm/registry.json",
    "artifacts/lgbm/*_metadata.json",
    "artifacts/lgbm_paper/registry.json",
    "artifacts/lgbm_paper/*_metadata.json",
    "artifacts/reports/prelive_gate/*.json",
    "artifacts/reports/backtest/*.json",
    "artifacts/reports/deploy/*.json",
    "artifacts/reports/service_policy_replay/*.json",
    "artifacts/reports/phase2_feature_backfill/*.json",
    "artifacts/reports/phase2_input_readiness/*.json",
    "artifacts/reports/dual_source_history/*.json",
    "artifacts/reports/exogenous_history/*.json",
    "artifacts/reports/c12_recheck/*.json",
    "artifacts/reports/service_readiness/*.json",
    "artifacts/reports/cost_aware_retraining/*.json",
    "artifacts/reports/label_horizon_scan/*.json",
    "artifacts/reports/model_registry/*.json",
    "artifacts/reports/paper_auto_trading/*.json",
)


def _git_file_candidates() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git ls-files failed")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _artifact_candidates() -> tuple[list[str], list[str]]:
    candidates: list[str] = []
    skipped_invalid_json: list[str] = []
    for pattern in _MINIMAL_ARTIFACT_PATTERNS:
        for path in ROOT.glob(pattern):
            if path.is_file():
                rel_path = str(path.relative_to(ROOT))
                if path.suffix == ".json":
                    try:
                        with path.open("r", encoding="utf-8") as fh:
                            json.load(fh)
                    except Exception:
                        skipped_invalid_json.append(rel_path)
                        continue
                candidates.append(rel_path)
    return sorted(set(candidates)), sorted(set(skipped_invalid_json))


def _looks_like_secret_source(rel_path: str) -> bool:
    name = Path(rel_path).name
    if name == ".env.example":
        return False
    return name == ".env" or name.startswith(".env.") or "/.env" in rel_path


def _is_source_candidate(rel_path: str) -> bool:
    return any(rel_path == prefix or rel_path.startswith(prefix) for prefix in _SOURCE_PREFIXES)


def _is_allowed(rel_path: str, *, allow_minimal_artifact: bool = False) -> bool:
    rel_path = rel_path.replace("\\", "/")
    path = ROOT / rel_path
    if not path.is_file():
        return False
    if _looks_like_secret_source(rel_path):
        return False
    if Path(rel_path).name in _DENY_NAMES:
        return False
    if "__pycache__/" in rel_path:
        return False
    if any(rel_path.startswith(prefix) for prefix in _DENY_PREFIXES):
        if not allow_minimal_artifact:
            return False
    if rel_path.endswith(_DENY_SUFFIXES):
        return False
    if allow_minimal_artifact:
        return True
    return _is_source_candidate(rel_path)


def _classify_entries(
    entries: list[str],
    minimal_artifacts: list[str],
) -> tuple[list[str], list[str]]:
    included: list[str] = []
    denied: list[str] = []
    minimal = set(minimal_artifacts)
    for rel_path in sorted(set(entries) | minimal):
        allow_minimal = rel_path in minimal
        if _is_allowed(rel_path, allow_minimal_artifact=allow_minimal):
            included.append(rel_path)
        else:
            denied.append(rel_path)
    return included, denied


def _scan_zip_names(zip_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    forbidden = [
        name
        for name in names
        if _looks_like_secret_source(name)
        or name.startswith(".git/")
        or "__pycache__/" in name
        or name.endswith((".pyc", ".pkl", ".parquet"))
    ]
    return {
        "entry_count": len(names),
        "forbidden_entries": sorted(forbidden),
    }


def build_release(output: Path, manifest_path: Path | None = None) -> dict[str, Any]:
    output = output if output.is_absolute() else ROOT / output
    manifest_path = manifest_path or output.with_suffix(".manifest.json")
    manifest_path = manifest_path if manifest_path.is_absolute() else ROOT / manifest_path
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    candidates = _git_file_candidates()
    minimal_artifacts, skipped_invalid_json = _artifact_candidates()
    included, denied = _classify_entries(candidates, minimal_artifacts)
    secret_sources_excluded = [path for path in denied if _looks_like_secret_source(path)]

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel_path in included:
            zf.write(ROOT / rel_path, arcname=rel_path)

    zip_scan = _scan_zip_names(output)
    status = "PASS" if not zip_scan["forbidden_entries"] else "FAIL"
    manifest = {
        "status": status,
        "generated_at": datetime.now(_KST).isoformat(),
        "release_zip": str(output),
        "release_zip_relative": str(output.relative_to(ROOT)),
        "zip_entry_count": zip_scan["entry_count"],
        "included_count": len(included),
        "denied_count": len(denied),
        "secret_sources_excluded_count": len(secret_sources_excluded),
        "secret_sources_in_zip": [
            name for name in zip_scan["forbidden_entries"] if _looks_like_secret_source(name)
        ],
        "forbidden_entries": zip_scan["forbidden_entries"],
        "minimal_artifact_patterns": list(_MINIMAL_ARTIFACT_PATTERNS),
        "minimal_artifact_entries": [
            path for path in included if path.startswith("artifacts/")
        ],
        "skipped_invalid_json_artifacts": skipped_invalid_json,
        "excluded_examples": denied[:100],
    }
    with manifest_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    manifest["manifest_path"] = str(manifest_path)
    manifest["manifest_path_relative"] = str(manifest_path.relative_to(ROOT))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_name = f"elephant_sanitized_service_release_{datetime.now(_KST):%Y%m%d_%H%M%S}.zip"
    parser.add_argument("--output", type=Path, default=Path("dist") / default_name)
    parser.add_argument("--manifest", type=Path, default=None)
    args = parser.parse_args(argv)
    report = build_release(args.output, args.manifest)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

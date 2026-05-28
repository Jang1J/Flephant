#!/usr/bin/env python
"""Validate deploy/research/paper artifact root separation.

This is a read-only guard for the "which path is production?" problem. It
checks that deploy, paper, paper-candidate, bundle, and research roots stay
distinct, and that research entrypoints do not default to production registry
write paths.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.utils.config_loader import load as config_load  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_REGISTRY_DEFAULT_RE = re.compile(
    r"(?:--registry-(?:root|dir)|registry_(?:root|dir)).{0,240}"
    r"(?:default\s*=\s*|or\s+)[\"']([^\"']+)[\"']",
    re.DOTALL,
)


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _resolve_repo_path(raw: str, *, root: Path = ROOT) -> Path:
    path = Path(str(raw))
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _same_or_nested(a: Path, b: Path) -> bool:
    a = a.resolve()
    b = b.resolve()
    return a == b or _is_relative_to(a, b) or _is_relative_to(b, a)


def _load_policy() -> dict[str, Any]:
    policy = config_load("risk_config.yaml", "artifact_root_policy") or {}
    if not isinstance(policy, dict):
        raise ValueError("artifact_root_policy must be a mapping")
    return policy


def _root_rows(policy: dict[str, Any], *, root: Path = ROOT) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    scalar_keys = [
        "production_model_registry",
        "paper_model_registry",
        "paper_candidate_registry_root",
        "candidate_bundle_root",
    ]
    for key in scalar_keys:
        value = policy.get(key)
        if value:
            rows.append({"role": key, "path": str(value)})
    for key in ("research_model_registry_roots", "experiment_report_roots"):
        for value in _as_list(policy.get(key)):
            rows.append({"role": key, "path": value})
    for row in rows:
        row["absolute_path"] = str(_resolve_repo_path(row["path"], root=root))
    return rows


def check_root_separation(
    policy: dict[str, Any],
    *,
    root: Path = ROOT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    rows = _root_rows(policy, root=root)

    required = [
        "production_model_registry",
        "paper_model_registry",
        "paper_candidate_registry_root",
        "candidate_bundle_root",
    ]
    for key in required:
        if not policy.get(key):
            blockers.append({"check": "root_policy", "reason": f"missing_{key}"})

    model_roles = {
        "production_model_registry",
        "paper_model_registry",
        "paper_candidate_registry_root",
        "candidate_bundle_root",
        "research_model_registry_roots",
    }
    model_rows = [row for row in rows if row["role"] in model_roles]
    for idx, left in enumerate(model_rows):
        left_path = Path(left["absolute_path"])
        for right in model_rows[idx + 1:]:
            right_path = Path(right["absolute_path"])
            if _same_or_nested(left_path, right_path):
                blockers.append({
                    "check": "model_root_separation",
                    "reason": "model_roots_overlap",
                    "left": left,
                    "right": right,
                })

    prod = _resolve_repo_path(str(policy.get("production_model_registry", "")), root=root)
    paper = _resolve_repo_path(str(policy.get("paper_model_registry", "")), root=root)
    for row in rows:
        if row["role"] != "experiment_report_roots":
            continue
        report_path = Path(row["absolute_path"])
        if _same_or_nested(report_path, prod) or _same_or_nested(report_path, paper):
            blockers.append({
                "check": "report_root_separation",
                "reason": "experiment_report_under_model_registry",
                "root": row,
            })

    seen: dict[str, str] = {}
    for row in rows:
        absolute = row["absolute_path"]
        if absolute in seen:
            warnings.append({
                "check": "root_policy",
                "reason": "duplicate_root_literal",
                "first_role": seen[absolute],
                "second_role": row["role"],
                "path": row["path"],
            })
        seen[absolute] = row["role"]

    return blockers, warnings, rows


def _registry_defaults(source: str) -> list[str]:
    return [match.group(1) for match in _REGISTRY_DEFAULT_RE.finditer(source)]


def _under_any(path: str, roots: list[str], *, root: Path = ROOT) -> bool:
    resolved = _resolve_repo_path(path, root=root)
    return any(_is_relative_to(resolved, _resolve_repo_path(item, root=root)) for item in roots)


def scan_experiment_entrypoints(
    policy: dict[str, Any],
    *,
    root: Path = ROOT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    research_roots = _as_list(policy.get("research_model_registry_roots"))
    production_root = str(policy.get("production_model_registry", "artifacts/lgbm"))
    paper_root = str(policy.get("paper_model_registry", "artifacts/lgbm_paper"))

    for rel in _as_list(policy.get("experiment_entrypoints")):
        path = root / rel
        if not path.is_file():
            blockers.append({
                "check": "experiment_entrypoint",
                "reason": "entrypoint_missing",
                "path": rel,
            })
            continue
        source = path.read_text(encoding="utf-8")
        defaults = _registry_defaults(source)
        for default in defaults:
            if default in {production_root, paper_root}:
                blockers.append({
                    "check": "experiment_entrypoint",
                    "reason": "registry_default_points_to_deploy_root",
                    "path": rel,
                    "default": default,
                })
            elif "lgbm" in default and not _under_any(default, research_roots, root=root):
                warnings.append({
                    "check": "experiment_entrypoint",
                    "reason": "registry_default_outside_research_root",
                    "path": rel,
                    "default": default,
                })
        if "ModelRegistry(" in source and not (
            any(root_literal in source for root_literal in research_roots)
            or "scratch_registry_dir" in source
        ):
            blockers.append({
                "check": "experiment_entrypoint",
                "reason": "model_registry_used_without_research_or_scratch_root",
                "path": rel,
            })
    return blockers, warnings


def scan_paper_entrypoints(
    policy: dict[str, Any],
    *,
    root: Path = ROOT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    production_root = str(policy.get("production_model_registry", "artifacts/lgbm"))
    paper_roots = {
        str(policy.get("paper_model_registry", "artifacts/lgbm_paper")),
        str(policy.get("paper_candidate_registry_root", "artifacts/lgbm_paper_candidate")),
    }
    for rel in _as_list(policy.get("paper_entrypoints")):
        path = root / rel
        if not path.is_file():
            blockers.append({
                "check": "paper_entrypoint",
                "reason": "entrypoint_missing",
                "path": rel,
            })
            continue
        source = path.read_text(encoding="utf-8")
        defaults = _registry_defaults(source)
        for default in defaults:
            if default == production_root:
                blockers.append({
                    "check": "paper_entrypoint",
                    "reason": "paper_registry_default_points_to_production",
                    "path": rel,
                    "default": default,
                })
            elif "lgbm" in default and not any(
                _under_any(default, [paper_root], root=root) for paper_root in paper_roots
            ):
                warnings.append({
                    "check": "paper_entrypoint",
                    "reason": "paper_registry_default_outside_paper_roots",
                    "path": rel,
                    "default": default,
                })
    return blockers, warnings


def check_production_registry_state(
    policy: dict[str, Any],
    *,
    root: Path = ROOT,
    allow_production_active: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    registry_dir = _resolve_repo_path(
        str(policy.get("production_model_registry", "artifacts/lgbm")),
        root=root,
    )
    registry_path = registry_dir / "registry.json"
    state: dict[str, Any] = {
        "path": _repo_relative(registry_path),
        "exists": registry_path.is_file(),
        "active_version": None,
        "version_count": 0,
    }
    if not registry_path.is_file():
        warnings.append({
            "check": "production_registry_state",
            "reason": "registry_missing",
            "path": _repo_relative(registry_path),
        })
        return blockers, warnings, state
    try:
        with registry_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:
        blockers.append({
            "check": "production_registry_state",
            "reason": "registry_json_unreadable",
            "path": _repo_relative(registry_path),
            "error": str(e),
        })
        return blockers, warnings, state
    active_version = data.get("active_version")
    versions = data.get("versions") or {}
    state["active_version"] = active_version
    state["version_count"] = (
        len(versions) if isinstance(versions, (dict, list)) else 0
    )
    version_items: list[tuple[str, dict[str, Any]]] = []
    if isinstance(versions, dict):
        for key, value in versions.items():
            if isinstance(value, dict):
                version_items.append((str(key), value))
    elif isinstance(versions, list):
        for value in versions:
            if not isinstance(value, dict):
                continue
            version_id = str(value.get("version") or value.get("id") or "")
            version_items.append((version_id, value))

    inactive_drift: list[dict[str, Any]] = []
    inactive_missing_artifacts: list[dict[str, Any]] = []
    for version_id, item in version_items:
        version = str(item.get("version") or item.get("id") or version_id)
        if not version or version == active_version:
            continue
        status = str(item.get("status", "")).lower()
        model_path = str(item.get("model_path") or "")
        metadata_path = str(item.get("metadata_path") or "")
        if version.startswith("live_") or status == "candidate":
            inactive_drift.append({
                "version": version,
                "status": status or None,
                "model_path": model_path or None,
                "metadata_path": metadata_path or None,
            })
        for field, raw_path in (("model_path", model_path), ("metadata_path", metadata_path)):
            if not raw_path:
                continue
            artifact_path = _resolve_repo_path(raw_path, root=root)
            if not artifact_path.exists():
                inactive_missing_artifacts.append({
                    "check": "production_registry_state",
                    "reason": "inactive_candidate_artifact_missing",
                    "version": version,
                    "field": field,
                    "path": raw_path,
                })
    if inactive_drift:
        state["inactive_candidate_drift_count"] = len(inactive_drift)
        state["inactive_candidate_drift"] = inactive_drift[:10]
    if inactive_missing_artifacts:
        state["inactive_candidate_missing_artifact_count"] = len(inactive_missing_artifacts)
        state["inactive_candidate_missing_artifacts"] = inactive_missing_artifacts[:10]
        warnings.append({
            "check": "production_registry_state",
            "reason": "inactive_candidate_artifacts_missing",
            "count": len(inactive_missing_artifacts),
            "items": inactive_missing_artifacts[:10],
        })
    if active_version and not allow_production_active:
        blockers.append({
            "check": "production_registry_state",
            "reason": "production_active_version_not_null",
            "path": _repo_relative(registry_path),
            "active_version": active_version,
        })
    return blockers, warnings, state


def build_report(
    *,
    root: Path = ROOT,
    allow_production_active: bool = False,
) -> dict[str, Any]:
    policy = _load_policy()
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    root_blockers, root_warnings, roots = check_root_separation(policy, root=root)
    exp_blockers, exp_warnings = scan_experiment_entrypoints(policy, root=root)
    paper_blockers, paper_warnings = scan_paper_entrypoints(policy, root=root)
    prod_blockers, prod_warnings, prod_state = check_production_registry_state(
        policy,
        root=root,
        allow_production_active=allow_production_active,
    )

    blockers.extend(root_blockers)
    blockers.extend(exp_blockers)
    blockers.extend(paper_blockers)
    blockers.extend(prod_blockers)
    warnings.extend(root_warnings)
    warnings.extend(exp_warnings)
    warnings.extend(paper_warnings)
    warnings.extend(prod_warnings)

    status = "FAIL" if blockers else "WARN" if warnings else "PASS"
    return {
        "schema_version": "1.0.0",
        "status": status,
        "generated_at": datetime.now(_KST).isoformat(),
        "policy_source": "new/config/risk_config.yaml:artifact_root_policy",
        "registry_mutated": False,
        "live_trading_allowed": False,
        "checks": {
            "root_separation": "PASS" if not root_blockers else "FAIL",
            "experiment_entrypoints": "PASS" if not exp_blockers else "FAIL",
            "paper_entrypoints": "PASS" if not paper_blockers else "FAIL",
            "production_registry_state": "PASS" if not prod_blockers else "FAIL",
        },
        "roots": roots,
        "production_registry": prod_state,
        "blockers": blockers,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate deploy/research/paper artifact root separation",
    )
    parser.add_argument("--allow-production-active", action="store_true")
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args(argv)

    report = build_report(allow_production_active=bool(args.allow_production_active))
    if bool(args.write_report):
        ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
        report_path = ROOT / "artifacts" / "reports" / "artifact_root_policy" / f"artifact_root_policy_{ts}.json"
        _write_json(report_path, report)
        report["report_path"] = str(report_path)
        report["report_path_relative"] = _repo_relative(report_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

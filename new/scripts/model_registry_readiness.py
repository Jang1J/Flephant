#!/usr/bin/env python
"""Model registry readiness checker.

Production registry active 승격은 하지 않는다. 이 스크립트는 registry와 특정
candidate가 paper rehearsal 또는 deploy gate 진입에 필요한 최소 조건을 갖췄는지만
검사한다.
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.models.registry import ModelRegistry, VersionNotFoundError  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_RECOMMENDED_PYTHON = Path("/opt/anaconda3/envs/elephant/bin/python")


def _resolve_path(raw: str | None, registry_dir: Path) -> Path | None:
    if not raw:
        return None
    path = Path(str(raw))
    if path.is_absolute():
        return path
    root_candidate = ROOT / path
    if root_candidate.exists():
        return root_candidate
    return registry_dir / path.name


def _required_metadata() -> list[str]:
    cfg = config_load("risk_config.yaml", "model_registry") or {}
    return list(cfg.get("metadata_required_fields", []))


def _feature_manifest_status(metadata: dict[str, Any]) -> dict[str, Any]:
    feature_cols = list(metadata.get("feature_cols", []))
    manifest = metadata.get("feature_manifest")
    required_count = len(feature_cols)
    if isinstance(manifest, dict):
        required_count = int(manifest.get("feature_count", required_count))
    return {
        "status": "PASS" if feature_cols else "FAIL",
        "feature_count": len(feature_cols),
        "manifest_feature_count": required_count,
        "requires_exogenous": (
            bool(manifest.get("requires_exogenous"))
            if isinstance(manifest, dict)
            else None
        ),
    }


def _runtime_report() -> dict[str, Any]:
    recommended = _RECOMMENDED_PYTHON
    return {
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "recommended_python": str(recommended),
        "recommended_python_exists": recommended.exists(),
    }


def _pickle_error_report(error: Exception) -> dict[str, Any]:
    message = str(error)
    runtime = _runtime_report()
    runtime_mismatch = "numpy.dtype size changed" in message
    report = {
        "status": "BLOCKED",
        "error": message,
        "error_type": type(error).__name__,
        "runtime": runtime,
    }
    if runtime_mismatch:
        report["runtime_mismatch_suspected"] = True
        report["operator_hint"] = (
            "LightGBM/sklearn/numpy ABI mismatch suspected. "
            f"Run this gate with {runtime['recommended_python']}."
        )
    return report


def build_report(
    *,
    registry_dir: str | None = None,
    candidate_version: str | None = None,
    require_active: bool = False,
) -> dict[str, Any]:
    registry_path = Path(registry_dir) if registry_dir else None
    registry = ModelRegistry(artifacts_dir=registry_path) if registry_path else ModelRegistry()
    registry_index = registry._read_registry_index()
    active_version = registry_index.get("active_version")
    versions = {str(v.get("version")): v for v in registry_index.get("versions", [])}
    target_version = str(candidate_version or active_version or "").strip()

    blockers: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {
        "active_version": {
            "status": "PASS" if active_version else "BLOCKED",
            "value": active_version,
        },
        "registry_path": str(registry.base_dir),
        "target_version": target_version or None,
        "runtime": _runtime_report(),
    }
    if require_active and not active_version:
        blockers.append("active_version_null")
    if not target_version:
        blockers.append("target_version_missing")

    metadata: dict[str, Any] = {}
    if target_version:
        try:
            model, metadata = registry.load_version(target_version)
            checks["pickle_load"] = {
                "status": "PASS",
                "model_type": f"{type(model).__module__}.{type(model).__name__}",
            }
        except VersionNotFoundError as e:
            blockers.append("target_version_not_found")
            checks["pickle_load"] = {"status": "BLOCKED", "error": str(e)}
        except Exception as e:
            blockers.append("pickle_load_failed")
            checks["pickle_load"] = _pickle_error_report(e)

    if metadata:
        missing = [field for field in _required_metadata() if field not in metadata]
        checks["c17_metadata"] = {
            "status": "PASS" if not missing else "FAIL",
            "missing": missing,
        }
        if missing:
            blockers.append("c17_metadata_missing")

        model_path = _resolve_path(str(metadata.get("model_path", "")), registry.base_dir)
        metadata_path = _resolve_path(str(metadata.get("metadata_path", "")), registry.base_dir)
        checks["paths"] = {
            "model_path": str(model_path) if model_path else None,
            "metadata_path": str(metadata_path) if metadata_path else None,
            "model_exists": bool(model_path and model_path.exists()),
            "metadata_exists": bool(metadata_path and metadata_path.exists()),
            "portable": not str(metadata.get("model_path", "")).startswith("/"),
        }
        if not checks["paths"]["model_exists"]:
            blockers.append("model_file_missing")
        if not checks["paths"]["metadata_exists"]:
            blockers.append("metadata_file_missing")
        if not checks["paths"]["portable"]:
            warnings.append("metadata_contains_nonportable_paths")

        checks["feature_manifest"] = _feature_manifest_status(metadata)
        if checks["feature_manifest"]["status"] != "PASS":
            blockers.append("feature_manifest_missing")

        metrics = metadata.get("metrics", {}) if isinstance(metadata.get("metrics"), dict) else {}
        deploy_quality = bool(metrics.get("deploy_quality", False))
        checks["deploy_quality"] = {
            "status": "PASS" if deploy_quality else "WARN",
            "value": deploy_quality,
            "metric_scope": metrics.get("metric_scope"),
        }
        if not deploy_quality:
            warnings.append("candidate_not_marked_deploy_quality")

    status = "BLOCKED" if blockers else ("WARN" if warnings else "PASS")
    return {
        "status": status,
        "generated_at": datetime.now(_KST).isoformat(),
        "blockers": blockers,
        "warnings": warnings,
        "active_version": active_version,
        "version_count": len(versions),
        "checks": checks,
    }


def _write_report(report: dict[str, Any], output_dir: str | None) -> Path | None:
    if not output_dir:
        return None
    out_dir = Path(output_dir)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S_%f")
    registry_name = Path(str(report.get("checks", {}).get("registry_path", "registry"))).name
    path = out_dir / f"model_registry_readiness_{registry_name}_{ts}.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Model registry readiness checker")
    parser.add_argument("--registry-dir", default=None)
    parser.add_argument("--candidate-version", default=None)
    parser.add_argument("--require-active", action="store_true")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args(argv)

    report = build_report(
        registry_dir=args.registry_dir,
        candidate_version=args.candidate_version,
        require_active=bool(args.require_active),
    )
    path = _write_report(report, args.output_dir)
    if path is not None:
        report["report_path"] = str(path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"PASS", "WARN"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

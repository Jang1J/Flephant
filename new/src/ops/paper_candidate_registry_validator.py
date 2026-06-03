"""Paper candidate registry validator for paper-auto start gates.

The check is intentionally lightweight: it reads registry JSON and verifies
referenced artifact paths, but it does not load model pickles, call external
services, or mutate any registry.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be object: {path}")
    return data


def _resolve_artifact_path(raw: Any, *, repo_root: Path, registry_dir: Path) -> Path | None:
    if not raw:
        return None
    path = Path(str(raw))
    if path.is_absolute():
        return _safe_resolve(path)
    repo_path = repo_root / path
    if _safe_exists(repo_path):
        return _safe_resolve(repo_path)
    return _safe_resolve(registry_dir / path.name)


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()


def _safe_exists(path: Path) -> bool:
    exists, _ = _safe_exists_result(path)
    return exists


def _safe_exists_result(path: Path) -> tuple[bool, str | None]:
    try:
        return path.exists(), None
    except OSError as e:
        return False, type(e).__name__


def _is_under(path: Path, root: Path) -> bool:
    try:
        _safe_resolve(path).relative_to(_safe_resolve(root))
        return True
    except (ValueError, OSError):
        return False


def _is_allowed_artifact_path(path: Path, *, repo_root: Path, registry_dir: Path) -> bool:
    return _is_under(path, repo_root) or _is_under(path, registry_dir)


def _feature_cols_from_manifest(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    cols = value.get("feature_cols")
    if not isinstance(cols, list):
        return []
    return [str(item) for item in cols]


def _feature_cols(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def validate_paper_candidate_registry(
    *,
    repo_root: Path,
    bundle_id: str,
    registry_dir: Path,
) -> dict[str, Any]:
    """Validate a paper-only LGBM registry for StartPaperAutoTrading."""
    blockers: list[str] = []
    warnings: list[str] = []
    registry_path = registry_dir / "registry.json"

    if not _safe_exists(registry_path):
        blockers.append("paper_candidate_registry_not_found")
        return {
            "status": "BLOCKED",
            "blockers": blockers,
            "warnings": warnings,
            "registry_path": str(registry_path),
        }

    try:
        registry = _read_json(registry_path)
    except Exception as e:
        blockers.append("paper_candidate_registry_invalid_json")
        return {
            "status": "BLOCKED",
            "blockers": blockers,
            "warnings": warnings,
            "registry_path": str(registry_path),
            "error_type": type(e).__name__,
            "error": str(e),
        }

    active_version = str(registry.get("active_version") or "").strip()
    versions_raw = registry.get("versions")
    versions = versions_raw if isinstance(versions_raw, list) else []
    version_map = {
        str(item.get("version") or "").strip(): item
        for item in versions
        if isinstance(item, dict)
    }
    target = version_map.get(active_version) if active_version else None

    if registry.get("paper_only_registry") is not True:
        blockers.append("paper_candidate_registry_not_paper_only")
    if bool(registry.get("live_trading_allowed")):
        blockers.append("paper_candidate_registry_live_allowed_true")
    if bool(registry.get("production_registry_mutated")):
        blockers.append("paper_candidate_registry_production_mutated")
    if not active_version:
        blockers.append("paper_candidate_registry_active_version_missing")
    if not target:
        blockers.append("paper_candidate_registry_active_version_not_found")

    model_exists = False
    metadata_exists = False
    feature_count = 0
    if isinstance(target, dict):
        if str(target.get("bundle_id") or "") != str(bundle_id):
            blockers.append("paper_candidate_registry_bundle_mismatch")
        if bool(target.get("live_trading_allowed")):
            blockers.append("paper_candidate_version_live_allowed_true")

        model_path = _resolve_artifact_path(
            target.get("model_path"),
            repo_root=repo_root,
            registry_dir=registry_dir,
        )
        metadata_path = _resolve_artifact_path(
            target.get("metadata_path"),
            repo_root=repo_root,
            registry_dir=registry_dir,
        )
        if model_path and not _is_allowed_artifact_path(
            model_path,
            repo_root=repo_root,
            registry_dir=registry_dir,
        ):
            blockers.append("paper_candidate_model_path_outside_allowed_roots")
        if metadata_path and not _is_allowed_artifact_path(
            metadata_path,
            repo_root=repo_root,
            registry_dir=registry_dir,
        ):
            blockers.append("paper_candidate_metadata_path_outside_allowed_roots")
        model_error = None
        metadata_error = None
        if model_path:
            model_exists, model_error = _safe_exists_result(model_path)
        if metadata_path:
            metadata_exists, metadata_error = _safe_exists_result(metadata_path)
        if model_error:
            blockers.append("paper_candidate_model_path_os_error")
            warnings.append(f"paper_candidate_model_path_error:{model_error}")
        elif not model_exists:
            blockers.append("paper_candidate_model_file_missing")
        if metadata_error:
            blockers.append("paper_candidate_metadata_path_os_error")
            warnings.append(f"paper_candidate_metadata_path_error:{metadata_error}")
        elif not metadata_exists:
            blockers.append("paper_candidate_metadata_file_missing")
        metadata: dict[str, Any] | None = None
        if metadata_exists and metadata_path:
            try:
                metadata = _read_json(metadata_path)
            except Exception as e:
                metadata = None
                blockers.append("paper_candidate_metadata_invalid_json")
                warnings.append(f"paper_candidate_metadata_error:{type(e).__name__}")

        feature_cols = _feature_cols(target.get("feature_cols"))
        feature_count = len(feature_cols)
        if feature_count <= 0:
            blockers.append("paper_candidate_feature_cols_missing")
        manifest_feature_cols = _feature_cols_from_manifest(target.get("feature_manifest"))
        if not manifest_feature_cols:
            blockers.append("paper_candidate_feature_manifest_missing")
        elif feature_cols and manifest_feature_cols != feature_cols:
            blockers.append("paper_candidate_feature_manifest_mismatch")
        if metadata is not None:
            metadata_bundle_id = str(metadata.get("bundle_id") or "").strip()
            if not metadata_bundle_id:
                blockers.append("paper_candidate_metadata_bundle_missing")
            elif metadata_bundle_id != str(bundle_id):
                blockers.append("paper_candidate_metadata_bundle_mismatch")
            metadata_feature_cols = _feature_cols(metadata.get("feature_cols"))
            if not metadata_feature_cols:
                blockers.append("paper_candidate_metadata_feature_cols_missing")
            elif feature_cols and metadata_feature_cols != feature_cols:
                blockers.append("paper_candidate_metadata_feature_cols_mismatch")
            metadata_manifest_feature_cols = _feature_cols_from_manifest(
                metadata.get("feature_manifest")
            )
            if not metadata_manifest_feature_cols:
                blockers.append("paper_candidate_metadata_feature_manifest_missing")
            elif feature_cols and metadata_manifest_feature_cols != feature_cols:
                blockers.append("paper_candidate_metadata_feature_manifest_mismatch")

    return {
        "status": "PASS" if not blockers else "BLOCKED",
        "blockers": blockers,
        "warnings": warnings,
        "registry_path": str(registry_path),
        "active_version": active_version or None,
        "bundle_id": bundle_id,
        "model_exists": model_exists,
        "metadata_exists": metadata_exists,
        "feature_count": feature_count,
        "live_trading_allowed": bool(registry.get("live_trading_allowed")),
        "paper_only_registry": bool(registry.get("paper_only_registry")),
    }

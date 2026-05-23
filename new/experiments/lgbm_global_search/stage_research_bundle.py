#!/usr/bin/env python
"""Stage a research LGBM candidate into an artifacts/bundles layout for validation."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
CONFIRM_PHRASE = "STAGE_RESEARCH_BUNDLE_OK"
SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[3]
RESEARCH_ROOT = (REPO_ROOT / "artifacts" / "lgbm_global_search").resolve()
BUNDLE_ROOT = (REPO_ROOT / "artifacts" / "bundles").resolve()
PRODUCTION_REGISTRY = (REPO_ROOT / "artifacts" / "lgbm").resolve()
PAPER_REGISTRY = (REPO_ROOT / "artifacts" / "lgbm_paper").resolve()


def _json_dump(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _json_load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return data


def _resolve_repo_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


def _repo_rel(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(REPO_ROOT))
    except ValueError:
        return str(resolved)


def _safe_source_registry(path: Path) -> Path:
    resolved = path.resolve()
    if not (resolved == RESEARCH_ROOT or RESEARCH_ROOT in resolved.parents):
        raise RuntimeError(f"source registry must be under {RESEARCH_ROOT}: {resolved}")
    if resolved in {PRODUCTION_REGISTRY, PAPER_REGISTRY}:
        raise RuntimeError(f"unsafe source registry: {resolved}")
    return resolved


def _safe_bundle_dir(bundle_id: str) -> Path:
    clean = str(bundle_id).strip()
    if not clean or "/" in clean or "\\" in clean or clean in {".", ".."}:
        raise ValueError(f"invalid bundle_id: {bundle_id}")
    resolved = (BUNDLE_ROOT / clean / "lgbm").resolve()
    if not (resolved == BUNDLE_ROOT or BUNDLE_ROOT in resolved.parents):
        raise RuntimeError(f"target bundle must be under {BUNDLE_ROOT}: {resolved}")
    return resolved


def _candidate_files(source_registry: Path, candidate_version: str) -> tuple[Path, Path]:
    pkl = source_registry / f"{candidate_version}.pkl"
    metadata = source_registry / f"{candidate_version}_metadata.json"
    if not pkl.exists():
        raise FileNotFoundError(f"candidate pkl not found: {pkl}")
    if not metadata.exists():
        raise FileNotFoundError(f"candidate metadata not found: {metadata}")
    return pkl, metadata


def stage_bundle(
    *,
    source_registry_dir: str,
    candidate_version: str,
    bundle_id: str,
    force: bool,
    confirm_phrase: str | None,
) -> dict[str, Any]:
    if confirm_phrase != CONFIRM_PHRASE:
        return {
            "status": "SKIP",
            "reason": "confirm_phrase_missing_or_mismatch",
            "required_phrase": CONFIRM_PHRASE,
            "production_registry_mutated": False,
            "paper_registry_mutated": False,
            "live_trading_allowed": False,
        }

    source_registry = _safe_source_registry(_resolve_repo_path(source_registry_dir))
    pkl, metadata_path = _candidate_files(source_registry, candidate_version)
    target_lgbm_dir = _safe_bundle_dir(bundle_id)
    if target_lgbm_dir.exists() and not force:
        raise FileExistsError(f"target bundle lgbm dir exists: {target_lgbm_dir}")

    target_lgbm_dir.mkdir(parents=True, exist_ok=True)
    target_model = target_lgbm_dir / "latest_model.pkl"
    target_metadata = target_lgbm_dir / "latest_model_metadata.json"
    shutil.copy2(pkl, target_model)
    metadata = _json_load(metadata_path)
    metadata.update(
        {
            "bundle_id": str(bundle_id),
            "registry_status": "research_candidate",
            "research_only": True,
            "deploy_quality": False,
            "requires_c12": True,
            "service_policy_replay_pass": False,
            "source_research_registry_dir": _repo_rel(source_registry),
            "source_research_version": str(candidate_version),
            "model_path": _repo_rel(target_model),
            "metadata_path": _repo_rel(target_metadata),
            "staged_for_validation_at": datetime.now(KST).isoformat(),
            "live_trading_allowed": False,
            "production_registry_mutated": False,
            "paper_registry_mutated": False,
        }
    )
    _json_dump(target_metadata, metadata)
    report = {
        "status": "PASS",
        "generated_at": datetime.now(KST).isoformat(),
        "action": "stage_research_lgbm_bundle",
        "bundle_id": str(bundle_id),
        "candidate_version": str(candidate_version),
        "source_registry_dir": str(source_registry),
        "target_lgbm_dir": str(target_lgbm_dir),
        "model_path": str(target_model),
        "metadata_path": str(target_metadata),
        "research_only": True,
        "deploy_quality": False,
        "requires_c12": True,
        "production_registry_mutated": False,
        "paper_registry_mutated": False,
        "live_trading_allowed": False,
    }
    _json_dump(target_lgbm_dir / "stage_research_bundle_report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage research LGBM candidate bundle for validation")
    parser.add_argument("--source-registry-dir", required=True)
    parser.add_argument("--candidate-version", required=True)
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--confirm-phrase", default=None)
    args = parser.parse_args(argv)
    report = stage_bundle(
        source_registry_dir=str(args.source_registry_dir),
        candidate_version=str(args.candidate_version),
        bundle_id=str(args.bundle_id),
        force=bool(args.force),
        confirm_phrase=args.confirm_phrase,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

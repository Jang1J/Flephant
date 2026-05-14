#!/usr/bin/env python
"""Prepare a paper-only LGBM registry copy.

Production ``artifacts/lgbm``는 수정하지 않는다. 이 스크립트는 source registry를
target directory로 복사한 뒤, target registry 안에서만 candidate를 active로 표시한다.
실계좌/live 배포 증거로 사용하면 안 된다.
"""
from __future__ import annotations

import argparse
import json
import shutil
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

_KST = ZoneInfo("Asia/Seoul")
_CONFIRM = "PREPARE_PAPER_LGBM_OK"


def _resolve_dir(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def _repo_rel(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(ROOT))
    except ValueError:
        return str(resolved)


def _copy_source(source: Path, target: Path, force: bool) -> None:
    if not source.exists():
        raise FileNotFoundError(f"source registry dir not found: {source}")
    if source.resolve() == target.resolve():
        raise ValueError("source-dir and target-dir must differ")
    if target.exists() and not force:
        raise FileExistsError(
            f"target already exists: {target}. Use --force to overlay paper copy."
        )
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)


def prepare(
    *,
    candidate_version: str,
    source_dir: str,
    target_dir: str,
    force: bool,
    confirm_phrase: str | None,
) -> dict[str, Any]:
    if confirm_phrase != _CONFIRM:
        return {
            "status": "SKIP",
            "reason": "confirm_phrase_missing_or_mismatch",
            "required_phrase": _CONFIRM,
            "production_registry_mutated": False,
        }

    source = _resolve_dir(source_dir)
    target = _resolve_dir(target_dir)
    _copy_source(source, target, force=force)

    source_registry = ModelRegistry(artifacts_dir=source)
    target_registry = ModelRegistry(artifacts_dir=target)
    source_before = source_registry._read_registry_index()
    registry = target_registry._read_registry_index()
    versions = list(registry.get("versions", []))

    candidate = None
    for entry in versions:
        if entry.get("version") == candidate_version:
            candidate = entry
            break
    if candidate is None:
        raise VersionNotFoundError(f"candidate not found in paper registry: {candidate_version}")

    pkl_path = target / f"{candidate_version}.pkl"
    meta_path = target / f"{candidate_version}_metadata.json"
    if not pkl_path.exists() or not meta_path.exists():
        raise VersionNotFoundError(f"candidate files missing in target: {candidate_version}")

    with meta_path.open("r", encoding="utf-8") as fh:
        metadata: dict[str, Any] = json.load(fh)

    metadata.update({
        "status": "active",
        "model_path": _repo_rel(pkl_path),
        "metadata_path": _repo_rel(meta_path),
        "paper_only_activation": True,
        "live_trading_allowed": False,
        "paper_activation_created_at": datetime.now(_KST).isoformat(),
    })
    target_registry._assert_metadata_fields(metadata)

    for entry in versions:
        if entry.get("version") == candidate_version:
            entry.update(metadata)
            entry["status"] = "active"
        elif entry.get("status") == "active":
            entry["status"] = "rollback"

    registry["active_version"] = candidate_version
    registry["versions"] = versions
    registry["paper_only_registry"] = True
    registry["live_trading_allowed"] = False

    with (target / "registry.json").open("w", encoding="utf-8") as fh:
        json.dump(registry, fh, ensure_ascii=False, indent=2)
    with meta_path.open("w", encoding="utf-8") as fh:
        json.dump(metadata, fh, ensure_ascii=False, indent=2)
    target_registry._update_latest_pointer(pkl_path)

    source_after = source_registry._read_registry_index()
    return {
        "status": "PASS",
        "generated_at": datetime.now(_KST).isoformat(),
        "candidate_version": candidate_version,
        "source_dir": str(source),
        "target_dir": str(target),
        "paper_only_activation": True,
        "live_trading_allowed": False,
        "active_version": registry.get("active_version"),
        "production_registry_mutated": source_before != source_after,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare paper-only LGBM registry")
    parser.add_argument("--candidate-version", required=True)
    parser.add_argument("--source-dir", default="artifacts/lgbm")
    parser.add_argument("--target-dir", default="artifacts/lgbm_paper")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--confirm-phrase", default=None)
    args = parser.parse_args(argv)

    report = prepare(
        candidate_version=str(args.candidate_version),
        source_dir=str(args.source_dir),
        target_dir=str(args.target_dir),
        force=bool(args.force),
        confirm_phrase=args.confirm_phrase,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

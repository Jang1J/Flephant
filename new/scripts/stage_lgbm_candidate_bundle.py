#!/usr/bin/env python
"""Stage an already-trained LightGBM candidate into a Mode B bundle.

This script is intentionally narrow: it does not train, run C12, mutate the
model registry, or read .env. It only copies a validated registry candidate into
``artifacts/bundles/{bundle_id}/lgbm`` so C12 can evaluate the exact model bytes
under an explicit bundle id.
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

REPO_ROOT = Path(__file__).resolve().parents[2]
NEW_ROOT = REPO_ROOT / "new"
if str(NEW_ROOT) not in sys.path:
    sys.path.insert(0, str(NEW_ROOT))

from scripts import prelive_gate  # noqa: E402
from src.utils.id_factory import generate_bundle_id  # noqa: E402
from src.utils.safe_cast import safe_bool  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_REPORT_ROOT = REPO_ROOT / "artifacts" / "reports" / "lgbm_bundle_staging"


def _repo_relative(path: Path, *, repo_root: Path = REPO_ROOT) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise TypeError(f"JSON root must be object: {path}")
    return data


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def _metadata_created_at(meta: dict[str, Any]) -> datetime:
    return prelive_gate._metadata_created_at_utc(meta)


def _candidate_blockers(meta: dict[str, Any], *, repo_root: Path) -> list[str]:
    blockers: list[str] = []
    if safe_bool(meta.get("synthetic_fallback"), default=False):
        blockers.append("synthetic_fallback")
    if meta.get("data_source") != "artifact_bars":
        blockers.append("not_artifact_bars")
    if meta.get("status") != "candidate":
        blockers.append("not_candidate_status")
    final_gate = prelive_gate._final_dataset_gate_result({"model_metadata": meta})
    if final_gate.get("status") != "PASS":
        blockers.append("final_dataset_gate_blocked")
    model_path = meta.get("model_path")
    if not model_path:
        blockers.append("model_path_missing")
    else:
        resolved = Path(str(model_path))
        if not resolved.is_absolute():
            resolved = repo_root / resolved
        if not resolved.is_file():
            blockers.append("model_file_missing")
        elif resolved.stat().st_size <= 0:
            blockers.append("model_file_empty")
    return blockers


def _select_candidate_metadata(
    *,
    repo_root: Path,
    registry_dir: str | Path | None,
    version: str | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    registry_base = Path(registry_dir) if registry_dir else repo_root / "artifacts" / "lgbm"
    if not registry_base.is_absolute():
        registry_base = repo_root / registry_base
    registry_path = registry_base / "registry.json"
    if not registry_path.is_file():
        return None, ["registry_missing"]
    registry = _load_json(registry_path)
    versions = registry.get("versions")
    if not isinstance(versions, list) or not versions:
        return None, ["registry_versions_missing"]

    if version:
        matches = [
            item for item in versions
            if isinstance(item, dict) and str(item.get("version")) == version
        ]
        if not matches:
            return None, ["version_not_found"]
        meta = dict(matches[-1])
        blockers = _candidate_blockers(meta, repo_root=repo_root)
        return meta, blockers

    candidates: list[dict[str, Any]] = []
    for item in versions:
        if not isinstance(item, dict):
            continue
        meta = dict(item)
        blockers = _candidate_blockers(meta, repo_root=repo_root)
        if blockers:
            continue
        candidates.append(meta)

    if not candidates:
        return None, ["no_stageable_candidate"]
    return sorted(candidates, key=_metadata_created_at)[-1], []


def stage_lgbm_candidate_bundle(
    *,
    bundle_id: str,
    version: str | None = None,
    registry_dir: str | Path | None = None,
    repo_root: Path = REPO_ROOT,
    write_report: bool = True,
) -> dict[str, Any]:
    bundle_id = str(bundle_id or "").strip() or generate_bundle_id()
    if not bundle_id.startswith("BUNDLE-"):
        raise ValueError("bundle_id must start with BUNDLE-")

    report: dict[str, Any] = {
        "status": "RUNNING",
        "generated_at": datetime.now(_KST).isoformat(),
        "bundle_id": bundle_id,
        "requested_version": version,
        "source_registry_dir": (
            str(Path(registry_dir)) if registry_dir is not None else "artifacts/lgbm"
        ),
        "registry_mutated": False,
        "live_trading_allowed": False,
    }

    meta, blockers = _select_candidate_metadata(
        repo_root=repo_root,
        registry_dir=registry_dir,
        version=version,
    )
    if meta is None or blockers:
        report.update(
            {
                "status": "BLOCKED",
                "blockers": blockers or ["candidate_metadata_missing"],
            }
        )
        if write_report:
            _write_report(report, repo_root=repo_root)
        return report

    model_src = Path(str(meta["model_path"]))
    if not model_src.is_absolute():
        model_src = repo_root / model_src
    version_id = str(meta.get("version") or "").strip()
    if not version_id:
        report.update({"status": "BLOCKED", "blockers": ["version_missing"]})
        if write_report:
            _write_report(report, repo_root=repo_root)
        return report

    bundle_root = repo_root / "artifacts" / "bundles" / bundle_id
    bundle_lgbm_dir = bundle_root / "lgbm"
    bundle_lgbm_dir.mkdir(parents=True, exist_ok=True)

    model_dest = bundle_lgbm_dir / "latest_model.pkl"
    committee_dest = bundle_lgbm_dir / "committee.pkl"
    shutil.copy2(model_src, model_dest)
    shutil.copy2(model_src, committee_dest)

    staged_meta = dict(meta)
    metadata_dest = bundle_lgbm_dir / "latest_model_metadata.json"
    staged_meta.update(
        {
            "bundle_id": bundle_id,
            "status": "candidate",
            "model_path": _repo_relative(model_dest, repo_root=repo_root),
            "metadata_path": _repo_relative(metadata_dest, repo_root=repo_root),
            "staged_from_existing_version": version_id,
            "staged_at": datetime.now(_KST).isoformat(),
        }
    )
    _write_json(metadata_dest, staged_meta)

    factor_zoo_dest = bundle_root / "alpha_factor" / "factor_zoo.jsonl"
    factor_zoo_dest.parent.mkdir(parents=True, exist_ok=True)
    live_factor_zoo = repo_root / "artifacts" / "alpha_factor" / "factor_zoo.jsonl"
    if live_factor_zoo.is_file():
        shutil.copy2(live_factor_zoo, factor_zoo_dest)
    else:
        factor_zoo_dest.write_text(
            json.dumps(
                {
                    "bundle_id": bundle_id,
                    "status": "empty_factor_zoo",
                    "created_at": datetime.now(_KST).isoformat(),
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    manifest = {
        "bundle_id": bundle_id,
        "component": "lgbm",
        "version": version_id,
        "model_path": _repo_relative(model_dest, repo_root=repo_root),
        "metadata_path": _repo_relative(metadata_dest, repo_root=repo_root),
        "committee_path": _repo_relative(committee_dest, repo_root=repo_root),
        "factor_zoo_path": _repo_relative(factor_zoo_dest, repo_root=repo_root),
        "staged_at": datetime.now(_KST).isoformat(),
        "source_model_path": _repo_relative(model_src, repo_root=repo_root),
        "registry_mutated": False,
    }
    _write_json(bundle_lgbm_dir / "candidate_manifest.json", manifest)

    final_gate = prelive_gate._final_dataset_gate_result({"model_metadata": staged_meta})
    report.update(
        {
            "status": "PASS",
            "version": version_id,
            "model_path": _repo_relative(model_dest, repo_root=repo_root),
            "metadata_path": _repo_relative(metadata_dest, repo_root=repo_root),
            "candidate_manifest_path": _repo_relative(
                bundle_lgbm_dir / "candidate_manifest.json",
                repo_root=repo_root,
            ),
            "final_dataset_gate": final_gate,
            "candidate_bundle_staged": True,
            "blockers": [],
        }
    )
    if write_report:
        _write_report(report, repo_root=repo_root)
    return report


def _write_report(report: dict[str, Any], *, repo_root: Path = REPO_ROOT) -> Path:
    out_dir = repo_root / _REPORT_ROOT.relative_to(REPO_ROOT)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"stage_lgbm_candidate_bundle_{ts}.json"
    report["report_path"] = str(path)
    report["report_path_relative"] = _repo_relative(path, repo_root=repo_root)
    _write_json(path, report)
    return path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", default="", help="BUNDLE-{YYYYMMDD}-{UUID8}")
    parser.add_argument("--version", default="", help="Registry version to stage")
    parser.add_argument(
        "--registry-dir",
        default="",
        help=(
            "Source registry directory. Defaults to artifacts/lgbm; "
            "research runs can use artifacts/lgbm_research/<bundle_id>."
        ),
    )
    parser.add_argument("--no-write-report", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = stage_lgbm_candidate_bundle(
        bundle_id=str(args.bundle_id).strip() or generate_bundle_id(),
        version=str(args.version).strip() or None,
        registry_dir=str(args.registry_dir).strip() or None,
        write_report=not bool(args.no_write_report),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

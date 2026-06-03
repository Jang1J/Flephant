#!/usr/bin/env python
"""Write the 2026-06-04 paper bake-off manifest.

This script is read-only with respect to broker/account state:
- does not read .env
- does not call KIS or external APIs
- does not mutate production registries
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATE = "20260604"
DEFAULT_BUNDLES = (
    ("baseline_control", "BUNDLE-20260521-POSTCLOSE", "THU_BAKEOFF_0521_BASELINE"),
    ("latest_candidate", "BUNDLE-20260602-DEED529F", "THU_BAKEOFF_0602_DEED529F"),
    ("fallback", "BUNDLE-20260601-DCD577EA", "THU_BAKEOFF_0601_DCD577EA"),
    ("optional_reference", "BUNDLE-20260528-9F5F4ABA", "THU_BAKEOFF_0528_9F5F4ABA"),
)


def _repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _parse_bundle_specs(raw_specs: list[str]) -> list[tuple[str, str, str]]:
    if not raw_specs:
        return list(DEFAULT_BUNDLES)
    parsed: list[tuple[str, str, str]] = []
    for index, raw in enumerate(raw_specs, start=1):
        parts = [part.strip() for part in str(raw).split(":")]
        if len(parts) == 1 and parts[0]:
            bundle_id = parts[0]
            role = f"candidate_{index}"
            track_id = f"BAKEOFF_{index}_{bundle_id}"
        elif len(parts) == 2 and all(parts):
            role, bundle_id = parts
            track_id = f"BAKEOFF_{index}_{bundle_id}"
        elif len(parts) == 3 and all(parts):
            role, bundle_id, track_id = parts
        else:
            raise ValueError(
                "bundle specs must be BUNDLE_ID, ROLE:BUNDLE_ID, or ROLE:BUNDLE_ID:TRACK_ID"
            )
        parsed.append((role, bundle_id, track_id))
    return parsed


def _candidate_entry(
    *,
    date: str,
    role: str,
    bundle_id: str,
    track_id: str,
    cycles: int,
    interval_sec: int,
    max_tickers: int,
    end_date: str,
    business_days: int,
) -> dict[str, Any]:
    registry_dir = ROOT / "artifacts" / "lgbm_paper_candidate" / bundle_id
    report_dir = (
        ROOT
        / "artifacts"
        / "reports"
        / "paper_auto_trading"
        / f"THU_10CYCLE_{bundle_id}_{date}"
    )
    registry_json = registry_dir / "registry.json"
    return {
        "role": role,
        "bundle_id": bundle_id,
        "track_id": track_id,
        "registry_dir": _repo_relative(registry_dir),
        "registry_json": _repo_relative(registry_json),
        "registry_exists": registry_json.is_file(),
        "report_dir": _repo_relative(report_dir),
        "stdout_stderr_path": _repo_relative(report_dir / "stdout_stderr.log"),
        "args": {
            "bundle_id": bundle_id,
            "registry_dir": _repo_relative(registry_dir),
            "end_date": end_date,
            "business_days": int(business_days),
            "max_tickers": int(max_tickers),
            "cycles": int(cycles),
            "interval_sec": int(interval_sec),
            "tickers": [],
            "prelive_scope": "paper-rehearsal",
            "shadow_only": True,
            "confirm_phrase": "PAPER_AUTO_OK",
            "report_dir": _repo_relative(report_dir),
            "track_id": track_id,
        },
        "precheck_status": "PASS" if registry_json.is_file() else "BLOCKED",
        "precheck_reason": "" if registry_json.is_file() else "paper_candidate_registry_missing",
    }


def build_manifest(
    *,
    date: str = DEFAULT_DATE,
    bundle_specs: list[str] | None = None,
    cycles: int = 10,
    interval_sec: int = 60,
    max_tickers: int = 30,
    end_date: str = "20260602",
    business_days: int = 260,
) -> dict[str, Any]:
    candidates = [
        _candidate_entry(
            date=date,
            role=role,
            bundle_id=bundle_id,
            track_id=track_id,
            cycles=cycles,
            interval_sec=interval_sec,
            max_tickers=max_tickers,
            end_date=end_date,
            business_days=business_days,
        )
        for role, bundle_id, track_id in _parse_bundle_specs(bundle_specs or [])
    ]
    return {
        "schema_version": "1.0.0",
        "status": "PASS" if all(item["registry_exists"] for item in candidates) else "BLOCKED",
        "action": "write_paper_bakeoff_manifest",
        "generated_at": datetime.now(KST).isoformat(),
        "date": date,
        "market": "KRX",
        "mode": "shadow_only_paper_rehearsal",
        "execution_policy": {
            "candidate_order": "sequential",
            "parallel_candidates_allowed": False,
            "be_scheduler_allowed": False,
            "env_contents_read_allowed": False,
        },
        "common_args": {
            "max_tickers": int(max_tickers),
            "cycles": int(cycles),
            "interval_sec": int(interval_sec),
            "shadow_only": True,
            "prelive_scope": "paper-rehearsal",
            "end_date": end_date,
            "business_days": int(business_days),
            "tickers": [],
        },
        "safety_invariants": {
            "active_version": None,
            "live_trading_allowed": False,
            "registry_mutated": False,
            "broker_order_submitted": False,
            "broker_submit_enabled": False,
            "live_enabled": False,
        },
        "candidates": candidates,
        "blockers": [
            f"{item['bundle_id']}:paper_candidate_registry_missing"
            for item in candidates
            if not item["registry_exists"]
        ],
    }


def write_manifest(manifest: dict[str, Any], output: str | Path | None = None) -> Path:
    date = str(manifest.get("date") or DEFAULT_DATE)
    out = Path(output) if output else (
        ROOT / "artifacts" / "reports" / f"paper_bakeoff_{date}" / "manifest.json"
    )
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest["manifest_path"] = _repo_relative(out)
    with out.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write paper bake-off manifest")
    parser.add_argument("--date", default=DEFAULT_DATE)
    parser.add_argument("--bundle", action="append", default=[])
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--interval-sec", type=int, default=60)
    parser.add_argument("--max-tickers", type=int, default=30)
    parser.add_argument("--end-date", default="20260602")
    parser.add_argument("--business-days", type=int, default=260)
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    manifest = build_manifest(
        date=str(args.date),
        bundle_specs=list(args.bundle or []),
        cycles=int(args.cycles),
        interval_sec=int(args.interval_sec),
        max_tickers=int(args.max_tickers),
        end_date=str(args.end_date),
        business_days=int(args.business_days),
    )
    path = write_manifest(manifest, args.output or None)
    print(json.dumps({**manifest, "manifest_path": _repo_relative(path)}, ensure_ascii=False, indent=2))
    return 0 if manifest.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

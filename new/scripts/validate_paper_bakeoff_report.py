#!/usr/bin/env python
"""Validate all reports referenced by a paper bake-off manifest."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from validate_paper_auto_report_semantics import build_report  # noqa: E402
from write_paper_bakeoff_manifest import build_manifest, write_manifest  # noqa: E402

KST = ZoneInfo("Asia/Seoul")


def _repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be object: {path}")
    return data


def validate_bakeoff(
    *,
    manifest_path: str | Path | None = None,
    date: str = "20260604",
    generated_date: str = "20260604",
    min_cycles: int = 10,
    expected_ticker_count: int = 30,
    expected_cold_fetch_n: int = 61,
    expected_incremental_fetch_n: int = 6,
    incremental_after_cycle: int = 1,
    max_cycle_start_gap_sec: float = 75.0,
) -> dict[str, Any]:
    if manifest_path:
        manifest = _load_json(_resolve(manifest_path))
    else:
        manifest = build_manifest(date=date)
        write_manifest(manifest)

    candidate_reports: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []
    for candidate in manifest.get("candidates", []):
        if not isinstance(candidate, dict):
            continue
        bundle_id = str(candidate.get("bundle_id") or "")
        registry_exists = bool(candidate.get("registry_exists"))
        if not registry_exists:
            blockers.append(f"{bundle_id}:paper_candidate_registry_missing")
            candidate_reports.append({
                "bundle_id": bundle_id,
                "status": "BLOCKED",
                "reason": "paper_candidate_registry_missing",
                "report_count": 0,
                "failure_count": 1,
            })
            continue
        report = build_report(
            bundle_id=bundle_id,
            report_dir=str(candidate.get("report_dir") or ""),
            generated_date=generated_date,
            strict_cadence=True,
            min_cycles=min_cycles,
            expected_ticker_count=expected_ticker_count,
            expected_cold_fetch_n=expected_cold_fetch_n,
            expected_incremental_fetch_n=expected_incremental_fetch_n,
            incremental_after_cycle=incremental_after_cycle,
            max_cycle_start_gap_sec=max_cycle_start_gap_sec,
            require_shadow_only=True,
        )
        candidate_reports.append(report)
        if report.get("status") != "PASS":
            blockers.append(f"{bundle_id}:validator_blocked")
        for failure in report.get("failures", []):
            if isinstance(failure, dict):
                reason = failure.get("reason") or ",".join(failure.get("blockers", []) or [])
                if reason:
                    blockers.append(f"{bundle_id}:{reason}")
        for item in report.get("reports", []):
            if isinstance(item, dict):
                warnings.extend(f"{bundle_id}:{w}" for w in item.get("warnings", []))

    return {
        "schema_version": "1.0.0",
        "status": "PASS" if not blockers else "BLOCKED",
        "action": "validate_paper_bakeoff_report",
        "generated_at": datetime.now(KST).isoformat(),
        "date": str(manifest.get("date") or date),
        "generated_date": generated_date,
        "manifest_path": str(manifest_path or manifest.get("manifest_path") or ""),
        "candidate_count": len(candidate_reports),
        "candidate_reports": candidate_reports,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "safety": {
            "external_api_called": False,
            "env_read": False,
            "registry_mutated": False,
            "live_trading_allowed": False,
        },
    }


def write_validation(report: dict[str, Any], output: str | Path | None = None) -> Path:
    date = str(report.get("date") or "20260604")
    out = Path(output) if output else (
        ROOT / "artifacts" / "reports" / f"paper_bakeoff_{date}" / "validation.json"
    )
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    report["report_path"] = _repo_relative(out)
    with out.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate paper bake-off reports")
    parser.add_argument("--manifest", default="")
    parser.add_argument("--date", default="20260604")
    parser.add_argument("--generated-date", default="20260604")
    parser.add_argument("--min-cycles", type=int, default=10)
    parser.add_argument("--expected-ticker-count", type=int, default=30)
    parser.add_argument("--expected-cold-fetch-n", type=int, default=61)
    parser.add_argument("--expected-incremental-fetch-n", type=int, default=6)
    parser.add_argument("--incremental-after-cycle", type=int, default=1)
    parser.add_argument("--max-cycle-start-gap-sec", type=float, default=75.0)
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    report = validate_bakeoff(
        manifest_path=args.manifest or None,
        date=str(args.date),
        generated_date=str(args.generated_date),
        min_cycles=int(args.min_cycles),
        expected_ticker_count=int(args.expected_ticker_count),
        expected_cold_fetch_n=int(args.expected_cold_fetch_n),
        expected_incremental_fetch_n=int(args.expected_incremental_fetch_n),
        incremental_after_cycle=int(args.incremental_after_cycle),
        max_cycle_start_gap_sec=float(args.max_cycle_start_gap_sec),
    )
    if args.write_report:
        write_validation(report, args.output or None)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

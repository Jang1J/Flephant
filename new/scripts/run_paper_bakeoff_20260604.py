#!/usr/bin/env python
"""Run the 2026-06-04 sequential paper bake-off.

The runner never sources or prints `.env`. It assumes required credentials were
loaded by the parent shell and forwards only the inherited environment to
`paper_auto_trade.py`.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from write_paper_bakeoff_manifest import build_manifest, write_manifest  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
CONFIRM_PHRASE = "RUN_PAPER_BAKEOFF_OK"


def _repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be object: {path}")
    return data


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def build_candidate_command(candidate: dict[str, Any], *, python: str) -> list[str]:
    args = candidate.get("args") if isinstance(candidate.get("args"), dict) else {}
    cmd = [
        python,
        str(ROOT / "new" / "scripts" / "paper_auto_trade.py"),
        "--bundle-id",
        str(args.get("bundle_id") or candidate.get("bundle_id") or ""),
        "--registry-dir",
        str(args.get("registry_dir") or candidate.get("registry_dir") or ""),
        "--end-date",
        str(args.get("end_date") or "20260602"),
        "--business-days",
        str(int(args.get("business_days") or 260)),
        "--max-tickers",
        str(int(args.get("max_tickers") or 30)),
        "--cycles",
        str(int(args.get("cycles") or 10)),
        "--interval-sec",
        str(int(args.get("interval_sec") or 60)),
        "--prelive-scope",
        str(args.get("prelive_scope") or "paper-rehearsal"),
        "--shadow-only",
        "--confirm-phrase",
        "PAPER_AUTO_OK",
        "--report-dir",
        str(args.get("report_dir") or candidate.get("report_dir") or ""),
        "--track-id",
        str(args.get("track_id") or candidate.get("track_id") or ""),
    ]
    return cmd


def _run_candidate(
    candidate: dict[str, Any],
    *,
    python: str,
    dry_run: bool,
) -> dict[str, Any]:
    report_dir = _resolve(str(candidate.get("report_dir") or ""))
    stdout_path = _resolve(str(candidate.get("stdout_stderr_path") or report_dir / "stdout_stderr.log"))
    registry_exists = bool(candidate.get("registry_exists"))
    result: dict[str, Any] = {
        "bundle_id": candidate.get("bundle_id"),
        "track_id": candidate.get("track_id"),
        "report_dir": _repo_relative(report_dir),
        "stdout_stderr_path": _repo_relative(stdout_path),
        "registry_exists": registry_exists,
    }
    if not registry_exists:
        result.update({
            "status": "BLOCKED",
            "reason": "paper_candidate_registry_missing",
            "returncode": None,
        })
        return result

    cmd = build_candidate_command(candidate, python=python)
    result["command"] = cmd
    if dry_run:
        result.update({"status": "DRY_RUN", "reason": "", "returncode": 0})
        return result

    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        with stdout_path.open("w", encoding="utf-8") as out:
            completed = subprocess.run(
                cmd,
                cwd=ROOT,
                stdout=out,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        result.update({
            "returncode": None,
            "status": "BLOCKED",
            "reason": f"runner_exception:{type(e).__name__}",
            "error_type": type(e).__name__,
            "error": str(e),
        })
        return result
    result["returncode"] = int(completed.returncode)
    result["status"] = "PASS" if completed.returncode == 0 else "BLOCKED"
    result["reason"] = "" if completed.returncode == 0 else "paper_auto_trade_exit_nonzero"
    return result


def run_bakeoff(
    *,
    manifest_path: str | Path | None = None,
    date: str = "20260604",
    python: str = sys.executable,
    continue_on_failure: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    if manifest_path:
        manifest = _load_json(_resolve(manifest_path))
    else:
        manifest = build_manifest(date=date)
        write_manifest(manifest)
    results: list[dict[str, Any]] = []
    for candidate in manifest.get("candidates", []):
        if not isinstance(candidate, dict):
            continue
        result = _run_candidate(candidate, python=python, dry_run=dry_run)
        results.append(result)
        if result["status"] == "BLOCKED" and not continue_on_failure:
            break
    blockers = [
        f"{item.get('bundle_id')}:{item.get('reason')}"
        for item in results
        if item.get("status") == "BLOCKED"
    ]
    status = "PASS" if not blockers else "BLOCKED"
    if dry_run and not blockers:
        status = "DRY_RUN"
    return {
        "schema_version": "1.0.0",
        "status": status,
        "action": "run_paper_bakeoff_20260604",
        "generated_at": datetime.now(KST).isoformat(),
        "dry_run": bool(dry_run),
        "continue_on_failure": bool(continue_on_failure),
        "manifest": manifest,
        "results": results,
        "blockers": blockers,
        "safety": {
            "env_read": False,
            "external_api_called_by_runner": False,
            "registry_mutated_by_runner": False,
            "live_trading_allowed": False,
        },
    }


def _write_run_report(report: dict[str, Any], output: str | Path | None = None) -> Path:
    date = str((report.get("manifest") or {}).get("date") or "20260604")
    out = Path(output) if output else (
        ROOT / "artifacts" / "reports" / f"paper_bakeoff_{date}" / "run_report.json"
    )
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    report["report_path"] = _repo_relative(out)
    with out.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run sequential 2026-06-04 paper bake-off")
    parser.add_argument("--manifest", default="")
    parser.add_argument("--date", default="20260604")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--stop-on-failure", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--output", default="")
    parser.add_argument("--confirm-phrase", default="")
    args = parser.parse_args(argv)

    if not args.dry_run and args.confirm_phrase != CONFIRM_PHRASE:
        report = {
            "status": "SKIP",
            "reason": "confirm_phrase_missing_or_mismatch",
            "required_phrase": CONFIRM_PHRASE,
            "safety": {
                "env_read": False,
                "external_api_called_by_runner": False,
                "registry_mutated_by_runner": False,
                "live_trading_allowed": False,
            },
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    report = run_bakeoff(
        manifest_path=args.manifest or None,
        date=str(args.date),
        python=str(args.python),
        continue_on_failure=not bool(args.stop_on_failure),
        dry_run=bool(args.dry_run),
    )
    if args.write_report:
        _write_run_report(report, args.output or None)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") in {"PASS", "DRY_RUN"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

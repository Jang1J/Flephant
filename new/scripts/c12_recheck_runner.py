#!/usr/bin/env python
"""Run or verify C12 backtest evidence without bypassing Mode B guards.

Default mode is read-only verification. With ``--run`` it calls the canonical
``src.jobs.run_backtest.run_backtest`` only when ELEPHANT_MODE=mode_b and the
configured KST Mode B window are both satisfied. It never edits registry files.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import prelive_gate  # noqa: E402
from src.jobs.run_backtest import run_backtest  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_REPORT_DIR = ROOT / "artifacts" / "reports" / "c12_recheck"


def _mode_b_window() -> tuple[int, int]:
    cfg = config_load("risk_config.yaml", "mode_b_scheduler.execution_window") or {}
    return int(cfg.get("start_hour", 18)), int(cfg.get("end_hour", 22))


def _window_state(now: datetime | None = None) -> dict[str, Any]:
    current = (now or datetime.now(_KST)).astimezone(_KST)
    start_hour, end_hour = _mode_b_window()
    return {
        "now": current.isoformat(),
        "timezone": "Asia/Seoul",
        "elephant_mode": os.environ.get("ELEPHANT_MODE"),
        "required_elephant_mode": "mode_b",
        "window": f"{start_hour:02d}:00-{end_hour:02d}:59 KST",
        "mode_ok": os.environ.get("ELEPHANT_MODE") == "mode_b",
        "window_ok": start_hour <= current.hour <= end_hour,
    }


def _latest_backtest(bundle_id: str) -> tuple[Path | None, dict[str, Any] | None]:
    return prelive_gate._latest_matching_report(
        ROOT / "artifacts" / "reports" / "backtest",
        f"backtest_{bundle_id}_",
        lambda payload: payload.get("bundle_id") == bundle_id,
    )


def _verify_backtest(bundle_id: str) -> dict[str, Any]:
    path, payload = _latest_backtest(bundle_id)
    if path is None or payload is None:
        return {
            "status": "MISSING",
            "report_path": None,
            "schema_current": False,
            "deployable": False,
        }
    return {
        "status": "PASS" if prelive_gate._is_deployable_backtest_report(payload, bundle_id) else "BLOCKED",
        "report_path": prelive_gate._repo_relative(path),
        "schema_current": "feature_quality" in payload and "service_policy_replay" in payload,
        "has_feature_quality": "feature_quality" in payload,
        "has_service_policy_replay": "service_policy_replay" in payload,
        "deployable": prelive_gate._is_deployable_backtest_report(payload, bundle_id),
        "verdict": payload.get("verdict"),
        "regression_risk": payload.get("regression_risk"),
        "minute_bar_leakage_check": payload.get("minute_bar_leakage_check"),
        "feature_quality_gate_pass": prelive_gate._feature_quality_gate_pass(payload),
        "service_policy_gate_pass": prelive_gate._service_policy_gate_pass(payload, bundle_id),
    }


def run_c12_recheck(
    *,
    bundle_id: str,
    run: bool,
    write_report: bool = True,
    output_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    window = _window_state(now)
    executed = False
    execution_error: str | None = None

    if run:
        if not window["mode_ok"] or not window["window_ok"]:
            execution_error = "mode_b_window_or_env_not_satisfied"
        else:
            try:
                run_backtest(bundle_id, write_report=True)
                executed = True
            except Exception as e:
                execution_error = f"{type(e).__name__}: {e}"

    latest = _verify_backtest(bundle_id)
    blockers: list[str] = []
    if run and execution_error:
        blockers.append(execution_error)
    if not latest.get("schema_current"):
        blockers.append("c12_schema_stale_or_missing")
    if not latest.get("deployable"):
        blockers.append("c12_not_deployable")
    status = "PASS" if not blockers else "BLOCKED"
    report = {
        "status": status,
        "generated_at": datetime.now(_KST).isoformat(),
        "bundle_id": bundle_id,
        "requested_run": bool(run),
        "executed": executed,
        "registry_mutated": False,
        "window_state": window,
        "latest_backtest": latest,
        "blockers": sorted(set(blockers)),
    }
    if write_report:
        out_dir = output_dir or _REPORT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"c12_recheck_{bundle_id}_{datetime.now(_KST).strftime('%Y%m%d_%H%M%S')}.json"
        report["report_path"] = str(path)
        try:
            report["report_path_relative"] = str(path.relative_to(ROOT))
        except ValueError:
            report["report_path_relative"] = str(path)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--run", action="store_true", help="Run C12 if Mode B env/window is satisfied")
    parser.add_argument("--no-write-report", action="store_true")
    parser.add_argument("--output-dir", default=str(_REPORT_DIR))
    args = parser.parse_args(argv)
    report = run_c12_recheck(
        bundle_id=str(args.bundle_id),
        run=bool(args.run),
        write_report=not bool(args.no_write_report),
        output_dir=Path(str(args.output_dir)),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Mode B backtest CLI.

This is the canonical command entrypoint for C12 BacktestAgent. It does not
read .env files and it does not bypass Mode B guards. Run it only with
``ELEPHANT_MODE=mode_b`` during the configured Mode B window.
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

from src.agents.mode_b.backtest import BacktestAgent
from src.mode_b.backtest_diagnostics import attach_service_policy_evidence
from src.utils.id_factory import generate_backtest_id

_KST = ZoneInfo("Asia/Seoul")
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_REPORT_DIR = _REPO_ROOT / "artifacts" / "reports" / "backtest"


def _empty_c12_metrics() -> dict[str, float]:
    return {
        "ic": 0.0,
        "icir": 0.0,
        "rank_ic": 0.0,
        "arr": 0.0,
        "ir": 0.0,
        "mdd": 0.0,
        "sr": 0.0,
    }


def _empty_daily_series() -> dict[str, Any]:
    return {
        "initial_capital": 0.0,
        "daily_pnl": [],
        "daily_returns": [],
        "daily_equity": [],
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run C12 BacktestAgent for a Mode B candidate bundle",
    )
    parser.add_argument(
        "--bundle-id",
        required=True,
        help="Candidate bundle id, e.g. BUNDLE-20260511-ABCDEF12",
    )
    parser.add_argument(
        "--output-dir",
        default=str(_DEFAULT_REPORT_DIR),
        help="Directory to write the backtest JSON report",
    )
    parser.add_argument(
        "--no-write-report",
        action="store_true",
        help="Print only; do not persist a report file",
    )
    return parser.parse_args(argv)


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)


def _write_report(report: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    bundle_id = str(report.get("bundle_id", "BUNDLE-UNKNOWN"))
    path = output_dir / f"backtest_{bundle_id}_{ts}.json"
    report["report_path"] = str(path)
    report["report_path_relative"] = _repo_relative(path)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path


def run_backtest(
    bundle_id: str,
    *,
    output_dir: Path | None = None,
    write_report: bool = True,
    agent: BacktestAgent | None = None,
) -> dict[str, Any]:
    backtest_agent = agent or BacktestAgent()
    try:
        report = backtest_agent.run(bundle_id)
    except Exception as e:
        now = datetime.now(_KST).isoformat()
        report = {
            "verdict": "fail",
            "bundle_id": bundle_id,
            "backtest_id": generate_backtest_id(),
            "started_at": now,
            "finished_at": now,
            "completed_at": now,
            "generated_at": now,
            "metrics": _empty_c12_metrics(),
            "regime_breakdown": [],
            "ablation": [],
            "regression_risk": {
                "flagged": True,
                "severity": "high",
                "evidence": [f"{type(e).__name__}: {e}"],
            },
            "regression_severity": "high",
            "diagnostic_notes": "BacktestAgent wrapper failed before C12 report generation.",
            "llm_reasoning_ref": "",
            "failure_case_cards": [],
            "regression_cases": [],
            "minute_bar_leakage_check": {
                "verdict": "fail",
                "leakage_detected": True,
                "evidence": ["wrapper_exception"],
            },
            "feature_quality": {},
            "folds": [],
            **_empty_daily_series(),
            "error": str(e),
            "error_type": type(e).__name__,
        }

    report = attach_service_policy_evidence(report)
    service_policy_evidence = report.get("service_policy_replay") or {}
    if service_policy_evidence.get("status") != "PASS":
        notes = str(report.get("diagnostic_notes", "") or "")
        report["diagnostic_notes"] = (
            f"{notes} service_policy_replay={service_policy_evidence.get('status')}"
        ).strip()

    if write_report:
        _write_report(report, output_dir or _DEFAULT_REPORT_DIR)
    return report


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if os.environ.get("ELEPHANT_MODE") != "mode_b":
        print(
            "[run_backtest] ELEPHANT_MODE=mode_b 환경에서만 실행할 수 있습니다.",
            file=sys.stderr,
        )
        return 2
    report = run_backtest(
        str(args.bundle_id),
        output_dir=Path(str(args.output_dir)),
        write_report=not bool(args.no_write_report),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("verdict") == "pass" or report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

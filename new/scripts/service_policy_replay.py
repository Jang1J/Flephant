"""CLI for cost-aware service-policy replay.

Read-only by design:
- does not read .env
- does not call KIS
- does not mutate registry artifacts
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.mode_b.service_policy_replay import ServicePolicyReplayEngine

_KST = ZoneInfo("Asia/Seoul")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_REPORT_DIR = _REPO_ROOT / "artifacts" / "reports" / "service_policy_replay"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run KIS paper cash-account service-policy replay for a bundle",
    )
    parser.add_argument("--bundle-id", required=True, help="Candidate bundle id")
    parser.add_argument(
        "--start-date",
        default=None,
        help="Inclusive replay start date in YYYYMMDD. Omit with --end-date for C12 first fold.",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help="Inclusive replay end date in YYYYMMDD. Omit with --start-date for C12 first fold.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(_DEFAULT_REPORT_DIR),
        help="Directory to write the replay JSON report",
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
    path = output_dir / f"service_policy_replay_{bundle_id}_{ts}.json"
    report["report_path"] = str(path)
    report["report_path_relative"] = _repo_relative(path)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path


def run_service_policy_replay(
    bundle_id: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    output_dir: Path | None = None,
    write_report: bool = True,
    engine: ServicePolicyReplayEngine | None = None,
) -> dict[str, Any]:
    replay_engine = engine or ServicePolicyReplayEngine()
    report = replay_engine.run(
        bundle_id,
        start_date=start_date,
        end_date=end_date,
    )
    if write_report:
        _write_report(report, output_dir or _DEFAULT_REPORT_DIR)
    return report


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = run_service_policy_replay(
        str(args.bundle_id),
        start_date=args.start_date,
        end_date=args.end_date,
        output_dir=Path(str(args.output_dir)),
        write_report=not bool(args.no_write_report),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

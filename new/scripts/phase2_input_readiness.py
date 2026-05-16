#!/usr/bin/env python
"""Read-only readiness check for Phase 2 historical feature inputs.

The checker does not read .env, does not call external APIs, and does not write
feature artifacts. It only verifies that required historical raw inputs or real
provider bindings appear available before the materializers are run.
"""
from __future__ import annotations

import argparse
import json
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

import materialize_dual_source_history as dual_source_history  # noqa: E402
import materialize_exogenous_history as exogenous_history  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_REPORT_DIR = ROOT / "artifacts" / "reports" / "phase2_input_readiness"
_DEFAULT_RAW_EVENTS_DIR = ROOT / "artifacts" / "raw" / "dual_source"


def _candidate_raw_paths(raw_events_dir: Path, date_key: str) -> list[Path]:
    return [
        raw_events_dir / f"{date_key}.json",
        raw_events_dir / f"dual_source_raw_{date_key}.json",
        raw_events_dir / f"events_{date_key}.json",
        raw_events_dir / f"news_community_{date_key}.json",
    ]


def _raw_payload_readiness(path: Path, date_key: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as e:
        return {
            "valid": False,
            "reason": f"raw_payload_unreadable:{type(e).__name__}",
            "event_count": 0,
        }
    if not isinstance(payload, dict):
        return {"valid": False, "reason": "raw_payload_not_object", "event_count": 0}
    reason = dual_source_history._non_deploy_quality_reason(payload)
    if reason:
        return {"valid": False, "reason": reason, "event_count": 0}

    events = payload.get("events")
    if not isinstance(events, list):
        return {"valid": False, "reason": "raw_payload_missing_events", "event_count": 0}
    if not events:
        return {"valid": False, "reason": "raw_payload_empty_events", "event_count": 0}

    provenance = payload.get("provenance") or {}
    try:
        declared_event_count = int(provenance.get("event_count"))
    except (TypeError, ValueError):
        return {
            "valid": False,
            "reason": "raw_payload_missing_provenance_event_count",
            "event_count": len(events),
        }
    if declared_event_count != len(events):
        return {
            "valid": False,
            "reason": "raw_payload_event_count_mismatch",
            "event_count": len(events),
        }

    snapshot = dual_source_history._snapshot_ts(date_key)
    for idx, event in enumerate(events):
        if not isinstance(event, dict):
            return {
                "valid": False,
                "reason": f"raw_payload_event_not_object:{idx}",
                "event_count": len(events),
            }
        try:
            event_ts = dual_source_history._required_ts(
                event.get("event_ts") or event.get("published_at") or event.get("ts"),
                field="events[].event_ts",
                snapshot=snapshot,
            )
        except Exception as e:
            return {
                "valid": False,
                "reason": f"raw_payload_event_timestamp_invalid:{type(e).__name__}",
                "event_count": len(events),
            }
        if event_ts > snapshot:
            return {
                "valid": False,
                "reason": "raw_payload_event_after_snapshot",
                "event_count": len(events),
            }
    return {"valid": True, "reason": None, "event_count": len(events)}


def _threshold() -> float:
    gate_cfg = (
        config_load("risk_config.yaml", "backtest_agent.deploy_decision_gate")
        or {}
    ).get("feature_quality_gate", {}) or {}
    return min(
        float(gate_cfg.get("min_dual_source_non_neutral_row_coverage", 0.8)),
        float(gate_cfg.get("min_exogenous_non_neutral_row_coverage", 0.8)),
    )


def check_phase2_input_readiness(
    *,
    end_date: str,
    business_days: int,
    raw_events_dir: Path,
    output_dir: Path | None = None,
    write_report: bool = True,
) -> dict[str, Any]:
    dates = dual_source_history._business_dates(end_date, business_days)
    raw_present: list[str] = []
    raw_missing: list[str] = []
    raw_invalid: list[dict[str, Any]] = []
    raw_examples: dict[str, str] = {}
    for date_key in dates:
        found = next((path for path in _candidate_raw_paths(raw_events_dir, date_key) if path.exists()), None)
        if found is None:
            raw_missing.append(date_key)
            continue
        readiness = _raw_payload_readiness(found, date_key)
        if readiness["valid"]:
            raw_present.append(date_key)
            raw_examples[date_key] = str(found)
        else:
            raw_invalid.append({
                "date": date_key,
                "path": str(found),
                "reason": readiness["reason"],
                "event_count": readiness["event_count"],
            })

    us_client = exogenous_history.USMarketClient()
    ecos_client = exogenous_history.ECOSRestClient()
    krx_client = exogenous_history.KRXRestClient()
    provider_availability = exogenous_history._provider_availability(
        us_client=us_client,
        ecos_client=ecos_client,
        krx_client=krx_client,
    )
    threshold = _threshold()
    dual_coverage = len(raw_present) / max(len(dates), 1)
    providers_ok = all(bool(v) for v in provider_availability.values())
    blockers: list[str] = []
    if dual_coverage < threshold:
        blockers.append("dual_source_raw_archive_coverage_below_threshold")
    if raw_invalid:
        blockers.append("dual_source_raw_archive_invalid")
    if not providers_ok:
        blockers.append("exogenous_required_provider_unavailable")
    report = {
        "status": "PASS" if not blockers else "BLOCKED",
        "generated_at": datetime.now(_KST).isoformat(),
        "end_date": end_date,
        "business_days_requested": business_days,
        "date_count": len(dates),
        "raw_events_dir": str(raw_events_dir),
        "coverage_threshold": threshold,
        "dual_source_raw": {
            "present_date_count": len(raw_present),
            "missing_date_count": len(raw_missing),
            "invalid_date_count": len(raw_invalid),
            "coverage": dual_coverage,
            "present_dates": raw_present,
            "missing_dates_sample": raw_missing[:10],
            "invalid_dates_sample": raw_invalid[:10],
            "examples": raw_examples,
        },
        "exogenous_providers": provider_availability,
        "blockers": blockers,
        "materializer_commands": {
            "dual_source": (
                "PYTHONPATH=new python new/scripts/materialize_dual_source_history.py "
                f"--end-date {end_date} --business-days {business_days} "
                f"--raw-events-dir {raw_events_dir}"
            ),
            "exogenous": (
                "PYTHONPATH=new python new/scripts/materialize_exogenous_history.py "
                f"--end-date {end_date} --business-days {business_days}"
            ),
        },
    }
    if write_report:
        out_dir = output_dir or _REPORT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"phase2_input_readiness_{datetime.now(_KST).strftime('%Y%m%d_%H%M%S')}.json"
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
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--business-days", type=int, default=80)
    parser.add_argument("--raw-events-dir", default=str(_DEFAULT_RAW_EVENTS_DIR))
    parser.add_argument("--output-dir", default=str(_REPORT_DIR))
    parser.add_argument("--no-write-report", action="store_true")
    args = parser.parse_args(argv)
    report = check_phase2_input_readiness(
        end_date=str(args.end_date),
        business_days=int(args.business_days),
        raw_events_dir=Path(str(args.raw_events_dir)),
        output_dir=Path(str(args.output_dir)),
        write_report=not bool(args.no_write_report),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

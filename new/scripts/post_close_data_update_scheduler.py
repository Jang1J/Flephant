#!/usr/bin/env python
"""Local post-close data-update scheduler.

Run this as a long-lived process after exporting the required API environment.
It wakes up every poll interval, and after the configured KST run time it
executes ``post_close_data_update.py`` once per PIT-safe target date.

Safety:
- does not read .env
- requires ELEPHANT_MODE=mode_b before real execution
- never enables live trading
- never mutates production registry
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time as time_module
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[1]
NEW_ROOT = ROOT / "new"
if str(NEW_ROOT) not in sys.path:
    sys.path.insert(0, str(NEW_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import post_close_data_update as updater  # noqa: E402
from src.ops.post_close_job_store import PostCloseJobStore  # noqa: E402
from src.utils.safe_cast import safe_bool, safe_int  # noqa: E402
from src.utils.trading_calendar import is_kospi_trading_day  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")


def _scheduler_cfg() -> dict[str, Any]:
    cfg = updater._post_close_cfg().get("scheduler", {})
    return cfg if isinstance(cfg, dict) else {}


def _parse_hhmm(value: Any, default: str = "18:05") -> time:
    raw = str(value or default).strip()
    try:
        hour_raw, minute_raw = raw.split(":", 1)
        hour = safe_int(hour_raw, default=18, min_value=0, max_value=23)
        minute = safe_int(minute_raw, default=5, min_value=0, max_value=59)
        return time(hour, minute)
    except ValueError:
        return time(18, 5)


def _state_path() -> Path:
    cfg = _scheduler_cfg()
    path = Path(str(cfg.get("state_path") or "artifacts/reports/post_close_data_update_scheduler/state.json"))
    return path if path.is_absolute() else ROOT / path


def _db_enabled() -> bool:
    db_cfg = _scheduler_cfg().get("db", {}) or {}
    return safe_bool(db_cfg.get("enabled"), default=True)


def _state_from_db_latest(target_end_date: str) -> dict[str, Any]:
    if not _db_enabled():
        return {}
    store = PostCloseJobStore()
    try:
        latest = store.latest_run(target_end_date)
    finally:
        store.close()
    if not latest:
        return {}
    return {
        "last_target_end_date": latest.get("target_end_date"),
        "last_status": latest.get("status"),
        "last_attempted_at": latest.get("started_at"),
        "last_bundle_id": latest.get("bundle_id"),
        "last_report_path": latest.get("report_path"),
    }


def _load_state(path: Path | None = None) -> dict[str, Any]:
    state_file = path or _state_path()
    if not state_file.exists():
        return {}
    try:
        with state_file.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(state: dict[str, Any], path: Path | None = None) -> None:
    state_file = path or _state_path()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with state_file.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)


def _target_bundle_id(template: str, target_end_date: str) -> str:
    return template.format(target_end_date=target_end_date)


def _seconds_since(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    try:
        then = datetime.fromisoformat(value)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=_KST)
    return (now - then.astimezone(_KST)).total_seconds()


def evaluate_tick(now: datetime | None = None, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return scheduler decision for the current tick without executing work."""
    cfg = _scheduler_cfg()
    current = (now or datetime.now(_KST)).astimezone(_KST)
    run_time = _parse_hhmm(cfg.get("run_time_kst"))
    enabled = safe_bool(cfg.get("enabled"), default=True)
    target_day = updater.latest_pit_safe_trading_day(current)
    target_end = updater._format_yyyymmdd(target_day)
    current_final = updater._current_final_dataset_end()
    if state is not None:
        state_payload = state
        state_source = "in_memory"
    else:
        state_payload = _state_from_db_latest(target_end) or _load_state()
        state_source = "sqlite" if state_payload.get("last_target_end_date") else "json_state"
    last_target = str(state_payload.get("last_target_end_date") or "")
    last_status = str(state_payload.get("last_status") or "")
    last_attempted_at = str(state_payload.get("last_attempted_at") or "")
    retry_after = safe_int(cfg.get("retry_failed_after_sec"), default=1800, min_value=0)
    elapsed = _seconds_since(last_attempted_at, current)

    decision = {
        "enabled": enabled,
        "now": current.isoformat(),
        "run_time_kst": run_time.strftime("%H:%M"),
        "target_end_date": target_end,
        "state_source": state_source,
        "last_target_end_date": last_target or None,
        "last_status": last_status or None,
        "should_run": False,
        "reason": "",
    }
    if not enabled:
        decision["reason"] = "scheduler_disabled"
    elif current.time() < run_time:
        decision["reason"] = "before_configured_run_time"
    elif not is_kospi_trading_day(current.date()):
        decision["reason"] = "today_not_kospi_trading_day"
    elif current_final is not None and current_final >= target_day:
        decision["reason"] = "final_dataset_already_at_or_after_target"
        decision["current_final_dataset_end_date"] = updater._format_yyyymmdd(current_final)
    elif last_target == target_end and last_status == "PASS":
        decision["reason"] = "target_already_passed"
    elif last_target == target_end and last_status in {"BLOCKED", "FAIL"} and elapsed is not None and elapsed < retry_after:
        decision["reason"] = "target_recently_failed_wait_retry"
        decision["retry_after_sec"] = retry_after
        decision["elapsed_since_failure_sec"] = elapsed
    else:
        decision["should_run"] = True
        decision["reason"] = "due"
    return decision


def run_once(
    *,
    dry_run: bool | None = None,
    force: bool = False,
    now: datetime | None = None,
    state_path: Path | None = None,
) -> dict[str, Any]:
    cfg = _scheduler_cfg()
    state = _load_state(state_path) if state_path is not None else None
    decision = evaluate_tick(now, state)
    report: dict[str, Any] = {
        "status": "SKIP",
        "action": "post_close_data_update_scheduler_once",
        "generated_at": datetime.now(_KST).isoformat(),
        "decision": decision,
        "registry_mutated": False,
        "live_trading_allowed": False,
    }
    if not force and not decision.get("should_run"):
        return report

    target_end = str(decision["target_end_date"])
    bundle_template = str(cfg.get("bundle_id_template") or "BUNDLE-{target_end_date}-POSTCLOSE")
    bundle_id = _target_bundle_id(bundle_template, target_end)
    run_dry = safe_bool(cfg.get("dry_run"), default=False) if dry_run is None else bool(dry_run)
    run_prelive = safe_bool(cfg.get("run_prelive"), default=True)
    business_days = updater._default_business_days()
    max_tickers = updater._default_max_tickers()
    store = PostCloseJobStore() if _db_enabled() else None
    run_id: str | None = None
    if store is not None:
        run_id = store.create_run(
            target_end_date=target_end,
            bundle_id=bundle_id,
            dry_run=run_dry,
            run_prelive=run_prelive,
            decision=decision,
        )

    try:
        update_report = updater.run_update(
            end_date=target_end,
            business_days=business_days,
            max_tickers=max_tickers,
            dry_run=run_dry,
            run_prelive=run_prelive,
            bundle_id=bundle_id,
        )
        if not run_dry:
            updater._write_report(update_report)
    except Exception as e:
        update_report = {
            "status": "FAIL",
            "error": str(e),
            "target_end_date": target_end,
            "registry_mutated": False,
            "live_trading_allowed": False,
        }
    report["status"] = "PASS" if update_report.get("status") == "PASS" else update_report.get("status", "BLOCKED")
    report["update_report"] = update_report
    if run_id is not None:
        report["job_run_id"] = run_id
    if store is not None:
        try:
            store.finish_run(
                run_id or "",
                status=str(update_report.get("status") or report["status"]),
                report_path=update_report.get("report_path_relative") or update_report.get("report_path"),
                blockers=list(update_report.get("blockers") or []),
                update_report=update_report,
                registry_mutated=safe_bool(update_report.get("registry_mutated"), default=False),
                live_trading_allowed=safe_bool(update_report.get("live_trading_allowed"), default=False),
            )
            report["job_db_path"] = str(store.db_path)
        finally:
            store.close()
    state_payload = _load_state(state_path)
    state_payload.update(
        {
            "last_target_end_date": target_end,
            "last_status": update_report.get("status"),
            "last_attempted_at": datetime.now(_KST).isoformat(),
            "last_bundle_id": bundle_id,
            "last_report_path": update_report.get("report_path_relative") or update_report.get("report_path"),
        }
    )
    _write_state(state_payload, state_path)
    return report


def run_forever(*, dry_run: bool | None = None) -> None:
    cfg = _scheduler_cfg()
    poll_interval = safe_int(cfg.get("poll_interval_sec"), default=60, min_value=1)
    while True:
        report = run_once(dry_run=dry_run)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        time_module.sleep(poll_interval)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Long-running post-close data update scheduler")
    parser.add_argument("--once", action="store_true", help="Evaluate one scheduler tick and exit.")
    parser.add_argument("--force", action="store_true", help="Run once even if the schedule says SKIP.")
    parser.add_argument("--dry-run", action="store_true", help="Pass dry-run mode into post_close_data_update.")
    parser.add_argument("--state-path", default="", help="Optional state path override for tests/manual runs.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    state_path = Path(args.state_path) if str(args.state_path).strip() else None
    if bool(args.once) or bool(args.force):
        report = run_once(
            dry_run=bool(args.dry_run),
            force=bool(args.force),
            state_path=state_path,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("status") in {"PASS", "DRY_RUN", "SKIP"} else 1
    run_forever(dry_run=bool(args.dry_run) if bool(args.dry_run) else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

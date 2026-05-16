#!/usr/bin/env python
"""Probe KIS historical 1-minute bar retention boundaries.

This script never reads `.env` files and never prints secret values. It uses
the current process environment only. Run it from a shell where KIS_* variables
are already exported.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.connectors.kis_rest import KISRestClient  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_REPORT_DIR = ROOT / "artifacts" / "reports" / "kis_retention"

DEFAULT_DATES = [
    "20250502",
    "20250509",
    "20250512",
    "20250513",
    "20250514",
    "20250515",
    "20250516",
    "20250519",
    "20250520",
    "20250523",
    "20250530",
    "20250602",
    "20250616",
    "20251215",
    "20260515",
]
DEFAULT_TICKERS = ["005930", "105560", "005380"]


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _mode_candidates(mode: str, suffix: str) -> list[str]:
    if mode == "virtual":
        return [f"KIS_PAPER_{suffix}", f"KIS_{suffix}"]
    if mode == "real":
        return [f"KIS_REAL_{suffix}", f"KIS_{suffix}"]
    return [f"KIS_{suffix}"]


def _selected_env(mode: str, suffix: str) -> str | None:
    for key in _mode_candidates(mode, suffix):
        if os.environ.get(key, "").strip():
            return key
    return None


def _env_presence() -> dict[str, Any]:
    mode = os.environ.get("KIS_MODE", "virtual").strip().lower()
    return {
        "kis_mode": mode,
        "selected_app_key_env": _selected_env(mode, "APP_KEY"),
        "selected_app_secret_env": _selected_env(mode, "APP_SECRET"),
        "selected_account_env": _selected_env(mode, "ACCOUNT_NUMBER"),
        "selected_account_product_env": _selected_env(mode, "ACCOUNT_PRODUCT_CODE"),
    }


def _summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    first = rows[0].get("ts_close") if rows else None
    last = rows[-1].get("ts_close") if rows else None
    return {
        "rows": len(rows),
        "first": first,
        "last": last,
    }


def _boundary(per_date: dict[str, dict[str, Any]], ticker_count: int) -> dict[str, Any]:
    ordered = sorted(per_date)
    all_success = [
        date
        for date in ordered
        if int(per_date[date].get("success_tickers", 0)) == ticker_count
    ]
    any_success = [
        date
        for date in ordered
        if int(per_date[date].get("success_tickers", 0)) > 0
    ]
    all_empty = [
        date
        for date in ordered
        if int(per_date[date].get("success_tickers", 0)) == 0
    ]

    first_all_success = all_success[0] if all_success else None
    first_any_success = any_success[0] if any_success else None
    last_all_empty_before_success = None
    if first_any_success:
        empty_before = [date for date in all_empty if date < first_any_success]
        last_all_empty_before_success = empty_before[-1] if empty_before else None

    return {
        "first_any_success_date": first_any_success,
        "first_all_success_date": first_all_success,
        "last_all_empty_before_success": last_all_empty_before_success,
        "recommended_start_date": first_all_success,
        "retention_boundary_interval": {
            "exclusive_lower_bound_empty": last_all_empty_before_success,
            "inclusive_upper_bound_success": first_all_success,
        },
    }


def run_probe(
    *,
    dates: list[str],
    tickers: list[str],
    n_bars: int,
    sleep_sec: float,
) -> dict[str, Any]:
    client = KISRestClient()
    calls: list[dict[str, Any]] = []
    per_date: dict[str, dict[str, Any]] = {}

    for date in dates:
        success_tickers = 0
        row_counts: dict[str, int] = {}
        date_errors: list[dict[str, str]] = []

        for ticker in tickers:
            entry: dict[str, Any] = {"date": date, "ticker": ticker}
            try:
                rows = client.inquire_minute_bar(ticker, n_bars=n_bars, date=date)
                entry.update(_summarize_rows(rows))
                row_counts[ticker] = int(entry["rows"])
                if entry["rows"] > 0:
                    success_tickers += 1
            except Exception as e:
                entry.update({
                    "rows": 0,
                    "error_type": type(e).__name__,
                    "error": str(e)[:240],
                })
                row_counts[ticker] = 0
                date_errors.append({
                    "ticker": ticker,
                    "error_type": type(e).__name__,
                    "error": str(e)[:240],
                })

            calls.append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
            if sleep_sec > 0:
                time.sleep(sleep_sec)

        summary = {
            "date": date,
            "success_tickers": success_tickers,
            "ticker_count": len(tickers),
            "all_success": success_tickers == len(tickers),
            "any_success": success_tickers > 0,
            "row_counts": row_counts,
            "errors": date_errors,
        }
        per_date[date] = summary
        print(json.dumps({"date_summary": summary}, ensure_ascii=False), flush=True)

    boundary = _boundary(per_date, len(tickers))
    return {
        "schema_version": "1.0.0",
        "generated_at": datetime.now(_KST).isoformat(),
        "env_presence": _env_presence(),
        "n_bars": n_bars,
        "sleep_sec": sleep_sec,
        "tickers": tickers,
        "dates": dates,
        "per_date": per_date,
        "boundary": boundary,
        "calls": calls,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe KIS minute-bar historical retention boundary."
    )
    parser.add_argument(
        "--dates",
        default=",".join(DEFAULT_DATES),
        help="comma-separated YYYYMMDD dates to probe",
    )
    parser.add_argument(
        "--tickers",
        default=",".join(DEFAULT_TICKERS),
        help="comma-separated KRX tickers",
    )
    parser.add_argument("--n-bars", type=int, default=5)
    parser.add_argument("--sleep-sec", type=float, default=1.2)
    parser.add_argument("--no-write-report", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dates = _split_csv(args.dates)
    tickers = _split_csv(args.tickers)
    report = run_probe(
        dates=dates,
        tickers=tickers,
        n_bars=max(1, int(args.n_bars)),
        sleep_sec=max(0.0, float(args.sleep_sec)),
    )

    if not args.no_write_report:
        _REPORT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
        out_path = _REPORT_DIR / f"kis_minute_retention_probe_{ts}.json"
        with out_path.open("w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        report["report_path"] = str(out_path)
        report["report_path_relative"] = str(out_path.relative_to(ROOT))

    print(json.dumps({"final_summary": report["boundary"], "report_path": report.get("report_path_relative")}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

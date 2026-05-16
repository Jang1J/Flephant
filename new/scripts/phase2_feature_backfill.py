#!/usr/bin/env python
"""Audit Phase 2 Dual-Source/exogenous historical feature coverage.

This script does not read .env, does not call KIS, and does not mutate any
registry.  It can optionally write clearly marked neutral placeholders for
paper rehearsal, but by default it only writes a coverage report.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.data.dataset_builder import DUAL_SOURCE_FEATURES, EXOGENOUS_FEATURES  # noqa: E402
from src.data.dual_source_runner import load_latest_scores  # noqa: E402
from src.data.exogenous_feature_store import (  # noqa: E402
    build_neutral_payload as build_neutral_exogenous_payload,
    is_non_neutral,
    load_exogenous_scores,
    write_exogenous_payload,
)
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.safe_cast import safe_bool  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402
from src.utils.trading_calendar import (  # noqa: E402
    kospi_trading_dates_between,
    kospi_trading_start_date,
)
from scripts.live_data_readiness import _inspect_bar_file  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_DATE_RE = re.compile(r"(20\d{6})")
_DUAL_SOURCE_DIR = SRC / "artifacts" / "dual_source"
_REPORT_DIR = ROOT / "artifacts" / "reports" / "phase2_feature_backfill"


def _active_tickers() -> list[str]:
    cfg = config_load("universe_config.yaml") or {}
    final_gate = (
        (config_load("risk_config.yaml", "backtest_agent") or {})
        .get("deploy_decision_gate", {})
        .get("final_dataset_gate", {})
    )
    if not isinstance(final_gate, dict):
        final_gate = {}
    include_pending = safe_bool(
        final_gate.get("include_pending_data_tickers"),
        default=False,
    )
    stock_statuses = {"active"}
    sector_statuses = {"confirmed"}
    if include_pending:
        stock_statuses = {
            str(status)
            for status in final_gate.get("allowed_stock_statuses", ["active", "pending_data"])
        }
        sector_statuses = {
            str(status)
            for status in final_gate.get(
                "allowed_sector_statuses",
                ["confirmed", "confirmed_pending_data"],
            )
        }
    tickers: list[str] = []
    for sector in (cfg.get("sectors") or {}).values():
        if str(sector.get("status")) not in sector_statuses:
            continue
        for item in sector.get("stocks", []) or []:
            if str(item.get("status", "")) in stock_statuses:
                tickers.append(pad_ticker(str(item.get("ticker", ""))))
    fallback = (cfg.get("backtest_universe_mode") or {}).get("fallback_tickers", [])
    tickers.extend(pad_ticker(str(t)) for t in fallback)
    return sorted(set(tickers))


def _extract_date(path: Path) -> str | None:
    match = _DATE_RE.search(path.name)
    if not match:
        return None
    try:
        datetime.strptime(match.group(1), "%Y%m%d")
    except ValueError:
        return None
    return match.group(1)


def _valid_bar_artifact(path: Path, *, date_key: str, ticker: str, min_rows: int) -> bool:
    if not path.name.startswith("bars_1m_"):
        return False
    if path.suffix not in {".parquet", ".jsonl"}:
        return False
    inspection = _inspect_bar_file(path, date_key, pad_ticker(ticker))
    rows = inspection.get("rows")
    return (
        rows is not None
        and int(rows) >= min_rows
        and inspection.get("timestamp_dates_match") is True
        and inspection.get("ticker_matches") is True
        and int(inspection.get("duplicate_ts_count") or 0) == 0
        and int(inspection.get("out_of_hours_count") or 0) == 0
        and inspection.get("session_span_ok") is True
        and inspection.get("max_gap_ok") is True
    )


def _common_artifact_dates(
    artifacts_dir: Path,
    tickers: list[str],
    *,
    min_rows: int,
) -> list[str]:
    counts: dict[str, int] = {}
    for ticker in tickers:
        ticker_dir = artifacts_dir / ticker
        if not ticker_dir.exists():
            continue
        seen: set[str] = set()
        for path in ticker_dir.iterdir():
            date_key = _extract_date(path)
            if date_key and _valid_bar_artifact(
                path,
                date_key=date_key,
                ticker=ticker,
                min_rows=min_rows,
            ):
                seen.add(date_key)
        for date_key in seen:
            counts[date_key] = counts.get(date_key, 0) + 1
    required = len(tickers)
    return sorted(date_key for date_key, count in counts.items() if count >= required)


def _select_dates(
    artifacts_dir: Path,
    tickers: list[str],
    *,
    end_date: str,
    business_days: int,
    min_rows: int,
) -> list[str]:
    expected = _expected_artifact_dates(end_date=end_date, business_days=business_days)
    common = set(_common_artifact_dates(artifacts_dir, tickers, min_rows=min_rows))
    return [date_key for date_key in expected if date_key in common]


def _expected_artifact_dates(*, end_date: str, business_days: int) -> list[str]:
    end_dt = datetime.strptime(end_date, "%Y%m%d").date()
    start_dt = kospi_trading_start_date(end_dt, business_days)
    return kospi_trading_dates_between(start_dt, end_dt)


def _dual_source_non_neutral(scores: list[dict[str, Any]]) -> bool:
    for row in scores:
        if abs(float(row.get("news_score_t", 0.0))) > 1e-12:
            return True
        if abs(float(row.get("comm_score_t_1", 0.0))) > 1e-12:
            return True
        if abs(float(row.get("comm_score_t_2", 0.0))) > 1e-12:
            return True
        if abs(float(row.get("news_comm_divergence", 0.0))) > 1e-12:
            return True
        if abs(float(row.get("community_noise_multiplier", 1.0)) - 1.0) > 1e-12:
            return True
    return False


def _neutral_dual_source_payload(date_key: str, tickers: list[str]) -> dict[str, Any]:
    batch_date = datetime.strptime(date_key, "%Y%m%d").date()
    scores = []
    for ticker in sorted({pad_ticker(t) for t in tickers}):
        row: dict[str, float | str] = {"ticker": ticker}
        for feature in DUAL_SOURCE_FEATURES:
            row[feature] = 1.0 if feature == "community_noise_multiplier" else 0.0
        scores.append(row)
    return {
        "batch_date": batch_date.isoformat(),
        "snapshot_ts": datetime(
            batch_date.year, batch_date.month, batch_date.day, 8, 30, tzinfo=_KST
        ).isoformat(),
        "generated_at": datetime.now(_KST).isoformat(),
        "ticker_count": len(scores),
        "source_stats": {
            "input_mode": "real",
            "news_mode": "unavailable_empty",
            "community_mode": "unavailable_empty",
            "neutral_rehearsal_file": True,
            "external_kis_api": False,
        },
        "scores": scores,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def run_phase2_feature_backfill(
    *,
    end_date: str,
    business_days: int,
    write_neutral_placeholders: bool,
    artifacts_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    gate_cfg = config_load("risk_config.yaml", "phase2_feature_backfill") or {}
    readiness_cfg = config_load("risk_config.yaml", "live_data_readiness") or {}
    min_artifact_rows = int(
        gate_cfg.get(
            "min_rows_per_day",
            readiness_cfg.get("train_min_rows_per_day", 300),
        )
    )
    tickers = _active_tickers()
    selected_dates = _select_dates(
        artifacts_dir,
        tickers,
        end_date=end_date,
        business_days=business_days,
        min_rows=min_artifact_rows,
    )
    expected_dates = _expected_artifact_dates(end_date=end_date, business_days=business_days)
    missing_artifact_dates = [
        date_key for date_key in expected_dates if date_key not in set(selected_dates)
    ]

    exog_cfg = config_load("risk_config.yaml", "exogenous_features") or {}
    defaults = {
        col: float((exog_cfg.get("neutral_defaults") or {}).get(col, 0.0))
        for col in EXOGENOUS_FEATURES
    }
    min_ds_cov = float(gate_cfg.get("min_dual_source_non_neutral_date_coverage", 0.8))
    min_exog_cov = float(gate_cfg.get("min_exogenous_non_neutral_date_coverage", 0.8))

    per_date: list[dict[str, Any]] = []
    dual_source_found = 0
    dual_source_non_neutral = 0
    exogenous_found = 0
    exogenous_non_neutral = 0
    files_written: list[str] = []

    for date_key in selected_dates:
        ds_path = _DUAL_SOURCE_DIR / f"{date_key}.json"
        ds_scores = load_latest_scores(date_key)
        ds_found = bool(ds_scores)
        ds_non_neutral = _dual_source_non_neutral(ds_scores)
        if not ds_found and write_neutral_placeholders:
            payload = _neutral_dual_source_payload(date_key, tickers)
            _write_json(ds_path, payload)
            files_written.append(str(ds_path.relative_to(ROOT)))
            ds_scores = payload["scores"]
            ds_found = True
            ds_non_neutral = False
        dual_source_found += int(ds_found)
        dual_source_non_neutral += int(ds_non_neutral)

        exog_scores, exog_stats = load_exogenous_scores(
            date_key,
            feature_cols=list(EXOGENOUS_FEATURES),
            defaults=defaults,
        )
        exog_found = exog_stats["status"] == "found"
        exog_non_neutral = any(is_non_neutral(values, defaults) for values in exog_scores.values())
        if not exog_found and write_neutral_placeholders:
            payload = build_neutral_exogenous_payload(
                date_key,
                tickers=tickers,
                feature_cols=list(EXOGENOUS_FEATURES),
                defaults=defaults,
            )
            exog_path = write_exogenous_payload(payload)
            files_written.append(str(exog_path.relative_to(ROOT)))
            exog_scores, exog_stats = load_exogenous_scores(
                date_key,
                feature_cols=list(EXOGENOUS_FEATURES),
                defaults=defaults,
            )
            exog_found = True
            exog_non_neutral = False
        exogenous_found += int(exog_found)
        exogenous_non_neutral += int(exog_non_neutral)

        per_date.append({
            "date": date_key,
            "dual_source_found": ds_found,
            "dual_source_non_neutral": ds_non_neutral,
            "dual_source_score_count": len(ds_scores),
            "exogenous_found": exog_found,
            "exogenous_non_neutral": exog_non_neutral,
            "exogenous_record_count": int(exog_stats.get("record_count", 0)),
        })

    date_count = len(selected_dates)
    dual_source_non_neutral_coverage = dual_source_non_neutral / max(date_count, 1)
    exogenous_non_neutral_coverage = exogenous_non_neutral / max(date_count, 1)
    blockers: list[str] = []
    if missing_artifact_dates:
        blockers.append("kis_1m_artifact_date_coverage_below_threshold")
    if dual_source_non_neutral_coverage < min_ds_cov:
        blockers.append("dual_source_non_neutral_coverage_below_threshold")
    if exogenous_non_neutral_coverage < min_exog_cov:
        blockers.append("exogenous_non_neutral_coverage_below_threshold")

    report = {
        "status": "PASS" if not blockers else "BLOCKED",
        "generated_at": datetime.now(_KST).isoformat(),
        "end_date": end_date,
        "business_days_requested": business_days,
        "date_count": date_count,
        "date_range": (
            {"start": selected_dates[0], "end": selected_dates[-1]}
            if selected_dates
            else {"start": None, "end": None}
        ),
        "artifact_date_coverage": {
            "expected_date_count": len(expected_dates),
            "selected_date_count": date_count,
            "missing_date_count": len(missing_artifact_dates),
            "missing_dates_sample": missing_artifact_dates[:20],
        },
        "ticker_count": len(tickers),
        "min_artifact_rows_per_day": min_artifact_rows,
        "thresholds": {
            "min_dual_source_non_neutral_date_coverage": min_ds_cov,
            "min_exogenous_non_neutral_date_coverage": min_exog_cov,
        },
        "coverage": {
            "dual_source_file_coverage": dual_source_found / max(date_count, 1),
            "dual_source_non_neutral_date_coverage": dual_source_non_neutral_coverage,
            "exogenous_file_coverage": exogenous_found / max(date_count, 1),
            "exogenous_non_neutral_date_coverage": exogenous_non_neutral_coverage,
        },
        "blockers": blockers,
        "files_written": files_written,
        "per_date": per_date,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"phase2_feature_backfill_{ts}.json"
    report["report_path"] = str(path)
    try:
        report["report_path_relative"] = str(path.relative_to(ROOT))
    except ValueError:
        report["report_path_relative"] = str(path)
    _write_json(path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end-date", required=True, help="YYYYMMDD")
    parser.add_argument("--business-days", type=int, default=80)
    parser.add_argument("--artifacts-dir", default=str(ROOT / "artifacts" / "data"))
    parser.add_argument("--output-dir", default=str(_REPORT_DIR))
    parser.add_argument(
        "--write-neutral-placeholders",
        action="store_true",
        help="Write clearly marked neutral placeholder artifacts for rehearsal only.",
    )
    args = parser.parse_args(argv)
    report = run_phase2_feature_backfill(
        end_date=str(args.end_date),
        business_days=int(args.business_days),
        write_neutral_placeholders=bool(args.write_neutral_placeholders),
        artifacts_dir=Path(str(args.artifacts_dir)),
        output_dir=Path(str(args.output_dir)),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

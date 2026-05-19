#!/usr/bin/env python
"""Sector-level readiness audit for data, model inputs, and risk sources.

This audit is intentionally read-only. It does not call external APIs, does not
read secrets, and does not mutate any registry. The goal is to avoid a single
global PASS hiding a weak sector.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq
import yaml

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.utils.ticker_utils import pad_ticker  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_REPORT_DIR = ROOT / "artifacts" / "reports" / "sector_readiness"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--output-dir", default=str(_REPORT_DIR))
    parser.add_argument("--no-write-report", action="store_true")
    return parser.parse_args(argv)


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"JSON object expected: {path}")
    return data


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML object expected: {path}")
    return data


def _metadata_path(bundle_id: str) -> Path:
    return ROOT / "artifacts" / "bundles" / bundle_id / "lgbm" / "latest_model_metadata.json"


def _date_from_bar_file(path: Path) -> str:
    stem = path.stem
    return stem.rsplit("_", 1)[-1]


def _bar_stats(
    ticker: str,
    *,
    start_date: str | None,
    end_date: str | None,
) -> dict[str, Any]:
    data_dir = ROOT / "artifacts" / "data" / ticker
    files = sorted(data_dir.glob("bars_1m_*.parquet"))
    if start_date:
        files = [path for path in files if _date_from_bar_file(path) >= start_date]
    if end_date:
        files = [path for path in files if _date_from_bar_file(path) <= end_date]
    rows = 0
    unreadable = 0
    for path in files:
        try:
            rows += int(pq.ParquetFile(path).metadata.num_rows)
        except Exception:
            unreadable += 1
    dates = [_date_from_bar_file(path) for path in files]
    return {
        "file_count": len(files),
        "row_count": rows,
        "unreadable_file_count": unreadable,
        "first_date": min(dates) if dates else None,
        "last_date": max(dates) if dates else None,
    }


def _dual_source_stats(
    tickers: set[str],
    *,
    start_date: str | None,
    end_date: str | None,
) -> dict[str, Any]:
    rows = 0
    news_non_neutral = 0
    community_non_neutral = 0
    files_seen = 0
    for path in sorted((ROOT / "artifacts" / "dual_source").glob("*.json")):
        date_key = path.stem
        if start_date and date_key < start_date:
            continue
        if end_date and date_key > end_date:
            continue
        data = _load_json(path)
        scores = data.get("scores") if isinstance(data.get("scores"), list) else []
        files_seen += 1
        for row in scores:
            if not isinstance(row, dict):
                continue
            ticker = pad_ticker(row.get("ticker", ""))
            if ticker not in tickers:
                continue
            rows += 1
            if abs(float(row.get("news_score_t", 0.0) or 0.0)) > 0.0:
                news_non_neutral += 1
            if (
                abs(float(row.get("comm_score_t_1", 0.0) or 0.0)) > 0.0
                or abs(float(row.get("comm_score_t_2", 0.0) or 0.0)) > 0.0
            ):
                community_non_neutral += 1
    return {
        "files_seen": files_seen,
        "score_rows": rows,
        "news_non_neutral_rows": news_non_neutral,
        "community_non_neutral_rows": community_non_neutral,
        "news_non_neutral_rate": news_non_neutral / max(rows, 1),
        "community_non_neutral_rate": community_non_neutral / max(rows, 1),
    }


def _exogenous_stats(
    tickers: set[str],
    *,
    start_date: str | None,
    end_date: str | None,
) -> dict[str, Any]:
    rows = 0
    non_neutral = 0
    files_seen = 0
    for path in sorted((ROOT / "artifacts" / "exogenous").glob("*.json")):
        date_key = path.stem
        if start_date and date_key < start_date:
            continue
        if end_date and date_key > end_date:
            continue
        data = _load_json(path)
        per_ticker = data.get("per_ticker") if isinstance(data.get("per_ticker"), dict) else {}
        files_seen += 1
        for ticker in tickers:
            values = per_ticker.get(ticker)
            if not isinstance(values, dict):
                continue
            rows += 1
            if any(float(value or 0.0) != 0.0 for value in values.values()):
                non_neutral += 1
    return {
        "files_seen": files_seen,
        "rows": rows,
        "non_neutral_rows": non_neutral,
        "non_neutral_rate": non_neutral / max(rows, 1),
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    bundle_id = str(args.bundle_id)
    metadata = _load_json(_metadata_path(bundle_id))
    sector_cfg = _load_yaml(ROOT / "new" / "config" / "sector_config.yaml")
    ticker_to_sector = {
        pad_ticker(ticker): str(sector)
        for ticker, sector in (sector_cfg.get("ticker_to_sector") or {}).items()
    }
    sector_names = {
        str(row.get("id")): str(row.get("name_ko"))
        for row in (sector_cfg.get("sectors") or [])
        if isinstance(row, dict)
    }
    loaded = {pad_ticker(ticker) for ticker in metadata.get("loaded_tickers", [])}
    requested = {pad_ticker(ticker) for ticker in metadata.get("requested_tickers", [])}
    start_date = str(args.start_date or metadata.get("train_start", "")).replace("-", "")
    end_date = str(args.end_date or metadata.get("train_end", "")).replace("-", "")

    tickers_by_sector: dict[str, list[str]] = defaultdict(list)
    for ticker in sorted(requested | set(ticker_to_sector)):
        sector = ticker_to_sector.get(ticker, "unknown")
        tickers_by_sector[sector].append(ticker)

    sectors: dict[str, Any] = {}
    for sector, tickers in sorted(tickers_by_sector.items()):
        ticker_set = set(tickers)
        bar_by_ticker = {
            ticker: _bar_stats(ticker, start_date=start_date, end_date=end_date)
            for ticker in tickers
        }
        file_counts = [stats["file_count"] for stats in bar_by_ticker.values()]
        row_counts = [stats["row_count"] for stats in bar_by_ticker.values()]
        unreadable = sum(stats["unreadable_file_count"] for stats in bar_by_ticker.values())
        missing_model_tickers = sorted(ticker_set - loaded)
        dual = _dual_source_stats(ticker_set, start_date=start_date, end_date=end_date)
        exog = _exogenous_stats(ticker_set, start_date=start_date, end_date=end_date)
        blockers: list[str] = []
        warnings: list[str] = []
        if missing_model_tickers:
            blockers.append("model_loaded_ticker_missing")
        if not file_counts or min(file_counts) <= 0:
            blockers.append("bar_files_missing")
        if unreadable:
            blockers.append("bar_parquet_unreadable")
        if not row_counts or min(row_counts) <= 0:
            blockers.append("bar_rows_missing")
        if dual["score_rows"] <= 0 or dual["news_non_neutral_rows"] <= 0:
            blockers.append("dual_source_news_missing")
        if exog["rows"] <= 0 or exog["non_neutral_rows"] <= 0:
            blockers.append("exogenous_missing")
        if dual["community_non_neutral_rows"] == 0:
            warnings.append("historical_community_non_neutral_zero")
        sectors[sector] = {
            "status": "PASS" if not blockers else "BLOCKED",
            "name_ko": sector_names.get(sector, sector),
            "tickers": tickers,
            "ticker_count": len(tickers),
            "model_loaded_ticker_count": len(ticker_set & loaded),
            "missing_model_tickers": missing_model_tickers,
            "bar_files": {
                "min_per_ticker": min(file_counts) if file_counts else 0,
                "max_per_ticker": max(file_counts) if file_counts else 0,
                "total": sum(file_counts),
                "total_rows": sum(row_counts),
                "min_rows_per_ticker": min(row_counts) if row_counts else 0,
                "unreadable_file_count": unreadable,
            },
            "dual_source": dual,
            "exogenous": exog,
            "blockers": blockers,
            "warnings": warnings,
        }

    sector_statuses = [row["status"] for row in sectors.values()]
    report = {
        "status": "PASS" if sector_statuses and all(s == "PASS" for s in sector_statuses) else "BLOCKED",
        "action": "sector_readiness_audit",
        "generated_at": datetime.now(_KST).isoformat(),
        "bundle_id": bundle_id,
        "read_only": True,
        "external_api_called": False,
        "registry_mutated": False,
        "date_range": {"start": start_date, "end": end_date},
        "model": {
            "version": metadata.get("version"),
            "target_col": metadata.get("target_col"),
            "data_source": metadata.get("data_source"),
            "n_train_rows": metadata.get("n_train_rows"),
            "n_tickers": metadata.get("n_tickers"),
        },
        "sector_count": len(sectors),
        "sectors": sectors,
        "global_warnings": [
            "Historical community signal remains zero by design; community is currently a live Cold Path risk proxy, not a historical C12 alpha feature."
        ],
    }
    return report


def _write_report(report: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle_id = str(report.get("bundle_id", "BUNDLE-UNKNOWN"))
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"sector_readiness_{bundle_id}_{ts}.json"
    report["report_path"] = str(path)
    report["report_path_relative"] = _repo_relative(path)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = build_report(args)
    if not bool(args.no_write_report):
        _write_report(report, Path(str(args.output_dir)))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

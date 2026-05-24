#!/usr/bin/env python
"""Read-only serving feature readiness check for paper-auto.

This command verifies model-required Dual-Source serving features before any
broker order path is touched. It does not read .env, call external APIs, or
mutate registries.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.data.dual_source_runner import (  # noqa: E402
    DEFAULT_DUAL_SOURCE_ARTIFACT_DIR,
    load_latest_scores,
)
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check current-day required serving features for paper-auto.",
    )
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument(
        "--tickers",
        required=True,
        help="Comma-separated ticker list, e.g. 005930,000660.",
    )
    parser.add_argument(
        "--asof",
        default="",
        help="KST ISO timestamp. Defaults to now.",
    )
    parser.add_argument(
        "--metadata-path",
        default="",
        help="Optional explicit latest_model_metadata.json path.",
    )
    parser.add_argument(
        "--artifact-dir",
        default=str(DEFAULT_DUAL_SOURCE_ARTIFACT_DIR),
        help="Dual-Source artifact directory.",
    )
    parser.add_argument("--write-report", action="store_true")
    return parser.parse_args(argv)


def _resolve_path(raw: str | Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _parse_asof(raw: str) -> datetime:
    if not raw:
        return datetime.now(_KST)
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_KST)
    return dt.astimezone(_KST)


def _parse_feature_ts(value: Any) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_KST)
    return dt.astimezone(_KST)


def _feature_date_key(value: Any) -> str:
    return _parse_feature_ts(value).strftime("%Y%m%d")


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _metadata_candidates(bundle_id: str) -> list[Path]:
    candidates = [
        ROOT / "artifacts" / "bundles" / bundle_id / "lgbm" / "latest_model_metadata.json",
    ]
    paper_dir = ROOT / "artifacts" / "lgbm_paper_candidate" / bundle_id
    if paper_dir.exists():
        candidates.extend(sorted(paper_dir.glob("*_metadata.json")))
    return candidates


def _load_metadata(bundle_id: str, metadata_path: str = "") -> tuple[dict[str, Any], Path]:
    candidates = [_resolve_path(metadata_path)] if metadata_path else _metadata_candidates(bundle_id)
    for path in candidates:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data, path
    raise FileNotFoundError(
        f"model metadata not found for bundle_id={bundle_id}; "
        f"checked={[str(p) for p in candidates]}"
    )


def _required_dual_source_cols(metadata: dict[str, Any]) -> list[str]:
    pp_cfg = config_load("risk_config.yaml", "preprocessor") or {}
    dual_source_cols = {str(col) for col in pp_cfg.get("dual_source_feature_cols", [])}
    return sorted(
        str(col)
        for col in metadata.get("feature_cols", [])
        if str(col) in dual_source_cols
    )


def build_report(
    *,
    bundle_id: str,
    tickers: list[str],
    asof: str = "",
    metadata_path: str = "",
    artifact_dir: str | Path = DEFAULT_DUAL_SOURCE_ARTIFACT_DIR,
) -> dict[str, Any]:
    asof_dt = _parse_asof(asof)
    date_key = asof_dt.strftime("%Y%m%d")
    metadata, resolved_metadata_path = _load_metadata(bundle_id, metadata_path)
    required_cols = _required_dual_source_cols(metadata)
    padded = [pad_ticker(str(ticker)) for ticker in tickers if str(ticker).strip()]

    base_report: dict[str, Any] = {
        "schema_version": "1.0.0",
        "action": "serving_feature_readiness",
        "generated_at": datetime.now(_KST).isoformat(),
        "bundle_id": bundle_id,
        "asof": asof_dt.isoformat(),
        "date_key": date_key,
        "metadata_path": _display_path(resolved_metadata_path),
        "required_feature_cols": required_cols,
        "tickers": padded,
        "safety": {
            "external_api_called": False,
            "env_read": False,
            "registry_mutated": False,
            "live_trading_allowed": False,
        },
    }

    if not required_cols:
        return {
            **base_report,
            "status": "PASS",
            "required": False,
            "reason": "no_required_dual_source_features",
        }

    artifact_path = _resolve_path(artifact_dir) / f"{date_key}.json"
    artifact_label = _display_path(artifact_path)
    if not artifact_path.exists():
        return {
            **base_report,
            "status": "FAIL",
            "required": True,
            "reason": "required_feature_artifact_missing",
            "artifact": artifact_label,
            "missing_artifacts": [artifact_label],
        }

    rows = load_latest_scores(date_key, artifact_dir=_resolve_path(artifact_dir))
    row_by_ticker = {
        pad_ticker(str(row.get("ticker", ""))): row
        for row in rows
        if isinstance(row, dict)
    }
    missing_tickers: list[str] = []
    missing_cols: dict[str, list[str]] = {}
    invalid_cols: dict[str, list[str]] = {}
    missing_timestamp_tickers: list[str] = []
    future_rows: list[dict[str, str]] = []
    date_mismatch_rows: list[dict[str, str]] = []

    for ticker in padded:
        row = row_by_ticker.get(ticker)
        if not row:
            missing_tickers.append(ticker)
            continue
        batch_date = row.get("batch_date")
        if batch_date not in (None, ""):
            try:
                batch_date_key = _feature_date_key(batch_date)
            except (TypeError, ValueError):
                date_mismatch_rows.append({
                    "ticker": ticker,
                    "field": "batch_date",
                    "value": str(batch_date),
                    "reason": "batch_date_parse_failed",
                })
            else:
                if batch_date_key != date_key:
                    date_mismatch_rows.append({
                        "ticker": ticker,
                        "field": "batch_date",
                        "value": str(batch_date),
                        "reason": "feature_artifact_date_mismatch",
                    })
        has_timestamp = False
        for key in ("snapshot_ts", "generated_at"):
            raw_ts = row.get(key)
            if raw_ts in (None, ""):
                continue
            has_timestamp = True
            try:
                ts = _parse_feature_ts(raw_ts)
            except (TypeError, ValueError):
                future_rows.append({
                    "ticker": ticker,
                    "field": key,
                    "value": str(raw_ts),
                    "reason": "timestamp_parse_failed",
                })
                continue
            if ts.strftime("%Y%m%d") != date_key:
                date_mismatch_rows.append({
                    "ticker": ticker,
                    "field": key,
                    "value": ts.isoformat(),
                    "reason": "feature_artifact_date_mismatch",
                })
            if ts > asof_dt:
                future_rows.append({
                    "ticker": ticker,
                    "field": key,
                    "value": ts.isoformat(),
                    "reason": "future_feature_artifact",
                })
        if not has_timestamp:
            missing_timestamp_tickers.append(ticker)
        for col in required_cols:
            if col not in row:
                missing_cols.setdefault(ticker, []).append(col)
                continue
            if _float_or_none(row.get(col)) is None:
                invalid_cols.setdefault(ticker, []).append(col)

    blockers = []
    if missing_tickers:
        blockers.append("required_feature_ticker_missing")
    if missing_cols:
        blockers.append("required_feature_col_missing")
    if invalid_cols:
        blockers.append("required_feature_col_invalid")
    if missing_timestamp_tickers:
        blockers.append("required_feature_timestamp_missing")
    if future_rows:
        blockers.append("required_feature_timestamp_not_pit_safe")
    if date_mismatch_rows:
        blockers.append("required_feature_date_mismatch")

    return {
        **base_report,
        "status": "FAIL" if blockers else "PASS",
        "required": True,
        "artifact": artifact_label,
        "blockers": blockers,
        "missing_tickers": missing_tickers,
        "missing_feature_cols_by_ticker": missing_cols,
        "invalid_feature_cols_by_ticker": invalid_cols,
        "missing_timestamp_tickers": missing_timestamp_tickers,
        "future_rows": future_rows,
        "date_mismatch_rows": date_mismatch_rows,
        "matched_ticker_count": len(padded) - len(missing_tickers),
        "ticker_count": len(padded),
    }


def _write_report(report: dict[str, Any]) -> Path:
    out_dir = ROOT / "artifacts" / "reports" / "serving_feature_readiness"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"serving_feature_readiness_{report['bundle_id']}_{ts}.json"
    report["report_path"] = str(path)
    report["report_path_relative"] = str(path.relative_to(ROOT))
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    tickers = [part.strip() for part in str(args.tickers).split(",") if part.strip()]
    report = build_report(
        bundle_id=str(args.bundle_id),
        tickers=tickers,
        asof=str(args.asof),
        metadata_path=str(args.metadata_path),
        artifact_dir=str(args.artifact_dir),
    )
    if args.write_report:
        _write_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

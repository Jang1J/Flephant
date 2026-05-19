"""Pre-live gate runner for steps 1~9 before real account switching.

This script never reads .env files and never submits orders. It only inspects
local artifacts, reports, and SSOT config to decide whether the pre-live gates
are complete or still blocked.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
NEW_ROOT = REPO_ROOT / "new"
if str(NEW_ROOT) not in sys.path:
    sys.path.insert(0, str(NEW_ROOT))

from src.mode_b.service_policy_verifier import (  # noqa: E402
    normalize_service_policy_universe,
    service_policy_gate_pass,
    service_policy_universe_hash,
)
from src.utils.safe_cast import safe_bool, safe_int  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402
from src.utils.trading_calendar import (  # noqa: E402
    kospi_trading_dates_between,
    kospi_trading_start_date,
    previous_kospi_trading_day,
)
from scripts.live_data_readiness import _inspect_bar_file as _inspect_live_bar_file  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_DATA_ROOT = REPO_ROOT / "artifacts" / "data"
_REPORT_ROOT = REPO_ROOT / "artifacts" / "reports"
_GATE_REPORT_ROOT = _REPORT_ROOT / "prelive_gate"


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _previous_business_day(today: date | None = None) -> date:
    return previous_kospi_trading_day(today)


def _business_start_date(end: date, business_days: int) -> date:
    return kospi_trading_start_date(end, business_days)


def _business_dates_between(start: date, end: date) -> list[str]:
    return kospi_trading_dates_between(start, end)


def _final_dataset_gate_cfg() -> dict[str, Any]:
    cfg = _load_yaml(NEW_ROOT / "config" / "risk_config.yaml")
    gate_cfg = (
        cfg.get("backtest_agent", {})
        .get("deploy_decision_gate", {})
        .get("final_dataset_gate", {})
    )
    return gate_cfg if isinstance(gate_cfg, dict) else {}


def _active_tickers(
    max_tickers: int | None = 30,
    *,
    include_pending_data: bool | None = None,
) -> list[str]:
    cfg = _load_yaml(NEW_ROOT / "config" / "universe_config.yaml")
    gate_cfg = _final_dataset_gate_cfg()
    include_pending = (
        safe_bool(
            gate_cfg.get("include_pending_data_tickers"),
            default=False,
        )
        if include_pending_data is None
        else bool(include_pending_data)
    )
    allowed_statuses = {"active"}
    if include_pending:
        configured = gate_cfg.get("allowed_stock_statuses") or [
            "active",
            "pending_data",
        ]
        allowed_statuses = {str(status) for status in configured}
    allowed_sector_statuses = {"confirmed"}
    if include_pending:
        configured_sectors = gate_cfg.get("allowed_sector_statuses") or [
            "confirmed",
            "confirmed_pending_data",
        ]
        allowed_sector_statuses = {str(status) for status in configured_sectors}

    tickers: list[str] = []
    for sector in (cfg.get("sectors") or {}).values():
        if str(sector.get("status")) not in allowed_sector_statuses:
            continue
        for stock in sector.get("stocks", []) or []:
            if str(stock.get("status")) in allowed_statuses:
                tickers.append(pad_ticker(str(stock["ticker"])))
    return tickers[:max_tickers] if max_tickers is not None else tickers


def _parse_dataset_date(value: Any) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
            return date.fromisoformat(raw[:10])
    except ValueError:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        return datetime.strptime(digits[:8], "%Y%m%d").date()
    except ValueError:
        return None


def _extract_model_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("model_metadata", "candidate_model_metadata", "candidate_metadata"):
        raw = payload.get(key)
        if isinstance(raw, dict):
            return raw
    artifact = payload.get("candidate_artifact")
    if isinstance(artifact, dict):
        raw_meta = artifact.get("metadata")
        if isinstance(raw_meta, dict):
            return raw_meta
        if any(
            key in artifact
            for key in ("train_start", "train_end", "requested_tickers", "n_tickers")
        ):
            return artifact
    return {}


def _metadata_ticker_count(metadata: dict[str, Any]) -> tuple[int, list[str]]:
    for key in ("requested_tickers", "loaded_tickers"):
        raw = metadata.get(key)
        if isinstance(raw, list):
            tickers = sorted({
                pad_ticker(str(ticker))
                for ticker in raw
                if str(ticker).strip()
            })
            if tickers:
                return len(tickers), tickers
    return safe_int(metadata.get("n_tickers", 0), default=0, min_value=0), []


def _metadata_ticker_sets(metadata: dict[str, Any]) -> dict[str, list[str]]:
    sets: dict[str, list[str]] = {}
    for key in ("requested_tickers", "loaded_tickers"):
        raw = metadata.get(key)
        if isinstance(raw, list):
            tickers = normalize_service_policy_universe([
                str(ticker)
                for ticker in raw
                if str(ticker).strip()
            ])
            if tickers:
                sets[key] = tickers
    return sets


def _final_dataset_gate_result(payload: dict[str, Any]) -> dict[str, Any]:
    gate_cfg = _final_dataset_gate_cfg()
    if not gate_cfg:
        return {
            "status": "BLOCKED",
            "blockers": ["final_dataset_gate_config_missing"],
        }
    if not safe_bool(gate_cfg.get("required", True), default=True):
        return {"status": "PASS", "required": False, "blockers": []}

    metadata = _extract_model_metadata(payload)
    blockers: list[str] = []
    if not metadata:
        blockers.append("model_metadata_missing")

    expected_start = _parse_dataset_date(gate_cfg.get("expected_start_date"))
    expected_end = _parse_dataset_date(gate_cfg.get("expected_end_date"))
    train_start = _parse_dataset_date(metadata.get("train_start"))
    train_end = _parse_dataset_date(metadata.get("train_end"))
    min_days = safe_int(gate_cfg.get("min_business_days"), default=0, min_value=1)
    min_tickers = safe_int(gate_cfg.get("min_tickers"), default=0, min_value=1)
    data_source_required = str(gate_cfg.get("allowed_model_data_source") or "").strip()
    data_source = str(metadata.get("data_source") or "").strip()

    if expected_start is None or expected_end is None or min_days <= 0:
        blockers.append("final_dataset_gate_config_invalid_date_window")
    if min_tickers <= 0:
        blockers.append("final_dataset_gate_config_invalid_min_tickers")
    if train_start is None:
        blockers.append("train_start_missing_or_invalid")
    if train_end is None:
        blockers.append("train_end_missing_or_invalid")

    business_day_count = 0
    if train_start is not None and train_end is not None:
        if train_start > train_end:
            blockers.append("train_date_range_inverted")
        else:
            business_day_count = len(kospi_trading_dates_between(train_start, train_end))
            if expected_start is not None and train_start > expected_start:
                blockers.append("train_start_after_required_dataset_start")
            if expected_end is not None and train_end < expected_end:
                blockers.append("train_end_before_required_dataset_end")
            if business_day_count < min_days:
                blockers.append("business_day_count_below_final_dataset_min")

    ticker_count, tickers = _metadata_ticker_count(metadata)
    if ticker_count < min_tickers:
        blockers.append("ticker_count_below_final_dataset_min")
    expected_tickers = (
        normalize_service_policy_universe(_active_tickers(min_tickers))
        if min_tickers > 0
        else []
    )
    if min_tickers > 0:
        if not expected_tickers:
            blockers.append("final_dataset_expected_universe_missing")
        elif len(expected_tickers) != min_tickers:
            blockers.append("final_dataset_expected_universe_incomplete")
    expected_universe_hash = (
        service_policy_universe_hash(expected_tickers) if expected_tickers else None
    )
    observed_sets = _metadata_ticker_sets(metadata)
    observed_hashes = {
        key: service_policy_universe_hash(value)
        for key, value in observed_sets.items()
    }
    if expected_tickers:
        if ticker_count != len(expected_tickers):
            blockers.append("ticker_count_final_universe_mismatch")
        if not observed_sets:
            blockers.append("ticker_set_missing_for_final_dataset")
        for key, observed_tickers in observed_sets.items():
            if observed_tickers != expected_tickers:
                blockers.append(f"{key}_final_universe_mismatch")
    if data_source_required and data_source != data_source_required:
        blockers.append("model_data_source_not_allowed_for_final_dataset")
    if safe_bool(metadata.get("synthetic_fallback"), default=False):
        blockers.append("synthetic_fallback_not_allowed_for_final_dataset")
    missing_tickers = metadata.get("missing_tickers")
    if isinstance(missing_tickers, list) and missing_tickers:
        blockers.append("missing_tickers_present")

    return {
        "status": "PASS" if not blockers else "BLOCKED",
        "required": True,
        "blockers": blockers,
        "expected_start_date": expected_start.strftime("%Y%m%d") if expected_start else None,
        "expected_end_date": expected_end.strftime("%Y%m%d") if expected_end else None,
        "train_start": train_start.strftime("%Y%m%d") if train_start else None,
        "train_end": train_end.strftime("%Y%m%d") if train_end else None,
        "business_day_count": business_day_count,
        "min_business_days": min_days,
        "ticker_count": ticker_count,
        "min_tickers": min_tickers,
        "sample_tickers": tickers[:5],
        "expected_ticker_count": len(expected_tickers),
        "expected_universe_hash": expected_universe_hash,
        "observed_universe_hashes": observed_hashes,
        "data_source": data_source or None,
        "required_data_source": data_source_required or None,
    }


def _final_dataset_gate_pass(payload: dict[str, Any]) -> bool:
    return _final_dataset_gate_result(payload).get("status") == "PASS"


def _label_target_gate_pass(payload: dict[str, Any]) -> bool:
    """Deployable C12 evidence must match the current label target SSOT."""
    cfg = _load_yaml(NEW_ROOT / "config" / "risk_config.yaml")
    allowed_target_cols = _allowed_deploy_target_cols(cfg)
    if not allowed_target_cols:
        return False
    metadata = _extract_model_metadata(payload)
    return str(metadata.get("target_col") or "").strip() in allowed_target_cols


def _allowed_deploy_target_cols(risk_cfg: dict[str, Any]) -> list[str]:
    label_cfg = risk_cfg.get("label") or {}
    values = label_cfg.get("deploy_target_cols")
    cols: list[str] = []
    if isinstance(values, list):
        cols.extend(str(value).strip() for value in values)
    fallback = str(label_cfg.get("target_col") or "").strip()
    if fallback:
        cols.append(fallback)
    return [col for col in dict.fromkeys(cols) if col]


def _final_gate_min_business_days(default: int = 80) -> int:
    gate_cfg = _final_dataset_gate_cfg()
    return safe_int(
        gate_cfg.get("min_business_days", default),
        default=default,
        min_value=1,
    )


def _final_gate_min_tickers(default: int = 30) -> int:
    gate_cfg = _final_dataset_gate_cfg()
    return safe_int(gate_cfg.get("min_tickers", default), default=default, min_value=1)


def _stage(
    status: str,
    message: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {"status": status, "message": message}
    if detail:
        payload.update(detail)
    return payload


def _count_rows(path: Path) -> int | None:
    try:
        return int(len(pd.read_parquet(path)))
    except Exception:
        return None


def _bar_artifact_paths_for_date(ticker: str, day: str) -> list[Path]:
    ticker_dir = _DATA_ROOT / ticker
    if not ticker_dir.exists():
        return []
    prefix = f"bars_1m_{day}"
    return sorted(
        path
        for path in ticker_dir.iterdir()
        if path.name.startswith(prefix) and path.suffix in {".parquet", ".jsonl"}
    )


def _duplicate_bar_artifact(
    paths: list[Path],
    ticker: str,
    day: str,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "rows": None,
        "valid_artifact": False,
        "reason": "duplicate_date_artifacts",
        "date": day,
        "duplicate_date_artifacts": [_repo_relative(path) for path in paths],
    }


def _inspect_bar_artifact(path: Path, ticker: str, day: str) -> dict[str, Any]:
    """Inspect saved 1m parquet before counting it as train-ready evidence."""
    if not path.exists():
        return {
            "ticker": ticker,
            "rows": None,
            "valid_artifact": False,
            "reason": "missing_file",
        }
    inspection = _inspect_live_bar_file(path, day, pad_ticker(ticker), min_rows_per_day=300)
    rows = inspection.get("rows")
    valid_artifact = (
        rows is not None
        and inspection.get("timestamp_dates_match") is True
        and inspection.get("ticker_matches") is True
        and int(inspection.get("duplicate_ts_count") or 0) == 0
        and int(inspection.get("out_of_hours_count") or 0) == 0
        and inspection.get("session_span_ok") is True
        and inspection.get("max_gap_ok") is True
        and int(inspection.get("missing_ohlcv_count") or 0) == 0
        and int(inspection.get("non_finite_ohlcv_count") or 0) == 0
        and int(inspection.get("invalid_ohlcv_count") or 0) == 0
    )
    reason = None if valid_artifact else "artifact_integrity_failed"
    return {
        "ticker": ticker,
        "rows": rows,
        "valid_artifact": valid_artifact,
        "reason": reason,
        "timestamp_dates_match": inspection.get("timestamp_dates_match"),
        "ticker_matches": inspection.get("ticker_matches"),
        "duplicate_ts_count": inspection.get("duplicate_ts_count"),
        "out_of_hours_count": inspection.get("out_of_hours_count"),
        "first_ts": inspection.get("first_ts"),
        "last_ts": inspection.get("last_ts"),
        "session_span_minutes": inspection.get("session_span_minutes"),
        "session_span_ok": inspection.get("session_span_ok"),
        "max_gap_minutes": inspection.get("max_gap_minutes"),
        "max_gap_ok": inspection.get("max_gap_ok"),
        "missing_ohlcv_count": inspection.get("missing_ohlcv_count"),
        "non_finite_ohlcv_count": inspection.get("non_finite_ohlcv_count"),
        "invalid_ohlcv_count": inspection.get("invalid_ohlcv_count"),
        "path": _repo_relative(path),
    }


def _latest_matching_report(
    report_dir: Path,
    prefix: str,
    predicate,
) -> tuple[Path | None, dict[str, Any] | None]:
    if not report_dir.exists():
        return None, None
    for path in sorted(report_dir.glob(f"{prefix}*.json"), reverse=True):
        try:
            data = _load_json(path)
        except Exception:
            continue
        if predicate(data):
            return path, data
    return None, None


def _metadata_created_at_utc(meta: dict[str, Any]) -> datetime:
    raw = meta.get("created_at")
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_KST)
    return parsed.astimezone(timezone.utc)


def _check_code_ssot() -> dict[str, Any]:
    required_files = [
        NEW_ROOT / "specs" / "api_contracts.md",
        NEW_ROOT / "docs" / "architecture.md",
        NEW_ROOT / "config" / "risk_config.yaml",
        NEW_ROOT / "config" / "universe_config.yaml",
    ]
    missing = [_repo_relative(path) for path in required_files if not path.exists()]
    try:
        risk_cfg = _load_yaml(NEW_ROOT / "config" / "risk_config.yaml")
        universe_cfg = _load_yaml(NEW_ROOT / "config" / "universe_config.yaml")
    except Exception as e:
        return _stage("FAIL", "SSOT config parse failed.", {"error": str(e)})

    if missing:
        return _stage("FAIL", "Required SSOT files are missing.", {"missing": missing})
    trade_universe_count = len(_active_tickers(_final_gate_min_tickers()))
    return _stage(
        "PASS",
        "SSOT files parse and final trade universe is available.",
        {
            "trade_universe_tickers_checked": trade_universe_count,
            "final_dataset_gate": _final_dataset_gate_cfg(),
            "execution_mode": risk_cfg.get("execution", {}).get("mode"),
            "live_enabled": risk_cfg.get("execution", {}).get("live_enabled"),
            "universe_sections": sorted((universe_cfg.get("sectors") or {}).keys()),
        },
    )


def _check_real_readiness(end_yyyymmdd: str) -> dict[str, Any]:
    def predicate(data: dict[str, Any]) -> bool:
        stages = data.get("stages", {})
        smoke = stages.get("smoke", {})
        backfill = stages.get("backfill", {})
        train = stages.get("train", {})
        return (
            data.get("status") == "PASS"
            and data.get("end_date") == end_yyyymmdd
            and bool(smoke)
            and backfill.get("status") == "PASS"
            and train.get("status") == "PASS"
            and not safe_bool(data.get("allow_mock", False), default=True)
        )

    path, data = _latest_matching_report(
        _REPORT_ROOT / "data_readiness",
        "data_readiness_",
        predicate,
    )
    if not path or not data:
        return _stage(
            "BLOCKED",
            "No PASS real readiness report with smoke+backfill was found.",
            {"end_date": end_yyyymmdd},
        )

    secret_presence = data.get("runtime", {}).get("secret_presence", {})
    smoke = data.get("stages", {}).get("smoke", {})
    backfill = data.get("stages", {}).get("backfill", {})
    train = data.get("stages", {}).get("train", {})
    train_result = train.get("result", {}) if isinstance(train, dict) else {}
    if not isinstance(train_result, dict):
        train_result = {}
    return _stage(
        "PASS",
        "Latest real readiness report passed.",
        {
            "report_path": _repo_relative(path),
            "generated_at": data.get("generated_at"),
            "secret_presence": secret_presence,
            "smoke_statuses": {
                key: value.get("status")
                for key, value in smoke.items()
                if isinstance(value, dict)
            },
            "backfill_tickers": len(backfill.get("counts", {})),
            "backfill_min_rows": (
                min(backfill.get("counts", {}).values())
                if backfill.get("counts")
                else None
            ),
            "train_status": train.get("status"),
            "train_data_source": train.get("data_source") or train_result.get("data_source"),
        },
    )


def _check_80_day_artifacts(
    tickers: list[str],
    end_yyyymmdd: str,
    business_days: int,
    min_rows_per_day: int,
) -> dict[str, Any]:
    end_dt = datetime.strptime(end_yyyymmdd, "%Y%m%d").date()
    start_dt = _business_start_date(end_dt, business_days)
    required_dates = _business_dates_between(start_dt, end_dt)
    valid_dates: list[str] = []
    sample_missing: dict[str, list[dict[str, Any]]] = {}

    for day in required_dates:
        missing_or_short: list[dict[str, Any]] = []
        for ticker in tickers:
            paths = _bar_artifact_paths_for_date(ticker, day)
            if len(paths) > 1:
                missing_or_short.append(_duplicate_bar_artifact(paths, ticker, day))
                continue
            path = paths[0] if paths else _DATA_ROOT / ticker / f"bars_1m_{day}.parquet"
            inspection = _inspect_bar_artifact(path, ticker, day)
            rows = inspection.get("rows")
            if (
                rows is None
                or rows < min_rows_per_day
                or inspection.get("valid_artifact") is not True
            ):
                missing_or_short.append(inspection)
        if missing_or_short:
            if len(sample_missing) < 5:
                sample_missing[day] = missing_or_short[:5]
        else:
            valid_dates.append(day)

    status = "PASS" if len(valid_dates) >= business_days else "BLOCKED"
    return _stage(
        status,
        "80-business-day real 1m artifact gate checked.",
        {
            "start_date": start_dt.strftime("%Y%m%d"),
            "end_date": end_yyyymmdd,
            "required_dates": business_days,
            "valid_dates": len(valid_dates),
            "min_rows_per_day": min_rows_per_day,
            "ticker_count": len(tickers),
            "first_valid_date": valid_dates[0] if valid_dates else None,
            "last_valid_date": valid_dates[-1] if valid_dates else None,
            "sample_missing_or_short": sample_missing,
        },
    )


def _latest_lgbm_metadata(
    bundle_id: str | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    registry_path = REPO_ROOT / "artifacts" / "lgbm" / "registry.json"
    if not registry_path.exists():
        return None, None
    registry = _load_json(registry_path)
    versions = registry.get("versions") or []
    if not versions:
        return None, None

    requested_bundle_id = str(bundle_id or "").strip()
    if requested_bundle_id:
        matching_versions = [
            meta
            for meta in versions
            if str(meta.get("bundle_id") or "") == requested_bundle_id
        ]
        if not matching_versions:
            return None, None
        latest_match = sorted(
            matching_versions,
            key=_metadata_created_at_utc,
        )[-1]
        return str(latest_match.get("version")), latest_match

    candidate_versions = [
        meta
        for meta in versions
        if meta.get("status") == "candidate"
    ]
    if candidate_versions:
        latest_candidate = sorted(
            candidate_versions,
            key=_metadata_created_at_utc,
        )[-1]
        return str(latest_candidate.get("version")), latest_candidate

    active_version = registry.get("active_version")
    if active_version:
        for meta in versions:
            if meta.get("version") == active_version:
                return str(active_version), meta
    latest = versions[-1]
    return str(latest.get("version")), latest


def _staged_lgbm_metadata(
    bundle_id: str,
) -> tuple[str | None, dict[str, Any] | None, Path, Path]:
    bundle_lgbm_dir = REPO_ROOT / "artifacts" / "bundles" / bundle_id / "lgbm"
    metadata_path = bundle_lgbm_dir / "latest_model_metadata.json"
    model_path = bundle_lgbm_dir / "latest_model.pkl"
    if not metadata_path.exists():
        return None, None, metadata_path, model_path
    meta = _load_json(metadata_path)
    if not isinstance(meta, dict):
        return None, None, metadata_path, model_path
    version = str(meta.get("version") or "staged_bundle")
    return version, meta, metadata_path, model_path


def _check_lgbm_real_train(bundle_id: str | None = None) -> dict[str, Any]:
    requested_bundle_id = str(bundle_id or "").strip()
    metadata_source = "production_registry"
    staged_metadata_path: Path | None = None
    staged_model_path: Path | None = None
    if requested_bundle_id:
        version, meta, staged_metadata_path, staged_model_path = _staged_lgbm_metadata(
            requested_bundle_id
        )
        metadata_source = "staged_bundle"
    else:
        version, meta = _latest_lgbm_metadata(None)
    if not meta:
        detail = (
            {
                "requested_bundle_id": requested_bundle_id,
                "metadata_source": metadata_source,
                "metadata_path": (
                    str(staged_metadata_path.relative_to(REPO_ROOT))
                    if staged_metadata_path is not None
                    else None
                ),
                "model_path": (
                    str(staged_model_path.relative_to(REPO_ROOT))
                    if staged_model_path is not None
                    else None
                ),
            }
            if requested_bundle_id
            else None
        )
        return _stage(
            "BLOCKED",
            (
                "No staged LightGBM metadata was found for the requested bundle id."
                if requested_bundle_id
                else "No LightGBM registry metadata was found."
            ),
            detail,
        )
    risk_cfg = _load_yaml(NEW_ROOT / "config" / "risk_config.yaml")
    label_cfg = risk_cfg.get("label") or {}
    required_label_version = label_cfg.get("generation_version")
    required_label_scope = label_cfg.get("session_scope")
    allowed_target_cols = _allowed_deploy_target_cols(risk_cfg)
    if not required_label_version:
        return _stage(
            "BLOCKED",
            "risk_config.yaml label.generation_version is required for real LightGBM gate.",
        )
    if not required_label_scope:
        return _stage(
            "BLOCKED",
            "risk_config.yaml label.session_scope is required for real LightGBM gate.",
        )
    if not allowed_target_cols:
        return _stage(
            "BLOCKED",
            "risk_config.yaml label.deploy_target_cols or label.target_col is required for real LightGBM gate.",
        )
    if staged_model_path is not None:
        model_path = str(staged_model_path.relative_to(REPO_ROOT))
        model_exists = staged_model_path.exists() and staged_model_path.stat().st_size > 0
    else:
        model_path = meta.get("model_path")
        model_exists = bool(model_path and (REPO_ROOT / str(model_path)).exists())
    synthetic = safe_bool(meta.get("synthetic_fallback"), default=False)
    data_source = meta.get("data_source")
    real_data_source = data_source == "artifact_bars"
    bundle_id = meta.get("bundle_id")
    bundle_id_matches_request = (
        bundle_id == requested_bundle_id if requested_bundle_id else None
    )
    actual_label_version = meta.get("label_generation_version")
    actual_label_scope = meta.get("label_session_scope")
    actual_target_col = meta.get("target_col")
    label_version_ok = actual_label_version == required_label_version
    label_scope_ok = actual_label_scope == required_label_scope
    target_col_ok = str(actual_target_col or "").strip() in allowed_target_cols
    final_dataset_gate = _final_dataset_gate_result({"model_metadata": meta})
    final_dataset_gate_ok = final_dataset_gate.get("status") == "PASS"
    status = (
        "PASS"
        if model_exists
        and not synthetic
        and real_data_source
        and label_version_ok
        and label_scope_ok
        and target_col_ok
        and final_dataset_gate_ok
        and (bundle_id_matches_request is not False)
        else "BLOCKED"
    )
    message = "Latest LightGBM artifact inspected."
    if bundle_id_matches_request is False:
        message = "Staged LightGBM artifact bundle_id does not match the requested bundle id."
    elif not model_exists:
        message = "Staged LightGBM model file is missing or empty." if requested_bundle_id else "Latest LightGBM model file is missing."
    elif not label_version_ok:
        message = "Latest LightGBM artifact has stale or missing label generation metadata."
    elif not label_scope_ok:
        message = "Latest LightGBM artifact has stale or missing label session scope metadata."
    elif not target_col_ok:
        message = "Latest LightGBM artifact target_col does not match risk_config.yaml label.target_col."
    elif not final_dataset_gate_ok:
        blockers = final_dataset_gate.get("blockers") or ["final_dataset_gate_blocked"]
        message = f"Latest LightGBM artifact failed final_dataset_gate: {blockers}"
    elif not real_data_source:
        message = "Latest LightGBM artifact is not marked as artifact_bars real data."
    return _stage(
        status,
        message,
        {
            "version": version,
            "requested_bundle_id": requested_bundle_id or None,
            "metadata_source": metadata_source,
            "metadata_path": (
                str(staged_metadata_path.relative_to(REPO_ROOT))
                if staged_metadata_path is not None
                else None
            ),
            "bundle_id": bundle_id,
            "candidate_bundle_id": bundle_id,
            "bundle_id_matches_request": bundle_id_matches_request,
            "registry_status": meta.get("status"),
            "model_path": model_path,
            "model_exists": model_exists,
            "synthetic_fallback": synthetic,
            "data_source": data_source,
            "real_data_source": real_data_source,
            "label_generation_version": actual_label_version,
            "required_label_generation_version": required_label_version,
            "label_session_scope": actual_label_scope,
            "required_label_session_scope": required_label_scope,
            "target_col": actual_target_col,
            "required_target_col": label_cfg.get("target_col"),
            "allowed_deploy_target_cols": allowed_target_cols,
            "n_train_rows": meta.get("n_train_rows"),
            "train_start": meta.get("train_start"),
            "train_end": meta.get("train_end"),
            "requested_tickers": meta.get("requested_tickers"),
            "loaded_tickers": meta.get("loaded_tickers"),
            "missing_tickers": meta.get("missing_tickers"),
            "n_tickers": meta.get("n_tickers"),
            "final_dataset_gate": final_dataset_gate,
            "metrics": meta.get("metrics", {}),
            "metric_scope": meta.get(
                "metric_scope",
                {
                    "scope": "trainer_validation_proxy",
                    "deploy_quality": False,
                    "reason": "C12 real backtest is required before deploy.",
                },
            ),
        },
    )


def _is_deployable_backtest_report(payload: dict[str, Any], bundle_id: str) -> bool:
    regression = payload.get("regression_risk") or {}
    leakage = payload.get("minute_bar_leakage_check") or {}
    return (
        payload.get("bundle_id") == bundle_id
        and payload.get("verdict") == "pass"
        and not safe_bool(regression.get("flagged", False), default=True)
        and leakage.get("verdict") == "pass"
        and _feature_quality_gate_pass(payload)
        and _service_policy_gate_pass(payload, bundle_id)
        and _final_dataset_gate_pass(payload)
        and _label_target_gate_pass(payload)
    )


def _feature_quality_gate_pass(payload: dict[str, Any]) -> bool:
    """Require non-neutral Dual-Source/exogenous coverage for deployability."""
    cfg = _load_yaml(NEW_ROOT / "config" / "risk_config.yaml")
    gate_cfg = (
        cfg.get("backtest_agent", {})
        .get("deploy_decision_gate", {})
        .get("feature_quality_gate", {})
    )
    min_dual = float(gate_cfg.get("min_dual_source_non_neutral_row_coverage", 0.8))
    min_exog = float(gate_cfg.get("min_exogenous_non_neutral_row_coverage", 0.8))
    feature_quality = payload.get("feature_quality") or {}
    dual_rows = int(feature_quality.get("dual_source_rows", 0) or 0)
    dual_non_neutral = int(feature_quality.get("dual_source_non_neutral_rows", 0) or 0)
    exog_rows = int(feature_quality.get("exogenous_rows", 0) or 0)
    exog_non_neutral = int(feature_quality.get("exogenous_non_neutral_rows", 0) or 0)
    if dual_rows <= 0 or exog_rows <= 0:
        return False
    dual_rate = dual_non_neutral / max(dual_rows, 1)
    exog_rate = exog_non_neutral / max(exog_rows, 1)
    return dual_rate >= min_dual and exog_rate >= min_exog


def _service_policy_gate_pass(payload: dict[str, Any], bundle_id: str) -> bool:
    """Require C12-embedded service-policy replay PASS before C14 deploy."""
    evidence = payload.get("service_policy_replay")
    return service_policy_gate_pass(
        evidence if isinstance(evidence, dict) else None,
        bundle_id=bundle_id,
        repo_root=_service_policy_repo_root(),
        expected_date_range=(
            payload.get("service_policy_expected_date_range")
            or payload.get("date_range")
        ),
        expected_universe=_active_tickers(_final_gate_min_tickers()),
    )


def _service_policy_repo_root() -> Path:
    if _REPORT_ROOT.name == "reports" and _REPORT_ROOT.parent.name == "artifacts":
        return _REPORT_ROOT.parents[1]
    return _REPORT_ROOT


def _check_backtest_gate(lgbm_stage: dict[str, Any]) -> dict[str, Any]:
    cli_path = NEW_ROOT / "src" / "jobs" / "run_backtest.py"
    if lgbm_stage.get("status") != "PASS":
        return _stage(
            "BLOCKED",
            "Real backtest requires a non-synthetic LightGBM candidate first.",
            {
                "upstream": "lgbm_real_train",
                "cli_available": cli_path.exists(),
                "cli_path": _repo_relative(cli_path),
            },
        )

    bundle_id = lgbm_stage.get("candidate_bundle_id") or lgbm_stage.get("bundle_id")
    if not bundle_id:
        return _stage(
            "BLOCKED",
            "No LightGBM candidate bundle id was found for real backtest.",
            {
                "upstream": "lgbm_real_train",
                "cli_available": cli_path.exists(),
                "cli_path": _repo_relative(cli_path),
                "lgbm_version": lgbm_stage.get("version"),
                "registry_status": lgbm_stage.get("registry_status"),
            },
        )

    prefix = f"backtest_{bundle_id}_"
    report_dir = _REPORT_ROOT / "backtest"
    path, data = _latest_matching_report(
        report_dir,
        prefix,
        lambda payload: _is_deployable_backtest_report(payload, bundle_id),
    )
    if path and data:
        return _stage(
            "PASS",
            "PASS real backtest report artifact was found for the candidate.",
            {
                "bundle_id": bundle_id,
                "report_path": _repo_relative(path),
                "generated_at": data.get("generated_at"),
                "backtest_id": data.get("backtest_id"),
                "verdict": data.get("verdict"),
                "metrics": data.get("metrics", {}),
                "regression_risk": data.get("regression_risk"),
                "minute_bar_leakage_check": data.get("minute_bar_leakage_check"),
                "final_dataset_gate": _final_dataset_gate_result(data),
                "cli_available": cli_path.exists(),
                "cli_path": _repo_relative(cli_path),
            },
        )

    latest_path, latest_data = _latest_matching_report(
        report_dir,
        prefix,
        lambda payload: payload.get("bundle_id") == bundle_id,
    )
    latest_detail: dict[str, Any] = {}
    if latest_path and latest_data:
        regression = latest_data.get("regression_risk") or {}
        leakage = latest_data.get("minute_bar_leakage_check") or {}
        service_policy = latest_data.get("service_policy_replay") or {}
        latest_detail = {
            "latest_report_path": _repo_relative(latest_path),
            "latest_verdict": latest_data.get("verdict"),
            "latest_status": latest_data.get("status"),
            "latest_regression_flagged": regression.get("flagged"),
            "latest_leakage_verdict": leakage.get("verdict"),
            "latest_feature_quality_gate_pass": _feature_quality_gate_pass(latest_data),
            "latest_feature_quality": latest_data.get("feature_quality") or {},
            "latest_final_dataset_gate": _final_dataset_gate_result(latest_data),
            "latest_service_policy_gate_pass": _service_policy_gate_pass(
                latest_data,
                bundle_id,
            ),
            "latest_service_policy_replay_status": (
                service_policy.get("status")
                if isinstance(service_policy, dict)
                else None
            ),
            "latest_service_policy_report_path": (
                service_policy.get("service_policy_report_path")
                or service_policy.get("report_path")
                if isinstance(service_policy, dict)
                else None
            ),
            "latest_service_policy_report_sha256": (
                service_policy.get("service_policy_report_sha256")
                if isinstance(service_policy, dict)
                else None
            ),
            "latest_service_policy_expected_date_range": (
                latest_data.get("service_policy_expected_date_range")
                or latest_data.get("date_range")
            ),
        }
    return _stage(
        "BLOCKED",
        "No PASS real backtest report artifact was found for the real candidate.",
        {
            "bundle_id": bundle_id,
            "cli_available": cli_path.exists(),
            "cli_path": _repo_relative(cli_path),
            **latest_detail,
        },
    )


def _latest_paper_report(action: str) -> tuple[Path | None, dict[str, Any] | None]:
    return _latest_matching_report(
        _REPORT_ROOT / "paper_trading",
        f"paper_trading_{action}_",
        lambda data: data.get("action") == action,
    )


def _paper_auto_bundle_ids(data: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in ("bundle_id", "model_bundle_id"):
        value = data.get(key)
        if value:
            out.add(str(value))
    active_model = (
        ((data.get("stages") or {}).get("paper_auto_cycle") or {})
        .get("stages", {})
        .get("active_model_guard")
    )
    if isinstance(active_model, dict) and active_model.get("bundle_id"):
        out.add(str(active_model["bundle_id"]))
    return out


def _paper_auto_cycle_history_matched(data: dict[str, Any]) -> bool:
    cycle_stage = ((data.get("stages") or {}).get("paper_auto_cycle") or {})
    cycle_stages = cycle_stage.get("stages") if isinstance(cycle_stage, dict) else None
    cycles = cycle_stages.get("cycles") if isinstance(cycle_stages, dict) else None
    items = cycles.get("items", []) if isinstance(cycles, dict) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        history = item.get("order_history_verification")
        if not isinstance(history, dict) or history.get("status") != "PASS":
            continue
        for query in history.get("queries", []) or []:
            if isinstance(query, dict) and safe_int(
                query.get("matched_order_count", 0),
                default=0,
                min_value=0,
            ) > 0:
                return True
    return False


def _parse_report_ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_KST)
    return parsed.astimezone(_KST)


def _paper_evidence_max_age_sec() -> int:
    cfg = _load_yaml(NEW_ROOT / "config" / "risk_config.yaml")
    paper_cfg = cfg.get("paper_trading", {})
    return safe_int(
        paper_cfg.get("evidence_max_age_sec", 86400),
        default=86400,
        min_value=1,
    )


def _fresh_generated_at_state(
    value: Any,
    *,
    max_age_sec: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    generated_at = _parse_report_ts(value)
    if generated_at is None:
        return {
            "status": "BLOCKED",
            "reason": "generated_at_missing_or_invalid",
            "generated_at": value,
            "max_age_sec": max_age_sec,
        }
    ref_now = now or datetime.now(_KST)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=_KST)
    age_sec = (ref_now.astimezone(_KST) - generated_at).total_seconds()
    fresh = 0 <= age_sec <= max_age_sec
    return {
        "status": "PASS" if fresh else "BLOCKED",
        "reason": None if fresh else "generated_at_stale_or_future",
        "generated_at": generated_at.isoformat(),
        "age_sec": round(age_sec, 3),
        "max_age_sec": max_age_sec,
    }


def _paper_auto_evidence_guard(data: dict[str, Any]) -> dict[str, Any]:
    guard = data.get("evidence_guard")
    if isinstance(guard, dict):
        return guard
    preflight = (data.get("stages") or {}).get("preflight")
    if not isinstance(preflight, dict):
        return {}
    paper_evidence = (preflight.get("stages") or {}).get("paper_evidence")
    if not isinstance(paper_evidence, dict):
        return {}
    nested_guard = paper_evidence.get("evidence_guard")
    return nested_guard if isinstance(nested_guard, dict) else {}


def _paper_auto_broker_evidence_nested_state(data: dict[str, Any]) -> dict[str, Any]:
    broker_evidence = data.get("broker_evidence")
    if not isinstance(broker_evidence, dict):
        return {
            "status": "BLOCKED",
            "reason": "broker_evidence_missing",
            "stage_statuses": {},
        }
    required = (
        "balance_reconciliation",
        "probe_order",
        "order_history_requery",
    )
    statuses: dict[str, str] = {}
    for name in required:
        stage = broker_evidence.get(name)
        statuses[name] = (
            str(stage.get("status", "MISSING")).upper()
            if isinstance(stage, dict)
            else "MISSING"
        )
    passed = all(status == "PASS" for status in statuses.values())
    return {
        "status": "PASS" if passed else "BLOCKED",
        "reason": None if passed else "broker_evidence_stage_not_pass",
        "stage_statuses": statuses,
    }


def _latest_paper_auto_bundle_report(
    bundle_id: str | None,
) -> tuple[Path | None, dict[str, Any] | None]:
    requested_bundle_id = str(bundle_id or "").strip()
    if not requested_bundle_id:
        return None, None
    return _latest_matching_report(
        _REPORT_ROOT / "paper_auto_trading",
        "paper_auto_service_rehearsal_",
        lambda data: (
            safe_bool(data.get("external_kis_api"), default=False)
            and requested_bundle_id in _paper_auto_bundle_ids(data)
        ),
    )


def _paper_auto_bundle_stage(
    bundle_id: str,
    stage_name: str,
    *,
    require_order_history_match: bool = False,
) -> dict[str, Any]:
    path, data = _latest_paper_auto_bundle_report(bundle_id)
    if not path or not data:
        return _stage(
            "BLOCKED",
            "No external paper-auto bundle evidence report was found.",
            {
                "blocker": "paper_auto_bundle_evidence_missing",
                "bundle_id": bundle_id,
            },
        )
    stage_statuses = data.get("stage_statuses", {})
    if not isinstance(stage_statuses, dict):
        stage_statuses = {}
    history_matched = _paper_auto_cycle_history_matched(data)
    freshness = _fresh_generated_at_state(
        data.get("generated_at"),
        max_age_sec=_paper_evidence_max_age_sec(),
    )
    nested_evidence = _paper_auto_broker_evidence_nested_state(data)
    evidence_guard = _paper_auto_evidence_guard(data)
    evidence_guard_pass = str(evidence_guard.get("status", "")).upper() == "PASS"
    passed = (
        data.get("status") == "PASS"
        and stage_statuses.get(stage_name) == "PASS"
        and freshness.get("status") == "PASS"
        and nested_evidence.get("status") == "PASS"
        and evidence_guard_pass
        and (
            not require_order_history_match
            or (
                stage_statuses.get("order_history_requery") == "PASS"
                and history_matched
            )
        )
    )
    return _stage(
        "PASS" if passed else "BLOCKED",
        "Latest external paper-auto bundle evidence inspected.",
        {
            "report_path": _repo_relative(path),
            "bundle_id": bundle_id,
            "bundle_ids": sorted(_paper_auto_bundle_ids(data)),
            "external_kis_api": safe_bool(data.get("external_kis_api"), default=False),
            "evidence_level": data.get("evidence_level"),
            "report_status": data.get("status"),
            "stage_name": stage_name,
            "stage_statuses": stage_statuses,
            "freshness": freshness,
            "broker_evidence_nested": nested_evidence,
            "evidence_guard": evidence_guard,
            "paper_auto_cycle_history_matched": history_matched,
        },
    )


def _matched_order_count(report_or_stage: dict[str, Any] | None) -> int:
    if not isinstance(report_or_stage, dict):
        return 0
    direct = report_or_stage.get("matched_order_count")
    if direct is not None:
        return safe_int(direct, default=0, min_value=0)
    stage = report_or_stage.get("stages", {}).get("order_history")
    if isinstance(stage, dict):
        return safe_int(stage.get("matched_order_count", 0), default=0, min_value=0)
    return 0


def _check_paper_balance(bundle_id: str | None = None) -> dict[str, Any]:
    requested_bundle_id = str(bundle_id or "").strip()
    if requested_bundle_id:
        return _paper_auto_bundle_stage(
            requested_bundle_id,
            "balance_reconciliation",
        )
    path, data = _latest_paper_report("balance_reconciliation")
    if not path or not data:
        return _stage("BLOCKED", "No paper balance report was found.")
    return _stage(
        "PASS" if data.get("status") == "PASS" else "BLOCKED",
        "Latest paper balance report inspected.",
        {
            "report_path": _repo_relative(path),
            "report_status": data.get("status"),
            "failures": data.get("failures", []),
            "mode_guard": data.get("stages", {}).get("mode_guard"),
            "balance": data.get("stages", {}).get("balance"),
        },
    )


def _check_paper_reconciliation(bundle_id: str | None = None) -> dict[str, Any]:
    requested_bundle_id = str(bundle_id or "").strip()
    if requested_bundle_id:
        return _paper_auto_bundle_stage(
            requested_bundle_id,
            "balance_reconciliation",
        )
    path, data = _latest_paper_report("balance_reconciliation")
    if not path or not data:
        return _stage("BLOCKED", "No paper reconciliation report was found.")
    recon = data.get("stages", {}).get("reconciliation")
    status = "PASS" if isinstance(recon, dict) and recon.get("status") == "PASS" else "BLOCKED"
    return _stage(
        status,
        "Latest paper reconciliation stage inspected.",
        {
            "report_path": _repo_relative(path),
            "reconciliation": recon,
        },
    )


def _check_probe_order(bundle_id: str | None = None) -> dict[str, Any]:
    requested_bundle_id = str(bundle_id or "").strip()
    if requested_bundle_id:
        return _paper_auto_bundle_stage(
            requested_bundle_id,
            "probe_order",
            require_order_history_match=True,
        )
    path, data = _latest_paper_report("submit_probe_order")
    if not path or not data:
        return _stage("BLOCKED", "No paper probe order report was found.")
    execution = data.get("stages", {}).get("execution")
    order_history = data.get("stages", {}).get("order_history")
    matched_order_count = _matched_order_count(data)
    status = (
        "PASS"
        if (
            isinstance(execution, dict)
            and execution.get("status") == "PASS"
            and isinstance(order_history, dict)
            and order_history.get("status") == "PASS"
            and matched_order_count > 0
        )
        else "BLOCKED"
    )
    return _stage(
        status,
        "Latest paper probe order report inspected.",
        {
            "report_path": _repo_relative(path),
            "report_status": data.get("status"),
            "order_guard": data.get("stages", {}).get("order_guard"),
            "execution": execution,
            "order_history": order_history,
            "matched_order_count": matched_order_count,
        },
    )


def _check_ops_risk() -> dict[str, Any]:
    risk_cfg = _load_yaml(NEW_ROOT / "config" / "risk_config.yaml")
    execution = risk_cfg.get("execution", {})
    paper = risk_cfg.get("paper_trading", {})
    paper_auto = risk_cfg.get("paper_auto_trading", {})
    paper_auto_qty_cap = int(paper_auto.get("max_order_qty_per_order_prelive_cap", 1))
    checks = {
        "execution_live_disabled": not safe_bool(execution.get("live_enabled"), default=True),
        "execution_not_live": execution.get("mode") != "live",
        "paper_requires_virtual": safe_bool(paper.get("require_virtual_mode"), default=False),
        "probe_qty_limited_to_one": int(paper.get("max_probe_order_qty", 0)) <= 1,
        "market_order_disabled": not safe_bool(paper.get("allow_market_order"), default=True),
        "confirm_phrase_configured": bool(paper.get("confirm_order_phrase")),
        "paper_auto_requires_virtual": safe_bool(paper_auto.get("require_virtual_mode"), default=False),
        "paper_auto_requires_prelive_pass": safe_bool(paper_auto.get("require_prelive_pass"), default=False),
        "paper_auto_requires_active_model": safe_bool(paper_auto.get("require_active_model"), default=False),
        "paper_auto_confirm_phrase_configured": bool(paper_auto.get("confirm_start_phrase")),
        "paper_auto_order_qty_limited": (
            int(paper_auto.get("max_order_qty_per_order", 0)) <= paper_auto_qty_cap
        ),
        "paper_auto_market_order_disabled": not safe_bool(paper_auto.get("allow_market_order"), default=True),
    }
    status = "PASS" if all(checks.values()) else "FAIL"
    return _stage(
        status,
        "Operational risk gates inspected from risk_config.yaml.",
        {
            "checks": checks,
            "execution": execution,
            "paper_trading": paper,
            "paper_auto_trading": paper_auto,
        },
    )


def _configured_train_min_rows(risk_cfg: dict[str, Any]) -> tuple[int | None, dict[str, Any] | None]:
    readiness_cfg = risk_cfg.get("live_data_readiness")
    if not isinstance(readiness_cfg, dict):
        return None, _stage(
            "BLOCKED",
            "risk_config.yaml live_data_readiness section is required.",
        )
    if "train_min_rows_per_day" not in readiness_cfg:
        return None, _stage(
            "BLOCKED",
            "risk_config.yaml live_data_readiness.train_min_rows_per_day is required.",
        )
    try:
        return int(readiness_cfg["train_min_rows_per_day"]), None
    except (TypeError, ValueError) as e:
        return None, _stage(
            "BLOCKED",
            "risk_config.yaml live_data_readiness.train_min_rows_per_day must be an integer.",
            {"error": str(e)},
        )


def build_report(
    *,
    end_date: str,
    business_days: int,
    max_tickers: int,
    bundle_id: str | None = None,
) -> dict[str, Any]:
    risk_cfg = _load_yaml(NEW_ROOT / "config" / "risk_config.yaml")
    tickers = _active_tickers(max_tickers)

    stages: dict[str, Any] = {}
    min_rows, config_blocker = _configured_train_min_rows(risk_cfg)
    stages["01_code_ssot"] = _check_code_ssot()
    if config_blocker is not None:
        stages["02_config"] = config_blocker
        blockers = [
            name
            for name, stage in stages.items()
            if stage.get("status") in {"BLOCKED", "FAIL"}
        ]
        return {
            "status": "BLOCKED",
            "generated_at": datetime.now(_KST).isoformat(),
            "end_date": end_date,
            "business_days": business_days,
            "tickers": tickers,
            "stages": stages,
            "blockers": blockers,
            "bundle_id": str(bundle_id or "").strip() or None,
            "next_commands": _next_commands(
                end_date=end_date,
                business_days=business_days,
                max_tickers=max_tickers,
            ),
        }
    stages["02_real_data_readiness"] = _check_real_readiness(end_date)
    stages["03_80_business_day_data"] = _check_80_day_artifacts(
        tickers=tickers,
        end_yyyymmdd=end_date,
        business_days=business_days,
        min_rows_per_day=int(min_rows),
    )
    requested_bundle_id = str(bundle_id or "").strip()
    stages["04_lgbm_real_train"] = _check_lgbm_real_train(
        requested_bundle_id or None
    )
    stages["05_backtest_real_candidate"] = _check_backtest_gate(
        stages["04_lgbm_real_train"]
    )
    stages["06_paper_balance"] = _check_paper_balance(
        requested_bundle_id or None
    )
    stages["07_paper_reconciliation"] = _check_paper_reconciliation(
        requested_bundle_id or None
    )
    stages["08_paper_probe_order"] = _check_probe_order(
        requested_bundle_id or None
    )
    stages["09_ops_risk"] = _check_ops_risk()

    blockers = [
        name
        for name, stage in stages.items()
        if stage.get("status") in {"BLOCKED", "FAIL"}
    ]
    report = {
        "status": "PASS" if not blockers else "BLOCKED",
        "generated_at": datetime.now(_KST).isoformat(),
        "end_date": end_date,
        "business_days": business_days,
        "bundle_id": requested_bundle_id or None,
        "tickers": tickers,
        "stages": stages,
        "blockers": blockers,
        "next_commands": _next_commands(
            end_date=end_date,
            business_days=business_days,
            max_tickers=max_tickers,
        ),
    }
    return report


def _next_commands(end_date: str, business_days: int, max_tickers: int) -> dict[str, str]:
    root = str(REPO_ROOT)
    py_prefix = f"PYTHONPATH={root}/new python"
    return {
        "real_readiness_final_dataset_user_terminal": (
            f"COMMUNITY_SCRAPE_ENABLED=1 {py_prefix} "
            f"new/scripts/live_data_readiness.py --all --end-date {end_date} "
            f"--business-days {business_days} --max-tickers {max_tickers} --require-train"
        ),
        "real_readiness_80d_rehearsal_user_terminal": (
            f"COMMUNITY_SCRAPE_ENABLED=1 {py_prefix} "
            f"new/scripts/live_data_readiness.py --all --end-date {end_date} "
            f"--business-days 80 --max-tickers {max_tickers} --require-train"
        ),
        "paper_balance_user_terminal": (
            f"{py_prefix} new/scripts/paper_trading_smoke.py --action balance"
        ),
        "real_backtest_after_bundle": (
            f"ELEPHANT_MODE=mode_b {py_prefix} -m src.jobs.run_backtest "
            "--bundle-id {bundle_id}"
        ),
        "deploy_candidate_after_backtest_pass": (
            f"ELEPHANT_MODE=mode_b {py_prefix} new/scripts/deploy_candidate.py "
            "--bundle-id {bundle_id} --dry-run"
        ),
        "paper_probe_user_terminal_after_balance_pass": (
            f"{py_prefix} new/scripts/paper_trading_smoke.py --action submit-probe "
            "--ticker 005930 --side buy --qty 1 --price {limit_price} "
            "--confirm-phrase PAPER_ORDER_OK"
        ),
        "paper_auto_after_gate_pass": (
            f"{py_prefix} new/scripts/paper_auto_trade.py --cycles 1 "
            "--bundle-id {bundle_id} --confirm-phrase PAPER_AUTO_OK"
        ),
    }


def write_report(report: dict[str, Any]) -> Path:
    _GATE_REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    path = _GATE_REPORT_ROOT / f"prelive_gate_{ts}.json"
    report["report_path"] = str(path)
    report["report_path_relative"] = _repo_relative(path)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path


def _default_cli_end_date() -> str:
    """Default strict pre-live checks to the audited final dataset SSOT.

    The pre-live gate validates a frozen candidate bundle by default. Falling
    back to the previous business day can make a valid frozen bundle look stale
    when the latest market day has not been promoted into that bundle yet.
    """
    gate_cfg = _final_dataset_gate_cfg()
    expected = _parse_dataset_date(gate_cfg.get("expected_end_date"))
    if expected is not None:
        return expected.strftime("%Y%m%d")
    return _previous_business_day().strftime("%Y%m%d")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_end = _default_cli_end_date()
    parser = argparse.ArgumentParser(description="Elephant Lab pre-live 1~9 gate")
    parser.add_argument(
        "--end-date",
        default=default_end,
        help="YYYYMMDD. Defaults to final_dataset_gate.expected_end_date.",
    )
    parser.add_argument("--business-days", type=int, default=_final_gate_min_business_days())
    parser.add_argument("--max-tickers", type=int, default=_final_gate_min_tickers())
    parser.add_argument(
        "--bundle-id",
        default="",
        help="Optional candidate bundle id to use for the C12 backtest gate.",
    )
    parser.add_argument("--no-write-report", action="store_true")
    return parser.parse_args(argv)


def main() -> int:
    args = _parse_args()
    report = build_report(
        end_date=str(args.end_date),
        business_days=int(args.business_days),
        max_tickers=int(args.max_tickers),
        bundle_id=str(args.bundle_id).strip() or None,
    )
    if not args.no_write_report:
        write_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

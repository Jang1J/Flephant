"""Read-only service readiness status for backend integration.

This module is intentionally side-effect free: it reads local JSON reports and
registry metadata, but it never reads .env, never calls brokers/providers, and
never mutates registry artifacts. Backend services can import
``build_service_status`` or call ``new/scripts/service_readiness_status.py``.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.mode_b.service_policy_verifier import (
    normalize_service_policy_universe,
    service_policy_gate_pass,
    service_policy_universe_hash,
)
from src.ops.paper_order_path_evidence import (
    find_fresh_paper_order_path_evidence,
)
from src.utils.config_loader import load as config_load
from src.utils.safe_cast import safe_bool, safe_int
from src.utils.ticker_utils import pad_ticker
from src.utils.trading_calendar import kospi_trading_dates_between

_KST = ZoneInfo("Asia/Seoul")
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _repo_relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _latest_json(root: Path, pattern: str) -> tuple[Path | None, dict[str, Any] | None]:
    candidates = [path for path in root.glob(pattern) if path.is_file()]
    if not candidates:
        return None, None
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            return path, _load_json(path)
        except Exception as e:
            _ = e
            continue
    return None, None


def _json_candidates(root: Path, pattern: str) -> list[tuple[Path, dict[str, Any]]]:
    candidates = [path for path in root.glob(pattern) if path.is_file()]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    out: list[tuple[Path, dict[str, Any]]] = []
    for path in candidates:
        try:
            out.append((path, _load_json(path)))
        except Exception as e:
            _ = e
            continue
    return out


def _registry_state(root: Path, relative_path: str) -> dict[str, Any]:
    path = root / relative_path
    if not path.exists():
        return {
            "status": "MISSING",
            "path": relative_path,
            "active_version": None,
        }
    data = _load_json(path)
    return {
        "status": "PASS",
        "path": relative_path,
        "active_version": data.get("active_version"),
        "version_count": len(data.get("versions", []) or []),
    }


def _feature_quality_gate_pass(backtest: dict[str, Any]) -> bool:
    gate_cfg = (
        config_load("risk_config.yaml", "backtest_agent.deploy_decision_gate")
        or {}
    ).get("feature_quality_gate")
    if not isinstance(gate_cfg, dict):
        return False
    min_dual_raw = gate_cfg.get("min_dual_source_non_neutral_row_coverage")
    min_exog_raw = gate_cfg.get("min_exogenous_non_neutral_row_coverage")
    if min_dual_raw is None or min_exog_raw is None:
        return False
    try:
        min_dual = float(min_dual_raw)
        min_exog = float(min_exog_raw)
    except (TypeError, ValueError):
        return False
    feature_quality = backtest.get("feature_quality") or {}
    dual_rows = int(feature_quality.get("dual_source_rows", 0) or 0)
    dual_non_neutral = int(feature_quality.get("dual_source_non_neutral_rows", 0) or 0)
    exog_rows = int(feature_quality.get("exogenous_rows", 0) or 0)
    exog_non_neutral = int(feature_quality.get("exogenous_non_neutral_rows", 0) or 0)
    if dual_rows <= 0 or exog_rows <= 0:
        return False
    return (
        dual_non_neutral / max(dual_rows, 1) >= min_dual
        and exog_non_neutral / max(exog_rows, 1) >= min_exog
    )


def _service_policy_gate_pass(
    backtest: dict[str, Any],
    bundle_id: str,
    repo_root: Path | None = None,
) -> bool:
    evidence = backtest.get("service_policy_replay")
    return service_policy_gate_pass(
        evidence if isinstance(evidence, dict) else None,
        bundle_id=bundle_id,
        repo_root=repo_root or _REPO_ROOT,
        expected_date_range=(
            backtest.get("service_policy_expected_date_range")
            or backtest.get("date_range")
        ),
        expected_universe=_final_dataset_tickers(),
    )


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


def _extract_model_metadata(backtest: dict[str, Any]) -> dict[str, Any]:
    for key in ("model_metadata", "candidate_model_metadata", "candidate_metadata"):
        raw = backtest.get(key)
        if isinstance(raw, dict):
            return raw
    artifact = backtest.get("candidate_artifact")
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


def _final_dataset_tickers() -> list[str]:
    gate_cfg = (
        config_load(
            "risk_config.yaml",
            "backtest_agent.deploy_decision_gate.final_dataset_gate",
        )
        or {}
    )
    universe_cfg = config_load("universe_config.yaml") or {}
    include_pending = safe_bool(
        gate_cfg.get("include_pending_data_tickers"),
        default=False,
    )
    stock_statuses = {"active"}
    sector_statuses = {"confirmed"}
    if include_pending:
        stock_statuses = {
            str(status)
            for status in gate_cfg.get("allowed_stock_statuses", ["active", "pending_data"])
        }
        sector_statuses = {
            str(status)
            for status in gate_cfg.get(
                "allowed_sector_statuses",
                ["confirmed", "confirmed_pending_data"],
            )
        }
    tickers: list[str] = []
    for sector in (universe_cfg.get("sectors") or {}).values():
        if not isinstance(sector, dict) or str(sector.get("status")) not in sector_statuses:
            continue
        for stock in sector.get("stocks", []) or []:
            if str(stock.get("status")) in stock_statuses:
                tickers.append(str(stock.get("ticker", "")))
    if not tickers:
        tickers.extend((universe_cfg.get("backtest_universe_mode") or {}).get("fallback_tickers", []))
    min_tickers = safe_int(gate_cfg.get("min_tickers"), default=0, min_value=0)
    selected = tickers[:min_tickers] if min_tickers else tickers
    return normalize_service_policy_universe(selected)


def _final_dataset_gate_state(backtest: dict[str, Any]) -> dict[str, Any]:
    gate_cfg = (
        config_load(
            "risk_config.yaml",
            "backtest_agent.deploy_decision_gate.final_dataset_gate",
        )
        or {}
    )
    if not isinstance(gate_cfg, dict) or not gate_cfg:
        return {
            "status": "BLOCKED",
            "blockers": ["final_dataset_gate_config_missing"],
        }
    if not safe_bool(gate_cfg.get("required", True), default=True):
        return {"status": "PASS", "required": False, "blockers": []}

    metadata = _extract_model_metadata(backtest)
    blockers: list[str] = []
    if not metadata:
        blockers.append("model_metadata_missing")

    expected_start = _parse_dataset_date(gate_cfg.get("expected_start_date"))
    expected_end = _parse_dataset_date(gate_cfg.get("expected_end_date"))
    train_start = _parse_dataset_date(metadata.get("train_start"))
    train_end = _parse_dataset_date(metadata.get("train_end"))
    min_days = safe_int(gate_cfg.get("min_business_days"), default=0, min_value=1)
    min_tickers = safe_int(gate_cfg.get("min_tickers"), default=0, min_value=1)
    required_data_source = str(gate_cfg.get("allowed_model_data_source") or "").strip()
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
    expected_tickers = _final_dataset_tickers()
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
    if required_data_source and data_source != required_data_source:
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
        "required_data_source": required_data_source or None,
    }


def _label_target_gate_state(backtest: dict[str, Any]) -> dict[str, Any]:
    """Deployable C12 evidence must match the current label target SSOT."""
    label_cfg = config_load("risk_config.yaml", "label") or {}
    required_target_col = str(label_cfg.get("target_col") or "").strip()
    allowed_target_cols = _allowed_deploy_target_cols(label_cfg)
    metadata = _extract_model_metadata(backtest)
    observed_target_col = str(metadata.get("target_col") or "").strip()
    blockers: list[str] = []
    if not allowed_target_cols:
        blockers.append("label_target_config_missing")
    if not metadata:
        blockers.append("model_metadata_missing")
    elif not observed_target_col:
        blockers.append("model_target_col_missing")
    elif observed_target_col not in allowed_target_cols:
        blockers.append("model_target_col_mismatch")
    return {
        "status": "PASS" if not blockers else "BLOCKED",
        "required": True,
        "blockers": blockers,
        "required_target_col": required_target_col or None,
        "allowed_deploy_target_cols": allowed_target_cols,
        "observed_target_col": observed_target_col or None,
    }


def _allowed_deploy_target_cols(label_cfg: dict[str, Any]) -> list[str]:
    values = label_cfg.get("deploy_target_cols")
    cols: list[str] = []
    if isinstance(values, list):
        cols.extend(str(value).strip() for value in values)
    fallback = str(label_cfg.get("target_col") or "").strip()
    if fallback:
        cols.append(fallback)
    return [col for col in dict.fromkeys(cols) if col]


def _backtest_state_from_report(
    root: Path,
    bundle_id: str,
    path: Path,
    data: dict[str, Any],
) -> dict[str, Any]:
    regression = data.get("regression_risk") or {}
    leakage = data.get("minute_bar_leakage_check") or {}
    feature_pass = _feature_quality_gate_pass(data)
    service_pass = _service_policy_gate_pass(data, bundle_id, repo_root=root)
    final_dataset_gate = _final_dataset_gate_state(data)
    final_dataset_pass = final_dataset_gate.get("status") == "PASS"
    label_target_gate = _label_target_gate_state(data)
    label_target_pass = label_target_gate.get("status") == "PASS"
    schema_current = "feature_quality" in data and "service_policy_replay" in data
    deployable = (
        data.get("verdict") == "pass"
        and not safe_bool(regression.get("flagged", False), default=True)
        and leakage.get("verdict") == "pass"
        and feature_pass
        and service_pass
        and final_dataset_pass
        and label_target_pass
    )
    return {
        "status": "PASS" if deployable else "BLOCKED",
        "report_path": _repo_relative(path, root),
        "schema_current": schema_current,
        "deployable": deployable,
        "verdict": data.get("verdict"),
        "regression_risk_flagged": regression.get("flagged"),
        "regression_risk_severity": regression.get("severity"),
        "minute_bar_leakage_verdict": leakage.get("verdict"),
        "feature_quality_gate_pass": feature_pass,
        "service_policy_gate_pass": service_pass,
        "final_dataset_gate_pass": final_dataset_pass,
        "final_dataset_gate": final_dataset_gate,
        "label_target_gate_pass": label_target_pass,
        "label_target_gate": label_target_gate,
        "metrics": data.get("metrics", {}),
    }


def _backtest_state(root: Path, bundle_id: str) -> dict[str, Any]:
    reports = _json_candidates(
        root,
        f"artifacts/reports/backtest/backtest_{bundle_id}_*.json",
    )
    if not reports:
        return {
            "status": "MISSING",
            "report_path": None,
            "deployable": False,
            "schema_current": False,
        }
    states = [
        _backtest_state_from_report(root, bundle_id, path, data)
        for path, data in reports
    ]
    latest = states[0]
    latest["selection"] = (
        "latest_report"
        if safe_bool(latest.get("deployable"), default=False)
        else "latest_non_deployable"
    )
    states[0]["ignored_newer_non_deployable_reports"] = 0
    older_deployable = [
        state for state in states[1:]
        if safe_bool(state.get("deployable"), default=False)
    ]
    if older_deployable:
        latest["older_deployable_report_path"] = older_deployable[0].get("report_path")
        latest["older_deployable_ignored"] = True
    return latest


def _report_status(data: dict[str, Any]) -> str:
    return str(data.get("status") or data.get("gate", {}).get("status") or "UNKNOWN")


def _latest_report_state(
    root: Path,
    pattern: str,
    *,
    prefer_pass: bool = False,
) -> dict[str, Any]:
    reports = _json_candidates(root, pattern)
    if not reports:
        return {"status": "MISSING", "report_path": None}
    path, data = reports[0]
    if prefer_pass:
        for candidate_path, candidate_data in reports:
            if _report_status(candidate_data) == "PASS":
                path, data = candidate_path, candidate_data
                break
    return {
        "status": _report_status(data),
        "report_path": _repo_relative(path, root),
        "blockers": data.get("blockers") or data.get("gate", {}).get("blockers") or [],
        "coverage": data.get("coverage", {}),
    }


def _broker_stage_statuses(data: dict[str, Any]) -> dict[str, Any]:
    stage_statuses = dict(data.get("stage_statuses") or {})
    preflight = (data.get("stages") or {}).get("preflight")
    if not isinstance(preflight, dict):
        return stage_statuses
    paper_evidence = (preflight.get("stages") or {}).get("paper_evidence")
    if not isinstance(paper_evidence, dict):
        return stage_statuses

    aliases = {
        "balance_reconciliation": "balance_reconciliation",
        "probe_order": "probe_order",
        "order_history": "order_history_requery",
        "paper_order_path": "paper_order_path",
    }
    for evidence_key, status_key in aliases.items():
        stage = paper_evidence.get(evidence_key)
        if status_key not in stage_statuses and isinstance(stage, dict):
            stage_statuses[status_key] = stage.get("status")
    return stage_statuses


def _parse_report_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_KST)
    return parsed.astimezone(_KST)


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
        "paper_order_path",
    )
    statuses: dict[str, str] = {}
    for name in required:
        stage = broker_evidence.get(name)
        if name == "paper_order_path" and not isinstance(stage, dict):
            probe = broker_evidence.get("probe_order")
            history = broker_evidence.get("order_history_requery")
            if isinstance(probe, dict) and isinstance(history, dict):
                stage = {
                    "status": (
                        "PASS"
                        if probe.get("status") == "PASS"
                        and history.get("status") == "PASS"
                        else "BLOCKED"
                    )
                }
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


def _matched_order_count(report_or_stage: dict[str, Any] | None) -> int:
    if not isinstance(report_or_stage, dict):
        return 0
    direct = report_or_stage.get("matched_order_count")
    if direct is not None:
        try:
            return int(direct)
        except (TypeError, ValueError):
            return 0
    stage = (report_or_stage.get("stages") or {}).get("order_history")
    if isinstance(stage, dict):
        return _matched_order_count(stage)
    return 0


def _contains_kis_virtual_mode(payload: Any) -> bool:
    if isinstance(payload, dict):
        if payload.get("_mode") == "virtual":
            return True
        runtime = payload.get("runtime")
        if isinstance(runtime, dict) and runtime.get("kis_mode") == "virtual":
            return True
        return any(_contains_kis_virtual_mode(value) for value in payload.values())
    if isinstance(payload, list):
        return any(_contains_kis_virtual_mode(item) for item in payload)
    return False


def _probe_order_blocker(probe: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(probe, dict):
        return {"error_code": "PROBE_ORDER_REPORT_MISSING"}
    if probe.get("status") == "PASS":
        return {}
    execution = (probe.get("stages") or {}).get("execution")
    result = execution.get("result") if isinstance(execution, dict) else {}
    execution_report = result.get("execution_report") if isinstance(result, dict) else {}
    rejections = (
        execution_report.get("rejections")
        if isinstance(execution_report, dict)
        else []
    )
    for rejection in rejections if isinstance(rejections, list) else []:
        if not isinstance(rejection, dict):
            continue
        error = str(rejection.get("error", ""))
        if (
            "msg_cd=40580000" in error
            or "msg_cd=40100000" in error
            or "장종료" in error
            or "영업일이 아닙니다" in error
        ):
            return {
                "error_code": "BROKER_MARKET_CLOSED",
                "message": "KIS virtual broker rejected probe order because paper market is closed.",
            }
    return {"error_code": "PROBE_ORDER_NOT_PASSED"}


def _paper_trading_evidence_state(root: Path) -> dict[str, Any]:
    balance_path, balance = _latest_json(
        root,
        "artifacts/reports/paper_trading/paper_trading_balance_reconciliation_*.json",
    )
    probe_path, probe = _latest_json(
        root,
        "artifacts/reports/paper_trading/paper_trading_submit_probe_order_*.json",
    )
    order_history_path, order_history = _latest_json(
        root,
        "artifacts/reports/paper_trading/paper_trading_order_history_*.json",
    )
    if balance is None and probe is None and order_history is None:
        return {
            "status": "MISSING",
            "external_kis_api": False,
            "evidence_level": "missing",
            "report_path": None,
            "stage_statuses": {},
        }

    balance_stage = (balance.get("stages") or {}).get("balance") if balance else {}
    balance_ok = bool(
        balance
        and balance.get("status") == "PASS"
        and isinstance(balance_stage, dict)
        and balance_stage.get("status") == "PASS"
    )
    probe_matched = _matched_order_count(probe)
    history_matched = _matched_order_count(order_history)
    probe_history = (probe.get("stages") or {}).get("order_history") if probe else {}
    probe_history_ok = bool(
        isinstance(probe_history, dict)
        and probe_history.get("status") == "PASS"
        and probe_matched > 0
    )
    external = any(
        _contains_kis_virtual_mode(item)
        for item in (balance, probe, order_history)
        if isinstance(item, dict)
    )
    profile_fingerprint = (
        str((balance.get("evidence") or {}).get("broker_env_fingerprint") or "")
        if isinstance(balance, dict) and isinstance(balance.get("evidence"), dict)
        else ""
    )
    order_path = find_fresh_paper_order_path_evidence(
        root=root,
        profile_fingerprint=profile_fingerprint or None,
    )
    order_path_ok = order_path.get("status") == "PASS"
    status = "PASS" if balance_ok and order_path_ok else "BLOCKED"
    report_path = probe_path or balance_path or order_history_path
    return {
        "status": status,
        "external_kis_api": external,
        "evidence_level": (
            "external_kis_virtual_paper_trading"
            if external else "paper_trading_report_unverified_source"
        ),
        "report_path": _repo_relative(report_path, root) if report_path else None,
        "stage_statuses": {
            "balance_reconciliation": "PASS" if balance_ok else "BLOCKED",
            "probe_order": "PASS" if order_path_ok else "BLOCKED",
            "order_history_requery": "PASS" if order_path_ok else "BLOCKED",
            "paper_order_path": "PASS" if order_path_ok else "BLOCKED",
        },
        "paper_trading_evidence": {
            "balance_reconciliation": {
                "status": "PASS" if balance_ok else "BLOCKED",
                "report_path": (
                    _repo_relative(balance_path, root) if balance_path else None
                ),
            },
            "probe_order": {
                "status": "PASS" if order_path_ok else "BLOCKED",
                "report_path": _repo_relative(probe_path, root) if probe_path else None,
                "deprecated": True,
                "replaced_by": "paper_order_path",
                "evidence_type": order_path.get("evidence_type"),
                "blocker": _probe_order_blocker(probe) if not order_path_ok else {},
            },
            "order_history": {
                "status": "PASS" if order_path_ok else "BLOCKED",
                "report_path": (
                    _repo_relative(order_history_path, root)
                    if order_history_path
                    else _repo_relative(probe_path, root)
                    if probe_history_ok and probe_path
                    else None
                ),
                "matched_order_count": max(
                    safe_int(order_path.get("matched_order_count", 0), default=0, min_value=0),
                    probe_matched,
                    history_matched,
                ),
            },
            "paper_order_path": order_path,
        },
    }


def _paper_auto_cycle_history_matched(data: dict[str, Any]) -> bool:
    order_path = data.get("paper_order_path_evidence")
    if isinstance(order_path, dict):
        return order_path.get("status") == "PASS"
    cycle_stage = ((data.get("stages") or {}).get("paper_auto_cycle") or {})
    if not isinstance(cycle_stage, dict):
        return False
    cycles = ((cycle_stage.get("stages") or {}).get("cycles") or {})
    items = cycles.get("items") if isinstance(cycles, dict) else []
    if not isinstance(items, list) or not items:
        return False
    for item in items:
        if not isinstance(item, dict):
            return False
        if item.get("status") != "PASS":
            return False
        if not _paper_auto_cycle_bar_readiness_matched(item):
            return False
        verification = item.get("order_history_verification")
        if not isinstance(verification, dict) or verification.get("status") != "PASS":
            return False
        queries = verification.get("queries")
        if not isinstance(queries, list) or not queries:
            return False
        for query in queries:
            if not isinstance(query, dict):
                return False
            try:
                matched = int(query.get("matched_order_count", 0) or 0)
            except (TypeError, ValueError):
                matched = 0
            if matched <= 0:
                return False
    return True


def _paper_auto_cycle_bar_readiness_matched(item: dict[str, Any]) -> bool:
    bar_readiness = item.get("hot_path_bar_readiness")
    if isinstance(bar_readiness, dict):
        return bar_readiness.get("status") == "PASS"

    # Legacy paper-auto reports before the explicit bar-readiness field can be
    # trusted only when they show enough consumed bars and active quant scores.
    try:
        n_bars = int(item.get("n_bars", 0) or 0)
    except (TypeError, ValueError):
        n_bars = 0
    hot_result = item.get("hot_result") if isinstance(item.get("hot_result"), dict) else {}
    quant_output = (
        hot_result.get("quant_output") if isinstance(hot_result, dict) else {}
    )
    scores = (
        quant_output.get("scores", {}) if isinstance(quant_output, dict) else {}
    )
    required_bars = safe_int(
        (config_load("risk_config.yaml", "quant_agent") or {}).get("warmup_bars"),
        default=60,
        min_value=1,
    )
    finite_scores = []
    if isinstance(scores, dict):
        for value in scores.values():
            try:
                score = float(value)
            except (TypeError, ValueError):
                return False
            if not math.isfinite(score):
                return False
            finite_scores.append(score)
    return (
        n_bars >= required_bars
        and isinstance(quant_output, dict)
        and str(quant_output.get("mode", "")).lower() == "active"
        and len(finite_scores) > 0
        and len(set(finite_scores)) > 1
    )


def _paper_auto_bundle_ids(data: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for value in (
        data.get("bundle_id"),
        data.get("model_bundle_id"),
    ):
        if value:
            out.add(str(value))
    cycle_stage = ((data.get("stages") or {}).get("paper_auto_cycle") or {})
    if isinstance(cycle_stage, dict):
        active_model = (cycle_stage.get("stages") or {}).get("active_model_guard")
        if isinstance(active_model, dict) and active_model.get("bundle_id"):
            out.add(str(active_model["bundle_id"]))
    return out


def _broker_evidence_state(root: Path, bundle_id: str) -> dict[str, Any]:
    paper_trading = _paper_trading_evidence_state(root)
    paper_cfg = config_load("risk_config.yaml", "paper_trading") or {}
    max_age_sec = safe_int(
        paper_cfg.get(
            "order_path_evidence_max_age_sec",
            paper_cfg.get("evidence_max_age_sec", 86400),
        ),
        default=86400,
        min_value=1,
    )
    order_path = find_fresh_paper_order_path_evidence(
        root=root,
        bundle_id=bundle_id,
        max_age_sec=max_age_sec,
    )
    paper_stage_statuses = paper_trading.get("stage_statuses", {})
    balance_ok = paper_stage_statuses.get("balance_reconciliation") == "PASS"
    if order_path.get("status") == "PASS":
        status = "PASS" if balance_ok else "BLOCKED"
        return {
            "status": status,
            "external_kis_api": bool(order_path.get("external_kis_api")),
            "evidence_level": "external_kis_virtual_order_path",
            "report_path": order_path.get("report_path"),
            "stage_statuses": {
                "balance_reconciliation": "PASS" if balance_ok else "BLOCKED",
                "probe_order": "PASS",
                "order_history_requery": "PASS",
                "paper_order_path": "PASS",
            },
            "freshness": order_path.get("freshness", {}),
            "broker_evidence_nested": {"status": "PASS", "stage_statuses": {"paper_order_path": "PASS"}},
            "evidence_guard": {"status": "PASS", "source": "paper_order_path"},
            "bundle_match": bool(order_path.get("bundle_match", True)),
            "bundle_ids": order_path.get("bundle_ids", []),
            "paper_auto_cycle_history_matched": True,
            "paper_order_path_evidence": order_path,
            "paper_trading_evidence": paper_trading.get("paper_trading_evidence", {}),
        }
    reports = _json_candidates(
        root,
        "artifacts/reports/paper_auto_trading/paper_auto_service_rehearsal_*.json",
    )
    if not reports:
        state = dict(paper_trading)
        if state.get("status") == "PASS":
            state["status"] = "BLOCKED"
        state["blocker"] = "paper_auto_bundle_evidence_missing"
        state["bundle_match"] = False
        state["bundle_ids"] = []
        state["paper_auto_cycle_history_matched"] = False
        return state

    latest_external_state: dict[str, Any] | None = None
    for path, data in reports:
        external = safe_bool(data.get("external_kis_api"), default=False)
        if not external:
            continue

        stage_statuses = _broker_stage_statuses(data)
        bundle_ids = _paper_auto_bundle_ids(data)
        bundle_match = bundle_id in bundle_ids
        cycle_history_matched = _paper_auto_cycle_history_matched(data)
        freshness = _fresh_generated_at_state(
            data.get("generated_at"),
            max_age_sec=max_age_sec,
        )
        nested_evidence = _paper_auto_broker_evidence_nested_state(data)
        evidence_guard = _paper_auto_evidence_guard(data)
        evidence_guard_pass = str(evidence_guard.get("status", "")).upper() == "PASS"
        passed = (
            data.get("status") == "PASS"
            and stage_statuses.get("paper_auto_cycle") == "PASS"
            and stage_statuses.get("balance_reconciliation") == "PASS"
            and stage_statuses.get("probe_order") == "PASS"
            and stage_statuses.get("order_history_requery") == "PASS"
            and freshness.get("status") == "PASS"
            and nested_evidence.get("status") == "PASS"
            and evidence_guard_pass
            and cycle_history_matched
            and bundle_match
        )
        state = {
            "status": "PASS" if passed else "BLOCKED",
            "external_kis_api": True,
            "evidence_level": data.get("evidence_level"),
            "report_path": _repo_relative(path, root),
            "stage_statuses": stage_statuses,
            "freshness": freshness,
            "broker_evidence_nested": nested_evidence,
            "evidence_guard": evidence_guard,
            "bundle_match": bundle_match,
            "bundle_ids": sorted(bundle_ids),
            "paper_auto_cycle_history_matched": cycle_history_matched,
            "paper_trading_evidence": paper_trading.get("paper_trading_evidence", {}),
        }
        if latest_external_state is None:
            latest_external_state = state
        if bundle_match:
            return state

    if latest_external_state is not None:
        return latest_external_state

    # Internal fake rehearsals and manual paper probes are useful smoke artifacts,
    # but final broker readiness requires external paper-auto evidence for this
    # bundle plus matched order-history proof.
    path, data = reports[0]
    external = safe_bool(data.get("external_kis_api"), default=False)
    stage_statuses = _broker_stage_statuses(data)
    bundle_ids = _paper_auto_bundle_ids(data)
    bundle_match = bundle_id in bundle_ids
    cycle_history_matched = _paper_auto_cycle_history_matched(data)
    freshness = _fresh_generated_at_state(
        data.get("generated_at"),
        max_age_sec=max_age_sec,
    )
    nested_evidence = _paper_auto_broker_evidence_nested_state(data)
    evidence_guard = _paper_auto_evidence_guard(data)
    evidence_guard_pass = str(evidence_guard.get("status", "")).upper() == "PASS"
    passed = (
        data.get("status") == "PASS"
        and external
        and stage_statuses.get("paper_auto_cycle") == "PASS"
        and stage_statuses.get("balance_reconciliation") == "PASS"
        and stage_statuses.get("probe_order") == "PASS"
        and stage_statuses.get("order_history_requery") == "PASS"
        and freshness.get("status") == "PASS"
        and nested_evidence.get("status") == "PASS"
        and evidence_guard_pass
        and cycle_history_matched
        and bundle_match
    )
    return {
        "status": "PASS" if passed else "BLOCKED",
        "external_kis_api": external,
        "evidence_level": data.get("evidence_level"),
        "report_path": _repo_relative(path, root),
        "stage_statuses": stage_statuses,
        "freshness": freshness,
        "broker_evidence_nested": nested_evidence,
        "evidence_guard": evidence_guard,
        "bundle_match": bundle_match,
        "bundle_ids": sorted(bundle_ids),
        "paper_auto_cycle_history_matched": cycle_history_matched,
        "paper_trading_evidence": paper_trading.get("paper_trading_evidence", {}),
    }


def build_service_status(
    *,
    bundle_id: str,
    root: Path | None = None,
) -> dict[str, Any]:
    """Build a BE-friendly readiness payload for a candidate bundle."""
    repo_root = root or _REPO_ROOT
    backtest = _backtest_state(repo_root, bundle_id)
    broker = _broker_evidence_state(repo_root, bundle_id)
    deploy_quality = "PASS" if backtest.get("deployable") else "BLOCKED"
    broker_status = broker.get("status", "MISSING")
    ssot_readiness = (
        "PASS"
        if deploy_quality == "PASS" and broker_status == "PASS"
        else "PARTIAL"
        if backtest.get("report_path")
        else "BLOCKED"
    )
    live_trading_allowed = False
    registry_mutated = False
    read_only = True
    external_api_called = False
    safe_to_enable_order_actions = (
        ssot_readiness == "PASS"
        and deploy_quality == "PASS"
        and broker_status == "PASS"
        and not live_trading_allowed
        and not registry_mutated
        and read_only
        and not external_api_called
    )
    return {
        "schema_version": "1.0.0",
        "status": ssot_readiness,
        "generated_at": datetime.now(_KST).isoformat(),
        "bundle_id": bundle_id,
        "read_only": read_only,
        "external_api_called": external_api_called,
        "registry_mutated": registry_mutated,
        "ssot_readiness": ssot_readiness,
        "deploy_quality": deploy_quality,
        "broker_evidence": broker_status,
        "live_trading_allowed": live_trading_allowed,
        "production_registry": _registry_state(repo_root, "artifacts/lgbm/registry.json"),
        "paper_registry": _registry_state(repo_root, "artifacts/lgbm_paper/registry.json"),
        "c12_backtest": backtest,
        "phase2_feature_backfill": _latest_report_state(
            repo_root,
            "artifacts/reports/phase2_feature_backfill/*.json",
        ),
        "dual_source_history": _latest_report_state(
            repo_root,
            "artifacts/reports/dual_source_history/*.json",
        ),
        "exogenous_history": _latest_report_state(
            repo_root,
            "artifacts/reports/exogenous_history/*.json",
        ),
        "service_policy_replay": _latest_report_state(
            repo_root,
            f"artifacts/reports/service_policy_replay/service_policy_replay_{bundle_id}_*.json",
            prefer_pass=True,
        ),
        "kis_broker_evidence": broker,
        "be_contract": {
            "safe_to_show_dashboard": True,
            "safe_to_enable_order_actions": safe_to_enable_order_actions,
            "safe_to_enable_live_actions": False,
            "status_endpoint_semantics": "read_only_local_artifact_status",
        },
    }

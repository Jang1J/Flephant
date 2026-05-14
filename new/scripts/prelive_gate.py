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

from src.utils.ticker_utils import pad_ticker  # noqa: E402
from src.mode_b.service_policy_verifier import service_policy_gate_pass  # noqa: E402
from src.utils.trading_calendar import (  # noqa: E402
    kospi_trading_dates_between,
    kospi_trading_start_date,
    previous_kospi_trading_day,
)

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


def _active_tickers(max_tickers: int | None = 20) -> list[str]:
    cfg = _load_yaml(NEW_ROOT / "config" / "universe_config.yaml")
    tickers: list[str] = []
    for sector in (cfg.get("sectors") or {}).values():
        for stock in sector.get("stocks", []) or []:
            if stock.get("status") == "active":
                tickers.append(pad_ticker(str(stock["ticker"])))
    return tickers[:max_tickers] if max_tickers is not None else tickers


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
    active_count = len(_active_tickers(20))
    return _stage(
        "PASS",
        "SSOT files parse and active universe is available.",
        {
            "active_tickers_checked": active_count,
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
        return (
            data.get("status") == "PASS"
            and data.get("end_date") == end_yyyymmdd
            and bool(smoke)
            and backfill.get("status") == "PASS"
            and not data.get("allow_mock", False)
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
            path = _DATA_ROOT / ticker / f"bars_1m_{day}.parquet"
            rows = _count_rows(path) if path.exists() else None
            if rows is None or rows < min_rows_per_day:
                missing_or_short.append({"ticker": ticker, "rows": rows})
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


def _check_lgbm_real_train(bundle_id: str | None = None) -> dict[str, Any]:
    requested_bundle_id = str(bundle_id or "").strip()
    version, meta = _latest_lgbm_metadata(requested_bundle_id or None)
    if not meta:
        detail = (
            {"requested_bundle_id": requested_bundle_id}
            if requested_bundle_id
            else None
        )
        return _stage(
            "BLOCKED",
            (
                "No LightGBM registry metadata was found for the requested bundle id."
                if requested_bundle_id
                else "No LightGBM registry metadata was found."
            ),
            detail,
        )
    risk_cfg = _load_yaml(NEW_ROOT / "config" / "risk_config.yaml")
    label_cfg = risk_cfg.get("label") or {}
    required_label_version = label_cfg.get("generation_version")
    required_label_scope = label_cfg.get("session_scope")
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
    model_path = meta.get("model_path")
    model_exists = bool(model_path and (REPO_ROOT / str(model_path)).exists())
    synthetic = bool(meta.get("synthetic_fallback"))
    data_source = meta.get("data_source")
    real_data_source = data_source == "artifact_bars"
    bundle_id = meta.get("bundle_id")
    actual_label_version = meta.get("label_generation_version")
    actual_label_scope = meta.get("label_session_scope")
    label_version_ok = actual_label_version == required_label_version
    label_scope_ok = actual_label_scope == required_label_scope
    status = (
        "PASS"
        if model_exists
        and not synthetic
        and real_data_source
        and label_version_ok
        and label_scope_ok
        else "BLOCKED"
    )
    message = "Latest LightGBM artifact inspected."
    if not label_version_ok:
        message = "Latest LightGBM artifact has stale or missing label generation metadata."
    elif not label_scope_ok:
        message = "Latest LightGBM artifact has stale or missing label session scope metadata."
    elif not real_data_source:
        message = "Latest LightGBM artifact is not marked as artifact_bars real data."
    return _stage(
        status,
        message,
        {
            "version": version,
            "requested_bundle_id": requested_bundle_id or None,
            "bundle_id": bundle_id,
            "candidate_bundle_id": bundle_id,
            "bundle_id_matches_request": (
                bundle_id == requested_bundle_id if requested_bundle_id else None
            ),
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
            "n_train_rows": meta.get("n_train_rows"),
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
        and regression.get("flagged") is False
        and leakage.get("verdict") == "pass"
        and _feature_quality_gate_pass(payload)
        and _service_policy_gate_pass(payload, bundle_id)
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


def _matched_order_count(report_or_stage: dict[str, Any] | None) -> int:
    if not isinstance(report_or_stage, dict):
        return 0
    direct = report_or_stage.get("matched_order_count")
    if direct is not None:
        try:
            return int(direct)
        except (TypeError, ValueError):
            return 0
    stage = report_or_stage.get("stages", {}).get("order_history")
    if isinstance(stage, dict):
        try:
            return int(stage.get("matched_order_count", 0) or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _check_paper_balance() -> dict[str, Any]:
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


def _check_paper_reconciliation() -> dict[str, Any]:
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


def _check_probe_order() -> dict[str, Any]:
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
    checks = {
        "execution_live_disabled": execution.get("live_enabled") is False,
        "execution_not_live": execution.get("mode") != "live",
        "paper_requires_virtual": paper.get("require_virtual_mode") is True,
        "probe_qty_limited_to_one": int(paper.get("max_probe_order_qty", 0)) <= 1,
        "market_order_disabled": paper.get("allow_market_order") is False,
        "confirm_phrase_configured": bool(paper.get("confirm_order_phrase")),
        "paper_auto_requires_virtual": paper_auto.get("require_virtual_mode") is True,
        "paper_auto_requires_prelive_pass": paper_auto.get("require_prelive_pass") is True,
        "paper_auto_requires_active_model": paper_auto.get("require_active_model") is True,
        "paper_auto_confirm_phrase_configured": bool(paper_auto.get("confirm_start_phrase")),
        "paper_auto_order_qty_limited": int(paper_auto.get("max_order_qty_per_order", 0)) <= 1,
        "paper_auto_market_order_disabled": paper_auto.get("allow_market_order") is False,
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
            "next_commands": _next_commands(end_date=end_date, business_days=business_days),
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
    stages["06_paper_balance"] = _check_paper_balance()
    stages["07_paper_reconciliation"] = _check_paper_reconciliation()
    stages["08_paper_probe_order"] = _check_probe_order()
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
        "next_commands": _next_commands(end_date=end_date, business_days=business_days),
    }
    return report


def _next_commands(end_date: str, business_days: int) -> dict[str, str]:
    root = str(REPO_ROOT)
    py_prefix = f"PYTHONPATH={root}/new python"
    return {
        "real_readiness_80d_user_terminal": (
            f"COMMUNITY_SCRAPE_ENABLED=1 {py_prefix} "
            f"new/scripts/live_data_readiness.py --all --end-date {end_date} "
            f"--business-days {business_days} --max-tickers 20 --require-train"
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
            "--bundle-id {bundle_id}"
        ),
        "paper_probe_user_terminal_after_balance_pass": (
            f"{py_prefix} new/scripts/paper_trading_smoke.py --action submit-probe "
            "--ticker 005930 --side buy --qty 1 --price {limit_price} "
            "--confirm-phrase PAPER_ORDER_OK"
        ),
        "paper_auto_after_gate_pass": (
            f"{py_prefix} new/scripts/paper_auto_trade.py --cycles 1 "
            "--confirm-phrase PAPER_AUTO_OK"
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


def _parse_args() -> argparse.Namespace:
    default_end = _previous_business_day().strftime("%Y%m%d")
    parser = argparse.ArgumentParser(description="Elephant Lab pre-live 1~9 gate")
    parser.add_argument("--end-date", default=default_end, help="YYYYMMDD")
    parser.add_argument("--business-days", type=int, default=80)
    parser.add_argument("--max-tickers", type=int, default=20)
    parser.add_argument(
        "--bundle-id",
        default="",
        help="Optional candidate bundle id to use for the C12 backtest gate.",
    )
    parser.add_argument("--no-write-report", action="store_true")
    return parser.parse_args()


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

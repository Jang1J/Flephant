"""Read-only service readiness status for backend integration.

This module is intentionally side-effect free: it reads local JSON reports and
registry metadata, but it never reads .env, never calls brokers/providers, and
never mutates registry artifacts. Backend services can import
``build_service_status`` or call ``new/scripts/service_readiness_status.py``.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.mode_b.service_policy_verifier import service_policy_gate_pass
from src.utils.config_loader import load as config_load

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
    ).get("feature_quality_gate", {}) or {}
    min_dual = float(gate_cfg.get("min_dual_source_non_neutral_row_coverage", 0.8))
    min_exog = float(gate_cfg.get("min_exogenous_non_neutral_row_coverage", 0.8))
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


def _service_policy_gate_pass(backtest: dict[str, Any], bundle_id: str) -> bool:
    evidence = backtest.get("service_policy_replay")
    return service_policy_gate_pass(
        evidence if isinstance(evidence, dict) else None,
        bundle_id=bundle_id,
        repo_root=_REPO_ROOT,
        expected_date_range=(
            backtest.get("service_policy_expected_date_range")
            or backtest.get("date_range")
        ),
    )


def _backtest_state(root: Path, bundle_id: str) -> dict[str, Any]:
    path, data = _latest_json(
        root,
        f"artifacts/reports/backtest/backtest_{bundle_id}_*.json",
    )
    if path is None or data is None:
        return {
            "status": "MISSING",
            "report_path": None,
            "deployable": False,
            "schema_current": False,
        }
    regression = data.get("regression_risk") or {}
    leakage = data.get("minute_bar_leakage_check") or {}
    feature_pass = _feature_quality_gate_pass(data)
    service_pass = _service_policy_gate_pass(data, bundle_id)
    schema_current = "feature_quality" in data and "service_policy_replay" in data
    deployable = (
        data.get("verdict") == "pass"
        and regression.get("flagged") is False
        and leakage.get("verdict") == "pass"
        and feature_pass
        and service_pass
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
        "metrics": data.get("metrics", {}),
    }


def _latest_report_state(root: Path, pattern: str) -> dict[str, Any]:
    path, data = _latest_json(root, pattern)
    if path is None or data is None:
        return {"status": "MISSING", "report_path": None}
    return {
        "status": data.get("status") or data.get("gate", {}).get("status") or "UNKNOWN",
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
    }
    for evidence_key, status_key in aliases.items():
        stage = paper_evidence.get(evidence_key)
        if status_key not in stage_statuses and isinstance(stage, dict):
            stage_statuses[status_key] = stage.get("status")
    return stage_statuses


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
        if "msg_cd=40580000" in error or "장종료" in error:
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
    probe_ok = bool(probe and probe.get("status") == "PASS")
    probe_matched = _matched_order_count(probe)
    history_matched = _matched_order_count(order_history)
    probe_history = (probe.get("stages") or {}).get("order_history") if probe else {}
    probe_history_ok = bool(
        isinstance(probe_history, dict)
        and probe_history.get("status") == "PASS"
        and probe_matched > 0
    )
    explicit_history_ok = bool(
        order_history
        and order_history.get("status") == "PASS"
        and history_matched > 0
    )
    order_history_ok = probe_history_ok or explicit_history_ok
    external = any(
        _contains_kis_virtual_mode(item)
        for item in (balance, probe, order_history)
        if isinstance(item, dict)
    )
    status = "PASS" if balance_ok and probe_ok and order_history_ok else "BLOCKED"
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
            "probe_order": "PASS" if probe_ok else "BLOCKED",
            "order_history_requery": "PASS" if order_history_ok else "BLOCKED",
        },
        "paper_trading_evidence": {
            "balance_reconciliation": {
                "status": "PASS" if balance_ok else "BLOCKED",
                "report_path": (
                    _repo_relative(balance_path, root) if balance_path else None
                ),
            },
            "probe_order": {
                "status": "PASS" if probe_ok else "BLOCKED",
                "report_path": _repo_relative(probe_path, root) if probe_path else None,
                "blocker": _probe_order_blocker(probe) if not probe_ok else {},
            },
            "order_history": {
                "status": "PASS" if order_history_ok else "BLOCKED",
                "report_path": (
                    _repo_relative(order_history_path, root)
                    if order_history_path
                    else _repo_relative(probe_path, root)
                    if probe_history_ok and probe_path
                    else None
                ),
                "matched_order_count": max(probe_matched, history_matched),
            },
        },
    }


def _paper_auto_cycle_history_matched(data: dict[str, Any]) -> bool:
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
    path, data = _latest_json(
        root,
        "artifacts/reports/paper_auto_trading/paper_auto_service_rehearsal_*.json",
    )
    if path is None or data is None:
        return paper_trading
    external = bool(data.get("external_kis_api"))
    stage_statuses = _broker_stage_statuses(data)
    bundle_ids = _paper_auto_bundle_ids(data)
    bundle_match = bundle_id in bundle_ids
    cycle_history_matched = _paper_auto_cycle_history_matched(data)
    passed = (
        data.get("status") == "PASS"
        and external
        and stage_statuses.get("paper_auto_cycle") == "PASS"
        and stage_statuses.get("balance_reconciliation") == "PASS"
        and stage_statuses.get("probe_order") == "PASS"
        and stage_statuses.get("order_history_requery") == "PASS"
        and cycle_history_matched
        and bundle_match
    )
    return {
        "status": "PASS" if passed else "BLOCKED",
        "external_kis_api": external,
        "evidence_level": data.get("evidence_level"),
        "report_path": _repo_relative(path, root),
        "stage_statuses": stage_statuses,
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
    return {
        "schema_version": "1.0.0",
        "status": ssot_readiness,
        "generated_at": datetime.now(_KST).isoformat(),
        "bundle_id": bundle_id,
        "read_only": True,
        "external_api_called": False,
        "registry_mutated": False,
        "ssot_readiness": ssot_readiness,
        "deploy_quality": deploy_quality,
        "broker_evidence": broker_status,
        "live_trading_allowed": False,
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
        ),
        "kis_broker_evidence": broker,
        "be_contract": {
            "safe_to_show_dashboard": True,
            "safe_to_enable_order_actions": False,
            "safe_to_enable_live_actions": False,
            "status_endpoint_semantics": "read_only_local_artifact_status",
        },
    }

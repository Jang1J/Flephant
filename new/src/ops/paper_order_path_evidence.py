"""Resolve verified KIS virtual paper order-path evidence.

The order-path gate proves broker submit plus order-history verification.  It
must not be satisfied by balance-only, shadow-only, no-submit, or FDA veto
reports.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.config_loader import load as config_load
from src.utils.safe_cast import safe_bool, safe_int

_KST = ZoneInfo("Asia/Seoul")
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _repo_relative(path: Path | None, root: Path) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _json_candidates(root: Path, patterns: list[str]) -> list[tuple[Path, dict[str, Any]]]:
    seen: set[Path] = set()
    candidates: list[Path] = []
    for pattern in patterns:
        for path in root.glob(pattern):
            if path.is_file() and path not in seen:
                seen.add(path)
                candidates.append(path)
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    out: list[tuple[Path, dict[str, Any]]] = []
    for path in candidates:
        try:
            out.append((path, _load_json(path)))
        except Exception as e:
            _ = e
            continue
    return out


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


def _paper_cfg() -> dict[str, Any]:
    return config_load("risk_config.yaml", "paper_trading") or {}


def order_path_max_age_sec() -> int:
    cfg = _paper_cfg()
    return safe_int(
        cfg.get("order_path_evidence_max_age_sec", cfg.get("evidence_max_age_sec", 86400)),
        default=86400,
        min_value=1,
    )


def _same_day_order_path_warning_enabled() -> bool:
    cfg = _paper_cfg()
    return safe_bool(cfg.get("same_day_order_path_warning", True), default=True)


def _freshness_state(
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
    ref_now = ref_now.astimezone(_KST)
    age_sec = (ref_now - generated_at).total_seconds()
    same_day = generated_at.date() == ref_now.date()
    fresh = 0 <= age_sec <= max_age_sec
    state = {
        "status": "PASS" if fresh else "BLOCKED",
        "reason": None if fresh else "generated_at_stale_or_future",
        "generated_at": generated_at.isoformat(),
        "age_sec": round(age_sec, 3),
        "max_age_sec": max_age_sec,
        "same_day_evidence": same_day,
    }
    if fresh and not same_day and _same_day_order_path_warning_enabled():
        state["stale_warning"] = "order_path_not_same_day_but_within_max_age"
    return state


def _production_registry_state(root: Path) -> dict[str, Any]:
    path = root / "artifacts/lgbm/registry.json"
    if not path.exists():
        return {"status": "MISSING", "active_version": None}
    try:
        data = _load_json(path)
    except Exception as e:
        return {"status": "BLOCKED", "active_version": None, "error": str(e)}
    active = data.get("active_version")
    return {
        "status": "PASS" if active in (None, "") else "BLOCKED",
        "active_version": active,
    }


def _bundle_ids(data: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in ("bundle_id", "model_bundle_id"):
        value = data.get(key)
        if value:
            out.add(str(value))
    params = data.get("params")
    if isinstance(params, dict) and params.get("required_bundle_id"):
        out.add(str(params["required_bundle_id"]))
    stages = data.get("stages") if isinstance(data.get("stages"), dict) else {}
    paper_cycle = stages.get("paper_auto_cycle") if isinstance(stages, dict) else {}
    if isinstance(paper_cycle, dict):
        out.update(_bundle_ids(paper_cycle))
        active = (paper_cycle.get("stages") or {}).get("active_model_guard")
        if isinstance(active, dict) and active.get("bundle_id"):
            out.add(str(active["bundle_id"]))
    active = stages.get("active_model_guard") if isinstance(stages, dict) else None
    if isinstance(active, dict) and active.get("bundle_id"):
        out.add(str(active["bundle_id"]))
    return out


def _evidence_metadata(data: dict[str, Any]) -> dict[str, Any]:
    evidence = data.get("evidence")
    return evidence if isinstance(evidence, dict) else {}


def _broker_fingerprint(data: dict[str, Any]) -> str:
    return str(_evidence_metadata(data).get("broker_env_fingerprint") or "")


def _kis_mode(data: dict[str, Any]) -> str:
    runtime = data.get("runtime")
    if isinstance(runtime, dict) and runtime.get("kis_mode"):
        return str(runtime["kis_mode"]).lower()
    stages = data.get("stages") if isinstance(data.get("stages"), dict) else {}
    mode_guard = stages.get("mode_guard") if isinstance(stages, dict) else {}
    if isinstance(mode_guard, dict) and mode_guard.get("current_mode"):
        return str(mode_guard["current_mode"]).lower()
    for stage in stages.values() if isinstance(stages, dict) else []:
        if isinstance(stage, dict) and stage.get("_mode"):
            return str(stage["_mode"]).lower()
    return ""


def _live_disabled(data: dict[str, Any]) -> bool:
    runtime = data.get("runtime")
    if isinstance(runtime, dict) and safe_bool(runtime.get("live_enabled"), default=False):
        return False
    if safe_bool(data.get("live_trading_enabled"), default=False):
        return False
    if safe_bool(data.get("live_trading_allowed"), default=False):
        return False
    return True


def _is_external_kis_virtual(data: dict[str, Any]) -> bool:
    if safe_bool(data.get("external_kis_api"), default=False):
        return True
    if str(data.get("action") or "") in {"submit_probe_order", "order_history"}:
        return _kis_mode(data) == "virtual"
    runtime = data.get("runtime")
    if isinstance(runtime, dict):
        return (
            str(runtime.get("kis_mode", "")).lower() == "virtual"
            and str(runtime.get("execution_mode", "")).lower() == "paper"
            and safe_bool(runtime.get("broker_submit_enabled"), default=False)
            and not safe_bool(runtime.get("shadow_only"), default=True)
        )
    return _kis_mode(data) == "virtual"


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


def _broker_order_ids_from_fills(fills: Any) -> list[str]:
    ids: list[str] = []
    for fill in fills if isinstance(fills, list) else []:
        if not isinstance(fill, dict):
            continue
        raw = (
            fill.get("broker_order_id")
            or fill.get("order_id")
            or (fill.get("broker_response") or {}).get("order_id")
            or (fill.get("broker_response") or {}).get("ODNO")
            or (fill.get("broker_response") or {}).get("odno")
        )
        if raw:
            ids.append(str(raw))
    return ids


def _verified_order_ids_from_queries(queries: Any) -> set[str]:
    verified: set[str] = set()
    for query in queries if isinstance(queries, list) else []:
        if not isinstance(query, dict):
            continue
        matched_orders = query.get("matched_orders")
        for order in matched_orders if isinstance(matched_orders, list) else []:
            if not isinstance(order, dict):
                continue
            for key in ("order_id", "broker_order_id", "ODNO", "odno"):
                raw = order.get(key)
                if raw:
                    verified.add(str(raw))
            response = order.get("broker_response")
            if isinstance(response, dict):
                for key in ("order_id", "broker_order_id", "ODNO", "odno"):
                    raw = response.get(key)
                    if raw:
                        verified.add(str(raw))
        raw_query = query.get("query")
        if isinstance(raw_query, dict) and query.get("status") == "PASS":
            order_id = raw_query.get("order_id")
            if order_id:
                verified.add(str(order_id))
    return verified


def _cycle_bar_ready(cycle: dict[str, Any]) -> bool:
    readiness = cycle.get("hot_path_bar_readiness")
    if isinstance(readiness, dict):
        return readiness.get("status") == "PASS"
    return True


def paper_auto_order_path_evidence_from_report(
    data: dict[str, Any],
    *,
    report_path: Path | None = None,
    root: Path | None = None,
    bundle_id: str | None = None,
    max_age_sec: int | None = None,
    now: datetime | None = None,
    profile_fingerprint: str | None = None,
) -> dict[str, Any]:
    repo_root = root or _REPO_ROOT
    max_age = max_age_sec or order_path_max_age_sec()
    freshness = _freshness_state(data.get("generated_at"), max_age_sec=max_age, now=now)
    bundles = _bundle_ids(data)
    requested_bundle_id = str(bundle_id or "").strip()
    bundle_match = not requested_bundle_id or requested_bundle_id in bundles
    fingerprint = _broker_fingerprint(data)
    fingerprint_match = (
        not profile_fingerprint
        or (bool(fingerprint) and fingerprint == str(profile_fingerprint))
    )
    cycles = _paper_auto_cycles(data)
    broker_order_ids: list[str] = []
    verified_order_ids: set[str] = set()
    failures: list[dict[str, Any]] = []
    matched_order_count = 0
    submitted_cycle_count = 0

    for cycle in cycles:
        if not isinstance(cycle, dict):
            continue
        execution = cycle.get("execution")
        execution_report = (
            execution.get("execution_report")
            if isinstance(execution, dict)
            else {}
        )
        fills = execution_report.get("fills") if isinstance(execution_report, dict) else []
        cycle_ids = _broker_order_ids_from_fills(fills)
        submitted = bool(cycle.get("broker_order_submitted")) or bool(cycle_ids)
        if not submitted:
            continue
        submitted_cycle_count += 1
        if not _cycle_bar_ready(cycle):
            failures.append({
                "cycle_index": cycle.get("cycle_index"),
                "reason": "hot_path_bar_readiness_not_pass",
            })
        if not cycle_ids:
            failures.append({
                "cycle_index": cycle.get("cycle_index"),
                "reason": "broker_order_id_missing",
            })
            continue
        broker_order_ids.extend(cycle_ids)
        verification = cycle.get("order_history_verification")
        if not isinstance(verification, dict) or verification.get("status") != "PASS":
            failures.append({
                "cycle_index": cycle.get("cycle_index"),
                "reason": "order_history_verification_not_pass",
            })
            continue
        queries = verification.get("queries")
        if not isinstance(queries, list) or not queries:
            failures.append({
                "cycle_index": cycle.get("cycle_index"),
                "reason": "order_history_queries_missing",
            })
            continue
        for query in queries:
            if not isinstance(query, dict):
                failures.append({
                    "cycle_index": cycle.get("cycle_index"),
                    "reason": "order_history_query_invalid",
                })
                continue
            matched = safe_int(query.get("matched_order_count", 0), default=0, min_value=0)
            if matched <= 0:
                failures.append({
                    "cycle_index": cycle.get("cycle_index"),
                    "reason": "matched_order_count_zero",
                    "query": query.get("query"),
                })
            else:
                matched_order_count += matched
        verified_order_ids.update(_verified_order_ids_from_queries(queries))

    unique_order_ids = sorted({order_id for order_id in broker_order_ids if order_id})
    unmatched = [
        order_id for order_id in unique_order_ids
        if order_id not in verified_order_ids
    ]
    if unmatched:
        failures.append({"reason": "submitted_order_unmatched", "order_ids": unmatched})
    if submitted_cycle_count <= 0:
        failures.append({"reason": "no_submitted_paper_orders"})
    if matched_order_count <= 0:
        failures.append({"reason": "matched_order_count_zero"})

    status_checks = {
        "report_status_pass": data.get("status") == "PASS",
        "external_kis_virtual": _is_external_kis_virtual(data),
        "kis_mode_virtual": _kis_mode(data) in {"", "virtual"},
        "live_disabled": _live_disabled(data),
        "freshness_pass": freshness.get("status") == "PASS",
        "bundle_match": bundle_match,
        "profile_fingerprint_match": fingerprint_match,
        "production_registry_inactive": _production_registry_state(repo_root).get("status") != "BLOCKED",
        "broker_order_ids_present": bool(unique_order_ids),
        "all_submitted_orders_verified": not failures,
    }
    for name, passed in status_checks.items():
        if not passed:
            failures.append({"reason": name})
    status = "PASS" if all(status_checks.values()) else "BLOCKED"
    return {
        "status": status,
        "evidence_type": "paper_auto_order",
        "selected_source": "paper_auto_order",
        "report_path": _repo_relative(report_path, repo_root),
        "generated_at": freshness.get("generated_at"),
        "age_sec": freshness.get("age_sec"),
        "max_age_sec": freshness.get("max_age_sec"),
        "same_day_evidence": freshness.get("same_day_evidence"),
        "stale_warning": freshness.get("stale_warning"),
        "external_kis_api": status_checks["external_kis_virtual"],
        "kis_mode": _kis_mode(data) or "virtual",
        "live_trading_allowed": False,
        "bundle_id": requested_bundle_id or None,
        "bundle_ids": sorted(bundles),
        "bundle_match": bundle_match,
        "broker_env_fingerprint": fingerprint,
        "profile_fingerprint_match": fingerprint_match,
        "broker_order_id_count": len(unique_order_ids),
        "broker_order_ids": unique_order_ids,
        "matched_order_count": matched_order_count,
        "verified_order_ids": sorted(verified_order_ids),
        "unmatched_order_count": len(unmatched),
        "submitted_cycle_count": submitted_cycle_count,
        "status_checks": status_checks,
        "freshness": freshness,
        "failures": failures,
    }


def probe_order_path_evidence_from_report(
    data: dict[str, Any],
    *,
    report_path: Path | None = None,
    root: Path | None = None,
    max_age_sec: int | None = None,
    now: datetime | None = None,
    profile_fingerprint: str | None = None,
) -> dict[str, Any]:
    repo_root = root or _REPO_ROOT
    max_age = max_age_sec or order_path_max_age_sec()
    freshness = _freshness_state(data.get("generated_at"), max_age_sec=max_age, now=now)
    fingerprint = _broker_fingerprint(data)
    fingerprint_match = (
        not profile_fingerprint
        or (bool(fingerprint) and fingerprint == str(profile_fingerprint))
    )
    execution = (data.get("stages") or {}).get("execution")
    execution_report = (
        ((execution or {}).get("result") or {}).get("execution_report")
        if isinstance(execution, dict)
        else {}
    )
    broker_order_ids = _broker_order_ids_from_fills(
        execution_report.get("fills") if isinstance(execution_report, dict) else []
    )
    order_history = (data.get("stages") or {}).get("order_history")
    matched_order_count = _matched_order_count(data)
    status_checks = {
        "report_status_pass": data.get("status") == "PASS",
        "execution_status_pass": isinstance(execution, dict) and execution.get("status") == "PASS",
        "order_history_status_pass": (
            isinstance(order_history, dict) and order_history.get("status") == "PASS"
        ),
        "matched_order_count_positive": matched_order_count > 0,
        "broker_order_ids_present": bool(broker_order_ids),
        "external_kis_virtual": _is_external_kis_virtual(data),
        "kis_mode_virtual": _kis_mode(data) == "virtual",
        "live_disabled": _live_disabled(data),
        "freshness_pass": freshness.get("status") == "PASS",
        "profile_fingerprint_match": fingerprint_match,
        "production_registry_inactive": _production_registry_state(repo_root).get("status") != "BLOCKED",
    }
    failures = [
        {"reason": name}
        for name, passed in status_checks.items()
        if not passed
    ]
    status = "PASS" if all(status_checks.values()) else "BLOCKED"
    return {
        "status": status,
        "evidence_type": "probe_order",
        "selected_source": "probe_order",
        "report_path": _repo_relative(report_path, repo_root),
        "generated_at": freshness.get("generated_at"),
        "age_sec": freshness.get("age_sec"),
        "max_age_sec": freshness.get("max_age_sec"),
        "same_day_evidence": freshness.get("same_day_evidence"),
        "stale_warning": freshness.get("stale_warning"),
        "external_kis_api": status_checks["external_kis_virtual"],
        "kis_mode": _kis_mode(data),
        "live_trading_allowed": False,
        "broker_env_fingerprint": fingerprint,
        "profile_fingerprint_match": fingerprint_match,
        "broker_order_id_count": len(set(broker_order_ids)),
        "broker_order_ids": sorted(set(broker_order_ids)),
        "matched_order_count": matched_order_count,
        "unmatched_order_count": 0 if matched_order_count > 0 else len(set(broker_order_ids)),
        "status_checks": status_checks,
        "freshness": freshness,
        "failures": failures,
    }


def explicit_order_history_evidence_from_report(
    data: dict[str, Any],
    *,
    report_path: Path | None = None,
    root: Path | None = None,
    max_age_sec: int | None = None,
    now: datetime | None = None,
    profile_fingerprint: str | None = None,
) -> dict[str, Any]:
    repo_root = root or _REPO_ROOT
    max_age = max_age_sec or order_path_max_age_sec()
    freshness = _freshness_state(data.get("generated_at"), max_age_sec=max_age, now=now)
    fingerprint = _broker_fingerprint(data)
    fingerprint_match = (
        not profile_fingerprint
        or (bool(fingerprint) and fingerprint == str(profile_fingerprint))
    )
    stage = (data.get("stages") or {}).get("order_history")
    matched_order_count = _matched_order_count(data)
    query = stage.get("query") if isinstance(stage, dict) else {}
    order_id = str((query or {}).get("order_id") or "")
    status_checks = {
        "report_status_pass": data.get("status") == "PASS",
        "order_history_status_pass": isinstance(stage, dict) and stage.get("status") == "PASS",
        "matched_order_count_positive": matched_order_count > 0,
        "known_order_id_present": bool(order_id),
        "external_kis_virtual": _is_external_kis_virtual(data),
        "kis_mode_virtual": _kis_mode(data) == "virtual",
        "live_disabled": _live_disabled(data),
        "freshness_pass": freshness.get("status") == "PASS",
        "profile_fingerprint_match": fingerprint_match,
        "production_registry_inactive": _production_registry_state(repo_root).get("status") != "BLOCKED",
    }
    failures = [
        {"reason": name}
        for name, passed in status_checks.items()
        if not passed
    ]
    status = "PASS" if all(status_checks.values()) else "BLOCKED"
    return {
        "status": status,
        "evidence_type": "explicit_order_history",
        "selected_source": "explicit_order_history",
        "report_path": _repo_relative(report_path, repo_root),
        "generated_at": freshness.get("generated_at"),
        "age_sec": freshness.get("age_sec"),
        "max_age_sec": freshness.get("max_age_sec"),
        "same_day_evidence": freshness.get("same_day_evidence"),
        "stale_warning": freshness.get("stale_warning"),
        "external_kis_api": status_checks["external_kis_virtual"],
        "kis_mode": _kis_mode(data),
        "live_trading_allowed": False,
        "broker_env_fingerprint": fingerprint,
        "profile_fingerprint_match": fingerprint_match,
        "broker_order_id_count": 1 if order_id else 0,
        "broker_order_ids": [order_id] if order_id else [],
        "matched_order_count": matched_order_count,
        "unmatched_order_count": 0 if matched_order_count > 0 else 1,
        "status_checks": status_checks,
        "freshness": freshness,
        "failures": failures,
    }


def _paper_auto_cycles(data: dict[str, Any]) -> list[dict[str, Any]]:
    stages = data.get("stages") if isinstance(data.get("stages"), dict) else {}
    cycles = stages.get("cycles") if isinstance(stages, dict) else {}
    if isinstance(cycles, dict) and isinstance(cycles.get("items"), list):
        return [item for item in cycles["items"] if isinstance(item, dict)]
    paper_auto_cycle = stages.get("paper_auto_cycle") if isinstance(stages, dict) else {}
    if isinstance(paper_auto_cycle, dict):
        return _paper_auto_cycles(paper_auto_cycle)
    direct_cycles = data.get("cycles")
    if isinstance(direct_cycles, list):
        return [item for item in direct_cycles if isinstance(item, dict)]
    return []


def summarize_paper_order_path_evidence(
    data: dict[str, Any],
    *,
    report_path: Path | None = None,
    root: Path | None = None,
    bundle_id: str | None = None,
    max_age_sec: int | None = None,
    now: datetime | None = None,
    profile_fingerprint: str | None = None,
) -> dict[str, Any]:
    action = str(data.get("action") or "")
    if action == "submit_probe_order":
        return probe_order_path_evidence_from_report(
            data,
            report_path=report_path,
            root=root,
            max_age_sec=max_age_sec,
            now=now,
            profile_fingerprint=profile_fingerprint,
        )
    if action == "order_history":
        return explicit_order_history_evidence_from_report(
            data,
            report_path=report_path,
            root=root,
            max_age_sec=max_age_sec,
            now=now,
            profile_fingerprint=profile_fingerprint,
        )
    return paper_auto_order_path_evidence_from_report(
        data,
        report_path=report_path,
        root=root,
        bundle_id=bundle_id,
        max_age_sec=max_age_sec,
        now=now,
        profile_fingerprint=profile_fingerprint,
    )


def find_fresh_paper_order_path_evidence(
    *,
    root: Path | None = None,
    bundle_id: str | None = None,
    max_age_sec: int | None = None,
    now: datetime | None = None,
    profile_fingerprint: str | None = None,
) -> dict[str, Any]:
    repo_root = root or _REPO_ROOT
    patterns = [
        "artifacts/reports/paper_auto_trading/**/paper_auto_trade_*.json",
        "artifacts/reports/paper_auto_trading/paper_auto_service_rehearsal_*.json",
        "artifacts/reports/paper_trading/paper_trading_submit_probe_order_*.json",
        "artifacts/reports/paper_trading/paper_trading_order_history_*.json",
    ]
    inspected: list[dict[str, Any]] = []
    for path, data in _json_candidates(repo_root, patterns):
        state = summarize_paper_order_path_evidence(
            data,
            report_path=path,
            root=repo_root,
            bundle_id=bundle_id,
            max_age_sec=max_age_sec,
            now=now,
            profile_fingerprint=profile_fingerprint,
        )
        inspected.append({
            "report_path": state.get("report_path"),
            "evidence_type": state.get("evidence_type"),
            "status": state.get("status"),
            "failures": state.get("failures", [])[:3],
        })
        if state.get("status") == "PASS":
            state["inspected_report_count"] = len(inspected)
            return state
    return {
        "status": "BLOCKED",
        "reason": "paper_order_path_evidence_missing",
        "evidence_type": "paper_order_path",
        "selected_source": None,
        "bundle_id": str(bundle_id or "").strip() or None,
        "matched_order_count": 0,
        "broker_order_id_count": 0,
        "unmatched_order_count": 0,
        "inspected_report_count": len(inspected),
        "inspected_reports": inspected[:10],
        "failures": [{"reason": "fresh_verified_order_path_evidence_not_found"}],
    }

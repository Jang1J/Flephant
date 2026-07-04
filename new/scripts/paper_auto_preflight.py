#!/usr/bin/env python
"""Paper-auto 전용 preflight gate.

Strict prelive gate는 C12 deploy-quality까지 요구한다. 이 좁은 gate는
KIS virtual paper rehearsal에 필요한 증거만 확인한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import model_registry_readiness  # noqa: E402
import print_env_readiness  # noqa: E402
from src.ops.paper_order_path_evidence import (  # noqa: E402
    find_fresh_paper_order_path_evidence,
    summarize_paper_order_path_evidence,
)
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.safe_cast import safe_bool, safe_int  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")


def _paper_report_dir() -> Path:
    cfg = config_load("risk_config.yaml", "paper_trading")
    path = Path(str(cfg["report_dir"]))
    return path if path.is_absolute() else ROOT / path


def _latest_json(pattern: str) -> dict[str, Any] | None:
    files = sorted(_paper_report_dir().glob(pattern))
    if not files:
        return None
    for path in reversed(files):
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                data["_path"] = str(path)
                return data
        except Exception:
            continue
    return None


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


def _evidence_fingerprint(report: dict[str, Any] | None) -> str:
    evidence = report.get("evidence") if isinstance(report, dict) else {}
    if not isinstance(evidence, dict):
        return ""
    return str(evidence.get("broker_env_fingerprint") or "")


def _evidence_generated_at(report: dict[str, Any] | None) -> datetime | None:
    if not isinstance(report, dict):
        return None
    raw = str(report.get("generated_at") or "")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_KST)
    return dt.astimezone(_KST)


def _evidence_set_guard(reports: list[dict[str, Any] | None]) -> dict[str, Any]:
    present = [report for report in reports if isinstance(report, dict)]
    if not present:
        return {"status": "BLOCKED", "reason": "evidence_reports_missing"}

    missing = [
        str(report.get("_path", "<unknown>"))
        for report in present
        if not _evidence_fingerprint(report)
    ]
    if missing:
        return {
            "status": "BLOCKED",
            "reason": "broker_env_fingerprint_missing",
            "reports": missing,
        }

    fingerprints = sorted({_evidence_fingerprint(report) for report in present})
    if len(fingerprints) != 1:
        return {
            "status": "BLOCKED",
            "reason": "broker_env_fingerprint_mismatch",
            "fingerprints": fingerprints,
        }

    cfg = config_load("risk_config.yaml", "paper_trading") or {}
    max_age_sec = int(cfg.get("evidence_max_age_sec", 86400))
    cutoff = datetime.now(_KST) - timedelta(seconds=max_age_sec)
    stale = []
    for report in present:
        generated_at = _evidence_generated_at(report)
        if generated_at is None or generated_at < cutoff:
            stale.append(str(report.get("_path", "<unknown>")))
    if stale:
        return {
            "status": "BLOCKED",
            "reason": "evidence_report_stale",
            "max_age_sec": max_age_sec,
            "reports": stale,
        }

    return {
        "status": "PASS",
        "broker_env_fingerprint": fingerprints[0],
        "max_age_sec": max_age_sec,
    }


def _probe_order_blocker(probe: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(probe, dict):
        return {"error_code": "PROBE_ORDER_REPORT_MISSING"}
    if probe.get("status") == "PASS":
        return {}
    execution = (probe.get("stages") or {}).get("execution")
    result = execution.get("result") if isinstance(execution, dict) else {}
    report = result.get("execution_report") if isinstance(result, dict) else {}
    rejections = report.get("rejections") if isinstance(report, dict) else []
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


def _paper_evidence() -> dict[str, Any]:
    balance = _latest_json("paper_trading_balance_reconciliation_*.json")
    probe = _latest_json("paper_trading_submit_probe_order_*.json")
    order_history = _latest_json("paper_trading_order_history_*.json")

    balance_stage = (balance.get("stages") or {}).get("balance") if balance else {}
    reconciliation_stage = (
        (balance.get("stages") or {}).get("reconciliation")
        if balance else {}
    )
    balance_ok = bool(
        balance
        and balance.get("status") == "PASS"
        and isinstance(balance_stage, dict)
        and balance_stage.get("status") == "PASS"
        and isinstance(reconciliation_stage, dict)
        and reconciliation_stage.get("status") == "PASS"
    )
    probe_matched_order_count = _matched_order_count(probe)
    history_matched_order_count = _matched_order_count(order_history)
    probe_history_ok = bool(
        probe
        and probe.get("stages", {}).get("order_history", {}).get("status") == "PASS"
        and probe_matched_order_count > 0
    )
    evidence_reports = [balance]
    if probe:
        evidence_reports.append(probe)
    if order_history and not probe_history_ok:
        evidence_reports.append(order_history)
    evidence_guard = _evidence_set_guard(evidence_reports)
    profile_fingerprint = _evidence_fingerprint(balance)
    order_path_candidates: list[dict[str, Any]] = []
    for item in (probe, order_history):
        if not isinstance(item, dict):
            continue
        raw_path = item.get("_path")
        path = Path(str(raw_path)) if raw_path else None
        order_path_candidates.append(
            summarize_paper_order_path_evidence(
                item,
                report_path=path,
                root=ROOT,
                profile_fingerprint=profile_fingerprint or None,
            )
        )
    order_path = next(
        (candidate for candidate in order_path_candidates if candidate.get("status") == "PASS"),
        None,
    )
    if order_path is None:
        order_path = find_fresh_paper_order_path_evidence(
            root=ROOT,
            profile_fingerprint=profile_fingerprint or None,
        )
    order_path_ok = order_path.get("status") == "PASS"
    status = (
        "PASS"
        if (
            balance_ok
            and order_path_ok
            and evidence_guard.get("status") == "PASS"
        )
        else "BLOCKED"
    )
    return {
        "status": status,
        "evidence_guard": evidence_guard,
        "balance_reconciliation": {
            "status": "PASS" if balance_ok else "BLOCKED",
            "report_path": balance.get("_path") if balance else None,
            "balance_status": (
                balance_stage.get("status") if isinstance(balance_stage, dict) else None
            ),
            "reconciliation_status": (
                reconciliation_stage.get("status")
                if isinstance(reconciliation_stage, dict)
                else None
            ),
        },
        "probe_order": {
            "status": "PASS" if order_path_ok else "BLOCKED",
            "report_path": probe.get("_path") if probe else None,
            "deprecated": True,
            "replaced_by": "paper_order_path",
            "evidence_type": order_path.get("evidence_type"),
            "blocker": _probe_order_blocker(probe) if not order_path_ok else {},
        },
        "order_history": {
            "status": "PASS" if order_path_ok else "BLOCKED",
            "report_path": (
                order_history.get("_path")
                if order_history
                else probe.get("_path") if probe_history_ok and probe else None
            ),
            "matched_order_count": max(
                safe_int(order_path.get("matched_order_count", 0), default=0, min_value=0),
                probe_matched_order_count,
                history_matched_order_count,
            ),
        },
        "paper_order_path": order_path,
    }


def _ops_risk() -> dict[str, Any]:
    exec_cfg = config_load("risk_config.yaml", "execution") or {}
    live_enabled = safe_bool(exec_cfg.get("live_enabled", False), default=False)
    return {
        "status": "PASS" if not live_enabled else "BLOCKED",
        "risk_config_live_enabled": live_enabled,
    }


def build_report(registry_dir: str | None = None) -> dict[str, Any]:
    env = print_env_readiness.build_report()
    registry = model_registry_readiness.build_report(
        registry_dir=registry_dir,
        require_active=True,
    )
    ops = _ops_risk()
    evidence = _paper_evidence()
    stages = {
        "env_readiness": env,
        "ops_risk": ops,
        "active_registry": registry,
        "paper_evidence": evidence,
    }
    blockers = [
        name
        for name, stage in stages.items()
        if stage.get("status") not in {"PASS", "WARN"}
    ]
    return {
        "status": "PASS" if not blockers else "BLOCKED",
        "generated_at": datetime.now(_KST).isoformat(),
        "scope": "paper-rehearsal",
        "blockers": blockers,
        "stage_statuses": {name: stage.get("status") for name, stage in stages.items()},
        "stages": stages,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paper-auto preflight")
    parser.add_argument("--registry-dir", default=None)
    parser.add_argument(
        "--no-write-report",
        action="store_true",
        help="Compatibility flag. This preflight is stdout-only and never writes a report.",
    )
    args = parser.parse_args(argv)
    report = build_report(registry_dir=args.registry_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

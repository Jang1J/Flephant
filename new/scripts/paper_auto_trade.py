#!/usr/bin/env python
"""KIS virtual 모의투자 자동매매 CLI.

프리라이브 게이트 PASS 이후에만 Hot Path → ExecutionGateway(paper)를 연결한다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import prelive_gate  # noqa: E402
from src.execution.paper_auto_trading import PaperAutoTrader  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.safe_cast import safe_bool  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402

_PROFILE_SUFFIXES = (
    "APP_KEY",
    "APP_SECRET",
    "ACCOUNT_NUMBER",
    "ACCOUNT_PRODUCT_CODE",
)
_KST = ZoneInfo("Asia/Seoul")
_PAPER_BROKER_STAGE_NAMES = (
    "06_paper_balance",
    "07_paper_reconciliation",
    "08_paper_probe_order",
)


def _profile_token(raw: str) -> str:
    return str(raw or "").strip().upper().replace("-", "_")


def _apply_kis_profile(profile: str) -> dict[str, Any]:
    """Map TRACK_KIS_PAPER_* env vars into the effective KIS paper env."""
    token = _profile_token(profile)
    if not token:
        return {"status": "PASS", "profile": ""}
    sources = {suffix: f"{token}_KIS_PAPER_{suffix}" for suffix in _PROFILE_SUFFIXES}
    missing = [env for env in sources.values() if not os.environ.get(env, "").strip()]
    if missing:
        return {
            "status": "BLOCKED",
            "reason": "kis_profile_env_missing",
            "profile": token,
            "missing_env": missing,
        }
    for suffix, source in sources.items():
        value = os.environ[source].strip()
        os.environ[f"KIS_PAPER_{suffix}"] = value
        os.environ[f"KIS_{suffix}"] = value
    return {
        "status": "PASS",
        "profile": token,
        "selected_env": list(sources.values()),
    }


def _load_active_tickers(max_tickers: int) -> list[str]:
    cfg = config_load("universe_config.yaml") or {}
    tickers: list[str] = []
    for sector in (cfg.get("sectors") or {}).values():
        for stock in sector.get("stocks", []):
            if stock.get("status") == "active" and stock.get("ticker"):
                tickers.append(pad_ticker(str(stock["ticker"])))
    return tickers[:max_tickers]


def _parse_tickers(raw: str, max_tickers: int) -> list[str]:
    if raw.strip():
        return [pad_ticker(t.strip()) for t in raw.split(",") if t.strip()]
    return _load_active_tickers(max_tickers)


def _repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _paper_trading_report_dir() -> Path:
    cfg = config_load("risk_config.yaml", "paper_trading") or {}
    path = Path(str(cfg.get("report_dir") or "artifacts/reports/paper_trading"))
    return path if path.is_absolute() else ROOT / path


def _latest_paper_trading_report(
    pattern: str,
    *,
    broker_env_fingerprint: str | None = None,
) -> tuple[Path | None, dict[str, Any] | None]:
    report_dir = _paper_trading_report_dir()
    for path in reversed(sorted(report_dir.glob(pattern))):
        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if broker_env_fingerprint:
            report_fp = _report_broker_env_fingerprint(payload)
            if report_fp != broker_env_fingerprint:
                continue
            return path, payload
    return None, None


def _report_generated_at(payload: dict[str, Any] | None) -> datetime | None:
    if not isinstance(payload, dict):
        return None
    raw = str(payload.get("generated_at") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_KST)
    return dt.astimezone(_KST)


def _report_freshness(payload: dict[str, Any] | None) -> dict[str, Any]:
    cfg = config_load("risk_config.yaml", "paper_trading") or {}
    max_age_sec = int(cfg.get("evidence_max_age_sec", 86400))
    require_same_day = safe_bool(cfg.get("require_same_day_evidence"), default=True)
    generated_at = _report_generated_at(payload)
    now = datetime.now(_KST)
    if generated_at is None:
        return {
            "status": "BLOCKED",
            "reason": "generated_at_missing_or_invalid",
            "max_age_sec": max_age_sec,
        }
    age_sec = (now - generated_at).total_seconds()
    if age_sec < 0 or generated_at < now - timedelta(seconds=max_age_sec):
        return {
            "status": "BLOCKED",
            "reason": "generated_at_stale_or_future",
            "generated_at": generated_at.isoformat(),
            "age_sec": round(age_sec, 3),
            "max_age_sec": max_age_sec,
        }
    if require_same_day and generated_at.date() != now.date():
        return {
            "status": "BLOCKED",
            "reason": "generated_at_not_same_trading_day",
            "generated_at": generated_at.isoformat(),
            "current_date": now.date().isoformat(),
            "age_sec": round(age_sec, 3),
            "max_age_sec": max_age_sec,
            "require_same_day_evidence": True,
        }
    return {
        "status": "PASS",
        "generated_at": generated_at.isoformat(),
        "age_sec": round(age_sec, 3),
        "max_age_sec": max_age_sec,
        "require_same_day_evidence": require_same_day,
    }


def _current_broker_env_fingerprint() -> str:
    mode = str(os.environ.get("KIS_MODE") or "").strip().lower()
    app_key = str(
        os.environ.get("KIS_PAPER_APP_KEY")
        or os.environ.get("KIS_APP_KEY")
        or ""
    ).strip()
    account_no = str(
        os.environ.get("KIS_PAPER_ACCOUNT_NUMBER")
        or os.environ.get("KIS_ACCOUNT_NUMBER")
        or ""
    ).strip()
    product_code = str(
        os.environ.get("KIS_PAPER_ACCOUNT_PRODUCT_CODE")
        or os.environ.get("KIS_ACCOUNT_PRODUCT_CODE")
        or ""
    ).strip()
    if not (mode or app_key or account_no):
        return ""
    return hashlib.sha256(
        f"{mode}|{app_key}|{account_no}|{product_code}".encode("utf-8")
    ).hexdigest()[:16]


def _report_broker_env_fingerprint(payload: dict[str, Any] | None) -> str:
    evidence = payload.get("evidence") if isinstance(payload, dict) else {}
    if not isinstance(evidence, dict):
        return ""
    return str(evidence.get("broker_env_fingerprint") or "").strip()


def _stage_status(payload: dict[str, Any] | None, stage_name: str) -> str:
    stages = payload.get("stages") if isinstance(payload, dict) else {}
    stage = stages.get(stage_name) if isinstance(stages, dict) else {}
    return str(stage.get("status") or "MISSING").upper() if isinstance(stage, dict) else "MISSING"


def _read_only_broker_bootstrap_gate() -> dict[str, Any]:
    """Allow paper-rehearsal startup without placing a daily probe order.

    Strict deploy/prelive evidence still requires external paper-auto evidence.
    This narrow runtime bootstrap only checks the current account is KIS virtual,
    live disabled, fresh balance is readable, and all-ticker unfilled order
    history is empty for the same broker fingerprint.
    """
    current_fp = _current_broker_env_fingerprint()
    blockers: list[str] = []
    if not current_fp:
        blockers.append("current_broker_env_fingerprint_missing")
    balance_path, balance = _latest_paper_trading_report(
        "paper_trading_balance_reconciliation_*.json",
        broker_env_fingerprint=current_fp or None,
    )
    history_path, history = _latest_paper_trading_report(
        "paper_trading_order_history_*.json",
        broker_env_fingerprint=current_fp or None,
    )

    balance_freshness = _report_freshness(balance)
    history_freshness = _report_freshness(history)
    if balance_path is None or not isinstance(balance, dict):
        blockers.append("fresh_balance_report_missing")
    elif balance.get("status") != "PASS":
        blockers.append("fresh_balance_report_not_pass")
    elif _stage_status(balance, "balance") != "PASS":
        blockers.append("fresh_balance_stage_not_pass")
    if balance_freshness.get("status") != "PASS":
        blockers.append("fresh_balance_report_stale")

    if history_path is None or not isinstance(history, dict):
        blockers.append("fresh_pending_order_history_missing")
    elif history.get("status") != "PASS":
        blockers.append("fresh_pending_order_history_not_pass")
    elif _stage_status(history, "order_history") != "PASS":
        blockers.append("fresh_pending_order_history_stage_not_pass")
    if history_freshness.get("status") != "PASS":
        blockers.append("fresh_pending_order_history_stale")

    balance_fp = _report_broker_env_fingerprint(balance)
    history_fp = _report_broker_env_fingerprint(history)
    if balance_fp and current_fp and balance_fp != current_fp:
        blockers.append("fresh_balance_broker_fingerprint_mismatch")
    if history_fp and current_fp and history_fp != current_fp:
        blockers.append("fresh_pending_history_broker_fingerprint_mismatch")
    if balance_fp and history_fp and balance_fp != history_fp:
        blockers.append("read_only_evidence_fingerprint_mismatch")

    runtime = balance.get("runtime") if isinstance(balance, dict) else {}
    if not isinstance(runtime, dict) or str(runtime.get("kis_mode", "")).lower() != "virtual":
        blockers.append("balance_report_not_virtual_mode")
    if isinstance(runtime, dict) and bool(runtime.get("live_enabled")):
        blockers.append("balance_report_live_enabled_true")

    balance_stage = (
        (balance.get("stages") or {}).get("balance")
        if isinstance(balance, dict)
        else {}
    )
    balance_payload = (
        balance_stage.get("balance")
        if isinstance(balance_stage, dict)
        else {}
    )
    cash = float(balance_payload.get("cash") or 0.0) if isinstance(balance_payload, dict) else 0.0
    net_asset = float(balance_payload.get("net_asset") or 0.0) if isinstance(balance_payload, dict) else 0.0
    if _stage_status(balance, "reconciliation") != "PASS":
        blockers.append("fresh_reconciliation_stage_not_pass")
    if cash <= 0:
        blockers.append("cash_not_positive")
    if net_asset <= 0:
        blockers.append("net_asset_not_positive")

    order_stage = (
        (history.get("stages") or {}).get("order_history")
        if isinstance(history, dict)
        else {}
    )
    query = order_stage.get("query") if isinstance(order_stage, dict) else {}
    matched_order_count = int(order_stage.get("matched_order_count") or 0) if isinstance(order_stage, dict) else 0
    if isinstance(query, dict):
        if str(query.get("ticker") or "") != "":
            blockers.append("pending_order_history_not_all_tickers")
        if str(query.get("execution_filter") or "").lower() != "unfilled":
            blockers.append("pending_order_history_not_unfilled_filter")
    else:
        blockers.append("pending_order_history_query_missing")
    if matched_order_count != 0:
        blockers.append("pending_orders_present")

    return {
        "status": "PASS" if not blockers else "BLOCKED",
        "scope": "paper-rehearsal-read-only-bootstrap",
        "blockers": blockers,
        "balance_report_path": _repo_relative(balance_path) if balance_path else None,
        "pending_order_history_report_path": _repo_relative(history_path) if history_path else None,
        "balance_freshness": balance_freshness,
        "pending_order_history_freshness": history_freshness,
        "broker_env_fingerprint": current_fp,
        "account_state": {
            "cash_positive": cash > 0,
            "net_asset_positive": net_asset > 0,
            "position_count": (
                balance_stage.get("position_count")
                if isinstance(balance_stage, dict)
                else None
            ),
            "reconciliation_status": _stage_status(balance, "reconciliation"),
            "pending_order_count": matched_order_count,
        },
        "note": (
            "No probe order was submitted. This only bootstraps paper-rehearsal "
            "runtime start; strict deploy/prelive evidence remains unchanged."
        ),
    }


def _risk_warning_payloads_from_report(report_path: str) -> list[dict[str, Any]]:
    raw_path = str(report_path).strip()
    if not raw_path:
        return []
    path = Path(raw_path)
    if not path.is_absolute():
        path = ROOT / path
    with path.open("r", encoding="utf-8") as fh:
        report = json.load(fh)

    warnings: list[dict[str, Any]] = []
    message_pool = report.get("message_pool") if isinstance(report, dict) else {}
    for message in (message_pool or {}).get("risk_warning_messages", []) or []:
        if not isinstance(message, dict):
            continue
        payload = message.get("payload")
        if isinstance(payload, dict):
            warnings.append(dict(payload))

    fda = report.get("fda") if isinstance(report, dict) else {}
    if isinstance(fda, dict) and fda.get("approved") is False:
        reason_code = str(fda.get("reason_code") or "NEWS_COMMUNITY_DIVERGENCE")
        warnings.insert(0, {
            "source": "cold_path_fda_report",
            "source_report_path_relative": _repo_relative(path),
            "risk_level": "high",
            "severity": "high",
            "stance": "veto_recommendation",
            "recommended_fda_reason_code": reason_code,
            "reason_code": reason_code,
            "reason": "cold_path_fda_veto",
            "active_report_ids": fda.get("active_report_ids", []),
            "stores_raw_content": False,
        })
    return warnings


def _default_strict_end_date() -> str:
    """Default paper-auto gate date to the audited final dataset SSOT.

    Strict paper-auto validates a frozen candidate bundle. Using "previous
    business day" by default can accidentally ask the gate for a date that has
    not been backfilled into that bundle yet.
    """
    gate_cfg = prelive_gate._final_dataset_gate_cfg()
    expected = prelive_gate._parse_dataset_date(gate_cfg.get("expected_end_date"))
    if expected is not None:
        return expected.strftime("%Y%m%d")
    return prelive_gate._previous_business_day().strftime("%Y%m%d")


def _default_strict_business_days() -> int:
    gate_cfg = prelive_gate._final_dataset_gate_cfg()
    return int(gate_cfg.get("rehearsal_business_days") or 80)


def _paper_rehearsal_gate_from_strict(prelive: dict[str, Any]) -> dict[str, Any]:
    """Allow paper-only runtime evidence while keeping deploy blockers explicit."""
    stages = prelive.get("stages") if isinstance(prelive, dict) else {}
    stages = stages if isinstance(stages, dict) else {}
    required_stages = [
        "01_code_ssot",
        "02_real_data_readiness",
        "03_80_business_day_data",
        "04_lgbm_real_train",
        "06_paper_balance",
        "07_paper_reconciliation",
        "08_paper_probe_order",
        "09_ops_risk",
    ]
    blockers: list[str] = []
    broker_stage_blockers: list[str] = []
    for stage_name in required_stages:
        stage = stages.get(stage_name)
        if not isinstance(stage, dict) or stage.get("status") != "PASS":
            if stage_name in _PAPER_BROKER_STAGE_NAMES:
                broker_stage_blockers.append(stage_name)
            else:
                blockers.append(stage_name)

    read_only_bootstrap = _read_only_broker_bootstrap_gate()
    broker_stage_substituted = bool(
        broker_stage_blockers and read_only_bootstrap.get("status") == "PASS"
    )
    if broker_stage_blockers and not broker_stage_substituted:
        blockers.extend(broker_stage_blockers)

    strict_blockers = [
        str(blocker)
        for blocker in (prelive.get("blockers") or [])
        if str(blocker).strip()
    ]
    allowed_strict_blockers = {"05_backtest_real_candidate"}
    if broker_stage_substituted:
        allowed_strict_blockers.update(_PAPER_BROKER_STAGE_NAMES)
    unexpected_strict_blockers = sorted(
        set(strict_blockers) - allowed_strict_blockers
    )
    blockers.extend(unexpected_strict_blockers)

    stage_01 = stages.get("01_code_ssot") if isinstance(stages, dict) else {}
    live_enabled = bool(
        stage_01.get("live_enabled")
    ) if isinstance(stage_01, dict) else True
    if live_enabled:
        blockers.append("live_enabled_true")

    return {
        "status": "PASS" if not blockers else "BLOCKED",
        "scope": "paper-rehearsal",
        "blockers": blockers,
        "allowed_strict_blockers": sorted(allowed_strict_blockers),
        "strict_prelive_status": prelive.get("status"),
        "strict_prelive_blockers": strict_blockers,
        "read_only_broker_bootstrap": read_only_bootstrap,
        "broker_stage_blockers": broker_stage_blockers,
        "broker_stage_substituted_by_read_only_bootstrap": broker_stage_substituted,
        "required_stage_statuses": {
            stage_name: (
                stages.get(stage_name, {}).get("status")
                if isinstance(stages.get(stage_name), dict)
                else None
            )
            for stage_name in required_stages
        },
    }


def main(argv: list[str] | None = None) -> int:
    cfg = config_load("risk_config.yaml", "paper_auto_trading")
    parser = argparse.ArgumentParser(description="KIS virtual paper auto trading")
    parser.add_argument("--tickers", default="", help="콤마 구분 ticker. 비우면 active universe")
    parser.add_argument("--cycles", type=int, default=int(cfg["default_max_cycles"]))
    parser.add_argument("--interval-sec", type=float, default=float(cfg["default_interval_sec"]))
    parser.add_argument("--max-tickers", type=int, default=int(cfg["max_tickers"]))
    parser.add_argument("--confirm-phrase", default=None)
    parser.add_argument(
        "--end-date",
        default=_default_strict_end_date(),
        help="prelive gate end date YYYYMMDD. Defaults to final_dataset_gate.expected_end_date.",
    )
    parser.add_argument("--business-days", type=int, default=_default_strict_business_days())
    parser.add_argument("--registry-dir", default=None)
    parser.add_argument("--bundle-id", default="", help="strict prelive/paper auto 대상 bundle id")
    parser.add_argument(
        "--report-dir",
        default="",
        help="Optional paper-auto report directory. Use a per-track directory for A/B runs.",
    )
    parser.add_argument("--track-id", default="", help="Optional track label, e.g. MAIN_BASELINE")
    parser.add_argument("--policy-hash", default="", help="Optional fixed policy hash for evidence")
    parser.add_argument(
        "--kis-profile",
        default="",
        help="Optional env profile token, e.g. ACTIVE_SMALL maps ACTIVE_SMALL_KIS_PAPER_* into KIS_PAPER_*.",
    )
    parser.add_argument("--max-orders-per-cycle", type=int, default=None)
    parser.add_argument("--max-order-qty-per-order", type=int, default=None)
    parser.add_argument(
        "--shadow-only",
        action="store_true",
        help="Run Hot Path/order guards but do not submit broker orders.",
    )
    parser.add_argument(
        "--cold-risk-report",
        default="",
        help="community/cold-path report JSON. If FDA veto is present, paper-auto honors it as risk warning.",
    )
    parser.add_argument(
        "--prelive-scope",
        choices=["strict", "paper-rehearsal"],
        default="strict",
        help="strict=C12/C14 prelive gate, paper-rehearsal=KIS virtual evidence gate",
    )
    parser.add_argument("--no-write-report", action="store_true")
    args = parser.parse_args(argv)

    if args.registry_dir:
        os.environ["ELEPHANT_LGBM_REGISTRY_DIR"] = str(args.registry_dir)

    requested_bundle_id = str(args.bundle_id).strip()
    if not requested_bundle_id:
        out = {
            "status": "BLOCKED",
            "action": "paper_auto_trade",
            "reason": "bundle_id_required_for_strict_paper_auto_trade",
            "prelive_scope": args.prelive_scope,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    profile_guard = _apply_kis_profile(str(args.kis_profile))
    if profile_guard.get("status") != "PASS":
        out = {
            "status": "BLOCKED",
            "action": "paper_auto_trade",
            "reason": "kis_profile_not_ready",
            "profile_guard": profile_guard,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    tickers = _parse_tickers(str(args.tickers), int(args.max_tickers))
    prelive = None
    strict_prelive = None
    if safe_bool(cfg.get("require_prelive_pass", True), default=True):
        strict_prelive = prelive_gate.build_report(
            end_date=str(args.end_date),
            business_days=int(args.business_days),
            max_tickers=int(args.max_tickers),
            bundle_id=requested_bundle_id,
        )
        prelive = (
            _paper_rehearsal_gate_from_strict(strict_prelive)
            if args.prelive_scope == "paper-rehearsal"
            else strict_prelive
        )
        if prelive.get("status") != "PASS":
            out = {
                "status": "BLOCKED",
                "action": "paper_auto_trade",
                "reason": "prelive_gate_not_pass",
                "prelive_scope": args.prelive_scope,
                "prelive_gate": prelive,
            }
            if strict_prelive is not None and strict_prelive is not prelive:
                out["strict_prelive_gate"] = strict_prelive
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 1

    risk_warnings = _risk_warning_payloads_from_report(args.cold_risk_report)
    trader = PaperAutoTrader(
        required_bundle_id=requested_bundle_id,
        report_dir=str(args.report_dir).strip() or None,
        track_id=str(args.track_id).strip() or _profile_token(str(args.kis_profile)),
        policy_hash=str(args.policy_hash).strip(),
        max_orders_per_cycle=args.max_orders_per_cycle,
        max_order_qty_per_order=args.max_order_qty_per_order,
        submit_orders=not bool(args.shadow_only),
    )
    report = trader.run(
        tickers=tickers,
        cycles=int(args.cycles),
        interval_sec=float(args.interval_sec),
        confirm_phrase=args.confirm_phrase,
        risk_warnings=risk_warnings,
        write_report=not bool(args.no_write_report),
    )
    if args.cold_risk_report:
        report["cold_risk_report_path"] = args.cold_risk_report
        report["cold_risk_warning_count"] = len(risk_warnings)
    if prelive is not None:
        report["prelive_gate_status"] = prelive.get("status")
        report["prelive_gate_blockers"] = prelive.get("blockers", [])
        report["prelive_scope"] = args.prelive_scope
    if strict_prelive is not None and strict_prelive is not prelive:
        report["strict_prelive_gate_status"] = strict_prelive.get("status")
        report["strict_prelive_gate_blockers"] = strict_prelive.get("blockers", [])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

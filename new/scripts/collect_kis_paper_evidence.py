#!/usr/bin/env python
"""Collect KIS virtual paper evidence in one Python process.

This avoids KIS token's one-token-per-minute throttle by sharing the in-memory
AuthManager token across balance, probe order, and paper-auto rehearsal.
"""
from __future__ import annotations

import argparse
import json
import sys
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import paper_auto_service_rehearsal  # noqa: E402
from src.connectors.kis_rest import KISRestClient  # noqa: E402
from src.execution.paper_trading import PaperTradingRunner  # noqa: E402
from src.utils.auth import AuthenticationError  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_DEFAULT_BUNDLE_ID = "BUNDLE-20260521-POSTCLOSE"


def _default_registry_dir(bundle_id: str) -> str:
    return f"artifacts/lgbm_paper_candidate/{bundle_id}"


def _auth_retry_sec() -> int:
    cfg = config_load("risk_config.yaml", "auth_defaults") or {}
    return int(cfg.get("token_rate_limit_retry_sec", 65))


def _run_with_token_retry(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return fn()
    except AuthenticationError as e:
        if "EGW00133" not in str(e):
            raise
        import time

        wait_sec = _auth_retry_sec()
        time.sleep(wait_sec)
        return fn()


def _auto_price(ticker: str) -> float:
    quote = KISRestClient().inquire_price(ticker)
    return float(quote["current_price"])


def _load_system_positions(
    path: str | None,
    *,
    assume_empty: bool = False,
) -> list[dict[str, Any]] | None:
    if assume_empty and path:
        raise ValueError("--system-positions-json and --assume-empty-system-positions are mutually exclusive")
    if assume_empty:
        return []
    if not path:
        return None
    with Path(path).open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and isinstance(data.get("positions"), list):
        return list(data["positions"])
    if isinstance(data, list):
        return data
    raise ValueError("system positions JSON must be a list or {'positions': [...]}")


def _write_service_rehearsal_report(report: dict[str, Any]) -> dict[str, Any]:
    report_path = paper_auto_service_rehearsal._write_report(report)
    report["report_path"] = str(report_path)
    try:
        report["report_path_relative"] = str(report_path.relative_to(ROOT))
    except ValueError:
        report["report_path_relative"] = str(report_path)
    return report


def _blocked_service_rehearsal_report(
    args: argparse.Namespace,
    error: Exception,
) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "action": "paper_auto_service_rehearsal",
        "generated_at": datetime.now(_KST).isoformat(),
        "bundle_id": args.bundle_id,
        "evidence_level": "external_kis_virtual",
        "external_kis_api": True,
        "real_hot_runner": bool(args.use_real_hot_runner),
        "live_trading_enabled": False,
        "cold_risk_report_path": str(args.cold_risk_report or ""),
        "cold_risk_warning_count": 0,
        "blockers": ["paper_auto_service_rehearsal_exception"],
        "stage_statuses": {"paper_auto_cycle": "BLOCKED"},
        "broker_evidence": {},
        "stages": {
            "paper_auto_cycle": {
                "status": "BLOCKED",
                "reason": "paper_auto_service_rehearsal_exception",
                "exception_type": type(error).__name__,
                "error": str(error),
                "fail_closed": True,
            },
        },
    }


def collect(args: argparse.Namespace) -> dict[str, Any]:
    runner = PaperTradingRunner()
    stages: dict[str, Any] = {}
    registry_dir = str(getattr(args, "registry_dir", "") or "").strip()
    if not registry_dir:
        registry_dir = _default_registry_dir(str(args.bundle_id))

    system_positions = _load_system_positions(
        args.system_positions_json,
        assume_empty=bool(args.assume_empty_system_positions),
    )
    stages["balance_reconciliation"] = _run_with_token_retry(
        lambda: runner.run_balance_reconciliation(
            system_positions=system_positions,
            write_report=not bool(args.no_write_report),
        )
    )

    price = args.price
    if price is None and bool(args.auto_price) and str(args.order_type) != "01":
        price = _run_with_token_retry(lambda: {"price": _auto_price(args.ticker)})["price"]

    stages["probe_order"] = _run_with_token_retry(
        lambda: runner.submit_probe_order(
            ticker=args.ticker,
            side=args.side,
            qty=int(args.qty),
            price=price,
            order_type=str(args.order_type),
            confirm_phrase=args.probe_confirm_phrase,
            write_report=not bool(args.no_write_report),
        )
    )

    rehearsal_args = Namespace(
        internal_fake_kis=False,
        bundle_id=args.bundle_id,
        tickers=args.tickers,
        cycles=int(args.cycles),
        interval_sec=float(args.interval_sec),
        registry_dir=registry_dir,
        confirm_phrase=args.auto_confirm_phrase,
        cold_risk_report=args.cold_risk_report,
        no_write_report=bool(args.no_write_report),
        use_real_hot_runner=bool(args.use_real_hot_runner),
    )
    try:
        stages["paper_auto_service_rehearsal"] = _run_with_token_retry(
            lambda: paper_auto_service_rehearsal.build_report(rehearsal_args)
        )
    except Exception as e:
        stages["paper_auto_service_rehearsal"] = _blocked_service_rehearsal_report(
            args,
            e,
        )
    if not bool(args.no_write_report):
        stages["paper_auto_service_rehearsal"] = _write_service_rehearsal_report(
            stages["paper_auto_service_rehearsal"]
        )

    blockers = [
        name
        for name, stage in stages.items()
        if isinstance(stage, dict) and stage.get("status") != "PASS"
    ]
    return {
        "status": "PASS" if not blockers else "BLOCKED",
        "generated_at": datetime.now(_KST).isoformat(),
        "action": "collect_kis_paper_evidence",
        "bundle_id": args.bundle_id,
        "registry_dir": registry_dir,
        "blockers": blockers,
        "stage_statuses": {
            name: stage.get("status") if isinstance(stage, dict) else "UNKNOWN"
            for name, stage in stages.items()
        },
        "stages": stages,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect KIS virtual paper evidence")
    parser.add_argument("--bundle-id", default=_DEFAULT_BUNDLE_ID)
    parser.add_argument(
        "--registry-dir",
        default="",
        help="Paper candidate registry dir. Defaults to artifacts/lgbm_paper_candidate/{bundle_id}.",
    )
    parser.add_argument("--ticker", default="005930")
    parser.add_argument("--tickers", default="005930")
    parser.add_argument("--side", choices=["buy", "sell"], default="buy")
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--auto-price", action="store_true")
    parser.add_argument("--order-type", default="00")
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--interval-sec", type=float, default=0.0)
    parser.add_argument("--system-positions-json", default=None)
    parser.add_argument("--assume-empty-system-positions", action="store_true")
    parser.add_argument("--probe-confirm-phrase", default="PAPER_ORDER_OK")
    parser.add_argument("--auto-confirm-phrase", default="PAPER_AUTO_OK")
    parser.add_argument(
        "--cold-risk-report",
        default="",
        help="community/cold-path report JSON to forward into paper-auto FDA.",
    )
    parser.add_argument("--use-real-hot-runner", action="store_true")
    parser.add_argument("--no-write-report", action="store_true")
    args = parser.parse_args(argv)

    report = collect(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

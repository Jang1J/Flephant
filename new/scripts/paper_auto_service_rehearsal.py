#!/usr/bin/env python
"""Paper-auto 1-cycle service rehearsal runner."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import paper_auto_preflight  # noqa: E402
from src.execution.paper_auto_trading import PaperAutoTrader  # noqa: E402
from src.ops.paper_order_path_evidence import summarize_paper_order_path_evidence  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.safe_cast import safe_float  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")


class _FakeKIS:
    mode = "virtual"

    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []

    def get_balance(self) -> dict[str, Any]:
        return {
            "balance": {"cash": 1_000_000.0, "net_asset": 1_000_000.0},
            "positions": [],
            "_mode": "virtual",
        }

    def inquire_minute_bar(self, ticker: str, n_bars: int) -> list[dict[str, Any]]:
        ticker_padded = pad_ticker(str(ticker))
        start = datetime(2026, 5, 12, 9, 0, 0, tzinfo=_KST)
        return [
            {
                "ticker": ticker_padded,
                "open": 70000 + i,
                "high": 70100 + i,
                "low": 69900 + i,
                "close": 70000 + i,
                "volume": 1000 + i,
                "ts_close": (start + timedelta(minutes=i)).isoformat(),
            }
            for i in range(n_bars)
        ]

    def submit_order(
        self,
        ticker: str,
        side: str,
        qty: int,
        price: float,
        order_type: str = "00",
    ) -> dict[str, Any]:
        order_id = f"PAPER-AUTO-FAKE-{len(self.orders) + 1}"
        order = {
            "ticker": pad_ticker(str(ticker)),
            "side": str(side).lower(),
            "qty": int(qty),
            "price": float(price),
            "order_type": str(order_type),
            "order_id": order_id,
        }
        self.orders.append(order)
        return {"status": "submitted", **order}

    def get_order_history(
        self,
        ticker: str = "",
        order_id: str = "",
        side: str = "all",
        execution_filter: str = "all",
    ) -> dict[str, Any]:
        ticker_padded = pad_ticker(str(ticker)) if str(ticker).strip() else ""
        side_norm = str(side or "all").lower()
        orders = []
        for order in self.orders:
            if order_id and order["order_id"] != order_id:
                continue
            if ticker_padded and order["ticker"] != ticker_padded:
                continue
            if side_norm != "all" and order["side"] != side_norm:
                continue
            orders.append({**order, "status": "submitted"})
        return {"orders": orders, "summary": {}, "_mode": "virtual"}


class _FakeHotRunner:
    def __init__(self, bundle_id: str | None = None) -> None:
        self.state = SimpleNamespace(value="BOOTSTRAP")
        self._quant = SimpleNamespace(
            has_model=True,
            model_metadata={
                "version": "paper_active",
                "bundle_id": str(bundle_id or "PAPER-REHEARSAL"),
            },
        )

    def start(self) -> None:
        self.state = SimpleNamespace(value="HOT_RUNNING")

    def run_once(self, **kwargs: Any) -> dict[str, Any]:
        latest_prices = kwargs.get("latest_prices", {})
        ticker = pad_ticker(str((kwargs.get("tickers") or ["005930"])[0]))
        price = safe_float(latest_prices.get(ticker), default=70000.0, min_value=1.0)
        return {
            "quant_output": {
                "mode": "active",
                "scores": {ticker: 1.0},
                "n_tickers": 1,
            },
            "final_decision": {
                "decision_id": "FDA-PAPER-AUTO-REHEARSAL",
                "approved": True,
                "reason_code": "NORMAL_APPROVE",
                "order_deltas": [{
                    "ticker": ticker,
                    "side": "buy",
                    "qty": 1,
                    "price": price,
                    "order_type": "00",
                    "reason": "rebalance",
                }],
            },
            "latency_ms": 1.0,
        }


def _report_dir() -> Path:
    cfg = config_load("risk_config.yaml", "paper_auto_trading")
    path = Path(str(cfg["report_dir"]))
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_report(report: dict[str, Any]) -> Path:
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    path = _report_dir() / f"paper_auto_service_rehearsal_{ts}.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path


def _repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


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


def _broker_evidence_from_preflight(preflight: dict[str, Any]) -> dict[str, Any]:
    """Extract the three KIS virtual evidence gates expected by service readiness."""
    stages = preflight.get("stages") if isinstance(preflight, dict) else {}
    evidence = (stages or {}).get("paper_evidence") if isinstance(stages, dict) else {}
    if not isinstance(evidence, dict):
        evidence = {}

    balance = evidence.get("balance_reconciliation")
    probe = evidence.get("probe_order")
    history = evidence.get("order_history")
    order_path = evidence.get("paper_order_path")
    return {
        "balance_reconciliation": balance if isinstance(balance, dict) else {"status": "MISSING"},
        "probe_order": probe if isinstance(probe, dict) else {"status": "MISSING"},
        "order_history_requery": history if isinstance(history, dict) else {"status": "MISSING"},
        "paper_order_path": order_path if isinstance(order_path, dict) else {"status": "MISSING"},
    }


def _stage_statuses(stages: dict[str, dict[str, Any]], preflight: dict[str, Any]) -> dict[str, str]:
    statuses = {name: str(stage.get("status")) for name, stage in stages.items()}
    broker_evidence = _broker_evidence_from_preflight(preflight)
    statuses.update({
        name: str(stage.get("status", "MISSING"))
        for name, stage in broker_evidence.items()
    })
    return statuses


def _exception_stage(reason: str, error: Exception) -> dict[str, Any]:
    return {
        "status": "FAIL",
        "reason": reason,
        "exception_type": type(error).__name__,
        "error": str(error),
        "fail_closed": True,
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    if args.registry_dir:
        os.environ["ELEPHANT_LGBM_REGISTRY_DIR"] = str(args.registry_dir)

    requested_bundle_id = (
        str(getattr(args, "bundle_id", "") or "").strip() or None
    )
    preflight = paper_auto_preflight.build_report(registry_dir=args.registry_dir)
    run_cycle = bool(args.internal_fake_kis) or preflight.get("status") == "PASS"
    stages: dict[str, Any] = {"preflight": preflight}
    use_real_hot_runner = bool(getattr(args, "use_real_hot_runner", False))
    risk_warnings = _risk_warning_payloads_from_report(
        str(getattr(args, "cold_risk_report", "") or "")
    )

    if run_cycle:
        kis_client = _FakeKIS() if args.internal_fake_kis else None
        hot_runner = (
            None
            if use_real_hot_runner
            else (
                _FakeHotRunner(bundle_id=requested_bundle_id)
                if args.internal_fake_kis
                else None
            )
        )
        trader_kwargs: dict[str, Any] = {}
        if args.internal_fake_kis:
            trader_kwargs["now_fn"] = lambda: datetime(2026, 5, 12, 10, 0, 0, tzinfo=_KST)
        if requested_bundle_id:
            trader_kwargs["required_bundle_id"] = requested_bundle_id
        trader = PaperAutoTrader(
            kis_client=kis_client,
            hot_runner=hot_runner,
            **trader_kwargs,
        )
        try:
            stages["paper_auto_cycle"] = trader.run(
                tickers=[pad_ticker(t.strip()) for t in str(args.tickers).split(",") if t.strip()],
                cycles=int(args.cycles),
                interval_sec=float(args.interval_sec),
                confirm_phrase=args.confirm_phrase,
                risk_warnings=risk_warnings,
                write_report=not bool(args.no_write_report),
            )
        except Exception as e:
            stages["paper_auto_cycle"] = _exception_stage(
                "paper_auto_cycle_exception",
                e,
            )
    else:
        stages["paper_auto_cycle"] = {
            "status": "SKIP",
            "reason": "preflight_blocked",
        }

    cycle_status = str(stages["paper_auto_cycle"].get("status"))
    status = "PASS" if cycle_status == "PASS" else ("BLOCKED" if not run_cycle else "FAIL")
    broker_evidence = _broker_evidence_from_preflight(preflight)
    active_model = (
        (stages["paper_auto_cycle"].get("stages") or {}).get("active_model_guard")
        if isinstance(stages["paper_auto_cycle"], dict)
        else {}
    )
    bundle_id = (
        active_model.get("bundle_id")
        if isinstance(active_model, dict)
        else None
    )
    report_bundle_id = requested_bundle_id or bundle_id
    paper_cycle = stages.get("paper_auto_cycle") if isinstance(stages, dict) else {}
    cycle_order_path = (
        summarize_paper_order_path_evidence(
            paper_cycle,
            root=ROOT,
            bundle_id=report_bundle_id,
        )
        if isinstance(paper_cycle, dict)
        else {"status": "MISSING"}
    )
    if cycle_order_path.get("status") == "PASS":
        broker_evidence["paper_order_path"] = cycle_order_path
        broker_evidence["probe_order"] = {
            "status": "PASS",
            "deprecated": True,
            "replaced_by": "paper_order_path",
            "evidence_type": cycle_order_path.get("evidence_type"),
        }
        broker_evidence["order_history_requery"] = {
            "status": "PASS",
            "evidence_type": cycle_order_path.get("evidence_type"),
            "matched_order_count": cycle_order_path.get("matched_order_count", 0),
        }
    evidence_level = "external_kis_virtual"
    if args.internal_fake_kis and use_real_hot_runner:
        evidence_level = "internal_fake_kis_real_hot_runner"
    elif args.internal_fake_kis:
        evidence_level = "internal_fake_kis"
    stage_statuses = _stage_statuses(stages, preflight)
    if cycle_order_path.get("status") == "PASS":
        stage_statuses["paper_order_path"] = "PASS"
        stage_statuses["probe_order"] = "PASS"
        stage_statuses["order_history_requery"] = "PASS"
    return {
        "status": status,
        "action": "paper_auto_service_rehearsal",
        "generated_at": datetime.now(_KST).isoformat(),
        "bundle_id": report_bundle_id,
        "model_bundle_id": bundle_id,
        "evidence_level": evidence_level,
        "external_kis_api": not bool(args.internal_fake_kis),
        "real_hot_runner": use_real_hot_runner,
        "live_trading_enabled": False,
        "cold_risk_report_path": str(getattr(args, "cold_risk_report", "") or ""),
        "cold_risk_warning_count": len(risk_warnings),
        "stage_statuses": stage_statuses,
        "broker_evidence": broker_evidence,
        "paper_order_path_evidence": cycle_order_path,
        "stages": stages,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paper-auto service rehearsal")
    parser.add_argument("--internal-fake-kis", action="store_true")
    parser.add_argument("--bundle-id", default=None)
    parser.add_argument("--tickers", default="005930")
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--interval-sec", type=float, default=0.0)
    parser.add_argument("--registry-dir", default=None)
    parser.add_argument("--confirm-phrase", default=None)
    parser.add_argument(
        "--cold-risk-report",
        default="",
        help="community/cold-path report JSON to forward risk warnings into paper-auto FDA.",
    )
    parser.add_argument("--no-write-report", action="store_true")
    parser.add_argument(
        "--use-real-hot-runner",
        action="store_true",
        help=(
            "With --internal-fake-kis, use the real HotRunner/QuantAgent path "
            "instead of the deterministic fake HotRunner."
        ),
    )
    args = parser.parse_args(argv)

    report = build_report(args)
    if not bool(args.no_write_report):
        report_path = _write_report(report)
        report["report_path"] = str(report_path)
        try:
            report["report_path_relative"] = str(report_path.relative_to(ROOT))
        except ValueError:
            report["report_path_relative"] = str(report_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

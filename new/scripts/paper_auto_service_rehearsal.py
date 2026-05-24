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
    def __init__(self) -> None:
        self.state = SimpleNamespace(value="BOOTSTRAP")
        self._quant = SimpleNamespace(
            has_model=True,
            model_metadata={"version": "paper_active", "bundle_id": "PAPER-REHEARSAL"},
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
                "scores": {
                    ticker: 0.2,
                    "000660": 0.1 if ticker != "000660" else 0.3,
                },
                "n_tickers": 2,
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


def _broker_evidence_from_preflight(preflight: dict[str, Any]) -> dict[str, Any]:
    """Extract the three KIS virtual evidence gates expected by service readiness."""
    stages = preflight.get("stages") if isinstance(preflight, dict) else {}
    evidence = (stages or {}).get("paper_evidence") if isinstance(stages, dict) else {}
    if not isinstance(evidence, dict):
        evidence = {}

    balance = evidence.get("balance_reconciliation")
    probe = evidence.get("probe_order")
    history = evidence.get("order_history")
    return {
        "balance_reconciliation": balance if isinstance(balance, dict) else {"status": "MISSING"},
        "probe_order": probe if isinstance(probe, dict) else {"status": "MISSING"},
        "order_history_requery": history if isinstance(history, dict) else {"status": "MISSING"},
    }


def _stage_statuses(stages: dict[str, dict[str, Any]], preflight: dict[str, Any]) -> dict[str, str]:
    statuses = {name: str(stage.get("status")) for name, stage in stages.items()}
    broker_evidence = _broker_evidence_from_preflight(preflight)
    statuses.update({
        name: str(stage.get("status", "MISSING"))
        for name, stage in broker_evidence.items()
    })
    return statuses


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    if args.registry_dir:
        os.environ["ELEPHANT_LGBM_REGISTRY_DIR"] = str(args.registry_dir)

    preflight = paper_auto_preflight.build_report(registry_dir=args.registry_dir)
    run_cycle = bool(args.internal_fake_kis) or preflight.get("status") == "PASS"
    stages: dict[str, Any] = {"preflight": preflight}
    use_real_hot_runner = bool(getattr(args, "use_real_hot_runner", False))

    if run_cycle:
        kis_client = _FakeKIS() if args.internal_fake_kis else None
        hot_runner = (
            None
            if use_real_hot_runner
            else (_FakeHotRunner() if args.internal_fake_kis else None)
        )
        trader_kwargs: dict[str, Any] = {}
        if args.internal_fake_kis:
            trader_kwargs["now_fn"] = lambda: datetime(2026, 5, 12, 10, 0, 0, tzinfo=_KST)
        trader = PaperAutoTrader(
            kis_client=kis_client,
            hot_runner=hot_runner,
            **trader_kwargs,
        )
        stages["paper_auto_cycle"] = trader.run(
            tickers=[pad_ticker(t.strip()) for t in str(args.tickers).split(",") if t.strip()],
            cycles=int(args.cycles),
            interval_sec=float(args.interval_sec),
            confirm_phrase=args.confirm_phrase,
            write_report=not bool(args.no_write_report),
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
    evidence_level = "external_kis_virtual"
    if args.internal_fake_kis and use_real_hot_runner:
        evidence_level = "internal_fake_kis_real_hot_runner"
    elif args.internal_fake_kis:
        evidence_level = "internal_fake_kis"
    return {
        "status": status,
        "action": "paper_auto_service_rehearsal",
        "generated_at": datetime.now(_KST).isoformat(),
        "bundle_id": bundle_id,
        "evidence_level": evidence_level,
        "external_kis_api": not bool(args.internal_fake_kis),
        "real_hot_runner": use_real_hot_runner,
        "live_trading_enabled": False,
        "stage_statuses": _stage_statuses(stages, preflight),
        "broker_evidence": broker_evidence,
        "stages": stages,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paper-auto service rehearsal")
    parser.add_argument("--internal-fake-kis", action="store_true")
    parser.add_argument("--tickers", default="005930")
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--interval-sec", type=float, default=0.0)
    parser.add_argument("--registry-dir", default=None)
    parser.add_argument("--confirm-phrase", default=None)
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

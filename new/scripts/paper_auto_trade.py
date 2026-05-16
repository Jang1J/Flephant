#!/usr/bin/env python
"""KIS virtual 모의투자 자동매매 CLI.

프리라이브 게이트 PASS 이후에만 Hot Path → ExecutionGateway(paper)를 연결한다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import prelive_gate  # noqa: E402
from src.execution.paper_auto_trading import PaperAutoTrader  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.safe_cast import safe_bool  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402


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
        default=prelive_gate._previous_business_day().strftime("%Y%m%d"),
        help="prelive gate end date YYYYMMDD",
    )
    parser.add_argument("--business-days", type=int, default=80)
    parser.add_argument("--registry-dir", default=None)
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

    if args.prelive_scope == "paper-rehearsal":
        out = {
            "status": "BLOCKED",
            "action": "paper_auto_trade",
            "reason": "paper_rehearsal_scope_not_allowed_for_auto_trade",
            "prelive_scope": args.prelive_scope,
            "required_prelive_scope": "strict",
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    tickers = _parse_tickers(str(args.tickers), int(args.max_tickers))
    prelive = None
    if safe_bool(cfg.get("require_prelive_pass", True), default=True):
        prelive = prelive_gate.build_report(
            end_date=str(args.end_date),
            business_days=int(args.business_days),
            max_tickers=int(args.max_tickers),
        )
        if prelive.get("status") != "PASS":
            out = {
                "status": "BLOCKED",
                "action": "paper_auto_trade",
                "reason": "prelive_gate_not_pass",
                "prelive_scope": args.prelive_scope,
                "prelive_gate": prelive,
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 1

    trader = PaperAutoTrader()
    report = trader.run(
        tickers=tickers,
        cycles=int(args.cycles),
        interval_sec=float(args.interval_sec),
        confirm_phrase=args.confirm_phrase,
        write_report=not bool(args.no_write_report),
    )
    if prelive is not None:
        report["prelive_gate_status"] = prelive.get("status")
        report["prelive_gate_blockers"] = prelive.get("blockers", [])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

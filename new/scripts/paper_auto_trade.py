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
from typing import Any

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

    tickers = _parse_tickers(str(args.tickers), int(args.max_tickers))
    prelive = None
    if safe_bool(cfg.get("require_prelive_pass", True), default=True):
        prelive = prelive_gate.build_report(
            end_date=str(args.end_date),
            business_days=int(args.business_days),
            max_tickers=int(args.max_tickers),
            bundle_id=requested_bundle_id,
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

    risk_warnings = _risk_warning_payloads_from_report(args.cold_risk_report)
    trader = PaperAutoTrader(required_bundle_id=requested_bundle_id)
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
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

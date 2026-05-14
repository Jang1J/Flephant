#!/usr/bin/env python
"""KIS virtual paper service rehearsal.

이 스크립트는 `.env`를 읽지 않는다. 현재 process environment만 확인한 뒤
balance/reconciliation, 선택적 probe order, broker order id 기반 order-history를
하나의 evidence report로 묶는다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import print_env_readiness  # noqa: E402
from src.connectors.kis_rest import KISRestClient  # noqa: E402
from src.execution.paper_trading import PaperTradingRunner  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")


def _report_dir() -> Path:
    cfg = config_load("risk_config.yaml", "paper_trading")
    path = Path(str(cfg["report_dir"]))
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _extract_broker_order_id(probe_report: dict[str, Any]) -> str | None:
    execution = (
        probe_report.get("stages", {})
        .get("execution", {})
        .get("result", {})
        .get("execution_report", {})
    )
    for fill in execution.get("fills", []):
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
            return str(raw)
    return None


def _load_system_positions(
    path: str | None,
    *,
    assume_empty: bool = False,
) -> list[dict[str, Any]] | None:
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


def _auto_price(ticker: str) -> float:
    quote = KISRestClient().inquire_price(ticker)
    return float(quote["current_price"])


def _stage_status(stage: dict[str, Any] | None) -> str:
    if not isinstance(stage, dict):
        return "SKIP"
    return str(stage.get("status", "SKIP"))


def _overall(stages: dict[str, Any]) -> str:
    statuses = [_stage_status(stage) for stage in stages.values()]
    if any(status == "FAIL" for status in statuses):
        return "FAIL"
    if any(status == "SKIP" for status in statuses):
        return "PARTIAL"
    return "PASS"


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    runner = PaperTradingRunner()
    stages: dict[str, Any] = {}
    stages["env_readiness"] = print_env_readiness.build_report()
    system_positions = _load_system_positions(
        args.system_positions_json,
        assume_empty=bool(args.assume_empty_system_positions),
    )
    stages["balance_reconciliation"] = runner.run_balance_reconciliation(
        system_positions=system_positions,
        write_report=True
    )

    broker_order_id: str | None = None
    probe_price = args.price
    price_source = "arg"
    if (
        bool(args.include_probe)
        and probe_price is None
        and bool(args.auto_price)
        and str(args.order_type) != "01"
    ):
        probe_price = _auto_price(args.ticker)
        price_source = "kis_current_price"
    if bool(args.include_probe):
        stages["probe_order"] = runner.submit_probe_order(
            ticker=args.ticker,
            side=args.side,
            qty=int(args.qty),
            price=probe_price,
            order_type=args.order_type,
            confirm_phrase=args.confirm_phrase,
            write_report=True,
        )
        if stages["probe_order"].get("status") == "PASS":
            broker_order_id = _extract_broker_order_id(stages["probe_order"])
    else:
        stages["probe_order"] = {
            "status": "SKIP",
            "reason": "include_probe_false",
        }

    if broker_order_id:
        stages["order_history_requery"] = runner.run_order_history(
            ticker=args.ticker,
            side=args.side,
            order_id=broker_order_id,
            execution_filter=args.execution_filter,
            write_report=True,
        )
    else:
        stages["order_history_requery"] = {
            "status": "SKIP",
            "reason": "broker_order_id_unavailable",
        }

    return {
        "status": _overall(stages),
        "action": "paper_service_rehearsal",
        "generated_at": datetime.now(_KST).isoformat(),
        "external_kis_api": True,
        "live_trading_enabled": False,
        "params": {
            "include_probe": bool(args.include_probe),
            "ticker": pad_ticker(str(args.ticker)),
            "side": str(args.side).lower(),
            "qty": int(args.qty),
            "price": float(probe_price) if probe_price is not None else None,
            "price_source": price_source,
            "order_type": str(args.order_type),
            "execution_filter": str(args.execution_filter),
            "system_positions_source": (
                "assume_empty"
                if bool(args.assume_empty_system_positions)
                else "json"
                if args.system_positions_json
                else "not_provided"
            ),
        },
        "broker_order_id": broker_order_id,
        "stage_statuses": {
            name: _stage_status(stage) for name, stage in stages.items()
        },
        "stages": stages,
    }


def _write_report(report: dict[str, Any]) -> Path:
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    path = _report_dir() / f"paper_service_rehearsal_{ts}.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KIS virtual paper service rehearsal")
    parser.add_argument("--include-probe", action="store_true")
    parser.add_argument("--system-positions-json", default=None)
    parser.add_argument(
        "--assume-empty-system-positions",
        action="store_true",
        help="신규 paper 계좌처럼 시스템 보유분도 0이라고 명시해 reconciliation PASS를 확인",
    )
    parser.add_argument("--ticker", default="005930")
    parser.add_argument("--side", choices=["buy", "sell"], default="buy")
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument(
        "--auto-price",
        action="store_true",
        help="probe 지정가 가격이 없으면 KIS 현재가를 read-only 조회해 사용",
    )
    parser.add_argument("--order-type", default="00")
    parser.add_argument("--execution-filter", choices=["all", "filled", "unfilled"], default="all")
    parser.add_argument("--confirm-phrase", default=None)
    parser.add_argument("--no-write-report", action="store_true")
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

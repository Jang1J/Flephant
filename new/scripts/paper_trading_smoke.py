#!/usr/bin/env python
"""S4-6 paper trading smoke CLI.

기본은 read-only balance/reconciliation이다. 모의 주문 제출은
``--action submit-probe``와 설정된 확인 문구를 함께 넘겼을 때만 실행한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.connectors.kis_rest import KISRestClient  # noqa: E402
from src.execution.paper_trading import PaperTradingRunner  # noqa: E402


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S4-6 paper trading smoke")
    parser.add_argument(
        "--action",
        choices=["balance", "submit-probe", "order-history"],
        default="balance",
        help="balance/order-history는 read-only, submit-probe는 확인 문구 필요",
    )
    parser.add_argument("--system-positions-json", default=None)
    parser.add_argument(
        "--assume-empty-system-positions",
        action="store_true",
        help="신규 paper 계좌처럼 시스템 보유분도 0이라고 명시해 reconciliation PASS를 확인",
    )
    parser.add_argument("--ticker", default="005930")
    parser.add_argument("--side", choices=["all", "buy", "sell"], default="buy")
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument(
        "--auto-price",
        action="store_true",
        help="submit-probe 지정가 가격이 없으면 KIS 현재가를 read-only 조회해 사용",
    )
    parser.add_argument("--order-type", default="00")
    parser.add_argument("--order-id", default=None)
    parser.add_argument("--execution-filter", choices=["all", "filled", "unfilled"], default="all")
    parser.add_argument(
        "--confirm-phrase",
        default=None,
        help="risk_config.yaml paper_trading.confirm_order_phrase 와 일치해야 주문 제출",
    )
    parser.add_argument("--no-write-report", action="store_true")
    args = parser.parse_args(argv)

    runner = PaperTradingRunner()
    if args.action == "balance":
        report = runner.run_balance_reconciliation(
            system_positions=_load_system_positions(
                args.system_positions_json,
                assume_empty=bool(args.assume_empty_system_positions),
            ),
            write_report=not bool(args.no_write_report),
        )
    elif args.action == "submit-probe":
        price = args.price
        if price is None and bool(args.auto_price) and str(args.order_type) != "01":
            price = _auto_price(args.ticker)
        report = runner.submit_probe_order(
            ticker=args.ticker,
            side=args.side,
            qty=args.qty,
            price=price,
            order_type=args.order_type,
            confirm_phrase=args.confirm_phrase,
            write_report=not bool(args.no_write_report),
        )
    else:
        report = runner.run_order_history(
            ticker=args.ticker,
            side=args.side,
            order_id=args.order_id,
            execution_filter=args.execution_filter,
            write_report=not bool(args.no_write_report),
        )

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

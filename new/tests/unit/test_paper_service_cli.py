from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import paper_service_rehearsal  # noqa: E402
import paper_trading_smoke  # noqa: E402


def test_paper_trading_smoke_can_assume_empty_system_positions() -> None:
    assert paper_trading_smoke._load_system_positions(  # noqa: SLF001
        None,
        assume_empty=True,
    ) == []


def test_paper_service_rehearsal_auto_price_and_empty_reconciliation(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeRunner:
        def run_balance_reconciliation(self, system_positions=None, write_report=True):
            calls["system_positions"] = system_positions
            calls["balance_write_report"] = write_report
            return {"status": "PASS"}

        def submit_probe_order(
            self,
            ticker,
            side,
            qty,
            price,
            order_type,
            confirm_phrase,
            write_report=True,
        ):
            calls["probe_price"] = price
            calls["probe_write_report"] = write_report
            return {
                "status": "PASS",
                "stages": {
                    "execution": {
                        "result": {
                            "execution_report": {
                                "fills": [{"broker_order_id": "OD-1"}],
                            },
                        },
                    },
                },
            }

        def run_order_history(
            self,
            ticker,
            side,
            order_id,
            execution_filter,
            write_report=True,
        ):
            calls["order_id"] = order_id
            return {"status": "PASS"}

    monkeypatch.setattr(paper_service_rehearsal, "PaperTradingRunner", FakeRunner)
    monkeypatch.setattr(
        paper_service_rehearsal.print_env_readiness,
        "build_report",
        lambda: {"status": "PASS"},
    )
    monkeypatch.setattr(paper_service_rehearsal, "_auto_price", lambda ticker: 71000.0)

    report = paper_service_rehearsal.build_report(
        argparse.Namespace(
            include_probe=True,
            system_positions_json=None,
            assume_empty_system_positions=True,
            ticker="005930",
            side="buy",
            qty=1,
            price=None,
            auto_price=True,
            order_type="00",
            execution_filter="all",
            confirm_phrase="PAPER_ORDER_OK",
        )
    )

    assert report["status"] == "PASS"
    assert report["params"]["price"] == 71000.0
    assert report["params"]["price_source"] == "kis_current_price"
    assert report["params"]["system_positions_source"] == "assume_empty"
    assert calls["system_positions"] == []
    assert calls["probe_price"] == 71000.0
    assert calls["order_id"] == "OD-1"

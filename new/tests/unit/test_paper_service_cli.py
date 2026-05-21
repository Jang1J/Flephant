from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import paper_service_rehearsal  # noqa: E402
import paper_trading_smoke  # noqa: E402
import collect_kis_paper_evidence  # noqa: E402


def test_paper_trading_smoke_can_assume_empty_system_positions() -> None:
    assert paper_trading_smoke._load_system_positions(  # noqa: SLF001
        None,
        assume_empty=True,
    ) == []


def test_collect_kis_paper_evidence_loads_system_positions_json(tmp_path: Path) -> None:
    positions_path = tmp_path / "positions.json"
    positions_path.write_text(
        '{"positions": [{"ticker": "005930", "qty": 74}]}',
        encoding="utf-8",
    )

    assert collect_kis_paper_evidence._load_system_positions(  # noqa: SLF001
        str(positions_path),
    ) == [{"ticker": "005930", "qty": 74}]


def test_collect_kis_paper_evidence_rejects_ambiguous_system_positions(tmp_path: Path) -> None:
    positions_path = tmp_path / "positions.json"
    positions_path.write_text("[]", encoding="utf-8")

    try:
        collect_kis_paper_evidence._load_system_positions(  # noqa: SLF001
            str(positions_path),
            assume_empty=True,
        )
    except ValueError as exc:
        assert "mutually exclusive" in str(exc)
    else:
        raise AssertionError("expected mutually exclusive ValueError")


def test_collect_kis_paper_evidence_forwards_cold_risk_report(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeRunner:
        def run_balance_reconciliation(self, system_positions=None, write_report=True):
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
            return {"status": "PASS"}

    def fake_service_rehearsal(args):
        calls["cold_risk_report"] = args.cold_risk_report
        calls["tickers"] = args.tickers
        return {"status": "PASS"}

    monkeypatch.setattr(collect_kis_paper_evidence, "PaperTradingRunner", FakeRunner)
    monkeypatch.setattr(
        collect_kis_paper_evidence.paper_auto_service_rehearsal,
        "build_report",
        fake_service_rehearsal,
    )

    report = collect_kis_paper_evidence.collect(
        argparse.Namespace(
            system_positions_json=None,
            assume_empty_system_positions=False,
            price=70000.0,
            auto_price=False,
            order_type="00",
            ticker="005930",
            side="buy",
            qty=1,
            probe_confirm_phrase="PAPER_ORDER_OK",
            auto_confirm_phrase="PAPER_AUTO_OK",
            tickers="005930,000660",
            cycles=1,
            interval_sec=0.0,
            registry_dir="artifacts/lgbm_paper_candidate/BUNDLE-TEST",
            cold_risk_report="artifacts/reports/community_live_risk/example.json",
            no_write_report=True,
            use_real_hot_runner=False,
            bundle_id="BUNDLE-TEST",
        )
    )

    assert report["status"] == "PASS"
    assert calls["cold_risk_report"] == "artifacts/reports/community_live_risk/example.json"
    assert calls["tickers"] == "005930,000660"


def test_collect_kis_paper_evidence_converts_service_exception_to_blocked(
    monkeypatch,
) -> None:
    class FakeRunner:
        def run_balance_reconciliation(self, system_positions=None, write_report=True):
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
            return {"status": "PASS"}

    def fail_service_rehearsal(args):
        raise ConnectionError("dns failed")

    monkeypatch.setattr(collect_kis_paper_evidence, "PaperTradingRunner", FakeRunner)
    monkeypatch.setattr(
        collect_kis_paper_evidence.paper_auto_service_rehearsal,
        "build_report",
        fail_service_rehearsal,
    )

    report = collect_kis_paper_evidence.collect(
        argparse.Namespace(
            system_positions_json=None,
            assume_empty_system_positions=False,
            price=70000.0,
            auto_price=False,
            order_type="00",
            ticker="005930",
            side="buy",
            qty=1,
            probe_confirm_phrase="PAPER_ORDER_OK",
            auto_confirm_phrase="PAPER_AUTO_OK",
            tickers="005930",
            cycles=1,
            interval_sec=0.0,
            registry_dir="artifacts/lgbm_paper_candidate/BUNDLE-TEST",
            cold_risk_report="artifacts/reports/community_live_risk/example.json",
            no_write_report=True,
            use_real_hot_runner=False,
            bundle_id="BUNDLE-TEST",
        )
    )

    assert report["status"] == "BLOCKED"
    assert report["stage_statuses"]["paper_auto_service_rehearsal"] == "BLOCKED"
    service = report["stages"]["paper_auto_service_rehearsal"]
    assert service["blockers"] == ["paper_auto_service_rehearsal_exception"]
    assert service["stages"]["paper_auto_cycle"]["exception_type"] == "ConnectionError"
    assert service["stages"]["paper_auto_cycle"]["fail_closed"] is True


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

from __future__ import annotations

from pathlib import Path

from src.execution.paper_trading import PaperTradingRunner


class FakePaperKIS:
    mode = "virtual"

    def __init__(self) -> None:
        self.orders: list[dict] = []

    def get_balance(self) -> dict:
        return {
            "balance": {"cash": 1_000_000.0, "total_eval": 1_710_000.0},
            "positions": [
                {"ticker": "005930", "qty": 10},
            ],
            "_mode": "virtual",
        }

    def submit_order(
        self,
        ticker: str,
        side: str,
        qty: int,
        price: float,
        order_type: str = "00",
    ) -> dict:
        self.orders.append({
            "ticker": ticker,
            "side": side,
            "qty": qty,
            "price": price,
            "order_type": order_type,
        })
        return {
            "status": "submitted",
            "order_id": "OD-PAPER-1",
            "price": price,
        }

    def get_order_history(
        self,
        ticker: str,
        side: str,
        execution_filter: str,
        order_id: str = "",
    ) -> dict:
        return {
            "orders": [{
                "order_id": "OD-PAPER-1",
                "ticker": ticker,
                "side": side,
                "status": "submitted",
            }],
            "_mode": "virtual",
        }


class FakeRealKIS(FakePaperKIS):
    mode = "real"


class FakePaperKISNoOrderHistoryMatch(FakePaperKIS):
    def get_order_history(
        self,
        ticker: str,
        side: str,
        execution_filter: str,
        order_id: str = "",
    ) -> dict:
        return {
            "orders": [{
                "order_id": "OD-OTHER",
                "ticker": ticker,
                "side": side,
                "status": "submitted",
            }],
            "_mode": "virtual",
        }


def test_paper_balance_reconciliation_pass(tmp_path: Path) -> None:
    runner = PaperTradingRunner(
        kis_client=FakePaperKIS(),
        report_dir=tmp_path,
    )

    report = runner.run_balance_reconciliation(
        system_positions=[{"ticker": "005930", "qty": 10}]
    )

    assert report["status"] == "PASS"
    assert report["stages"]["mode_guard"]["status"] == "PASS"
    assert report["stages"]["reconciliation"]["ok"] is True
    assert Path(report["report_path"]).exists()


def test_paper_balance_reconciliation_mismatch_fails(tmp_path: Path) -> None:
    runner = PaperTradingRunner(
        kis_client=FakePaperKIS(),
        report_dir=tmp_path,
    )

    report = runner.run_balance_reconciliation(
        system_positions=[{"ticker": "005930", "qty": 9}]
    )

    assert report["status"] == "FAIL"
    assert report["failures"] == ["reconciliation"]
    assert report["stages"]["reconciliation"]["n_mismatches"] == 1


def test_paper_submit_probe_requires_confirm_phrase(tmp_path: Path) -> None:
    client = FakePaperKIS()
    runner = PaperTradingRunner(kis_client=client, report_dir=tmp_path)

    report = runner.submit_probe_order(
        ticker="005930",
        side="buy",
        qty=1,
        price=70000,
        confirm_phrase=None,
    )

    assert report["status"] == "SKIP"
    assert client.orders == []
    assert report["stages"]["order_guard"]["required_phrase"] == "PAPER_ORDER_OK"


def test_paper_submit_probe_uses_execution_gateway(tmp_path: Path) -> None:
    client = FakePaperKIS()
    runner = PaperTradingRunner(kis_client=client, report_dir=tmp_path)

    report = runner.submit_probe_order(
        ticker="5930",
        side="buy",
        qty=1,
        price=70000,
        confirm_phrase=runner.confirm_phrase,
    )

    assert report["status"] == "PASS"
    assert client.orders == [{
        "ticker": "005930",
        "side": "buy",
        "qty": 1,
        "price": 70000.0,
        "order_type": "00",
    }]
    execution = report["stages"]["execution"]["result"]["execution_report"]
    assert execution["execution_mode"] == "paper"
    assert execution["fills"][0]["broker_order_id"] == "OD-PAPER-1"
    assert report["stages"]["order_history"]["status"] == "PASS"
    assert report["stages"]["order_history"]["query"]["order_id"] == "OD-PAPER-1"
    assert report["stages"]["order_history"]["matched_order_count"] == 1


def test_paper_order_history_action_requeries_by_broker_id(tmp_path: Path) -> None:
    runner = PaperTradingRunner(kis_client=FakePaperKIS(), report_dir=tmp_path)

    report = runner.run_order_history(
        ticker="005930",
        side="buy",
        order_id="OD-PAPER-1",
        execution_filter="all",
        write_report=False,
    )

    assert report["status"] == "PASS"
    assert report["stages"]["order_history"]["matched_order_count"] == 1


def test_paper_submit_probe_fails_without_broker_history_match(tmp_path: Path) -> None:
    runner = PaperTradingRunner(kis_client=FakePaperKISNoOrderHistoryMatch(), report_dir=tmp_path)

    report = runner.submit_probe_order(
        ticker="005930",
        side="buy",
        qty=1,
        price=70000,
        confirm_phrase=runner.confirm_phrase,
    )

    assert report["status"] == "FAIL"
    assert report["stages"]["order_history"]["status"] == "FAIL"
    assert report["stages"]["order_history"]["matched_order_count"] == 0
    assert report["stages"]["order_history"]["reason"] == "broker_order_id_not_found_in_history"


def test_paper_submit_probe_rejects_real_mode(tmp_path: Path) -> None:
    runner = PaperTradingRunner(kis_client=FakeRealKIS(), report_dir=tmp_path)

    report = runner.submit_probe_order(
        ticker="005930",
        side="buy",
        qty=1,
        price=70000,
        confirm_phrase=runner.confirm_phrase,
    )

    assert report["status"] == "FAIL"
    assert report["stages"]["mode_guard"]["error_code"] == "PAPER_MODE_REQUIRED"


def test_paper_submit_probe_enforces_probe_qty_limit(tmp_path: Path) -> None:
    client = FakePaperKIS()
    runner = PaperTradingRunner(kis_client=client, report_dir=tmp_path)

    report = runner.submit_probe_order(
        ticker="005930",
        side="buy",
        qty=2,
        price=70000,
        confirm_phrase=runner.confirm_phrase,
    )

    assert report["status"] == "FAIL"
    assert report["stages"]["order_guard"]["reason"] == "qty_out_of_probe_limit"
    assert client.orders == []

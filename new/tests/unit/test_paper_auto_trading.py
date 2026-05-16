from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.execution import paper_auto_trading as paper_auto_module
from src.execution.paper_auto_trading import PaperAutoTrader


def _load_paper_auto_trade_script():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "paper_auto_trade.py"
    scripts_dir = str(script_path.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("paper_auto_trade", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakePaperKIS:
    mode = "virtual"

    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []

    def get_balance(self) -> dict[str, Any]:
        return {
            "balance": {"cash": 1_000_000.0, "net_asset": 1_000_000.0},
            "positions": [],
            "_mode": self.mode,
        }

    def inquire_minute_bar(self, ticker: str, n_bars: int) -> list[dict[str, Any]]:
        return [
            {
                "ticker": ticker,
                "open": 70000 + i,
                "high": 70100 + i,
                "low": 69900 + i,
                "close": 70000 + i,
                "volume": 1000 + i,
                "ts_close": f"2026-05-12T09:{i % 60:02d}:00+09:00",
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
        self.orders.append({
            "ticker": ticker,
            "side": side,
            "qty": qty,
            "price": price,
            "order_type": order_type,
        })
        return {
            "status": "submitted",
            "order_id": "PAPER-AUTO-1",
            "price": price,
        }

    def get_order_history(
        self,
        ticker: str = "",
        order_id: str = "",
        side: str = "all",
        execution_filter: str = "all",
    ) -> dict[str, Any]:
        orders = []
        for order in self.orders:
            if order_id and order_id != "PAPER-AUTO-1":
                continue
            if ticker and order["ticker"] != ticker:
                continue
            if side != "all" and order["side"] != side:
                continue
            orders.append({**order, "order_id": "PAPER-AUTO-1", "status": "submitted"})
        return {"orders": orders, "summary": {}, "_mode": self.mode}


class FakeRealKIS(FakePaperKIS):
    mode = "real"


class FakePaperKISRejects(FakePaperKIS):
    def submit_order(
        self,
        ticker: str,
        side: str,
        qty: int,
        price: float,
        order_type: str = "00",
    ) -> dict[str, Any]:
        return {
            "status": "rejected",
            "error": "[kis_rest] KIS API 오류 msg_cd=40580000 msg=모의투자 장종료 입니다.",
        }


class FakePaperKISNoHistoryMatch(FakePaperKIS):
    def get_order_history(
        self,
        ticker: str = "",
        order_id: str = "",
        side: str = "all",
        execution_filter: str = "all",
    ) -> dict[str, Any]:
        return {"orders": [], "summary": {}, "_mode": self.mode}


class FakePaperKISNoBrokerOrderId(FakePaperKIS):
    def submit_order(
        self,
        ticker: str,
        side: str,
        qty: int,
        price: float,
        order_type: str = "00",
    ) -> dict[str, Any]:
        self.orders.append({
            "ticker": ticker,
            "side": side,
            "qty": qty,
            "price": price,
            "order_type": order_type,
        })
        return {
            "status": "submitted",
            "price": price,
        }


class FakeHotRunner:
    def __init__(self, qty: int = 1, approved: bool = True) -> None:
        self.state = SimpleNamespace(value="BOOTSTRAP")
        self._quant = SimpleNamespace(
            has_model=True,
            model_metadata={"version": "active_v1", "bundle_id": "BUNDLE-TEST"},
        )
        self.qty = qty
        self.approved = approved

    def start(self) -> None:
        self.state = SimpleNamespace(value="HOT_RUNNING")

    def run_once(self, **_: Any) -> dict[str, Any]:
        return {
            "final_decision": {
                "decision_id": "FDA-TEST",
                "approved": self.approved,
                "reason_code": "APPROVED" if self.approved else "RISK_VETO",
                "order_deltas": [
                    {
                        "ticker": "005930",
                        "side": "buy",
                        "qty": self.qty,
                        "price": 70000.0,
                        "order_type": "00",
                    }
                ] if self.approved else [],
            },
            "latency_ms": 1.0,
        }


class FakeMarketHotRunner(FakeHotRunner):
    def run_once(self, **_: Any) -> dict[str, Any]:
        result = super().run_once(**_)
        result["final_decision"]["order_deltas"][0]["order_type"] = "01"
        result["final_decision"]["order_deltas"][0]["price"] = 0.0
        return result


def test_paper_auto_requires_confirm_phrase(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeHotRunner(),
        report_dir=tmp_path,
    )

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=None,
        write_report=False,
    )

    assert report["status"] == "SKIP"
    assert client.orders == []
    assert report["stages"]["start_guard"]["required_phrase"] == "PAPER_AUTO_OK"


def test_paper_auto_executes_paper_order(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeHotRunner(qty=1),
        report_dir=tmp_path,
    )

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    assert report["status"] == "PASS"
    assert client.orders == [{
        "ticker": "005930",
        "side": "buy",
        "qty": 1,
        "price": 70000.0,
        "order_type": "00",
    }]
    cycle = report["stages"]["cycles"]["items"][0]
    assert cycle["execution"]["execution_report"]["execution_mode"] == "paper"
    assert cycle["order_history_verification"]["status"] == "PASS"
    assert cycle["order_history_verification"]["queries"][0]["matched_order_count"] == 1


def test_paper_auto_rejects_zero_cycles_before_starting_hot_runner(tmp_path: Path) -> None:
    client = FakePaperKIS()
    hot_runner = FakeHotRunner(qty=1)
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=hot_runner,
        report_dir=tmp_path,
    )

    report = trader.run(
        tickers=["005930"],
        cycles=0,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    assert report["status"] == "FAIL"
    assert report["stages"]["cycles"]["reason"] == "cycles_must_be_positive"
    assert report["stages"]["cycles"]["items"] == []
    assert hot_runner.state.value == "BOOTSTRAP"
    assert client.orders == []


def test_paper_auto_broker_rejection_fails_cycle(tmp_path: Path) -> None:
    trader = PaperAutoTrader(
        kis_client=FakePaperKISRejects(),
        hot_runner=FakeHotRunner(qty=1),
        report_dir=tmp_path,
    )

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    assert report["status"] == "FAIL"
    cycle = report["stages"]["cycles"]["items"][0]
    assert cycle["status"] == "FAIL"
    assert cycle["execution"]["execution_report"]["status"] == "rejected"
    assert cycle["broker_blockers"][0]["error_code"] == "BROKER_MARKET_CLOSED"


def test_paper_auto_fails_when_broker_order_id_not_in_history(tmp_path: Path) -> None:
    trader = PaperAutoTrader(
        kis_client=FakePaperKISNoHistoryMatch(),
        hot_runner=FakeHotRunner(qty=1),
        report_dir=tmp_path,
    )

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    cycle = report["stages"]["cycles"]["items"][0]
    assert report["status"] == "FAIL"
    assert cycle["order_history_verification"]["status"] == "FAIL"
    assert cycle["order_history_verification"]["failures"][0]["error_code"] == (
        "BROKER_ORDER_ID_NOT_FOUND_IN_HISTORY"
    )


def test_paper_auto_fails_when_broker_order_id_missing(tmp_path: Path) -> None:
    trader = PaperAutoTrader(
        kis_client=FakePaperKISNoBrokerOrderId(),
        hot_runner=FakeHotRunner(qty=1),
        report_dir=tmp_path,
    )

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    cycle = report["stages"]["cycles"]["items"][0]
    assert report["status"] == "FAIL"
    assert cycle["order_history_verification"]["status"] == "FAIL"
    assert cycle["order_history_verification"]["failures"][0]["error_code"] == (
        "BROKER_ORDER_ID_MISSING"
    )


def test_paper_auto_clips_qty_over_limit_downward(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeHotRunner(qty=2),
        report_dir=tmp_path,
    )

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    assert report["status"] == "PASS"
    assert client.orders[0]["qty"] == 1
    cycle = report["stages"]["cycles"]["items"][0]
    clipping = cycle["order_guard"]["qty_clipping"]["items"][0]
    assert clipping["original_qty"] == 2
    assert clipping["clipped_qty"] == 1
    assert clipping["direction"] == "decrease_only"


def test_paper_auto_treats_string_false_market_order_as_disabled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        paper_auto_module,
        "config_load",
        lambda file_name, section: (
            {
                "report_dir": str(tmp_path),
                "confirm_start_phrase": "PAPER_AUTO_OK",
                "require_virtual_mode": "true",
                "require_active_model": "true",
                "max_orders_per_cycle": 3,
                "max_order_qty_per_order": 1,
                "allow_market_order": "false",
                "use_latest_ppo_policy_if_available": "false",
            }
            if section == "paper_auto_trading"
            else {"warmup_bars": 60} if section == "quant_agent"
            else {}
        ),
    )
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeMarketHotRunner(qty=1),
        report_dir=tmp_path,
    )

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    cycle = report["stages"]["cycles"]["items"][0]
    assert report["status"] == "FAIL"
    assert cycle["order_guard"]["status"] == "FAIL"
    assert cycle["order_guard"]["violations"][0]["reason"] == "market_order_not_allowed"
    assert client.orders == []


def test_paper_auto_rejects_malformed_qty_without_crash(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeHotRunner(qty="abc"),  # type: ignore[arg-type]
        report_dir=tmp_path,
    )

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    cycle = report["stages"]["cycles"]["items"][0]
    assert report["status"] == "FAIL"
    assert cycle["order_guard"]["status"] == "FAIL"
    assert cycle["order_guard"]["violations"][0]["reason"] == "qty_out_of_limit"
    assert client.orders == []


def test_paper_auto_rejects_real_mode(tmp_path: Path) -> None:
    trader = PaperAutoTrader(
        kis_client=FakeRealKIS(),
        hot_runner=FakeHotRunner(qty=1),
        report_dir=tmp_path,
    )

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    assert report["status"] == "FAIL"
    assert report["stages"]["mode_guard"]["error_code"] == "PAPER_MODE_REQUIRED"


def test_paper_auto_cli_rejects_paper_rehearsal_scope(capsys) -> None:
    script = _load_paper_auto_trade_script()

    rc = script.main(["--prelive-scope", "paper-rehearsal", "--no-write-report"])

    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "BLOCKED"
    assert out["reason"] == "paper_rehearsal_scope_not_allowed_for_auto_trade"

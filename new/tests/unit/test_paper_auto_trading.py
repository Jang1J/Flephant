from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from src.connectors.kis_rest import KISAPIError
from src.execution import paper_auto_trading as paper_auto_module
from src.execution.kill_switch import KillSwitch
from src.execution.paper_auto_trading import PaperAutoTrader

_KST = ZoneInfo("Asia/Seoul")


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


def _paper_session_now() -> datetime:
    return datetime(2026, 5, 12, 10, 0, tzinfo=_KST)


def _paper_preopen_now() -> datetime:
    return datetime(2026, 5, 12, 8, 30, tzinfo=_KST)


def _paper_weekend_now() -> datetime:
    return datetime(2026, 5, 16, 10, 0, tzinfo=_KST)


def _paper_close_now() -> datetime:
    return datetime(2026, 5, 12, 15, 31, tzinfo=_KST)


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


class FakePaperKISBalanceError(FakePaperKIS):
    def get_balance(self) -> dict[str, Any]:
        raise ConnectionError("dns failed")


class FakePaperKISBalanceTransientThenSuccess(FakePaperKIS):
    def __init__(self, failures_before_success: int) -> None:
        super().__init__()
        self.failures_before_success = failures_before_success

    def get_balance(self) -> dict[str, Any]:
        if self.failures_before_success > 0:
            self.failures_before_success -= 1
            raise ConnectionError("HTTP timeout (10s): read timed out")
        return super().get_balance()


class FakePaperKISBalanceNonTransientError(FakePaperKIS):
    def get_balance(self) -> dict[str, Any]:
        raise KISAPIError(
            "[kis_rest] KIS API 오류 path=/uapi/domestic-stock/v1/trading/"
            "inquire-balance msg_cd=OPSQ2000 msg=계좌 확인 오류"
        )


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


class FakePaperKISNoHistoryMethod:
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
        return FakePaperKIS().inquire_minute_bar(ticker, n_bars)

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
            "order_id": "PAPER-AUTO-NO-HISTORY",
            "price": price,
        }


class FakeHotRunner:
    def __init__(
        self,
        qty: int = 1,
        approved: bool = True,
        price: float = 70000.0,
    ) -> None:
        self.state = SimpleNamespace(value="BOOTSTRAP")
        self._quant = SimpleNamespace(
            has_model=True,
            model_metadata={"version": "active_v1", "bundle_id": "BUNDLE-TEST"},
        )
        self.qty = qty
        self.approved = approved
        self.price = price
        self.calls: list[dict[str, Any]] = []

    def start(self) -> None:
        self.state = SimpleNamespace(value="HOT_RUNNING")

    def run_once(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
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
                        "price": self.price,
                        "order_type": "00",
                        "reason": "rebalance",
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


class FakeMultiOrderHotRunner(FakeHotRunner):
    def run_once(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "final_decision": {
                "decision_id": "FDA-MULTI",
                "approved": True,
                "reason_code": "APPROVED",
                "order_deltas": [
                    {
                        "ticker": "035420",
                        "side": "buy",
                        "qty": 7,
                        "price": 180000.0,
                        "order_type": "00",
                        "reason": "rebalance",
                        "delta_weight": 0.01,
                    },
                    {
                        "ticker": "000660",
                        "side": "buy",
                        "qty": 8,
                        "price": 150000.0,
                        "order_type": "00",
                        "reason": "rebalance",
                        "delta_weight": 0.05,
                    },
                    {
                        "ticker": "005930",
                        "side": "buy",
                        "qty": 6,
                        "price": 70000.0,
                        "order_type": "00",
                        "reason": "rebalance",
                        "delta_weight": 0.03,
                    },
                    {
                        "ticker": "042700",
                        "side": "buy",
                        "qty": 9,
                        "price": 110000.0,
                        "order_type": "00",
                        "reason": "rebalance",
                        "delta_weight": 0.02,
                    },
                    {
                        "ticker": "403870",
                        "side": "buy",
                        "qty": 10,
                        "price": 45000.0,
                        "order_type": "00",
                        "reason": "rebalance",
                        "delta_weight": 0.04,
                    },
                ],
            },
            "latency_ms": 1.0,
        }


class FakeRiskAwareHotRunner(FakeHotRunner):
    def run_once(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        warnings = list(kwargs.get("risk_warnings") or [])
        if warnings:
            reason_code = warnings[0].get("recommended_fda_reason_code") or "RISK_FAST_TRIGGER"
            return {
                "final_decision": {
                    "decision_id": "FDA-COLD-RISK",
                    "approved": False,
                    "reason_code": reason_code,
                    "order_deltas": [],
                },
                "latency_ms": 1.0,
            }
        return super().run_once(**kwargs)


class FakeNoModelFlagHotRunner(FakeHotRunner):
    def __init__(self) -> None:
        super().__init__(qty=1)
        self._quant = SimpleNamespace(
            model_metadata={"version": "active_v1", "bundle_id": "BUNDLE-TEST"},
        )


class FakeNoBundleHotRunner(FakeHotRunner):
    def __init__(self) -> None:
        super().__init__(qty=1)
        self._quant = SimpleNamespace(
            has_model=True,
            model_metadata={"version": "active_v1"},
        )


class FakeOtherBundleHotRunner(FakeHotRunner):
    def __init__(self) -> None:
        super().__init__(qty=1)
        self._quant = SimpleNamespace(
            has_model=True,
            model_metadata={"version": "active_v1", "bundle_id": "BUNDLE-OTHER"},
        )


class FakeStringFalseModelHotRunner(FakeHotRunner):
    def __init__(self) -> None:
        super().__init__(qty=1)
        self._quant = SimpleNamespace(
            has_model="false",
            model_metadata={"version": "active_v1", "bundle_id": "BUNDLE-TEST"},
        )


def test_paper_auto_requires_confirm_phrase(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeHotRunner(),
        report_dir=tmp_path,
        now_fn=_paper_session_now,
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
    hot_runner = FakeHotRunner(qty=1)
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=hot_runner,
        report_dir=tmp_path,
        now_fn=_paper_session_now,
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
    assert hot_runner.calls[0]["dependency_status"] == {
        "news": "skipped",
        "risk": "done",
        "quant": "done",
        "debate": "skipped",
    }


def test_paper_auto_fails_fast_when_requested_ticker_is_not_active(
    tmp_path: Path,
) -> None:
    client = FakePaperKIS()
    hot_runner = FakeHotRunner(qty=1)
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=hot_runner,
        report_dir=tmp_path,
        now_fn=_paper_session_now,
    )
    trader._active_trade_universe = {"005930"}  # noqa: SLF001

    report = trader.run(
        tickers=["035420"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    assert report["status"] == "FAIL"
    guard = report["stages"]["requested_ticker_universe_guard"]
    assert guard["reason"] == "requested_ticker_not_active_universe"
    assert guard["blocked_tickers"] == ["035420"]
    assert hot_runner.state.value == "BOOTSTRAP"
    assert hot_runner.calls == []
    assert client.orders == []


def test_paper_auto_skips_single_transient_balance_error_without_order(
    tmp_path: Path,
) -> None:
    client = FakePaperKISBalanceError()
    hot_runner = FakeHotRunner(qty=1)
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=hot_runner,
        report_dir=tmp_path,
        now_fn=_paper_session_now,
    )
    trader._active_trade_universe = {"005930"}  # noqa: SLF001

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    assert report["status"] == "PASS"
    cycle = report["stages"]["cycles"]["items"][0]
    assert cycle["status"] == "SKIP"
    assert cycle["reason"] == "paper_auto_read_transient_error_skip"
    assert cycle["exception_type"] == "ConnectionError"
    assert cycle["safe_skip"] is True
    assert cycle["fail_closed"] is False
    assert hot_runner.calls == []
    assert client.orders == []


def test_paper_auto_fails_closed_after_consecutive_transient_read_errors(
    tmp_path: Path,
) -> None:
    client = FakePaperKISBalanceError()
    hot_runner = FakeHotRunner(qty=1)
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=hot_runner,
        report_dir=tmp_path,
        now_fn=_paper_session_now,
    )
    trader._active_trade_universe = {"005930"}  # noqa: SLF001

    report = trader.run(
        tickers=["005930"],
        cycles=4,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    assert report["status"] == "FAIL"
    cycles = report["stages"]["cycles"]["items"]
    assert [cycle["status"] for cycle in cycles] == ["SKIP", "SKIP", "SKIP", "FAIL"]
    assert cycles[-1]["reason"] == "paper_auto_read_error_budget_exhausted"
    assert cycles[-1]["consecutive_read_errors"] == 4
    assert cycles[-1]["max_consecutive_read_error_skips"] == 3
    assert cycles[-1]["fail_closed"] is True
    assert hot_runner.calls == []
    assert client.orders == []


def test_paper_auto_continues_after_transient_read_error_recovers(
    tmp_path: Path,
) -> None:
    client = FakePaperKISBalanceTransientThenSuccess(failures_before_success=1)
    hot_runner = FakeHotRunner(qty=1)
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=hot_runner,
        report_dir=tmp_path,
        now_fn=_paper_session_now,
    )
    trader._active_trade_universe = {"005930"}  # noqa: SLF001

    report = trader.run(
        tickers=["005930"],
        cycles=2,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    cycles = report["stages"]["cycles"]["items"]
    assert report["status"] == "PASS"
    assert [cycle["status"] for cycle in cycles] == ["SKIP", "PASS"]
    assert cycles[0]["reason"] == "paper_auto_read_transient_error_skip"
    assert len(hot_runner.calls) == 1
    assert client.orders == [{
        "ticker": "005930",
        "side": "buy",
        "qty": 1,
        "price": 70000.0,
        "order_type": "00",
    }]


def test_paper_auto_non_transient_balance_error_remains_fail_closed(
    tmp_path: Path,
) -> None:
    client = FakePaperKISBalanceNonTransientError()
    hot_runner = FakeHotRunner(qty=1)
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=hot_runner,
        report_dir=tmp_path,
        now_fn=_paper_session_now,
    )
    trader._active_trade_universe = {"005930"}  # noqa: SLF001

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
    assert cycle["reason"] == "paper_auto_cycle_exception"
    assert cycle["exception_type"] == "KISAPIError"
    assert cycle["fail_closed"] is True
    assert hot_runner.calls == []
    assert client.orders == []


def test_paper_auto_forwards_cold_path_risk_warnings_and_skips_order(
    tmp_path: Path,
) -> None:
    client = FakePaperKIS()
    hot_runner = FakeRiskAwareHotRunner(qty=1)
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=hot_runner,
        report_dir=tmp_path,
        now_fn=_paper_session_now,
    )

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        risk_warnings=[
            {
                "risk_level": "high",
                "recommended_fda_reason_code": "NEWS_COMMUNITY_DIVERGENCE",
                "reason": "cold_path_fda_veto",
            }
        ],
        write_report=False,
    )

    assert report["status"] == "PASS"
    assert client.orders == []
    assert report["stages"]["cold_path_risk_warnings"]["count"] == 1
    assert hot_runner.calls[0]["risk_warnings"][0]["severity"] == "high"
    cycle = report["stages"]["cycles"]["items"][0]
    assert cycle["cold_path_risk_warning_count"] == 1
    assert cycle["order_guard"]["reason"] == "fda_veto"
    assert cycle["order_guard"]["reason_code"] == "NEWS_COMMUNITY_DIVERGENCE"


def test_paper_auto_normalizes_limit_price_to_krx_tick(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeHotRunner(qty=1, price=275750.0),
        report_dir=tmp_path,
        now_fn=_paper_session_now,
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
        "price": 276000.0,
        "order_type": "00",
    }]
    fill = report["stages"]["cycles"]["items"][0]["execution"]["execution_report"]["fills"][0]
    assert fill["broker_response"]["price"] == 276000.0


def test_paper_auto_respects_active_kill_switch(tmp_path: Path) -> None:
    client = FakePaperKIS()
    kill_switch = KillSwitch()
    kill_switch.trigger("overnight_test")
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeHotRunner(qty=1),
        report_dir=tmp_path,
        now_fn=_paper_session_now,
        kill_switch=kill_switch,
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
    assert cycle["execution"]["execution_report"]["status"] == "rejected"
    assert "kill_switch_active" in cycle["execution"]["execution_report"]["rejection_reason"]
    assert client.orders == []


def test_paper_auto_checks_market_session_each_cycle(tmp_path: Path) -> None:
    client = FakePaperKIS()
    times = iter([
        datetime(2026, 5, 12, 15, 29, tzinfo=_KST),
        datetime(2026, 5, 12, 15, 29, tzinfo=_KST),
        _paper_close_now(),
    ])
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeHotRunner(qty=1),
        report_dir=tmp_path,
        now_fn=lambda: next(times),
    )

    report = trader.run(
        tickers=["005930"],
        cycles=2,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    cycles = report["stages"]["cycles"]["items"]
    assert len(cycles) == 2
    assert client.orders == [{
        "ticker": "005930",
        "side": "buy",
        "qty": 1,
        "price": 70000.0,
        "order_type": "00",
    }]
    assert cycles[1]["market_session_guard"]["reason"] == "outside_market_session"
    assert cycles[1]["execution"] is None


def test_paper_auto_skips_before_market_open(tmp_path: Path) -> None:
    client = FakePaperKIS()
    hot_runner = FakeHotRunner(qty=1)
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=hot_runner,
        report_dir=tmp_path,
        now_fn=_paper_preopen_now,
    )

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    assert report["status"] == "SKIP"
    assert report["stages"]["market_session_guard"]["reason"] == "outside_market_session"
    assert hot_runner.state.value == "BOOTSTRAP"
    assert client.orders == []


def test_paper_auto_skips_non_trading_day(tmp_path: Path) -> None:
    client = FakePaperKIS()
    hot_runner = FakeHotRunner(qty=1)
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=hot_runner,
        report_dir=tmp_path,
        now_fn=_paper_weekend_now,
    )

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    assert report["status"] == "SKIP"
    assert report["stages"]["market_session_guard"]["reason"] == "not_kospi_trading_day"
    assert hot_runner.state.value == "BOOTSTRAP"
    assert client.orders == []


def test_paper_auto_run_once_requires_start_guard(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeHotRunner(qty=1),
        report_dir=tmp_path,
        now_fn=_paper_session_now,
    )

    result = trader.run_once(tickers=["005930"], cycle_index=0)

    assert result["status"] == "FAIL"
    assert result["reason"] == "run_once_requires_start_guard"
    assert client.orders == []


def test_paper_auto_active_model_guard_requires_has_model_flag(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeNoModelFlagHotRunner(),
        report_dir=tmp_path,
        now_fn=_paper_session_now,
    )

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    assert report["status"] == "FAIL"
    assert report["stages"]["active_model_guard"]["error_code"] == "ACTIVE_MODEL_REQUIRED"
    assert report["stages"]["active_model_guard"]["has_model"] is False
    assert client.orders == []


def test_paper_auto_active_model_guard_requires_bundle_id(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeNoBundleHotRunner(),
        report_dir=tmp_path,
        now_fn=_paper_session_now,
    )

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    assert report["status"] == "FAIL"
    assert report["stages"]["active_model_guard"]["error_code"] == "ACTIVE_MODEL_REQUIRED"
    assert report["stages"]["active_model_guard"]["bundle_id"] is None
    assert client.orders == []


def test_paper_auto_active_model_guard_requires_requested_bundle(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeOtherBundleHotRunner(),
        report_dir=tmp_path,
        required_bundle_id="BUNDLE-TEST",
        now_fn=_paper_session_now,
    )

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    assert report["status"] == "FAIL"
    guard = report["stages"]["active_model_guard"]
    assert guard["error_code"] == "ACTIVE_MODEL_BUNDLE_MISMATCH"
    assert guard["bundle_id"] == "BUNDLE-OTHER"
    assert guard["required_bundle_id"] == "BUNDLE-TEST"
    assert client.orders == []


def test_paper_auto_active_model_guard_treats_string_false_as_false(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeStringFalseModelHotRunner(),
        report_dir=tmp_path,
        now_fn=_paper_session_now,
    )

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    assert report["status"] == "FAIL"
    assert report["stages"]["active_model_guard"]["error_code"] == "ACTIVE_MODEL_REQUIRED"
    assert report["stages"]["active_model_guard"]["has_model"] is False
    assert client.orders == []


def test_paper_auto_rejects_zero_cycles_before_starting_hot_runner(tmp_path: Path) -> None:
    client = FakePaperKIS()
    hot_runner = FakeHotRunner(qty=1)
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=hot_runner,
        report_dir=tmp_path,
        now_fn=_paper_session_now,
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
        now_fn=_paper_session_now,
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
        now_fn=_paper_session_now,
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
        now_fn=_paper_session_now,
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
    assert cycle["execution"]["execution_report"]["status"] == "rejected"
    assert cycle["broker_blockers"][0]["reason"] == "broker_order_id_missing"
    assert cycle["order_history_verification"]["status"] == "FAIL"
    assert cycle["order_history_verification"]["reason"] == "no_broker_fills"


def test_paper_auto_fails_when_order_history_method_missing(tmp_path: Path) -> None:
    trader = PaperAutoTrader(
        kis_client=FakePaperKISNoHistoryMethod(),
        hot_runner=FakeHotRunner(qty=1),
        report_dir=tmp_path,
        now_fn=_paper_session_now,
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
    assert cycle["execution"]["execution_report"]["status"] == "submitted"
    assert cycle["order_history_verification"]["status"] == "FAIL"
    assert cycle["order_history_verification"]["reason"] == "kis_client_no_get_order_history"


def test_paper_auto_caps_qty_over_limit_before_execution(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeHotRunner(qty=3),
        report_dir=tmp_path,
        now_fn=_paper_session_now,
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
        "qty": 2,
        "price": 70000.0,
        "order_type": "00",
    }]
    cycle = report["stages"]["cycles"]["items"][0]
    assert cycle["order_guard"]["status"] == "PASS"
    assert cycle["order_caps_applied"] == [{
        "index": 0,
        "ticker": "005930",
        "side": "buy",
        "original_qty": 3,
        "capped_qty": 2,
        "max_order_qty_per_order": 2,
    }]
    assert cycle["hot_result"]["final_decision"]["order_deltas"][0]["qty"] == 3
    assert cycle["execution"]["execution_report"]["status"] == "submitted"


def test_paper_auto_caps_order_count_before_order_guard(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeMultiOrderHotRunner(),
        report_dir=tmp_path,
        now_fn=_paper_session_now,
    )
    trader._active_trade_universe = {  # noqa: SLF001
        "005930",
        "000660",
        "035420",
        "042700",
        "403870",
    }

    report = trader.run(
        tickers=["005930", "000660", "035420", "042700", "403870"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    cycle = report["stages"]["cycles"]["items"][0]
    assert report["status"] == "PASS"
    assert cycle["order_guard"] == {"status": "PASS", "n_orders": 3}
    assert len(cycle["hot_result"]["final_decision"]["order_deltas"]) == 5
    assert [order["ticker"] for order in client.orders] == [
        "000660",
        "403870",
        "005930",
    ]
    assert [order["qty"] for order in client.orders] == [2, 2, 2]
    assert cycle["order_count_caps_applied"][0]["original_count"] == 5
    assert cycle["order_count_caps_applied"][0]["capped_count"] == 3
    assert cycle["order_count_caps_applied"][0]["max_orders_per_cycle"] == 3
    assert [
        item["ticker"] for item in cycle["order_count_caps_applied"][0]["kept"]
    ] == ["000660", "403870", "005930"]
    assert [
        item["ticker"] for item in cycle["order_count_caps_applied"][0]["dropped"]
    ] == ["042700", "035420"]
    assert len(cycle["order_caps_applied"]) == 3


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
        now_fn=_paper_session_now,
    )
    trader._active_trade_universe = {"005930"}  # noqa: SLF001

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
        now_fn=_paper_session_now,
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


def test_paper_auto_rejects_fractional_qty_without_truncation(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeHotRunner(qty="1.9"),  # type: ignore[arg-type]
        report_dir=tmp_path,
        now_fn=_paper_session_now,
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


def test_paper_auto_current_positions_do_not_truncate_fractional_qty() -> None:
    positions = PaperAutoTrader._current_positions(
        [{"ticker": "005930", "qty": "1.9", "current_price": 70000.0}],
        latest_prices={"005930": 70000.0},
        portfolio_value=1_000_000.0,
    )

    assert positions == [{
        "ticker": "005930",
        "qty": 0,
        "available_qty": 0,
        "weight": 0.0,
    }]


def test_paper_auto_current_positions_preserve_available_qty() -> None:
    positions = PaperAutoTrader._current_positions(
        [{
            "ticker": "403870",
            "qty": 2,
            "available_qty": 0,
            "current_price": 53600.0,
        }],
        latest_prices={"403870": 53600.0},
        portfolio_value=10_720_000.0,
    )

    assert positions == [{
        "ticker": "403870",
        "qty": 2,
        "available_qty": 0,
        "weight": 0.01,
    }]


def test_paper_auto_rejects_real_mode(tmp_path: Path) -> None:
    trader = PaperAutoTrader(
        kis_client=FakeRealKIS(),
        hot_runner=FakeHotRunner(qty=1),
        report_dir=tmp_path,
        now_fn=_paper_session_now,
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


def test_paper_auto_cli_requires_bundle_id_for_strict(capsys) -> None:
    script = _load_paper_auto_trade_script()

    rc = script.main(["--tickers", "005930", "--no-write-report"])

    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "BLOCKED"
    assert out["reason"] == "bundle_id_required_for_strict_paper_auto_trade"


def test_paper_auto_cli_defaults_to_final_dataset_gate_date(monkeypatch, capsys) -> None:
    script = _load_paper_auto_trade_script()
    calls: dict[str, Any] = {}

    monkeypatch.setattr(
        script.prelive_gate,
        "_final_dataset_gate_cfg",
        lambda: {"expected_end_date": "20260515", "rehearsal_business_days": 80},
    )
    monkeypatch.setattr(
        script,
        "config_load",
        lambda _file_name, _section=None: {
            "default_max_cycles": 1,
            "default_interval_sec": 0,
            "max_tickers": 1,
            "require_prelive_pass": True,
        },
    )

    def fake_build_report(**kwargs: Any) -> dict[str, Any]:
        calls["prelive"] = kwargs
        return {"status": "BLOCKED", "blockers": ["expected_test_blocker"]}

    monkeypatch.setattr(script.prelive_gate, "build_report", fake_build_report)

    rc = script.main([
        "--tickers",
        "005930",
        "--bundle-id",
        "BUNDLE-TEST",
        "--confirm-phrase",
        "PAPER_AUTO_OK",
        "--no-write-report",
    ])

    assert rc == 1
    assert calls["prelive"]["end_date"] == "20260515"
    assert calls["prelive"]["business_days"] == 80
    out = json.loads(capsys.readouterr().out)
    assert out["reason"] == "prelive_gate_not_pass"


def test_paper_auto_cli_passes_bundle_to_prelive_and_trader(monkeypatch, capsys) -> None:
    script = _load_paper_auto_trade_script()
    calls: dict[str, Any] = {}

    monkeypatch.setattr(
        script,
        "config_load",
        lambda _file_name, _section=None: {
            "default_max_cycles": 1,
            "default_interval_sec": 0,
            "max_tickers": 1,
            "require_prelive_pass": True,
        },
    )

    def fake_build_report(**kwargs: Any) -> dict[str, Any]:
        calls["prelive"] = kwargs
        return {"status": "PASS", "blockers": []}

    class FakeTrader:
        def __init__(self, *, required_bundle_id: str | None = None) -> None:
            calls["required_bundle_id"] = required_bundle_id

        def run(self, **kwargs: Any) -> dict[str, Any]:
            calls["run"] = kwargs
            return {"status": "PASS", "stages": {}}

    monkeypatch.setattr(script.prelive_gate, "build_report", fake_build_report)
    monkeypatch.setattr(script, "PaperAutoTrader", FakeTrader)

    rc = script.main([
        "--tickers",
        "005930",
        "--bundle-id",
        "BUNDLE-TEST",
        "--confirm-phrase",
        "PAPER_AUTO_OK",
        "--no-write-report",
    ])

    assert rc == 0
    assert calls["prelive"]["bundle_id"] == "BUNDLE-TEST"
    assert calls["required_bundle_id"] == "BUNDLE-TEST"
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "PASS"

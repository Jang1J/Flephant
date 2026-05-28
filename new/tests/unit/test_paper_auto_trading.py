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


def _write_warmup_parquet(path: Path, ticker: str, rows: int) -> None:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "ticker": ticker,
            "open": 69000 + i,
            "high": 69100 + i,
            "low": 68900 + i,
            "close": 69000 + i,
            "volume": 500 + i,
            "ts_close": f"2026-05-21T14:{i:02d}:00+09:00",
        }
        for i in range(rows)
    ]
    pd.DataFrame(records).to_parquet(path, index=False)


def _write_custom_warmup_parquet(
    path: Path,
    ticker: str,
    timestamps: list[str],
) -> None:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "ticker": ticker,
            "open": 69000 + i,
            "high": 69100 + i,
            "low": 68900 + i,
            "close": 69000 + i,
            "volume": 500 + i,
            "ts_close": ts,
        }
        for i, ts in enumerate(timestamps)
    ]
    pd.DataFrame(records).to_parquet(path, index=False)


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


class FakePaperKISWithPosition(FakePaperKIS):
    def __init__(
        self,
        *,
        ticker: str = "005930",
        qty: int = 1,
        current_price: float = 70000.0,
    ) -> None:
        super().__init__()
        self.ticker = ticker
        self.qty = qty
        self.current_price = current_price

    def get_balance(self) -> dict[str, Any]:
        return {
            "balance": {"cash": 1_000_000.0, "net_asset": 1_000_000.0},
            "positions": [
                {
                    "ticker": self.ticker,
                    "qty": self.qty,
                    "available_qty": self.qty,
                    "current_price": self.current_price,
                }
            ],
            "_mode": self.mode,
        }


class ShortWindowPaperKIS(FakePaperKIS):
    def inquire_minute_bar(self, ticker: str, n_bars: int) -> list[dict[str, Any]]:
        return [
            {
                "ticker": ticker,
                "open": 70000 + i,
                "high": 70100 + i,
                "low": 69900 + i,
                "close": 70000 + i,
                "volume": 1000 + i,
                "ts_close": f"2026-05-26T09:{i:02d}:00+09:00",
            }
            for i in range(30)
        ]


class InvalidTimestampPaperKIS(FakePaperKIS):
    def inquire_minute_bar(self, ticker: str, n_bars: int) -> list[dict[str, Any]]:
        bars = super().inquire_minute_bar(ticker, n_bars)
        bars[-1]["ts_close"] = "not-a-timestamp"
        return bars


class FutureTimestampPaperKIS(FakePaperKIS):
    def inquire_minute_bar(self, ticker: str, n_bars: int) -> list[dict[str, Any]]:
        bars = super().inquire_minute_bar(ticker, n_bars)
        bars[-1]["ts_close"] = "2026-05-26T10:01:00+09:00"
        return bars


class ShortWindowFutureTimestampPaperKIS(ShortWindowPaperKIS):
    def inquire_minute_bar(self, ticker: str, n_bars: int) -> list[dict[str, Any]]:
        bars = super().inquire_minute_bar(ticker, n_bars)
        bars[-1]["ts_close"] = "2026-05-26T10:01:00+09:00"
        return bars


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
        self.start_calls = 0

    @staticmethod
    def active_quant_output() -> dict[str, Any]:
        return {
            "mode": "active",
            "scores": {"005930": 0.2, "000660": 0.1},
            "n_tickers": 2,
        }

    def start(self) -> None:
        self.start_calls += 1
        self.state = SimpleNamespace(value="HOT_RUNNING")

    def run_once(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "quant_output": self.active_quant_output(),
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
            "quant_output": self.active_quant_output(),
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
                "quant_output": self.active_quant_output(),
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


class FakeZeroScoreHotRunner(FakeHotRunner):
    def run_once(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "quant_output": {
                "mode": "warmup",
                "scores": {},
                "n_tickers": 0,
            },
            "final_decision": {
                "decision_id": "FDA-ZERO-SCORE",
                "approved": True,
                "reason_code": "NORMAL_APPROVE",
                "order_deltas": [],
            },
            "latency_ms": 1.0,
        }


class FakeUnrankableScoreHotRunner(FakeHotRunner):
    def run_once(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "quant_output": {
                "mode": "active",
                "scores": {"005930": 0.0, "000660": 0.0},
                "n_tickers": 2,
            },
            "final_decision": {
                "decision_id": "FDA-UNRANKABLE",
                "approved": True,
                "reason_code": "NORMAL_APPROVE",
                "order_deltas": [
                    {
                        "ticker": "005930",
                        "side": "buy",
                        "qty": 1,
                        "price": 70000.0,
                        "order_type": "00",
                    }
                ],
            },
            "latency_ms": 1.0,
        }


class FakeFlatNonZeroScoreHotRunner(FakeHotRunner):
    def run_once(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "quant_output": {
                "mode": "active",
                "scores": {"005930": -0.00125, "000660": -0.00125},
                "n_tickers": 2,
            },
            "final_decision": {
                "decision_id": "FDA-FLAT-SCORE",
                "approved": True,
                "reason_code": "NORMAL_APPROVE",
                "order_deltas": [
                    {
                        "ticker": "005930",
                        "side": "buy",
                        "qty": 1,
                        "price": 70000.0,
                        "order_type": "00",
                    }
                ],
            },
            "latency_ms": 1.0,
        }


class FakeZeroScoreExitHotRunner(FakeHotRunner):
    def run_once(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "quant_output": {
                "mode": "warmup",
                "scores": {},
                "n_tickers": 0,
            },
            "final_decision": {
                "decision_id": "FDA-ZERO-SCORE-EXIT",
                "approved": True,
                "reason_code": "NORMAL_APPROVE",
                "order_deltas": [
                    {
                        "ticker": "005930",
                        "side": "sell",
                        "qty": 2,
                        "price": 70000.0,
                        "order_type": "00",
                        "reason": "rebalance",
                    }
                ],
            },
            "latency_ms": 1.0,
        }


class FakeSellHotRunner(FakeHotRunner):
    def __init__(self, *, reason: str = "rebalance", qty: int = 1) -> None:
        super().__init__(qty=qty)
        self.reason = reason

    def run_once(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "quant_output": self.active_quant_output(),
            "final_decision": {
                "decision_id": "FDA-SELL",
                "approved": True,
                "reason_code": "NORMAL_APPROVE",
                "order_deltas": [
                    {
                        "ticker": "005930",
                        "side": "sell",
                        "qty": self.qty,
                        "price": 70000.0,
                        "order_type": "00",
                        "reason": self.reason,
                        "delta_weight": -0.01,
                    }
                ],
            },
            "latency_ms": 1.0,
        }


class FakeVetoRiskReduceHotRunner(FakeHotRunner):
    def run_once(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "quant_output": self.active_quant_output(),
            "final_decision": {
                "decision_id": "FDA-VETO-RISK-REDUCE",
                "approved": False,
                "reason_code": "RISK_FAST_TRIGGER",
                "veto_reason": "high risk, but PM had risk_reduce sell",
                "order_deltas": [
                    {
                        "ticker": "005930",
                        "side": "sell",
                        "qty": 1,
                        "price": 70000.0,
                        "order_type": "00",
                        "reason": "risk_reduce",
                        "delta_weight": -0.01,
                    }
                ],
            },
            "latency_ms": 1.0,
        }


class FakeVetoMixedRiskReduceHotRunner(FakeHotRunner):
    def run_once(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "quant_output": self.active_quant_output(),
            "final_decision": {
                "decision_id": "FDA-VETO-MIXED",
                "approved": False,
                "reason_code": "RISK_FAST_TRIGGER",
                "veto_reason": "high risk mixed patch",
                "order_deltas": [
                    {
                        "ticker": "005930",
                        "side": "sell",
                        "qty": 1,
                        "price": 70000.0,
                        "order_type": "00",
                        "reason": "risk_reduce",
                        "delta_weight": -0.01,
                    },
                    {
                        "ticker": "000660",
                        "side": "buy",
                        "qty": 1,
                        "price": 150000.0,
                        "order_type": "00",
                        "reason": "rebalance",
                        "delta_weight": 0.01,
                    },
                ],
            },
            "latency_ms": 1.0,
        }


class FakeBlockedQuantHotRunner(FakeHotRunner):
    def run_once(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "status": "FAIL",
            "failure_stage": "quant_feature_readiness",
            "quant_output": {
                "mode": "blocked",
                "blocker": "required_feature_missing",
                "scores": {},
                "n_tickers": 0,
            },
            "final_decision": {
                "decision_id": "FDA-BLOCKED-QUANT",
                "approved": False,
                "reason_code": "QUANT_FEATURE_BLOCKED",
                "order_deltas": [],
            },
            "latency_ms": 1.0,
        }


class FakeFeatureReadinessBlockedHotRunner(FakeHotRunner):
    def __init__(self) -> None:
        super().__init__(qty=1)
        self.start_called = False

        def readiness(_tickers, _asof):
            return {
                "status": "FAIL",
                "error_code": "REQUIRED_DUAL_SOURCE_FEATURE_MISSING",
                "reason": "required_dual_source_feature_missing",
                "required_dual_source_cols": ["news_score_t"],
            }

        self._quant = SimpleNamespace(
            has_model=True,
            model_metadata={"version": "active_v1", "bundle_id": "BUNDLE-TEST"},
            serving_feature_readiness=readiness,
        )

    def start(self) -> None:
        self.start_called = True
        super().start()


class ExplodingReadKIS(FakePaperKIS):
    def get_balance(self) -> dict[str, Any]:
        raise AssertionError("broker read must not run when feature readiness fails")


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


def test_paper_auto_report_embeds_track_metadata(tmp_path: Path) -> None:
    trader = PaperAutoTrader(
        kis_client=FakePaperKIS(),
        hot_runner=FakeHotRunner(),
        report_dir=tmp_path,
        now_fn=_paper_session_now,
        required_bundle_id="BUNDLE-TEST",
        track_id="MAIN_BASELINE",
        policy_hash="policy123",
        max_orders_per_cycle=4,
        max_order_qty_per_order=1,
    )

    report = trader._base_report(tickers=["5930"], cycles=2, interval_sec=60)

    assert report["params"]["track_id"] == "MAIN_BASELINE"
    assert report["params"]["policy_hash"] == "policy123"
    assert report["params"]["required_bundle_id"] == "BUNDLE-TEST"
    assert report["params"]["tickers"] == ["005930"]
    assert report["params"]["execution_policy"] == {
        "max_orders_per_cycle": trader._max_orders_per_cycle,
        "max_order_qty_per_order": trader._max_order_qty_per_order,
        "allow_position_pyramiding": trader._allow_position_pyramiding,
        "min_holding_bars": trader._min_holding_bars,
        "rebalance_cooldown_bars": trader._rebalance_cooldown_bars,
        "policy_source": "service_policy_replay",
    }


def test_paper_auto_tops_up_short_kis_window_with_past_artifact_bars(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    _write_warmup_parquet(
        data_dir / "005930" / "bars_1m_20260521.parquet",
        "005930",
        rows=35,
    )
    trader = PaperAutoTrader(
        kis_client=ShortWindowPaperKIS(),
        hot_runner=FakeHotRunner(),
        report_dir=tmp_path,
        now_fn=_paper_session_now,
    )
    trader._cfg["historical_warmup_topup"] = {  # noqa: SLF001
        "enabled": True,
        "data_dir": str(data_dir),
        "max_files_per_ticker": 2,
    }

    bars = trader._fetch_recent_bars(  # noqa: SLF001
        ["005930"],
        asof="2026-05-26T10:00:00+09:00",
    )["005930"]

    assert len(bars) == 60
    assert bars[0]["ts_close"].startswith("2026-05-21")
    assert bars[-1]["ts_close"] == "2026-05-26T09:29:00+09:00"
    assert {bar["ticker"] for bar in bars} == {"005930"}
    metadata = trader._last_bar_fetch_metadata  # noqa: SLF001
    assert metadata["historical_topup_rows_by_ticker"] == {"005930": 30}
    assert metadata["live_rows_by_ticker"] == {"005930": 30}
    assert metadata["tickers"]["005930"]["topup_applied"] is True
    assert metadata["tickers"]["005930"]["files_used"] == [
        str(data_dir / "005930" / "bars_1m_20260521.parquet")
    ]


def test_paper_auto_topup_continues_when_latest_file_is_cutoff_filtered(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    _write_custom_warmup_parquet(
        data_dir / "005930" / "bars_1m_20260526.parquet",
        "005930",
        [f"2026-05-26T09:{30 + i:02d}:00+09:00" for i in range(25)],
    )
    _write_warmup_parquet(
        data_dir / "005930" / "bars_1m_20260521.parquet",
        "005930",
        rows=35,
    )
    trader = PaperAutoTrader(
        kis_client=ShortWindowPaperKIS(),
        hot_runner=FakeHotRunner(),
        report_dir=tmp_path,
        now_fn=_paper_session_now,
    )
    trader._cfg["historical_warmup_topup"] = {  # noqa: SLF001
        "enabled": True,
        "data_dir": str(data_dir),
        "max_files_per_ticker": 2,
    }

    bars = trader._fetch_recent_bars(  # noqa: SLF001
        ["005930"],
        asof="2026-05-26T10:00:00+09:00",
    )["005930"]

    assert len(bars) == 60
    assert bars[0]["ts_close"].startswith("2026-05-21")
    assert all("2026-05-26T09:3" not in bar["ts_close"] for bar in bars)
    metadata = trader._last_bar_fetch_metadata  # noqa: SLF001
    assert metadata["historical_topup_rows_by_ticker"] == {"005930": 30}
    assert len(metadata["tickers"]["005930"]["files_scanned"]) == 2
    assert metadata["tickers"]["005930"]["files_used"] == [
        str(data_dir / "005930" / "bars_1m_20260521.parquet")
    ]


def test_paper_auto_bar_limit_zero_returns_empty() -> None:
    bars = [
        {
            "ticker": "005930",
            "ts_close": "2026-05-26T09:00:00+09:00",
            "close": 70000,
        }
    ]

    assert PaperAutoTrader._dedupe_sort_limit_bars(bars, 0) == []  # noqa: SLF001


def test_paper_auto_default_hot_runner_injects_dual_source_loader(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    def sentinel_loader(_date: str | None = None) -> list[dict[str, Any]]:
        return []

    class FakeQuant:
        has_model = False
        model_metadata = None

        def __init__(self, dual_source_loader=None) -> None:
            captured["dual_source_loader"] = dual_source_loader

    class FakeRunner:
        def __init__(self, quant, ppo) -> None:
            self._quant = quant
            self._ppo = ppo

    monkeypatch.setattr(paper_auto_module, "load_latest_scores", sentinel_loader)
    monkeypatch.setattr(paper_auto_module, "QuantAgent", FakeQuant)
    monkeypatch.setattr(paper_auto_module, "HotRunner", FakeRunner)

    trader = PaperAutoTrader(
        kis_client=FakePaperKIS(),
        report_dir=tmp_path,
        now_fn=_paper_session_now,
    )

    assert captured["dual_source_loader"] is sentinel_loader
    assert trader._hot_runner._quant.has_model is False  # noqa: SLF001


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
    assert report["account_initial_state"]["position_count"] == 0
    assert cycle["account_state"]["position_count"] == 0
    assert cycle["hot_path_bar_readiness"]["status"] == "PASS"
    assert cycle["hot_path_bar_readiness"]["required_bars"] == 60
    assert cycle["hot_path_bar_readiness"]["rows_by_ticker"] == {"005930": 60}
    assert cycle["hot_path_bar_readiness"]["bar_warmup_topup"]["tickers"]["005930"] == {
        "raw_live_bar_count": 60,
        "live_bar_count": 60,
        "future_bar_filtered_count": 0,
        "future_bar_filtered_rows": [],
        "historical_topup_count": 0,
        "final_bar_count": 60,
        "topup_needed": False,
        "topup_applied": False,
        "topup_enabled": False,
        "cutoff_ts": None,
        "files_scanned": [],
        "files_used": [],
        "max_files_per_ticker": None,
        "reason": "live_window_sufficient",
    }
    assert cycle["broker_order_submitted"] is True
    assert cycle["submitted_order_deltas"][0]["ticker"] == "005930"
    assert cycle["execution"]["execution_report"]["execution_mode"] == "paper"
    assert cycle["order_history_verification"]["status"] == "PASS"
    assert cycle["order_history_verification"]["queries"][0]["matched_order_count"] == 1
    assert hot_runner.calls[0]["asof"] == _paper_session_now().isoformat()
    assert hot_runner.calls[0]["dependency_status"] == {
        "news": "skipped",
        "risk": "done",
        "quant": "done",
        "debate": "skipped",
    }


def test_paper_auto_fails_closed_when_bar_readiness_missing(tmp_path: Path) -> None:
    client = ShortWindowPaperKIS()
    hot_runner = FakeHotRunner()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=hot_runner,
        report_dir=tmp_path,
        now_fn=lambda: datetime(2026, 5, 26, 10, 0, tzinfo=_KST),
    )
    trader._cfg["historical_warmup_topup"] = {"enabled": False}  # noqa: SLF001

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    cycle = report["stages"]["cycles"]["items"][0]
    assert report["status"] == "FAIL"
    assert cycle["reason"] == "hot_path_bar_readiness"
    assert cycle["hot_path_bar_readiness"]["status"] == "FAIL"
    assert cycle["hot_path_bar_readiness"]["missing_bars_by_ticker"] == {"005930": 30}
    assert hot_runner.start_calls == 0
    assert hot_runner.calls == []
    assert client.orders == []


def test_paper_auto_fails_closed_when_live_bar_timestamp_invalid(tmp_path: Path) -> None:
    client = InvalidTimestampPaperKIS()
    hot_runner = FakeHotRunner()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=hot_runner,
        report_dir=tmp_path,
        now_fn=_paper_session_now,
    )
    trader._cfg["historical_warmup_topup"] = {"enabled": False}  # noqa: SLF001

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    cycle = report["stages"]["cycles"]["items"][0]
    assert report["status"] == "FAIL"
    assert cycle["reason"] == "hot_path_bar_readiness"
    assert cycle["hot_path_bar_readiness"]["invalid_rows"] == [
        {"ticker": "005930", "ts_close": "not-a-timestamp"}
    ]
    assert hot_runner.start_calls == 0
    assert client.orders == []


def test_paper_auto_filters_future_live_bar_and_fails_closed_if_still_short(
    tmp_path: Path,
) -> None:
    client = FutureTimestampPaperKIS()
    hot_runner = FakeHotRunner()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=hot_runner,
        report_dir=tmp_path,
        now_fn=lambda: datetime(2026, 5, 26, 10, 0, tzinfo=_KST),
    )
    trader._cfg["historical_warmup_topup"] = {"enabled": False}  # noqa: SLF001

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    cycle = report["stages"]["cycles"]["items"][0]
    assert report["status"] == "FAIL"
    assert cycle["reason"] == "hot_path_bar_readiness"
    assert cycle["hot_path_bar_readiness"]["future_rows"] == []
    assert cycle["hot_path_bar_readiness"]["missing_bars_by_ticker"] == {"005930": 1}
    topup_meta = cycle["hot_path_bar_readiness"]["bar_warmup_topup"]
    assert topup_meta["future_bar_filtered"] is True
    assert topup_meta["future_rows_kept_for_readiness"] is False
    assert topup_meta["tickers"]["005930"]["future_bar_filtered_count"] == 1
    assert hot_runner.start_calls == 0
    assert client.orders == []


def test_paper_auto_tops_up_after_filtering_future_live_bar(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_warmup_parquet(
        data_dir / "005930" / "bars_1m_20260521.parquet",
        "005930",
        rows=35,
    )
    client = ShortWindowFutureTimestampPaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeHotRunner(),
        report_dir=tmp_path,
        now_fn=lambda: datetime(2026, 5, 26, 10, 0, tzinfo=_KST),
    )
    trader._cfg["historical_warmup_topup"] = {  # noqa: SLF001
        "enabled": True,
        "data_dir": str(data_dir),
        "max_files_per_ticker": 2,
    }

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    cycle = report["stages"]["cycles"]["items"][0]
    readiness = cycle["hot_path_bar_readiness"]
    assert report["status"] == "PASS"
    assert readiness["status"] == "PASS"
    assert readiness["future_rows"] == []
    assert readiness["rows_by_ticker"] == {"005930": 60}
    topup_meta = readiness["bar_warmup_topup"]
    assert topup_meta["future_bar_filtered"] is True
    assert topup_meta["future_rows_kept_for_readiness"] is False
    assert topup_meta["live_rows_by_ticker"] == {"005930": 29}
    assert topup_meta["historical_topup_rows_by_ticker"] == {"005930": 31}
    assert topup_meta["tickers"]["005930"]["future_bar_filtered_count"] == 1
    assert client.orders


def test_paper_auto_shadow_only_does_not_submit_broker_order(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeHotRunner(qty=1),
        report_dir=tmp_path,
        now_fn=_paper_session_now,
        submit_orders=False,
    )

    report = trader.run(
        tickers=["005930"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    cycle = report["stages"]["cycles"]["items"][0]
    assert report["status"] == "PASS"
    assert report["runtime"]["broker_submit_enabled"] is False
    assert report["runtime"]["shadow_only"] is True
    assert cycle["reason"] == "shadow_only_no_broker_submit"
    assert cycle["broker_order_submitted"] is False
    assert cycle["quant_signal_guard"]["status"] == "PASS"
    assert cycle["quant_signal_guard"]["rankable"] is True
    assert cycle["would_submit_count"] == 1
    assert cycle["execution"]["status"] == "NOT_SUBMITTED_SHADOW"
    assert cycle["shadow_order_deltas"][0]["ticker"] == "005930"
    assert cycle["execution_order_deltas"][0]["ticker"] == "005930"
    assert client.orders == []


def test_paper_auto_fails_zero_score_no_order_run(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeZeroScoreHotRunner(),
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
    assert cycle["quant_signal_guard"]["status"] == "FAIL"
    assert cycle["quant_signal_guard"]["reason"] == "active_model_quant_scores_unavailable"
    assert client.orders == []


def test_paper_auto_fails_all_zero_active_score_before_broker(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeUnrankableScoreHotRunner(),
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
    assert cycle["reason"] == "quant_signal_readiness"
    assert cycle["quant_signal_guard"]["reason"] == "active_model_quant_scores_all_zero"
    assert cycle["quant_signal_guard"]["all_zero"] is True
    assert client.orders == []


def test_paper_auto_skips_flat_nonzero_active_scores_without_broker(
    tmp_path: Path,
) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeFlatNonZeroScoreHotRunner(),
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
    assert cycle["status"] == "SKIP"
    assert cycle["safe_skip"] is True
    assert cycle["reason"] == "quant_signal_readiness"
    assert cycle["quant_signal_guard"]["reason"] == "active_model_quant_scores_not_rankable"
    assert cycle["quant_signal_guard"]["all_zero"] is False
    assert cycle["broker_order_submitted"] is False
    assert cycle["execution"]["reason"] == "quant_scores_not_rankable_no_broker_submit"
    assert client.orders == []


def test_paper_auto_fails_zero_score_even_if_exit_orders_present(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeZeroScoreExitHotRunner(),
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
    assert cycle["reason"] == "quant_signal_readiness"
    assert cycle["quant_signal_guard"]["quant_mode"] == "warmup"
    assert cycle["hot_result"]["final_decision"]["order_deltas"][0]["side"] == "sell"
    assert client.orders == []


def test_paper_auto_fails_closed_when_quant_feature_blocked(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeBlockedQuantHotRunner(),
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
    assert cycle["reason"] == "quant_feature_readiness"
    assert cycle["hot_result"]["quant_output"]["mode"] == "blocked"
    assert client.orders == []


def test_paper_auto_preflight_blocks_missing_feature_before_broker_read(
    tmp_path: Path,
) -> None:
    hot_runner = FakeFeatureReadinessBlockedHotRunner()
    trader = PaperAutoTrader(
        kis_client=ExplodingReadKIS(),
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

    assert report["status"] == "FAIL"
    guard = report["stages"]["serving_feature_readiness"]
    assert guard["error_code"] == "REQUIRED_DUAL_SOURCE_FEATURE_MISSING"
    assert "cycles" not in report["stages"]
    assert hot_runner.start_called is False


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
    assert report["stages"]["cycles"]["stop_reason"] == "outside_market_session"
    assert client.orders == [{
        "ticker": "005930",
        "side": "buy",
        "qty": 1,
        "price": 70000.0,
        "order_type": "00",
    }]
    assert cycles[1]["status"] == "SKIP"
    assert cycles[1]["market_session_guard"]["reason"] == "outside_market_session"
    assert cycles[1]["execution"] is None


def test_paper_auto_interrupt_writes_safe_skip_cycle(tmp_path: Path) -> None:
    client = FakePaperKIS()

    def interrupt_sleep(_: float) -> None:
        raise KeyboardInterrupt

    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeHotRunner(qty=1),
        report_dir=tmp_path,
        now_fn=_paper_session_now,
        sleep_fn=interrupt_sleep,
    )

    report = trader.run(
        tickers=["005930"],
        cycles=2,
        interval_sec=60,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    cycles = report["stages"]["cycles"]["items"]
    assert report["status"] == "PASS"
    assert report["stages"]["cycles"]["stop_reason"] == "paper_auto_interrupted"
    assert [cycle["status"] for cycle in cycles] == ["PASS", "SKIP"]
    assert cycles[1]["reason"] == "paper_auto_interrupted"
    assert cycles[1]["broker_order_submitted"] is False


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
    assert trader._last_order_cycle_by_ticker == {"005930": 0}


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
        "qty": 1,
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
        "capped_qty": 1,
        "max_order_qty_per_order": 1,
    }]
    assert cycle["hot_result"]["final_decision"]["order_deltas"][0]["qty"] == 3
    assert cycle["submitted_order_deltas"][0]["qty"] == 1
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
    assert [order["qty"] for order in client.orders] == [1, 1, 1]
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
    assert [
        order["ticker"] for order in cycle["submitted_order_deltas"]
    ] == ["000660", "403870", "005930"]
    assert [
        order["qty"] for order in cycle["submitted_order_deltas"]
    ] == [1, 1, 1]


def test_paper_auto_blocks_buy_pyramiding_before_broker_submit(tmp_path: Path) -> None:
    client = FakePaperKISWithPosition(ticker="005930", qty=1)
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeHotRunner(qty=1),
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
    assert report["status"] == "PASS"
    assert cycle["status"] == "SKIP"
    assert cycle["order_guard"]["status"] == "SKIP"
    assert cycle["order_guard"]["reason"] == "runtime_service_policy_filtered_all_orders"
    applied = cycle["runtime_service_policy_applied"][0]
    assert applied["policy"] == "service_policy_replay"
    assert applied["dropped"][0]["reason"] == "position_pyramiding_disabled"
    assert applied["dropped"][0]["ticker"] == "005930"
    assert cycle["execution"] is None
    assert client.orders == []


def test_paper_auto_min_holding_blocks_rebalance_sell(tmp_path: Path) -> None:
    client = FakePaperKISWithPosition(ticker="005930", qty=2)
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeSellHotRunner(),
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
    assert report["status"] == "PASS"
    assert cycle["status"] == "SKIP"
    assert cycle["order_guard"]["status"] == "SKIP"
    applied = cycle["runtime_service_policy_applied"][0]
    assert applied["dropped"][0]["reason"] == "min_holding_bars_active"
    assert cycle["execution"] is None
    assert client.orders == []


def test_paper_auto_runtime_policy_allows_risk_reduce_sell(tmp_path: Path) -> None:
    client = FakePaperKISWithPosition(ticker="005930", qty=2)
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeSellHotRunner(reason="risk_reduce"),
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
    assert report["status"] == "PASS"
    assert cycle["order_guard"] == {"status": "PASS", "n_orders": 1}
    assert cycle["runtime_service_policy_applied"] == []
    assert cycle["submitted_order_deltas"][0]["side"] == "sell"
    assert client.orders == [{
        "ticker": "005930",
        "side": "sell",
        "qty": 1,
        "price": 70000.0,
        "order_type": "00",
    }]


def test_paper_auto_runtime_policy_drops_risk_reduce_without_position(
    tmp_path: Path,
) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeSellHotRunner(reason="risk_reduce"),
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
    assert cycle["status"] == "SKIP"
    assert cycle["order_guard"]["status"] == "SKIP"
    assert cycle["runtime_service_policy_applied"][0]["dropped"][0]["reason"] == (
        "risk_reduce_requires_held_position"
    )
    assert client.orders == []


def test_paper_auto_runtime_policy_drops_risk_reduce_qty_above_position(
    tmp_path: Path,
) -> None:
    client = FakePaperKISWithPosition(ticker="005930", qty=1)
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeSellHotRunner(reason="risk_reduce", qty=2),
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
    assert cycle["status"] == "SKIP"
    assert cycle["order_guard"]["status"] == "SKIP"
    assert cycle["runtime_service_policy_applied"][0]["dropped"][0]["reason"] == (
        "risk_reduce_qty_exceeds_held_qty"
    )
    assert client.orders == []


def test_paper_auto_preserves_fda_veto_when_risk_reduce_delta_survives_policy(
    tmp_path: Path,
) -> None:
    client = FakePaperKISWithPosition(ticker="005930", qty=2)
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeVetoRiskReduceHotRunner(),
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
    assert report["status"] == "PASS"
    assert cycle["status"] == "SKIP"
    assert cycle["order_guard"]["status"] == "SKIP"
    assert cycle["order_guard"]["reason"] == "fda_veto"
    assert cycle["order_guard"].get("runtime_service_policy_applied", []) == []
    assert cycle["runtime_service_policy_applied"] == []
    assert cycle["execution"] is None
    assert client.orders == []


def test_paper_auto_does_not_mask_fda_veto_as_runtime_filter_when_kept_delta_exists(
    tmp_path: Path,
) -> None:
    class TwoPositionKIS(FakePaperKIS):
        def get_balance(self) -> dict[str, Any]:
            return {
                "balance": {"cash": 1_000_000.0, "net_asset": 1_000_000.0},
                "positions": [
                    {
                        "ticker": "005930",
                        "qty": 2,
                        "available_qty": 2,
                        "current_price": 70000.0,
                    },
                    {
                        "ticker": "000660",
                        "qty": 1,
                        "available_qty": 1,
                        "current_price": 150000.0,
                    },
                ],
                "_mode": self.mode,
            }

    client = TwoPositionKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeVetoMixedRiskReduceHotRunner(),
        report_dir=tmp_path,
        now_fn=_paper_session_now,
    )
    trader._active_trade_universe = {"005930", "000660"}  # noqa: SLF001

    report = trader.run(
        tickers=["005930", "000660"],
        cycles=1,
        interval_sec=0,
        confirm_phrase=trader.confirm_start_phrase,
        write_report=False,
    )

    cycle = report["stages"]["cycles"]["items"][0]
    assert cycle["status"] == "SKIP"
    assert cycle["order_guard"]["reason"] == "fda_veto"
    applied = cycle["runtime_service_policy_applied"][0]
    assert applied["kept_count"] == 1
    assert applied["dropped"][0]["reason"] == "position_pyramiding_disabled"
    assert cycle["order_guard"]["runtime_service_policy_applied"] == cycle[
        "runtime_service_policy_applied"
    ]
    assert client.orders == []


def test_paper_auto_runtime_policy_blocks_cooldown_rebuy(tmp_path: Path) -> None:
    client = FakePaperKIS()
    trader = PaperAutoTrader(
        kis_client=client,
        hot_runner=FakeHotRunner(qty=1),
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

    first, second = report["stages"]["cycles"]["items"]
    assert first["status"] == "PASS"
    assert second["status"] == "SKIP"
    assert second["order_guard"]["status"] == "SKIP"
    applied = second["runtime_service_policy_applied"][0]
    assert applied["dropped"][0]["reason"] == "rebalance_cooldown_active"
    assert len(client.orders) == 1


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


def test_paper_auto_cli_allows_paper_rehearsal_scope_for_paper_only_gate(
    monkeypatch,
    capsys,
) -> None:
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
        return {
            "status": "BLOCKED",
            "blockers": ["05_backtest_real_candidate"],
            "stages": {
                "01_code_ssot": {"status": "PASS", "live_enabled": False},
                "02_real_data_readiness": {"status": "PASS"},
                "03_80_business_day_data": {"status": "PASS"},
                "04_lgbm_real_train": {"status": "PASS"},
                "06_paper_balance": {"status": "PASS"},
                "07_paper_reconciliation": {"status": "PASS"},
                "08_paper_probe_order": {"status": "PASS"},
                "09_ops_risk": {"status": "PASS"},
            },
        }

    class FakeTrader:
        def __init__(self, **kwargs: Any) -> None:
            calls["init"] = kwargs

        def run(self, **kwargs: Any) -> dict[str, Any]:
            calls["run"] = kwargs
            return {"status": "PASS", "stages": {}}

    monkeypatch.setattr(script.prelive_gate, "build_report", fake_build_report)
    monkeypatch.setattr(script, "PaperAutoTrader", FakeTrader)

    rc = script.main([
        "--prelive-scope",
        "paper-rehearsal",
        "--bundle-id",
        "BUNDLE-TEST",
        "--tickers",
        "005930",
        "--confirm-phrase",
        "PAPER_AUTO_OK",
        "--no-write-report",
    ])

    assert rc == 0
    assert calls["prelive"]["bundle_id"] == "BUNDLE-TEST"
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "PASS"
    assert out["prelive_scope"] == "paper-rehearsal"
    assert out["prelive_gate_status"] == "PASS"
    assert out["strict_prelive_gate_status"] == "BLOCKED"
    assert out["strict_prelive_gate_blockers"] == ["05_backtest_real_candidate"]


def test_paper_auto_cli_allows_read_only_bootstrap_for_stale_probe_gate(
    monkeypatch,
    capsys,
) -> None:
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
    monkeypatch.setattr(
        script,
        "_read_only_broker_bootstrap_gate",
        lambda: {
            "status": "PASS",
            "scope": "paper-rehearsal-read-only-bootstrap",
            "blockers": [],
            "account_state": {
                "cash_positive": True,
                "net_asset_positive": True,
                "pending_order_count": 0,
            },
        },
    )

    def fake_build_report(**kwargs: Any) -> dict[str, Any]:
        calls["prelive"] = kwargs
        return {
            "status": "BLOCKED",
            "blockers": [
                "05_backtest_real_candidate",
                "06_paper_balance",
                "07_paper_reconciliation",
                "08_paper_probe_order",
            ],
            "stages": {
                "01_code_ssot": {"status": "PASS", "live_enabled": False},
                "02_real_data_readiness": {"status": "PASS"},
                "03_80_business_day_data": {"status": "PASS"},
                "04_lgbm_real_train": {"status": "PASS"},
                "06_paper_balance": {"status": "BLOCKED"},
                "07_paper_reconciliation": {"status": "BLOCKED"},
                "08_paper_probe_order": {"status": "BLOCKED"},
                "09_ops_risk": {"status": "PASS"},
            },
        }

    class FakeTrader:
        def __init__(self, **kwargs: Any) -> None:
            calls["init"] = kwargs

        def run(self, **kwargs: Any) -> dict[str, Any]:
            calls["run"] = kwargs
            return {"status": "PASS", "stages": {}}

    monkeypatch.setattr(script.prelive_gate, "build_report", fake_build_report)
    monkeypatch.setattr(script, "PaperAutoTrader", FakeTrader)

    rc = script.main([
        "--prelive-scope",
        "paper-rehearsal",
        "--bundle-id",
        "BUNDLE-TEST",
        "--tickers",
        "005930",
        "--confirm-phrase",
        "PAPER_AUTO_OK",
        "--no-write-report",
    ])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "PASS"
    assert out["prelive_gate_status"] == "PASS"
    assert out["prelive_gate_blockers"] == []
    assert out["strict_prelive_gate_blockers"] == [
        "05_backtest_real_candidate",
        "06_paper_balance",
        "07_paper_reconciliation",
        "08_paper_probe_order",
    ]


def test_read_only_bootstrap_selects_reports_for_current_broker_fingerprint(
    monkeypatch,
    tmp_path,
) -> None:
    script = _load_paper_auto_trade_script()
    report_dir = tmp_path / "paper_trading"
    report_dir.mkdir()
    now = datetime.now(_KST).isoformat()

    def write_report(name: str, fingerprint: str, action: str) -> None:
        if action == "balance":
            stages = {
                "balance": {
                    "status": "PASS",
                    "balance": {"cash": 1_000_000, "net_asset": 1_000_000},
                    "position_count": 0,
                },
                "reconciliation": {"status": "PASS"},
            }
        else:
            stages = {
                "order_history": {
                    "status": "PASS",
                    "query": {
                        "ticker": "",
                        "side": "all",
                        "execution_filter": "unfilled",
                    },
                    "matched_order_count": 0,
                }
            }
        (report_dir / name).write_text(
            json.dumps(
                {
                    "status": "PASS",
                    "generated_at": now,
                    "evidence": {"broker_env_fingerprint": fingerprint},
                    "runtime": {"kis_mode": "virtual", "live_enabled": False},
                    "stages": stages,
                }
            ),
            encoding="utf-8",
        )

    write_report(
        "paper_trading_balance_reconciliation_20260528_093000.json",
        "current",
        "balance",
    )
    write_report(
        "paper_trading_order_history_20260528_093000.json",
        "current",
        "history",
    )
    write_report(
        "paper_trading_balance_reconciliation_20260528_093100.json",
        "other",
        "balance",
    )
    write_report(
        "paper_trading_order_history_20260528_093100.json",
        "other",
        "history",
    )

    monkeypatch.setattr(script, "_paper_trading_report_dir", lambda: report_dir)
    monkeypatch.setattr(script, "_current_broker_env_fingerprint", lambda: "current")
    monkeypatch.setattr(
        script,
        "config_load",
        lambda _file_name, _section=None: {"evidence_max_age_sec": 86400},
    )

    result = script._read_only_broker_bootstrap_gate()

    assert result["status"] == "PASS"
    assert result["broker_env_fingerprint"] == "current"
    assert result["balance_report_path"].endswith(
        "paper_trading_balance_reconciliation_20260528_093000.json"
    )
    assert result["pending_order_history_report_path"].endswith(
        "paper_trading_order_history_20260528_093000.json"
    )


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
        def __init__(
            self,
            *,
            required_bundle_id: str | None = None,
            report_dir: str | None = None,
            track_id: str = "",
            policy_hash: str = "",
            max_orders_per_cycle: int | None = None,
            max_order_qty_per_order: int | None = None,
            submit_orders: bool = True,
        ) -> None:
            calls["required_bundle_id"] = required_bundle_id
            calls["report_dir"] = report_dir
            calls["track_id"] = track_id
            calls["policy_hash"] = policy_hash
            calls["max_orders_per_cycle"] = max_orders_per_cycle
            calls["max_order_qty_per_order"] = max_order_qty_per_order
            calls["submit_orders"] = submit_orders

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
    assert calls["report_dir"] is None
    assert calls["track_id"] == ""
    assert calls["policy_hash"] == ""
    assert calls["max_orders_per_cycle"] is None
    assert calls["max_order_qty_per_order"] is None
    assert calls["submit_orders"] is True
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "PASS"


def test_paper_auto_cli_maps_kis_profile_and_policy_overrides(
    monkeypatch,
    capsys,
) -> None:
    script = _load_paper_auto_trade_script()
    calls: dict[str, Any] = {}

    monkeypatch.setattr(
        script,
        "config_load",
        lambda _file_name, _section=None: {
            "default_max_cycles": 1,
            "default_interval_sec": 0,
            "max_tickers": 1,
            "require_prelive_pass": False,
        },
    )
    for key, value in {
        "ACTIVE_SMALL_KIS_PAPER_APP_KEY": "app_key",
        "ACTIVE_SMALL_KIS_PAPER_APP_SECRET": "app_secret",
        "ACTIVE_SMALL_KIS_PAPER_ACCOUNT_NUMBER": "12345678",
        "ACTIVE_SMALL_KIS_PAPER_ACCOUNT_PRODUCT_CODE": "01",
    }.items():
        monkeypatch.setenv(key, value)

    class FakeTrader:
        def __init__(self, **kwargs: Any) -> None:
            calls["init"] = kwargs

        def run(self, **kwargs: Any) -> dict[str, Any]:
            calls["run"] = kwargs
            return {"status": "PASS", "stages": {}}

    monkeypatch.setattr(script, "PaperAutoTrader", FakeTrader)

    rc = script.main([
        "--tickers",
        "005930",
        "--bundle-id",
        "BUNDLE-TEST",
        "--confirm-phrase",
        "PAPER_AUTO_OK",
        "--kis-profile",
        "ACTIVE_SMALL",
        "--max-orders-per-cycle",
        "4",
        "--max-order-qty-per-order",
        "1",
        "--shadow-only",
        "--no-write-report",
    ])

    assert rc == 0
    assert calls["init"]["track_id"] == "ACTIVE_SMALL"
    assert calls["init"]["max_orders_per_cycle"] == 4
    assert calls["init"]["max_order_qty_per_order"] == 1
    assert calls["init"]["submit_orders"] is False
    assert script.os.environ["KIS_PAPER_ACCOUNT_NUMBER"] == "12345678"
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "PASS"

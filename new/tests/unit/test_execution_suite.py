"""S1-6 + S1-7 + S1-9 통합 단위 테스트.

- ExecutionGateway (C10) mock 모드
- KillSwitch trigger/reset/check
- AuditLogger JSONL append/read
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.execution.execution_gateway import ExecutionGateway
from src.execution.kill_switch import (
    InvalidOperatorTokenError,
    KillSwitch,
)
from src.ops.audit_logger import AuditLogger


# ====================================================================== #
# KillSwitch
# ====================================================================== #


def test_kill_switch_init_loads_config() -> None:
    ks = KillSwitch()
    assert ks.threshold == pytest.approx(0.05)   # risk_config.yaml 기본값
    assert ks.action == "emergency_halt"
    assert ks.is_active() is False


def test_kill_switch_trigger_activates() -> None:
    ks = KillSwitch()
    ks.trigger("manual test")
    assert ks.is_active() is True
    assert ks.status["reason"] == "manual test"
    assert ks.status["triggered_at"] is not None


def test_kill_switch_check_daily_pnl_triggers() -> None:
    ks = KillSwitch()
    # daily_pnl -6% < -threshold(-5%)
    is_active = ks.check_daily_pnl(-0.06)
    assert is_active is True
    assert ks.is_active() is True
    # Reason에 수치 포함
    assert "daily_pnl" in ks.status["reason"]


def test_kill_switch_check_daily_pnl_below_threshold() -> None:
    ks = KillSwitch()
    is_active = ks.check_daily_pnl(-0.03)   # -3% > -5%
    assert is_active is False
    assert ks.is_active() is False


def test_kill_switch_reset_requires_operator_token() -> None:
    ks = KillSwitch()
    ks.trigger("test")
    with pytest.raises(InvalidOperatorTokenError):
        ks.reset("WRONG_TOKEN")
    assert ks.is_active() is True


def test_kill_switch_reset_with_valid_token() -> None:
    ks = KillSwitch()
    ks.trigger("test")
    ks.reset("OPERATOR_RESET")
    assert ks.is_active() is False
    assert ks.status["reason"] is None


def test_kill_switch_trigger_idempotent() -> None:
    ks = KillSwitch()
    ks.trigger("first")
    ks.trigger("second")
    # 첫 reason 유지
    assert ks.status["reason"] == "first"


# ====================================================================== #
# AuditLogger
# ====================================================================== #


def test_audit_logger_appends_jsonl(tmp_path: Path) -> None:
    log = AuditLogger(log_path=tmp_path / "audit.jsonl")
    log.log("test_event", {"a": 1, "b": "hello"})
    log.log("another_event", {"x": 42})

    assert log.count() == 2
    assert log.count_from_file() == 2

    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 2
    rec1 = json.loads(lines[0])
    assert rec1["event_type"] == "test_event"
    assert rec1["payload"]["a"] == 1
    assert "ts" in rec1


def test_audit_logger_read_recent(tmp_path: Path) -> None:
    log = AuditLogger(log_path=tmp_path / "audit.jsonl")
    for i in range(5):
        log.log("evt", {"i": i})
    recent = log.read_recent(n=3)
    assert len(recent) == 3
    assert [r["payload"]["i"] for r in recent] == [2, 3, 4]


def test_audit_logger_empty_file(tmp_path: Path) -> None:
    log = AuditLogger(log_path=tmp_path / "empty.jsonl")
    assert log.read_recent() == []
    assert log.count_from_file() == 0


def test_audit_logger_clear(tmp_path: Path) -> None:
    log = AuditLogger(log_path=tmp_path / "audit.jsonl")
    log.log("evt", {"x": 1})
    assert log.count_from_file() == 1
    log.clear()
    assert log.count_from_file() == 0


# ====================================================================== #
# ExecutionGateway
# ====================================================================== #


@pytest.fixture
def gateway(tmp_path: Path) -> ExecutionGateway:
    return ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
    )


def _final_decision(approved: bool, order_deltas: list[dict] | None = None) -> dict:
    return {
        "decision_id": "DEC-20260420-001",
        "approved": approved,
        "target_weights": {"005930": 0.1} if approved else {},
        "order_deltas": order_deltas or [],
        "veto_reason": None if approved else "some reason",
        "reason_code": "NORMAL_APPROVE" if approved else "RISK_FAST_TRIGGER",
    }


def _execution_universe_config() -> dict:
    return {
        "sectors": {
            "반도체": {
                "status": "confirmed",
                "stocks": [
                    {"ticker": "005930", "status": "active"},
                    {"ticker": "000660", "status": "active"},
                ],
            },
            "금융": {
                "status": "confirmed_pending_data",
                "stocks": [
                    {"ticker": "105560", "status": "pending_data"},
                ],
            },
        },
    }


def _patch_execution_config(monkeypatch, mode: str, live_enabled: bool = False) -> None:
    def fake_config_load(file_name: str, section: str):
        if file_name == "universe_config.yaml":
            return _execution_universe_config()
        if section == "execution":
            return {"mode": mode, "live_enabled": live_enabled}
        if section == "execution_cost_model":
            return {"components": {"slippage_bps": 10}}
        return {}

    monkeypatch.setattr(
        "src.execution.execution_gateway.config_load",
        fake_config_load,
    )


def test_execute_mock_filled(gateway: ExecutionGateway) -> None:
    fd = _final_decision(
        approved=True,
        order_deltas=[
            {"ticker": "005930", "side": "buy", "qty": 10, "reason": "rebalance", "price": 70000.0},
        ],
    )
    result = gateway.execute(fd)
    report = result["execution_report"]
    assert report["status"] == "filled"
    assert report["execution_mode"] == "mock"
    assert len(report["fills"]) == 1
    assert report["fills"][0]["qty"] == 10
    assert report["order_plan_id"].startswith("OP-")
    assert result["final_decision_ref"] == "DEC-20260420-001"


def test_execute_veto_rejected(gateway: ExecutionGateway) -> None:
    fd = _final_decision(approved=False)
    result = gateway.execute(fd)
    report = result["execution_report"]
    assert report["status"] == "rejected"
    assert "veto" in report["rejection_reason"].lower()


def test_execute_kill_switch_active_rejected(tmp_path: Path) -> None:
    ks = KillSwitch()
    ks.trigger("test halt")
    gw = ExecutionGateway(
        kill_switch=ks,
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 10, "price": 70000.0}],
    )
    result = gw.execute(fd)
    assert result["execution_report"]["status"] == "rejected"
    assert "kill_switch" in result["execution_report"]["rejection_reason"]


def test_execute_audit_log_records(gateway: ExecutionGateway, tmp_path: Path) -> None:
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 10, "price": 70000.0}],
    )
    gateway.execute(fd)
    # audit log에 기록됨
    records = gateway._audit_logger.read_recent(n=10)
    assert len(records) == 1
    assert records[0]["event_type"] == "execution_success"


def test_execute_mode_mock_default(gateway: ExecutionGateway) -> None:
    assert gateway.mode == "mock"
    assert gateway.live_enabled is False


def test_execute_estimated_cost_calculation(gateway: ExecutionGateway) -> None:
    fd = _final_decision(
        approved=True,
        order_deltas=[
            {"ticker": "005930", "side": "buy", "qty": 10, "price": 70000.0},
            {"ticker": "000660", "side": "sell", "qty": 5, "price": 50000.0},
        ],
    )
    result = gateway.execute(fd)
    # estimated_cost = 10 * 70000 + 5 * 50000 = 950,000
    assert result["execution_report"]["estimated_cost"] == pytest.approx(950_000.0)


def test_execute_no_audit_logger_works(tmp_path: Path) -> None:
    """audit_logger=None 인 경우에도 동작."""
    gw = ExecutionGateway(kill_switch=KillSwitch(), audit_logger=None)
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 10, "price": 70000.0}],
    )
    result = gw.execute(fd)
    assert result["execution_report"]["status"] == "filled"


def test_execute_empty_order_deltas_rejected(gateway: ExecutionGateway) -> None:
    """approved=True여도 주문 후보가 없으면 filled evidence로 과장하지 않는다."""
    fd = _final_decision(approved=True, order_deltas=[])
    result = gateway.execute(fd)
    assert result["execution_report"]["status"] == "rejected"
    assert result["execution_report"]["rejection_reason"] == "no_order_deltas"
    assert result["execution_report"]["fills"] == []
    assert result["n_fills"] == 0


def test_execute_paper_submits_via_injected_kis_client(monkeypatch, tmp_path: Path) -> None:
    """paper 모드는 NotImplemented가 아니라 주입된 KIS client로 주문 제출한다."""
    _patch_execution_config(monkeypatch, mode="paper")

    class FakeKISClient:
        mode = "virtual"

        def __init__(self) -> None:
            self.calls: list[tuple[str, str, int, float]] = []

        def submit_order(self, ticker: str, side: str, qty: int, price: float) -> dict:
            self.calls.append((ticker, side, qty, price))
            return {
                "status": "submitted",
                "order_id": "OD-001",
                "price": price,
            }

    client = FakeKISClient()
    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=client,
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "5930", "side": "buy", "qty": 10, "price": 70000.0}],
    )

    result = gw.execute(fd)
    report = result["execution_report"]

    assert report["status"] == "submitted"
    assert report["execution_mode"] == "paper"
    assert report["fills"][0]["broker_order_id"] == "OD-001"
    assert client.calls == [("005930", "buy", 10, 70000.0)]


def test_execute_paper_rejects_pending_universe_ticker_without_submit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """paper 주문은 pending_data 종목을 broker submit 전에 차단한다."""
    _patch_execution_config(monkeypatch, mode="paper")

    class FakeKISClient:
        mode = "virtual"

        def __init__(self) -> None:
            self.called = False

        def submit_order(self, ticker: str, side: str, qty: int, price: float) -> dict:
            self.called = True
            raise AssertionError("pending_data ticker must not be submitted")

    client = FakeKISClient()
    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=client,
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "105560", "side": "buy", "qty": 1, "price": 70000.0}],
    )

    result = gw.execute(fd)
    report = result["execution_report"]

    assert report["status"] == "rejected"
    assert "order_ticker_not_active_universe: 105560" in report["rejection_reason"]
    assert client.called is False


def test_execute_paper_rejects_kis_error_without_status(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _patch_execution_config(monkeypatch, mode="paper")

    class FakeKISClient:
        mode = "virtual"

        def submit_order(self, ticker: str, side: str, qty: int, price: float) -> dict:
            return {"rt_cd": "1", "msg_cd": "EGW00001", "msg1": "주문거부"}

    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=FakeKISClient(),
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 1, "price": 70000.0}],
    )

    result = gw.execute(fd)
    report = result["execution_report"]

    assert report["status"] == "rejected"
    assert report["fills"] == []
    assert report["rejections"][0]["reason"] == "broker_rt_cd_1"


def test_execute_paper_accepts_kis_success_code_with_order_id(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _patch_execution_config(monkeypatch, mode="paper")

    class FakeKISClient:
        mode = "virtual"

        def submit_order(self, ticker: str, side: str, qty: int, price: float) -> dict:
            return {"rt_cd": "0", "odno": "OD-RTCD", "price": price}

    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=FakeKISClient(),
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 1, "price": 70000.0}],
    )

    result = gw.execute(fd)
    report = result["execution_report"]

    assert report["status"] == "submitted"
    assert report["fills"][0]["broker_order_id"] == "OD-RTCD"


def test_execute_paper_requires_audit_logger_before_submit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _patch_execution_config(monkeypatch, mode="paper")

    class FakeKISClient:
        mode = "virtual"

        def __init__(self) -> None:
            self.called = False

        def submit_order(self, ticker: str, side: str, qty: int, price: float) -> dict:
            self.called = True
            raise AssertionError("broker submit must require audit sink")

    client = FakeKISClient()
    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=None,
        kis_client=client,
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 1, "price": 70000.0}],
    )

    result = gw.execute(fd)

    assert result["execution_report"]["status"] == "rejected"
    assert "audit_logger_missing" in result["execution_report"]["rejection_reason"]
    assert client.called is False


def test_execute_paper_requires_kis_client(monkeypatch, tmp_path: Path) -> None:
    """paper/live는 명시적인 broker client 없이 조용히 mock으로 흐르지 않는다."""
    _patch_execution_config(monkeypatch, mode="paper")
    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=None,
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 10, "price": 70000.0}],
    )

    result = gw.execute(fd)
    report = result["execution_report"]

    assert report["status"] == "rejected"
    assert "broker_dependency_missing" in report["rejection_reason"]
    assert report["execution_mode"] == "paper"


def test_execute_paper_rejects_client_without_submit_order(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _patch_execution_config(monkeypatch, mode="paper")

    class NoSubmitKISClient:
        mode = "virtual"

    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=NoSubmitKISClient(),
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 10, "price": 70000.0}],
    )

    result = gw.execute(fd)
    report = result["execution_report"]

    assert report["status"] == "rejected"
    assert "broker_dependency_missing" in report["rejection_reason"]


def test_execution_gateway_reads_nested_slippage_bps(monkeypatch) -> None:
    def fake_config_load(file_name: str, section: str):
        if section == "execution":
            return {"mode": "mock", "live_enabled": False}
        if section == "execution_cost_model":
            return {"components": {"slippage_bps": 25}}
        return {}

    monkeypatch.setattr(
        "src.execution.execution_gateway.config_load",
        fake_config_load,
    )

    gw = ExecutionGateway()

    assert gw._slippage_bps == 25


def test_execute_paper_rejects_malformed_numeric_fields(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """paper/live broker 경로도 malformed qty/price에서 crash 대신 rejection을 남긴다."""
    _patch_execution_config(monkeypatch, mode="paper")

    class FakeKISClient:
        mode = "virtual"

        def submit_order(self, ticker: str, side: str, qty: int) -> dict:
            raise AssertionError("invalid broker order must not be submitted")

    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=FakeKISClient(),
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{
            "ticker": "005930",
            "side": "buy",
            "qty": "many",
            "price": "market-ish",
        }],
    )

    result = gw.execute(fd)
    report = result["execution_report"]

    assert report["status"] == "rejected"
    assert "invalid_order_deltas" in report["rejection_reason"]
    assert "invalid_order_delta" in report["rejection_reason"]


def test_execute_live_requires_live_enabled(monkeypatch, tmp_path: Path) -> None:
    """live_enabled=false이면 KIS client가 있어도 C10 rejected report로 차단한다."""
    _patch_execution_config(monkeypatch, mode="live", live_enabled=False)
    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=object(),
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 10, "price": 70000.0}],
    )

    result = gw.execute(fd)
    report = result["execution_report"]
    assert report["status"] == "rejected"
    assert report["execution_mode"] == "live"
    assert "live_enabled=false" in report["rejection_reason"]


def test_execute_live_treats_string_false_override_as_disabled(monkeypatch, tmp_path: Path) -> None:
    """operator/BE 입력의 문자열 false도 실계좌 enable로 해석하지 않는다."""
    _patch_execution_config(monkeypatch, mode="live", live_enabled=False)

    class BrokerClient:
        mode = "real"

        def submit_order(self, ticker: str, side: str, qty: int) -> dict:
            raise AssertionError("live broker must not be called")

    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=BrokerClient(),
        live_enabled_override="false",  # type: ignore[arg-type]
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 1, "price": 70000.0}],
    )

    result = gw.execute(fd)

    report = result["execution_report"]
    assert gw.live_enabled is False
    assert report["status"] == "rejected"
    assert "live_enabled=false" in report["rejection_reason"]


def _live_approval_proof(**overrides) -> dict:
    proof = {
        "prelive_gate": "PASS",
        "deploy_quality": "PASS",
        "broker_evidence": {
            "status": "PASS",
            "external_kis_api": True,
            "bundle_match": True,
            "paper_auto_cycle_history_matched": True,
            "stage_statuses": {
                "paper_auto_cycle": "PASS",
                "balance_reconciliation": "PASS",
                "probe_order": "PASS",
                "order_history_requery": "PASS",
            },
        },
        "production_registry": {"active_version": "MODEL-LIVE-1"},
        "live_trading_allowed": True,
    }
    proof.update(overrides)
    return proof


def test_execute_live_enabled_real_broker_requires_live_approval_proof(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """live_enabled=True만으로는 실계좌 broker 호출까지 갈 수 없다."""
    _patch_execution_config(monkeypatch, mode="live", live_enabled=True)

    class RealBrokerClient:
        mode = "real"

        def __init__(self) -> None:
            self.called = False

        def submit_order(self, ticker: str, side: str, qty: int, price: float) -> dict:
            self.called = True
            raise AssertionError("live broker must require approval proof")

    client = RealBrokerClient()
    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=client,
        mode_override="live",
        live_enabled_override=True,
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 1, "price": 70000.0}],
    )

    result = gw.execute(fd)
    report = result["execution_report"]

    assert report["status"] == "rejected"
    assert "live_approval_missing" in report["rejection_reason"]
    assert client.called is False


def test_execute_live_rejects_incomplete_live_approval_proof(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _patch_execution_config(monkeypatch, mode="live", live_enabled=True)

    class RealBrokerClient:
        mode = "real"

        def __init__(self) -> None:
            self.called = False

        def submit_order(self, ticker: str, side: str, qty: int, price: float) -> dict:
            self.called = True
            raise AssertionError("live broker must not be called")

    client = RealBrokerClient()
    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=client,
        mode_override="live",
        live_enabled_override=True,
        live_approval_proof=_live_approval_proof(live_trading_allowed=False),
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 1, "price": 70000.0}],
    )

    result = gw.execute(fd)
    report = result["execution_report"]

    assert report["status"] == "rejected"
    assert "live_trading_allowed" in report["rejection_reason"]
    assert client.called is False


def test_execute_live_with_complete_approval_proof_submits(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """완전한 proof가 있을 때만 live broker까지 도달한다."""
    _patch_execution_config(monkeypatch, mode="live", live_enabled=True)

    class RealBrokerClient:
        mode = "real"

        def __init__(self) -> None:
            self.calls: list[tuple[str, str, int, float]] = []

        def submit_order(self, ticker: str, side: str, qty: int, price: float) -> dict:
            self.calls.append((ticker, side, qty, price))
            return {"status": "submitted", "order_id": "OD-LIVE", "price": price}

    client = RealBrokerClient()
    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=client,
        mode_override="live",
        live_enabled_override=True,
        live_approval_proof=_live_approval_proof(),
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 1, "price": 70000.0}],
    )

    result = gw.execute(fd)
    report = result["execution_report"]

    assert report["status"] == "submitted"
    assert report["execution_mode"] == "live"
    assert report["fills"][0]["broker_order_id"] == "OD-LIVE"
    assert client.calls == [("005930", "buy", 1, 70000.0)]


def test_execute_live_rejects_unknown_universe_ticker_before_submit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """live 주문도 active universe 밖 종목은 approval proof가 있어도 broker 호출 금지."""
    _patch_execution_config(monkeypatch, mode="live", live_enabled=True)

    class RealBrokerClient:
        mode = "real"

        def __init__(self) -> None:
            self.called = False

        def submit_order(self, ticker: str, side: str, qty: int, price: float) -> dict:
            self.called = True
            raise AssertionError("unknown ticker must not be submitted")

    client = RealBrokerClient()
    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=client,
        mode_override="live",
        live_enabled_override=True,
        live_approval_proof=_live_approval_proof(),
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "999999", "side": "buy", "qty": 1, "price": 70000.0}],
    )

    result = gw.execute(fd)
    report = result["execution_report"]

    assert report["status"] == "rejected"
    assert "order_ticker_not_active_universe: 999999" in report["rejection_reason"]
    assert client.called is False


def test_execute_live_rejects_missing_kill_switch_even_with_complete_proof(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _patch_execution_config(monkeypatch, mode="live", live_enabled=True)

    class RealBrokerClient:
        mode = "real"

        def submit_order(self, ticker: str, side: str, qty: int, price: float) -> dict:
            raise AssertionError("live broker must require kill switch")

    gw = ExecutionGateway(
        kill_switch=None,
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=RealBrokerClient(),
        mode_override="live",
        live_enabled_override=True,
        live_approval_proof=_live_approval_proof(),
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 1, "price": 70000.0}],
    )

    result = gw.execute(fd)

    assert result["execution_report"]["status"] == "rejected"
    assert "kill_switch_missing" in result["execution_report"]["rejection_reason"]


def test_execute_live_rejects_top_level_active_version_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _patch_execution_config(monkeypatch, mode="live", live_enabled=True)

    class RealBrokerClient:
        mode = "real"

        def submit_order(self, ticker: str, side: str, qty: int, price: float) -> dict:
            raise AssertionError("top-level active_version must not authorize live")

    proof = _live_approval_proof(
        production_registry={"active_version": None},
        active_version="MODEL-LIVE-1",
    )
    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=RealBrokerClient(),
        mode_override="live",
        live_enabled_override=True,
        live_approval_proof=proof,
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 1, "price": 70000.0}],
    )

    result = gw.execute(fd)

    assert result["execution_report"]["status"] == "rejected"
    assert "production_registry.active_version" in result["execution_report"]["rejection_reason"]


def test_execute_live_rejects_bare_string_broker_evidence_pass(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _patch_execution_config(monkeypatch, mode="live", live_enabled=True)

    class RealBrokerClient:
        mode = "real"

        def __init__(self) -> None:
            self.called = False

        def submit_order(self, ticker: str, side: str, qty: int, price: float) -> dict:
            self.called = True
            raise AssertionError("bare string broker_evidence must not submit")

    client = RealBrokerClient()
    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=client,
        mode_override="live",
        live_enabled_override=True,
        live_approval_proof=_live_approval_proof(broker_evidence="PASS"),
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 1, "price": 70000.0}],
    )

    result = gw.execute(fd)

    assert result["execution_report"]["status"] == "rejected"
    assert "broker_evidence" in result["execution_report"]["rejection_reason"]
    assert client.called is False


def test_execute_live_rejects_unmatched_broker_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _patch_execution_config(monkeypatch, mode="live", live_enabled=True)

    class RealBrokerClient:
        mode = "real"

        def submit_order(self, ticker: str, side: str, qty: int, price: float) -> dict:
            raise AssertionError("unmatched broker_evidence must not submit")

    proof = _live_approval_proof()
    proof["broker_evidence"] = {
        **proof["broker_evidence"],
        "bundle_match": False,
    }
    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=RealBrokerClient(),
        mode_override="live",
        live_enabled_override=True,
        live_approval_proof=proof,
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 1, "price": 70000.0}],
    )

    result = gw.execute(fd)

    assert result["execution_report"]["status"] == "rejected"
    assert "broker_evidence" in result["execution_report"]["rejection_reason"]


def test_execute_rejects_fractional_qty_without_submit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _patch_execution_config(monkeypatch, mode="paper")

    class PaperKISClient:
        mode = "virtual"

        def submit_order(self, ticker: str, side: str, qty: int, price: float) -> dict:
            raise AssertionError("fractional qty must not be truncated and submitted")

    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=PaperKISClient(),
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": "1.9", "price": 70000.0}],
    )

    result = gw.execute(fd)

    assert result["execution_report"]["status"] == "rejected"
    assert "invalid_order_deltas" in result["execution_report"]["rejection_reason"]


def test_execute_rejects_non_whitelisted_order_type_without_submit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _patch_execution_config(monkeypatch, mode="paper")

    class PaperKISClient:
        mode = "virtual"

        def submit_order(self, ticker: str, side: str, qty: int, price: float) -> dict:
            raise AssertionError("non-whitelisted order_type must not submit")

    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=PaperKISClient(),
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[
            {
                "ticker": "005930",
                "side": "buy",
                "qty": 1,
                "price": 70000.0,
                "order_type": "02",
            }
        ],
    )

    result = gw.execute(fd)

    assert result["execution_report"]["status"] == "rejected"
    assert "invalid_order_deltas" in result["execution_report"]["rejection_reason"]


def test_rejected_execution_does_not_emit_success_audit(tmp_path: Path) -> None:
    audit = AuditLogger(log_path=tmp_path / "exec.jsonl")
    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=audit,
    )
    fd = _final_decision(
        approved=False,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 1, "price": 70000.0}],
    )

    gw.execute(fd)

    records = audit.read_recent(n=10)
    assert [record["event_type"] for record in records] == ["execution_rejected"]


def test_execute_broker_partial_fill_reports_rejections(monkeypatch, tmp_path: Path) -> None:
    """broker 제출 일부 실패는 C10 partial_filled와 rejections로 남긴다."""
    _patch_execution_config(monkeypatch, mode="paper")

    class PartialKISClient:
        mode = "virtual"

        def submit_order(self, ticker: str, side: str, qty: int) -> dict:
            if ticker == "000660":
                return {"status": "rejected", "message": "insufficient cash"}
            return {"status": "filled", "order_no": "OD-OK", "avg_fill_price": 70000.0}

    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=PartialKISClient(),
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[
            {"ticker": "005930", "side": "buy", "qty": 10, "price": 70000.0},
            {"ticker": "000660", "side": "buy", "qty": 3, "price": 120000.0},
        ],
    )

    result = gw.execute(fd)
    report = result["execution_report"]

    assert report["status"] == "partial_filled"
    assert report["fills"][0]["broker_order_id"] == "OD-OK"
    assert report["rejections"][0]["ticker"] == "000660"
    assert result["feedback_record"]["lesson_stub"] == "1 broker submission(s) rejected"


def test_execute_mode_override_routes_to_paper_and_passes_order_type(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """config mock 상태에서도 paper smoke는 명시 override로 C10 paper 경로를 검증한다."""
    _patch_execution_config(monkeypatch, mode="mock")

    class PaperKISClient:
        mode = "virtual"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def submit_order(
            self,
            ticker: str,
            side: str,
            qty: int,
            price: float,
            order_type: str,
        ) -> dict:
            self.calls.append({
                "ticker": ticker,
                "side": side,
                "qty": qty,
                "price": price,
                "order_type": order_type,
            })
            return {"status": "submitted", "order_id": "OD-PAPER", "price": price}

    client = PaperKISClient()
    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=client,
        mode_override="paper",
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{
            "ticker": "005930",
            "side": "buy",
            "qty": 1,
            "price": 70000.0,
            "order_type": "00",
        }],
    )

    result = gw.execute(fd)

    assert result["execution_report"]["execution_mode"] == "paper"
    assert client.calls == [{
        "ticker": "005930",
        "side": "buy",
        "qty": 1,
        "price": 70000.0,
        "order_type": "00",
    }]


def test_execute_paper_rejects_real_mode_kis_client(monkeypatch, tmp_path: Path) -> None:
    """paper 경로는 real KIS client를 주입받아도 broker submit 전에 차단한다."""
    _patch_execution_config(monkeypatch, mode="paper")

    class RealKISClient:
        mode = "real"

        def __init__(self) -> None:
            self.called = False

        def submit_order(self, ticker: str, side: str, qty: int, price: float) -> dict:
            self.called = True
            raise AssertionError("real broker must not be called from paper mode")

    client = RealKISClient()
    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=client,
        mode_override="paper",
    )
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 1, "price": 70000.0}],
    )

    result = gw.execute(fd)
    report = result["execution_report"]

    assert report["status"] == "rejected"
    assert "paper_mode_requires_virtual_kis_client" in report["rejection_reason"]
    assert client.called is False


def test_execute_string_false_approved_is_rejected(gateway: ExecutionGateway) -> None:
    """BE/direct 입력의 approved='false'는 C9 bool 위반으로 reject한다."""
    fd = _final_decision(
        approved="false",
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 1, "price": 70000.0}],
    )

    result = gateway.execute(fd)

    assert result["execution_report"]["status"] == "rejected"
    assert result["execution_report"]["rejection_reason"] == "approved_must_be_bool"


def test_execute_string_yes_approved_is_rejected_without_submit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """C10은 approved='yes' 같은 bool-like 문자열을 FDA 승인으로 승격하지 않는다."""
    _patch_execution_config(monkeypatch, mode="paper")

    class PaperKISClient:
        mode = "virtual"

        def submit_order(self, ticker: str, side: str, qty: int, price: float) -> dict:
            raise AssertionError("approved string must not submit")

    gw = ExecutionGateway(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "exec.jsonl"),
        kis_client=PaperKISClient(),
    )
    fd = _final_decision(
        approved="yes",
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 1, "price": 70000.0}],
    )

    result = gw.execute(fd)

    assert result["execution_report"]["status"] == "rejected"
    assert result["execution_report"]["rejection_reason"] == "approved_must_be_bool"


def test_execute_rejects_non_list_order_deltas(gateway: ExecutionGateway) -> None:
    fd = _final_decision(approved=True, order_deltas={"ticker": "005930"})

    result = gateway.execute(fd)

    assert result["execution_report"]["status"] == "rejected"
    assert "order_deltas_must_be_list" in result["execution_report"]["rejection_reason"]


def test_execute_mock_rejects_invalid_order_delta(gateway: ExecutionGateway) -> None:
    fd = _final_decision(
        approved=True,
        order_deltas=[{"ticker": "ABC", "side": "buy", "qty": 1, "price": 70000.0}],
    )

    result = gateway.execute(fd)

    assert result["execution_report"]["status"] == "rejected"
    assert "invalid_order_delta" in result["execution_report"]["rejection_reason"]

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


def test_execute_empty_order_deltas_still_filled(gateway: ExecutionGateway) -> None:
    """order_deltas 비어있어도 approved=True면 filled 상태."""
    fd = _final_decision(approved=True, order_deltas=[])
    result = gateway.execute(fd)
    assert result["execution_report"]["status"] == "filled"
    assert result["execution_report"]["fills"] == []
    assert result["n_fills"] == 0

"""S1-7 SafetyGuards + S1-9 OpsMonitor unit tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.execution.kill_switch import KillSwitch
from src.ops.audit_logger import AuditLogger
from src.ops.monitor import OpsMonitor
from src.ops.safety_guards import SafetyGuards


# ====================================================================== #
# SafetyGuards
# ====================================================================== #


@pytest.fixture
def guards(tmp_path: Path) -> SafetyGuards:
    return SafetyGuards(
        auth_manager=None,
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "audit.jsonl"),
    )


def test_sanity_check_detects_large_drop(guards: SafetyGuards) -> None:
    curr = {"005930": 45000.0}   # -10% from 50000
    prev = {"005930": 50000.0}
    guards.check_price_sanity(curr, prev)  # boundary case probe
    # 10% 변동 = threshold와 같음. 더 큰 변화에서 감지되는지 확인
    curr2 = {"005930": 44000.0}  # -12%
    anomalies2 = guards.check_price_sanity(curr2, prev)
    assert len(anomalies2) == 1
    assert anomalies2[0]["ticker"] == "005930"


def test_sanity_check_normal_no_anomaly(guards: SafetyGuards) -> None:
    curr = {"005930": 50500.0}   # +1%
    prev = {"005930": 50000.0}
    assert guards.check_price_sanity(curr, prev) == []


def test_sanity_check_zero_prev_skipped(guards: SafetyGuards) -> None:
    curr = {"005930": 50000.0}
    prev = {"005930": 0.0}
    assert guards.check_price_sanity(curr, prev) == []


def test_reconcile_positions_ok(guards: SafetyGuards) -> None:
    system = [{"ticker": "005930", "qty": 10}, {"ticker": "000660", "qty": 20}]
    kis = [{"ticker": "005930", "qty": 10}, {"ticker": "000660", "qty": 20}]
    result = guards.reconcile_positions(system, kis)
    assert result["ok"] is True
    assert result["n_mismatches"] == 0


def test_reconcile_positions_mismatch(guards: SafetyGuards) -> None:
    system = [{"ticker": "005930", "qty": 10}]
    kis = [{"ticker": "005930", "qty": 5}]
    result = guards.reconcile_positions(system, kis)
    assert result["ok"] is False
    assert result["mismatches"][0]["diff"] == 5


def test_reconcile_detects_missing_ticker(guards: SafetyGuards) -> None:
    system = [{"ticker": "005930", "qty": 10}]
    kis = []
    result = guards.reconcile_positions(system, kis)
    assert result["ok"] is False
    assert result["n_mismatches"] == 1


def test_emergency_halt_triggers_kill_switch(guards: SafetyGuards) -> None:
    out = guards.emergency_halt("test reason")
    assert out["triggered"] is True
    assert guards._kill.is_active() is True


def test_emergency_halt_without_kill_switch() -> None:
    g = SafetyGuards(auth_manager=None, kill_switch=None, audit_logger=None)
    out = g.emergency_halt("test")
    assert out["triggered"] is False


def test_audit_no_logger_returns_false() -> None:
    g = SafetyGuards(auth_manager=None, kill_switch=None, audit_logger=None)
    assert g.audit("test", {}) is False


def test_check_all_kill_switch_on_daily_loss(guards: SafetyGuards) -> None:
    # -6% daily_pnl → KillSwitch 자동 발동 (threshold 5%)
    result = guards.check_all(daily_pnl=-0.06)
    assert result.ok is False
    # S-1 issue 포함
    severities = [i["guard"] for i in result.issues]
    assert "S-1" in severities


def test_check_all_sanity_anomaly_high_severity(guards: SafetyGuards) -> None:
    result = guards.check_all(
        current_prices={"005930": 40000.0},   # -20%
        previous_prices={"005930": 50000.0},
    )
    assert result.ok is False
    assert any(i["guard"] == "S-3" for i in result.issues)


def test_check_all_reconciliation_high_severity(guards: SafetyGuards) -> None:
    result = guards.check_all(
        system_positions=[{"ticker": "005930", "qty": 10}],
        kis_positions=[{"ticker": "005930", "qty": 5}],
    )
    assert result.ok is False
    assert any(i["guard"] == "S-4" for i in result.issues)


# ====================================================================== #
# OpsMonitor
# ====================================================================== #


@pytest.fixture
def monitor(tmp_path: Path) -> OpsMonitor:
    return OpsMonitor(
        kill_switch=KillSwitch(),
        audit_logger=AuditLogger(log_path=tmp_path / "audit.jsonl"),
    )


def test_record_latency_percentiles(monitor: OpsMonitor) -> None:
    for i in range(1, 11):
        monitor.record_latency("quant", float(i))
    stats = monitor.latency_percentiles("quant")
    assert stats["n"] == 10
    assert stats["p50"] == pytest.approx(5.5, abs=1.0)
    assert stats["max"] == 10.0


def test_record_latency_empty_stage(monitor: OpsMonitor) -> None:
    stats = monitor.latency_percentiles("nonexistent")
    assert stats["n"] == 0
    assert stats["p50"] == 0.0


def test_rejection_rate_calculation(monitor: OpsMonitor) -> None:
    monitor.record_execution_result("filled")
    monitor.record_execution_result("filled")
    monitor.record_execution_result("rejected", "kill_switch_active")
    monitor.record_execution_result("rejected", "veto")
    r = monitor.rejection_rate()
    assert r["total"] == 4
    assert r["rejected"] == 2
    assert r["rate"] == 0.5
    assert "kill_switch_active" in r["by_reason"]


def test_rejection_rate_empty(monitor: OpsMonitor) -> None:
    r = monitor.rejection_rate()
    assert r["total"] == 0
    assert r["rate"] == 0.0


def test_update_daily_pnl_triggers_kill_switch(monitor: OpsMonitor) -> None:
    # -6% → threshold 5% 초과
    out = monitor.update_daily_pnl(-0.06)
    assert out["kill_switch_active"] is True
    assert out["triggered_now"] is True


def test_update_daily_pnl_no_trigger_below_threshold(monitor: OpsMonitor) -> None:
    out = monitor.update_daily_pnl(-0.03)
    assert out["kill_switch_active"] is False


def test_reset_daily(monitor: OpsMonitor) -> None:
    monitor.update_daily_pnl(-0.02)
    monitor.record_execution_result("rejected", "x")
    monitor.reset_daily()
    assert monitor._daily_pnl == 0.0
    assert monitor.rejection_rate()["total"] == 0


def test_check_sla_violations_no_records(monitor: OpsMonitor) -> None:
    assert monitor.check_sla_violations() == []


def test_check_sla_violations_over_100ms(monitor: OpsMonitor) -> None:
    # 10개 quant latency = 150ms
    for _ in range(10):
        monitor.record_latency("quant", 150.0)
    violations = monitor.check_sla_violations()
    assert len(violations) == 1
    assert violations[0]["stage"] == "quant"
    assert violations[0]["p95_ms"] >= 100.0


def test_summary_structure(monitor: OpsMonitor) -> None:
    monitor.record_latency("quant", 50.0)
    monitor.record_execution_result("filled")
    s = monitor.summary()
    assert "latency" in s
    assert "rejection" in s
    assert "daily_pnl" in s
    assert "kill_switch_active" in s
    assert s["latency"]["quant"]["n"] == 1

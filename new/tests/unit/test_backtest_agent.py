"""C12 BacktestAgent 유닛 테스트.

테스트 목록:
  1. test_backtest_agent_run_calls_engine        - run()이 BacktestEngine.run() 호출
  2. test_backtest_agent_run_returns_c12_schema  - run() 반환 dict C12 필수 필드 검증
  3. test_backtest_agent_report_returns_c5_schema - report() 반환 C5 필드 검증
  4. test_backtest_agent_run_handles_data_unavailable - DataUnavailable 처리 후 fail 반환
  5. test_backtest_agent_mode_b_only             - Mode A 호출 시 RuntimeError
  6. test_backtest_agent_report_invalid_type     - 유효하지 않은 report_type ValueError
  7. test_backtest_agent_verdict_pass            - sr>=0, ic>=0 → verdict=pass
  8. test_backtest_agent_verdict_fail            - sr<-0.5 → verdict=fail
  9. test_backtest_agent_regression_severity     - metrics 기준 severity 계산
"""
from __future__ import annotations

import os
import re
from unittest.mock import MagicMock, patch

import pytest


# ────────────────────────────────────────────────────────────────────────
# Helpers / fixtures
# ────────────────────────────────────────────────────────────────────────

class _mode_b_env:
    """ELEPHANT_MODE=mode_b context manager."""

    def __enter__(self):
        os.environ["ELEPHANT_MODE"] = "mode_b"
        return self

    def __exit__(self, *_):
        os.environ.pop("ELEPHANT_MODE", None)


def _make_engine_result(sr: float = 0.8, ic: float = 0.05) -> dict:
    """BacktestEngine.run() 반환 dict 모의."""
    return {
        "run_id": "BT-20260503-ABCD1234",
        "started_at": "2026-05-03T18:00:00+09:00",
        "finished_at": "2026-05-03T18:05:00+09:00",
        "metrics": {
            "ic": ic,
            "icir": 0.4,
            "rank_ic": 0.03,
            "arr": 0.12,
            "ir": 0.9,
            "mdd": -0.08,
            "sr": sr,
        },
        "daily_pnl": [1000.0, -200.0, 500.0],
        "trade_log": [],
        "bar_count": 120,
    }


def _make_agent(engine_mock=None) -> "BacktestAgent":
    """BacktestAgent 인스턴스 생성 with mock engine."""
    cfg_patch = {
        "verdict_required": "pass",
        "regression_severity_block": "high",
    }
    with patch(
        "src.agents.mode_b.backtest.config_load",
        return_value=cfg_patch,
    ):
        from src.agents.mode_b.backtest import BacktestAgent
        agent = BacktestAgent(engine=engine_mock)
    return agent


# ────────────────────────────────────────────────────────────────────────
# 1. run()이 BacktestEngine.run() 호출
# ────────────────────────────────────────────────────────────────────────

def test_backtest_agent_run_calls_engine():
    """BacktestAgent.run() → BacktestEngine.run() 실호출 검증."""
    mock_engine = MagicMock()
    mock_engine.run.return_value = _make_engine_result()

    agent = _make_agent(engine_mock=mock_engine)

    with _mode_b_env():
        with patch("src.agents.mode_b.backtest.config_load", return_value={}):
            result = agent.run("BUNDLE-20260503-TESTTEST")

    mock_engine.run.assert_called_once()
    call_kwargs = mock_engine.run.call_args
    assert call_kwargs is not None
    # bundle_id가 bundle_ref로 전달됐는지 확인
    args, kwargs = call_kwargs
    bundle_ref = kwargs.get("bundle_ref") or (args[0] if args else None)
    assert bundle_ref == "BUNDLE-20260503-TESTTEST"


# ────────────────────────────────────────────────────────────────────────
# 2. run() 반환 C12 필수 필드 검증
# ────────────────────────────────────────────────────────────────────────

def test_backtest_agent_run_returns_c12_schema():
    """run() 반환 dict에 C12 BacktestResult 필수 필드가 모두 있어야 한다."""
    mock_engine = MagicMock()
    mock_engine.run.return_value = _make_engine_result(sr=0.9, ic=0.06)

    agent = _make_agent(engine_mock=mock_engine)

    with _mode_b_env():
        with patch("src.agents.mode_b.backtest.config_load", return_value={}):
            result = agent.run("BUNDLE-20260503-TESTTEST")

    required_fields = {
        "backtest_id", "bundle_id", "metrics", "folds",
        "started_at", "completed_at", "verdict", "regression_severity",
    }
    missing = required_fields - set(result.keys())
    assert not missing, f"C12 필수 필드 누락: {missing}"

    # backtest_id 형식: BT-yyyymmdd-UUID8
    pattern = re.compile(r"^BT-\d{8}-[0-9A-F]{8}$")
    assert pattern.match(result["backtest_id"]), (
        f"backtest_id 형식 불일치: {result['backtest_id']!r}"
    )

    # metrics 7개 키
    metric_keys = {"ic", "icir", "rank_ic", "arr", "ir", "mdd", "sr"}
    assert metric_keys.issubset(set(result["metrics"].keys())), (
        f"metrics 키 누락: {metric_keys - set(result['metrics'].keys())}"
    )


# ────────────────────────────────────────────────────────────────────────
# 3. report() 반환 C5 AgentReport 필드 검증
# ────────────────────────────────────────────────────────────────────────

def test_backtest_agent_report_returns_c5_schema():
    """report() 반환 dict에 C5 AgentReport 필수 필드가 있어야 한다."""
    agent = _make_agent()

    payload = {"verdict": "pass", "metrics": {"sr": 0.8}}

    with _mode_b_env():
        result = agent.report("backtest_summary", payload)

    required_fields = {"report_id", "agent", "report_type", "content", "ts"}
    missing = required_fields - set(result.keys())
    assert not missing, f"C5 필수 필드 누락: {missing}"

    assert result["agent"] == "backtest"
    assert result["report_type"] == "backtest_summary"
    assert result["content"] == payload

    # report_id 형식: RPT-yyyymmdd-UUID8
    pattern = re.compile(r"^RPT-\d{8}-[0-9A-F]{8}$")
    assert pattern.match(result["report_id"]), (
        f"report_id 형식 불일치: {result['report_id']!r}"
    )


# ────────────────────────────────────────────────────────────────────────
# 4. run()이 DataUnavailable 처리 후 fail verdict 반환
# ────────────────────────────────────────────────────────────────────────

def test_backtest_agent_run_handles_data_unavailable():
    """BacktestEngine.run()이 DataUnavailable raise 시 verdict=fail 반환."""
    from src.mode_b.validation_tools import DataUnavailable

    mock_engine = MagicMock()
    mock_engine.run.side_effect = DataUnavailable("테스트 데이터 없음")

    agent = _make_agent(engine_mock=mock_engine)

    with _mode_b_env():
        with patch("src.agents.mode_b.backtest.config_load", return_value={}):
            result = agent.run("BUNDLE-20260503-TESTTEST")

    assert result["verdict"] == "fail"
    assert result["regression_severity"] == "high"
    assert "error" in result
    assert result["backtest_id"].startswith("BT-")


# ────────────────────────────────────────────────────────────────────────
# 5. Mode A 호출 시 RuntimeError
# ────────────────────────────────────────────────────────────────────────

def test_backtest_agent_mode_b_only():
    """ELEPHANT_MODE != 'mode_b'이면 RuntimeError (mode_b_only 데코레이터)."""
    os.environ.pop("ELEPHANT_MODE", None)

    from src.agents.mode_b.backtest import BacktestAgent
    agent = BacktestAgent()

    with pytest.raises(RuntimeError, match="Mode B 전용"):
        agent.run("BUNDLE-TEST")


# ────────────────────────────────────────────────────────────────────────
# 6. 유효하지 않은 report_type → ValueError
# ────────────────────────────────────────────────────────────────────────

def test_backtest_agent_report_invalid_type():
    """유효하지 않은 report_type 전달 시 ValueError."""
    agent = _make_agent()

    with _mode_b_env():
        with pytest.raises(ValueError, match="미지원"):
            agent.report("invalid_type", {})


# ────────────────────────────────────────────────────────────────────────
# 7. verdict=pass 조건: sr>=0 and ic>=0
# ────────────────────────────────────────────────────────────────────────

def test_backtest_agent_verdict_pass():
    """sr>=0, ic>=0 이면 verdict=pass."""
    mock_engine = MagicMock()
    mock_engine.run.return_value = _make_engine_result(sr=1.2, ic=0.08)

    agent = _make_agent(engine_mock=mock_engine)

    with _mode_b_env():
        with patch("src.agents.mode_b.backtest.config_load", return_value={}):
            result = agent.run("BUNDLE-PASS-TEST")

    assert result["verdict"] == "pass", f"expected pass, got {result['verdict']}"


# ────────────────────────────────────────────────────────────────────────
# 8. verdict=fail 조건: sr < -0.5
# ────────────────────────────────────────────────────────────────────────

def test_backtest_agent_verdict_fail():
    """sr<-0.5, ic<-0.1 이면 verdict=fail."""
    mock_engine = MagicMock()
    mock_engine.run.return_value = _make_engine_result(sr=-1.5, ic=-0.5)

    agent = _make_agent(engine_mock=mock_engine)

    with _mode_b_env():
        with patch("src.agents.mode_b.backtest.config_load", return_value={}):
            result = agent.run("BUNDLE-FAIL-TEST")

    assert result["verdict"] == "fail", f"expected fail, got {result['verdict']}"


# ────────────────────────────────────────────────────────────────────────
# 9. regression_severity 계산
# ────────────────────────────────────────────────────────────────────────

def test_backtest_agent_regression_severity():
    """sr 기준 regression_severity 계산 검증."""
    from src.agents.mode_b.backtest import BacktestAgent

    agent = BacktestAgent()

    # sr >= 0.5 → none
    assert agent._calc_regression_severity({"sr": 0.8}, {}) == "none"
    # 0 <= sr < 0.5 → low
    assert agent._calc_regression_severity({"sr": 0.2}, {}) == "low"
    # -0.5 <= sr < 0 → medium
    assert agent._calc_regression_severity({"sr": -0.3}, {}) == "medium"
    # sr < -0.5 → high
    assert agent._calc_regression_severity({"sr": -1.0}, {}) == "high"
    # metrics 비어있으면 high
    assert agent._calc_regression_severity({}, {}) == "high"

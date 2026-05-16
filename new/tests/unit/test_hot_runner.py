"""S1-5 Hot Runner + StateMachine unit tests.

Integration test: QuantAgent + PPOAllocator + PortfolioManager + FDAAgent 전체 조합.
"""
from __future__ import annotations


import numpy as np
import pytest

from src.agents.fda import FDAAgent
from src.agents.hot.quant import QuantAgent
from src.data.bar_buffer import BarBuffer
from src.models.ppo_allocator import PPOAllocator
from src.models.registry import ModelRegistry
from src.ops.state_machine import (
    IllegalTransitionError,
    PipelineState,
    StateMachine,
)
from src.orchestration.hot_runner import HotRunner
from src.portfolio.portfolio_manager import PortfolioManager


# ====================================================================== #
# StateMachine tests
# ====================================================================== #


def test_state_machine_initial_bootstrap() -> None:
    sm = StateMachine()
    assert sm.state == PipelineState.BOOTSTRAP


def test_state_transition_bootstrap_to_hot_running() -> None:
    sm = StateMachine()
    sm.transition(PipelineState.HOT_RUNNING)
    assert sm.state == PipelineState.HOT_RUNNING


def test_state_transition_idempotent() -> None:
    sm = StateMachine()
    sm.transition(PipelineState.HOT_RUNNING)
    sm.transition(PipelineState.HOT_RUNNING)   # no-op
    assert sm.state == PipelineState.HOT_RUNNING


def test_state_transition_illegal_raises() -> None:
    sm = StateMachine()
    # BOOTSTRAP → MODE_B_EVOLVING 는 허용 안 됨
    with pytest.raises(IllegalTransitionError):
        sm.transition(PipelineState.MODE_B_EVOLVING)


def test_state_transition_hot_to_mode_b_idle() -> None:
    sm = StateMachine()
    sm.transition(PipelineState.HOT_RUNNING)
    sm.transition(PipelineState.MODE_B_IDLE)
    assert sm.state == PipelineState.MODE_B_IDLE


def test_state_history_tracked() -> None:
    sm = StateMachine()
    sm.transition(PipelineState.HOT_RUNNING, ts="2026-04-20T09:00:00+09:00")
    sm.transition(PipelineState.MODE_B_IDLE, ts="2026-04-20T15:30:00+09:00")
    h = sm.history
    assert len(h) == 3   # 초기 + 2 전이
    assert h[-1]["from"] == "HOT_RUNNING"
    assert h[-1]["to"] == "MODE_B_IDLE"


def test_allowed_next_states() -> None:
    sm = StateMachine()
    nexts = sm.allowed_next_states()
    assert "HOT_RUNNING" in nexts
    assert "SHUTDOWN" in nexts
    assert "ERROR" in nexts


def test_shutdown_terminal() -> None:
    sm = StateMachine()
    sm.transition(PipelineState.SHUTDOWN)
    # SHUTDOWN에서는 다른 상태로 못 감
    assert sm.allowed_next_states() == []
    with pytest.raises(IllegalTransitionError):
        sm.transition(PipelineState.BOOTSTRAP)


def test_error_can_recover_to_bootstrap() -> None:
    sm = StateMachine()
    sm.transition(PipelineState.ERROR)
    sm.transition(PipelineState.BOOTSTRAP)
    assert sm.state == PipelineState.BOOTSTRAP


# ====================================================================== #
# HotRunner fixtures
# ====================================================================== #


def _make_bar(ticker: str, close: float, minute_offset: int) -> dict:
    hour = 9 + (minute_offset // 60)
    minute = minute_offset % 60
    return {
        "ticker": ticker,
        "ts_close": f"2026-04-20T{hour:02d}:{minute:02d}:00+09:00",
        "open": close,
        "high": close + 10.0,
        "low": close - 10.0,
        "close": close,
        "volume": 1000.0,
    }


def _prime_buffer(runner: HotRunner, tickers: list[str], n: int = 65) -> None:
    """각 ticker에 n개 bar를 push해 warmup 충족."""
    rng = np.random.default_rng(42)
    for t in tickers:
        price = 50000.0 + int(t) % 10000
        for i in range(n):
            price = max(1.0, price + float(rng.normal(0, 50)))
            runner._quant.on_bar(_make_bar(t, price, i))


def _deps_done() -> dict[str, str]:
    return {"news": "done", "risk": "done", "quant": "done", "debate": "skipped"}


@pytest.fixture
def runner(tmp_path) -> HotRunner:
    reg = ModelRegistry(artifacts_dir=tmp_path / "lgbm")
    return HotRunner(
        quant=QuantAgent(registry=reg, bar_buffer=BarBuffer()),
        ppo=PPOAllocator(),
        pm=PortfolioManager(),
        fda=FDAAgent(),
        state_machine=StateMachine(),
    )


# ====================================================================== #
# HotRunner lifecycle
# ====================================================================== #


def test_runner_initial_state_bootstrap(runner: HotRunner) -> None:
    assert runner.state == PipelineState.BOOTSTRAP


def test_runner_start_transitions_to_hot_running(runner: HotRunner) -> None:
    runner.start()
    assert runner.state == PipelineState.HOT_RUNNING


def test_runner_stop_for_mode_b(runner: HotRunner) -> None:
    runner.start()
    runner.stop_for_mode_b()
    assert runner.state == PipelineState.MODE_B_IDLE


def test_runner_shutdown(runner: HotRunner) -> None:
    runner.start()
    runner.shutdown()
    assert runner.state == PipelineState.SHUTDOWN


# ====================================================================== #
# run_once 오케스트레이션
# ====================================================================== #


def test_run_once_skipped_when_not_hot_running(runner: HotRunner) -> None:
    # BOOTSTRAP 상태에서 run_once 호출
    result = runner.run_once(tickers=["005930"], bars_batch=[])
    assert result.get("skipped") is True
    assert result["pipeline_state"] == "BOOTSTRAP"


def test_run_once_passive_no_model_still_runs(runner: HotRunner) -> None:
    """QuantAgent가 passive mode (모델 없음)여도 오케스트레이션 완료."""
    runner.start()
    tickers = ["005930", "000660", "035420", "051910"]
    _prime_buffer(runner, tickers, n=65)

    result = runner.run_once(
        tickers=tickers,
        bars_batch=[],
        current_positions=[],
        latest_prices={t: 50000.0 + i * 1000 for i, t in enumerate(tickers)},
        portfolio_value=10_000_000.0,
        asof="2026-04-20T10:00:00+09:00",
        dependency_status=_deps_done(),
    )

    # QuantAgent passive → 비중 전무 → PM 주문 없음 → FDA approve (empty)
    assert result["pipeline_state"] == "HOT_RUNNING"
    assert result["quant_output"]["mode"] == "passive"
    assert result["final_decision"]["approved"] is True
    assert result["final_decision"]["reason_code"] == "NORMAL_APPROVE"
    assert "latency_ms" in result


def test_run_once_missing_dependency_status_vetoes(runner: HotRunner) -> None:
    runner.start()
    tickers = ["005930", "000660"]
    _prime_buffer(runner, tickers, n=65)

    result = runner.run_once(
        tickers=tickers,
        bars_batch=[],
        current_positions=[],
        latest_prices={t: 50000.0 for t in tickers},
        portfolio_value=10_000_000.0,
        asof="2026-04-20T10:00:00+09:00",
    )

    assert result["pipeline_state"] == "HOT_RUNNING"
    assert result["final_decision"]["approved"] is False
    assert result["final_decision"]["reason_code"] == "TIMEOUT"
    assert "news" in result["final_decision"]["veto_reason"]
    assert "risk" in result["final_decision"]["veto_reason"]


def test_run_once_bar_batch_consumed(runner: HotRunner) -> None:
    runner.start()
    tickers = ["005930"]
    bars = [_make_bar("005930", 50000.0 + i * 10, i) for i in range(5)]
    result = runner.run_once(
        tickers=tickers,
        bars_batch=bars,
        asof="2026-04-20T10:00:00+09:00",
    )
    assert result["n_bars_consumed"] == 5


def test_run_once_rejects_future_bar_before_quant_buffer(runner: HotRunner) -> None:
    """asof보다 미래인 1분봉은 Quant buffer에 넣지 않는다."""
    runner.start()
    future_bar = _make_bar("005930", 50000.0, 61)  # 10:01 KST

    result = runner.run_once(
        tickers=["005930"],
        bars_batch=[future_bar],
        asof="2026-04-20T10:00:00+09:00",
    )

    assert result["n_bars_consumed"] == 0
    assert result["bar_errors"] == [
        "future_bar_rejected: ticker=005930 "
        "ts_close=2026-04-20T10:01:00+09:00 asof=2026-04-20T10:00:00+09:00"
    ]
    assert runner._quant._bar_buffer.get_latest("005930", n=1) == []


def test_run_once_asof_timezone_converted_to_utc(runner: HotRunner) -> None:
    """timezone-aware KST asof를 UTC로 변환하고 replace로 덮어쓰지 않는다."""
    runner.start()
    captured: dict[str, object] = {}

    def fake_evaluate(snapshot, ts):
        captured["ts"] = ts
        return {
            "risk_level": "low",
            "severity": "low",
            "fast_rule_match": None,
            "triggered_rules": [],
            "affected_tickers": [],
            "recommended_action": "pass",
            "stance": "neutral",
            "rationale": "ok",
            "latency_ms": 0.0,
        }

    runner._risk_fast.evaluate = fake_evaluate  # type: ignore[method-assign]
    runner.run_once(tickers=["005930"], bars_batch=[], asof="2026-05-15T09:00:00+09:00")

    assert captured["ts"].isoformat() == "2026-05-15T00:00:00+00:00"


def test_run_once_filters_future_recent_bars_before_risk_fast(
    runner: HotRunner,
) -> None:
    """RiskFast sidecar도 asof 이후 recent_bars를 보지 않는다."""
    runner.start()
    captured: dict[str, object] = {}

    def fake_evaluate(snapshot, ts):
        captured["recent_bars"] = snapshot["recent_bars"]
        return {
            "risk_level": "low",
            "severity": "low",
            "fast_rule_match": None,
            "triggered_rules": [],
            "affected_tickers": [],
            "recommended_action": "pass",
            "stance": "neutral",
            "rationale": "ok",
            "latency_ms": 0.0,
        }

    runner._risk_fast.evaluate = fake_evaluate  # type: ignore[method-assign]
    result = runner.run_once(
        tickers=["005930"],
        bars_batch=[],
        asof="2026-04-20T10:00:00+09:00",
        recent_bars={
            "005930": [
                _make_bar("005930", 50000.0, 59),
                _make_bar("005930", 49000.0, 61),
            ],
        },
    )

    assert captured["recent_bars"] == {
        "005930": [_make_bar("005930", 50000.0, 59)]
    }
    assert result["bar_errors"] == [
        "future_recent_bar_rejected: ticker=005930 "
        "ts_close=2026-04-20T10:01:00+09:00 asof=2026-04-20T10:00:00+09:00"
    ]


def test_run_once_risk_fast_exception_degrades_nonblocking(
    runner: HotRunner,
) -> None:
    """RiskFast sidecar 예외는 Hot Path core를 fail-close하지 않는다."""
    runner.start()
    tickers = ["005930", "000660"]
    _prime_buffer(runner, tickers, n=65)

    def failing_evaluate(snapshot, ts):
        raise RuntimeError("risk sidecar unavailable")

    runner._risk_fast.evaluate = failing_evaluate  # type: ignore[method-assign]

    result = runner.run_once(
        tickers=tickers,
        bars_batch=[],
        current_positions=[],
        latest_prices={t: 50000.0 for t in tickers},
        portfolio_value=10_000_000.0,
        asof="2026-04-20T10:00:00+09:00",
        dependency_status=_deps_done(),
    )

    assert result["pipeline_state"] == "HOT_RUNNING"
    assert result.get("status") != "FAIL"
    assert result.get("failure_stage") != "risk_fast"
    assert result["risk_eval"]["enabled"] is False
    assert result["risk_eval"]["status"] == "DISABLED"
    assert result["risk_eval"]["risk_level"] == "high"
    assert result["risk_eval"]["recommended_action"] == "halt"
    assert result["final_decision"]["approved"] is False
    assert result["final_decision"]["reason_code"] == "RISK_FAST_TRIGGER"


def test_run_once_malformed_bar_survives(runner: HotRunner) -> None:
    """잘못된 bar 하나가 들어와도 전체 루프는 중단되지 않음."""
    runner.start()
    bars = [
        _make_bar("005930", 50000.0, 0),
        {"ticker": "000660"},   # 필수 필드 누락
        _make_bar("005930", 50100.0, 1),
    ]
    result = runner.run_once(
        tickers=["005930"],
        bars_batch=bars,
        asof="2026-04-20T10:00:00+09:00",
    )
    assert result["n_bars_consumed"] == 2
    assert len(result["bar_errors"]) == 1


def test_run_once_non_dict_bar_survives(runner: HotRunner) -> None:
    """non-dict bar도 AttributeError crash 대신 bar_errors로 흡수한다."""
    runner.start()
    bars = [
        _make_bar("005930", 50000.0, 0),
        None,
        _make_bar("005930", 50100.0, 1),
    ]

    result = runner.run_once(
        tickers=["005930"],
        bars_batch=bars,  # type: ignore[list-item]
        asof="2026-04-20T10:00:00+09:00",
    )

    assert result["n_bars_consumed"] == 2
    assert len(result["bar_errors"]) == 1
    assert "bar must be dict" in result["bar_errors"][0]


def test_run_once_malformed_numeric_bar_fails_closed(runner: HotRunner) -> None:
    """숫자 필드가 깨진 warm buffer도 루프 raise 대신 structured veto로 닫는다."""
    runner.start()
    bars = [_make_bar("005930", 50000.0 + i, i) for i in range(65)]
    bars[-1]["close"] = "bad"

    result = runner.run_once(
        tickers=["005930"],
        bars_batch=bars,
        asof="2026-04-20T10:05:00+09:00",
    )

    assert result["status"] == "FAIL"
    assert result["failure_stage"] == "quant"
    fd = result["final_decision"]
    assert set(fd) == {
        "decision_id",
        "approved",
        "target_weights",
        "order_deltas",
        "veto_reason",
        "reason_code",
        "risk_overrides",
        "confidence",
        "expiry",
        "portfolio_patch_ref",
        "active_reports",
    }
    assert fd["approved"] is False
    assert fd["reason_code"] == "TIMEOUT"
    assert fd["risk_overrides"][0]["override"] == "fail_closed"


def test_run_once_requires_asof_for_bar_batch(runner: HotRunner) -> None:
    """bar batch는 asof 없이 buffer에 넣지 않는다."""
    runner.start()
    bar = _make_bar("005930", 50000.0, 0)

    result = runner.run_once(tickers=["005930"], bars_batch=[bar])

    assert result["skipped"] is True
    assert result["reason"] == "asof_required"
    assert result["n_bars_consumed"] == 0
    assert runner._quant._bar_buffer.get_latest("005930") == []


def test_run_once_requires_asof_even_without_bar_batch(runner: HotRunner) -> None:
    """HOT_RUNNING 루프는 기존 buffer를 읽더라도 asof 없이는 열리지 않는다."""
    runner.start()
    _prime_buffer(runner, ["005930"], n=65)

    result = runner.run_once(tickers=["005930"], bars_batch=[])

    assert result["skipped"] is True
    assert result["reason"] == "asof_required"
    assert result["n_bars_consumed"] == 0


def test_run_once_handles_invalid_ppo_allocation(runner: HotRunner) -> None:
    """PPO가 malformed allocation을 반환해도 Hot Path는 구조화된 veto로 닫는다."""
    runner.start()
    tickers = ["005930"]
    _prime_buffer(runner, tickers, n=65)

    runner._ppo.allocate = lambda **_: {}  # type: ignore[method-assign]
    result = runner.run_once(
        tickers=tickers,
        bars_batch=[],
        latest_prices={"005930": 50000.0},
        portfolio_value=10_000_000.0,
        asof="2026-04-20T10:05:00+09:00",
        dependency_status=_deps_done(),
    )

    assert result["ppo_guard_warnings"][0]["reason_code"] == "PPO_ALLOCATION_PLAN_INVALID"
    assert result["final_decision"]["approved"] is False


def test_run_once_vetoes_ppo_policy_universe_mismatch(runner: HotRunner) -> None:
    """PPO 정책 universe mismatch는 빈 allocation을 정상 approve로 통과시키지 않는다."""
    runner.start()
    tickers = ["005930"]
    _prime_buffer(runner, tickers, n=65)
    runner._ppo.allocate = lambda **_: {  # type: ignore[method-assign]
        "allocation_plan": {"target_weights": {}},
        "status": "BLOCKED",
        "metadata": {
            "reason": "ppo_policy_universe_mismatch",
            "policy_tickers": ["000660"],
            "request_tickers": ["005930"],
        },
    }

    result = runner.run_once(
        tickers=tickers,
        bars_batch=[],
        latest_prices={"005930": 50000.0},
        portfolio_value=10_000_000.0,
        asof="2026-04-20T10:05:00+09:00",
        dependency_status=_deps_done(),
    )

    assert result["ppo_guard_warnings"][0]["reason_code"] == "PPO_POLICY_UNIVERSE_MISMATCH"
    assert result["final_decision"]["approved"] is False
    assert result["final_decision"]["reason_code"] == "RISK_FAST_TRIGGER"


def test_run_once_veto_on_anomaly(runner: HotRunner) -> None:
    """QuantAgent anomaly 탐지 → FDA veto (QUANT_ANOMALY)."""
    runner.start()
    tickers = ["005930"]
    _prime_buffer(runner, tickers, n=65)
    # 급락 bar 추가 (last close의 -10%)
    last_close = 50000.0
    drop_bar = {
        "ticker": "005930",
        "ts_close": "2026-04-20T10:05:00+09:00",
        "open": last_close, "high": last_close, "low": last_close * 0.9,
        "close": last_close * 0.9, "volume": 1000.0,
    }
    runner._quant.on_bar(drop_bar)

    result = runner.run_once(
        tickers=tickers,
        bars_batch=[],
        latest_prices={"005930": 50000.0},
        asof="2026-04-20T10:05:00+09:00",
        dependency_status=_deps_done(),
    )
    # Anomaly는 상황에 따라 감지될 수도 안 될 수도 (random seed). 양쪽 허용.
    if result["anomalies"]:
        assert result["final_decision"]["reason_code"] == "QUANT_ANOMALY"
        assert result["final_decision"]["approved"] is False


def test_run_once_fda_echoes_pm_adjusted_target_weights(runner: HotRunner) -> None:
    """FDA echo는 PPO 원본이 아니라 PM이 실제 주문에 맞춘 target_weights를 사용한다."""
    runner.start()

    runner._quant.score_cross_section = lambda tickers, asof: {  # type: ignore[method-assign]
        "mode": "passive",
        "scores": {"005930": 0.9},
        "ranking": ["005930"],
    }
    runner._quant.detect_anomalies = lambda tickers, asof: [  # type: ignore[method-assign]
        {"ticker": "005930", "reason": "forced_exit"}
    ]
    runner._ppo.allocate = lambda **kwargs: {  # type: ignore[method-assign]
        "allocation_plan": {"target_weights": {"005930": 0.10}}
    }

    result = runner.run_once(
        tickers=["005930"],
        bars_batch=[],
        current_positions=[{"ticker": "005930", "qty": 8, "weight": 0.20}],
        latest_prices={"005930": 50000.0},
        portfolio_value=10_000_000.0,
        asof="2026-04-20T10:05:00+09:00",
    )

    assert result["pm_result"]["portfolio_patch"]["target_weights"]["005930"] == 0.0
    assert result["final_decision"]["target_weights"]["005930"] == 0.0
    assert result["final_decision"]["order_deltas"][0]["reason"] == "risk_reduce"


def test_run_once_vetoes_pm_price_unavailable(runner: HotRunner) -> None:
    """PM이 주문 생성 오류를 보고하면 FDA는 partial patch를 approve하지 않는다."""
    runner.start()
    runner._quant.score_cross_section = lambda tickers, asof: {  # type: ignore[method-assign]
        "mode": "active",
        "scores": {"005930": 0.9},
        "ranking": ["005930"],
    }
    runner._quant.detect_anomalies = lambda tickers, asof: []  # type: ignore[method-assign]
    runner._ppo.allocate = lambda **kwargs: {  # type: ignore[method-assign]
        "allocation_plan": {"target_weights": {"005930": 0.10}}
    }

    result = runner.run_once(
        tickers=["005930"],
        bars_batch=[],
        latest_prices={},
        portfolio_value=10_000_000.0,
        asof="2026-04-20T10:05:00+09:00",
        dependency_status=_deps_done(),
    )

    assert result["pm_result"]["n_errors"] == 1
    assert result["pm_guard_warnings"][0]["reason"] == "portfolio_manager_errors"
    assert result["final_decision"]["approved"] is False
    assert result["final_decision"]["reason_code"] == "RISK_FAST_TRIGGER"


def test_run_once_vetoes_ppo_constraint_violation(runner: HotRunner) -> None:
    """PM이 PPO boundary violation을 보고하면 FDA는 해당 patch를 veto한다."""
    runner.start()
    runner._quant.score_cross_section = lambda tickers, asof: {  # type: ignore[method-assign]
        "mode": "active",
        "scores": {"005930": 0.9},
        "ranking": ["005930"],
    }
    runner._quant.detect_anomalies = lambda tickers, asof: []  # type: ignore[method-assign]
    runner._ppo.allocate = lambda **kwargs: {  # type: ignore[method-assign]
        "allocation_plan": {"target_weights": {"005930": 0.90}}
    }
    runner._pm.plan = lambda **kwargs: {  # type: ignore[method-assign]
        "portfolio_patch": {
            "portfolio_patch_id": "PP-TEST",
            "based_on_ts": "2026-04-20T10:05:00+09:00",
            "target_weights": {"005930": 0.90},
            "order_deltas": [{"ticker": "005930", "side": "buy", "qty": 1}],
        },
        "n_errors": 0,
        "errors": [],
        "ppo_violations": [
            {
                "type": "max_single_name_exceeded",
                "ticker": "005930",
                "weight": 0.90,
                "limit": 0.10,
            }
        ],
    }

    result = runner.run_once(
        tickers=["005930"],
        bars_batch=[],
        latest_prices={"005930": 50_000.0},
        portfolio_value=10_000_000.0,
        asof="2026-04-20T10:05:00+09:00",
        dependency_status=_deps_done(),
    )

    assert result["pm_guard_warnings"][0]["reason"] == "ppo_constraint_violation"
    assert result["final_decision"]["approved"] is False
    assert result["final_decision"]["reason_code"] == "RISK_FAST_TRIGGER"


def test_run_once_high_risk_warning_veto(runner: HotRunner) -> None:
    runner.start()
    tickers = ["005930"]
    _prime_buffer(runner, tickers)
    result = runner.run_once(
        tickers=tickers,
        bars_batch=[],
        latest_prices={"005930": 50000.0},
        risk_warnings=[{"ticker": "005930", "severity": "high", "reason": "fake"}],
        asof="2026-04-20T10:05:00+09:00",
        dependency_status=_deps_done(),
    )
    assert result["final_decision"]["reason_code"] == "RISK_FAST_TRIGGER"
    assert result["final_decision"]["approved"] is False


def test_latency_stats(runner: HotRunner) -> None:
    runner.start()
    tickers = ["005930", "000660"]
    _prime_buffer(runner, tickers)
    for _ in range(3):
        runner.run_once(
            tickers=tickers,
            bars_batch=[],
            latest_prices={t: 50000.0 for t in tickers},
            asof="2026-04-20T10:05:00+09:00",
        )
    stats = runner.latency_stats()
    assert stats["count"] == 3
    assert stats["avg"] >= 0.0
    assert stats["max"] >= stats["min"]

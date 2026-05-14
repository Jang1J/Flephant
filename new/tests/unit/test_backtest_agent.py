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
        "feature_quality": {
            "dual_source_rows": 100,
            "dual_source_non_neutral_rows": 90,
            "exogenous_rows": 100,
            "exogenous_non_neutral_rows": 80,
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


def _config_load_with_real_universe(extra: dict | None = None):
    """SHIP-fix C-3 (GPT Pro 2026-05-09): universe_config.yaml 호출은 실 SSOT 로드,
    risk_config.yaml 호출은 test 별 cfg 주입.

    이전: patch return_value={fallback_tickers} → universe sectors 비어 fallback 발동 → W-4 force_verdict_fail.
    현재: universe_config.yaml 실 로드 → sectors 4 confirmed × 5 active = 20종목 정상.
    """
    overrides = extra or {}

    def _side(file_name, key=None):
        if file_name == "universe_config.yaml":
            from src.utils.config_loader import load
            return load("universe_config.yaml")
        if file_name == "risk_config.yaml":
            return overrides.get(("risk_config.yaml", key), overrides.get("default", {}))
        return overrides.get((file_name, key), overrides.get("default", {}))

    return _side


# ────────────────────────────────────────────────────────────────────────
# 1. run()이 BacktestEngine.run() 호출
# ────────────────────────────────────────────────────────────────────────

def test_backtest_agent_run_calls_engine():
    """BacktestAgent.run() → BacktestEngine.run() 실호출 검증."""
    mock_engine = MagicMock()
    mock_engine.run.return_value = _make_engine_result()

    agent = _make_agent(engine_mock=mock_engine)

    with _mode_b_env():
        with patch("src.agents.mode_b.backtest.config_load", side_effect=_config_load_with_real_universe()):
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
        with patch("src.agents.mode_b.backtest.config_load", side_effect=_config_load_with_real_universe()):
            result = agent.run("BUNDLE-20260503-TESTTEST")

    required_fields = {
        "backtest_id", "bundle_id", "metrics", "folds",
        "started_at", "completed_at", "verdict", "regression_severity",
        "regression_risk", "diagnostic_notes", "llm_reasoning_ref",
        "failure_case_cards", "regression_cases", "minute_bar_leakage_check",
        "feature_quality",
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
    assert result["regression_risk"]["flagged"] is False
    assert isinstance(result["regression_risk"]["evidence"], list)
    assert result["minute_bar_leakage_check"]["verdict"] in ("pass", "fail")
    assert result["minute_bar_leakage_check"]["verdict"] == "pass"
    assert isinstance(result["failure_case_cards"], list)
    assert isinstance(result["regression_cases"], list)
    assert result["feature_quality"]["dual_source_rows"] == 100
    assert result["feature_quality"]["exogenous_non_neutral_rows"] == 80


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
        with patch("src.agents.mode_b.backtest.config_load", side_effect=_config_load_with_real_universe()):
            result = agent.run("BUNDLE-20260503-TESTTEST")

    assert result["verdict"] == "fail"
    assert result["regression_severity"] == "high"
    assert "error" in result
    assert result["backtest_id"].startswith("BT-")
    assert result["regression_risk"]["flagged"] is True
    assert isinstance(result["regression_risk"]["evidence"], list)
    assert result["minute_bar_leakage_check"]["verdict"] == "fail"
    assert result["feature_quality"] == {}


# ────────────────────────────────────────────────────────────────────────
# 5. Mode A 호출 시 RuntimeError
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.no_mode_b
def test_backtest_agent_mode_b_only(monkeypatch):
    """ELEPHANT_MODE != 'mode_b'이면 RuntimeError (mode_b_only 데코레이터).

    no_mode_b 마커로 root/unit conftest의 autouse ELEPHANT_MODE fixture 비활성화.
    """
    monkeypatch.delenv("ELEPHANT_MODE", raising=False)

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
        with patch("src.agents.mode_b.backtest.config_load", side_effect=_config_load_with_real_universe()):
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
        with patch("src.agents.mode_b.backtest.config_load", side_effect=_config_load_with_real_universe()):
            result = agent.run("BUNDLE-FAIL-TEST")

    assert result["verdict"] == "fail", f"expected fail, got {result['verdict']}"


def test_backtest_agent_blocks_synthetic_candidate_artifact():
    """candidate artifact가 synthetic fallback이면 metrics가 좋아도 verdict=fail."""
    mock_engine = MagicMock()
    engine_result = _make_engine_result(sr=1.2, ic=0.08)
    engine_result["candidate_artifact"] = {
        "loaded": False,
        "synthetic_fallback": True,
        "source": "missing_candidate_bundle",
    }
    mock_engine.run.return_value = engine_result

    agent = _make_agent(engine_mock=mock_engine)

    with _mode_b_env():
        with patch("src.agents.mode_b.backtest.config_load", side_effect=_config_load_with_real_universe()):
            result = agent.run("BUNDLE-MISSING-CANDIDATE")

    assert result["verdict"] == "fail"
    assert result["regression_severity"] == "high"
    assert result["regression_risk"]["flagged"] is True
    assert any(
        "candidate_artifact_synthetic_fallback" in item
        for item in result["regression_risk"]["evidence"]
    )


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



# ────────────────────────────────────────────────────────────────────────
# 10. SHIP-fix R-3: universe SSOT 20종목 실로드 검증
# ────────────────────────────────────────────────────────────────────────

def test_backtest_agent_universe_loads_20_active_tickers():
    """SHIP-5 핵심 변경 검증: universe_config.yaml.sectors[X].stocks 에서 active 20종목 로드.

    config_load mock 없이 실제 yaml 참조. status='active' + sector status='confirmed' 필터링 확인.
    """
    mock_engine = MagicMock()
    mock_engine.run.return_value = _make_engine_result(sr=0.8, ic=0.05)

    agent = _make_agent(engine_mock=mock_engine)

    with _mode_b_env():
        # config_load mock 안 함 → 실제 universe_config.yaml 로드
        result = agent.run("BUNDLE-UNIVERSE-TEST")

    # BacktestEngine.run 에 전달된 universe 인자 검증
    call_args = mock_engine.run.call_args
    universe = call_args.kwargs["universe"]

    # 20종목 (4 confirmed sector × 5 active stocks)
    assert len(universe) == 20, f"expected 20 active tickers, got {len(universe)}"

    # 6자리 zero-padded 종목코드
    for t in universe:
        assert len(t) == 6 and t.isdigit(), f"ticker {t} not 6-digit zero-padded"

    # 핵심 종목 포함 검증
    assert "005930" in universe, "삼성전자(005930) 누락"
    assert "000660" in universe, "SK하이닉스(000660) 누락"
    assert "012450" in universe, "한화에어로스페이스(012450) 누락"


# ────────────────────────────────────────────────────────────────────────
# 11. SHIP-fix NEW-2: deploy_decision_gate yaml 임계값 SSOT 검증
# ────────────────────────────────────────────────────────────────────────

def test_backtest_agent_verdict_uses_yaml_thresholds():
    """SHIP-fix NEW-2: verdict 계산이 risk_config.yaml deploy_decision_gate 값 사용 확인.

    config_load mock에 명시적 임계값 주입 → verdict 결과가 yaml 값에 따라 분기되는지.
    이전 테스트 (verdict_pass/fail) 는 mock {} 였음 → default fallback 만 검증.
    이 테스트는 yaml SSOT 우선 적용 확인.
    """
    mock_engine = MagicMock()
    # SR 0.3, IC 0.02 (default 임계값에서는 pass, 엄격한 임계값에서는 fail)
    mock_engine.run.return_value = _make_engine_result(sr=0.3, ic=0.02)

    agent = _make_agent(engine_mock=mock_engine)

    # 엄격한 yaml 임계값 주입 (SR 0.5 / IC 0.1 미만이면 fail)
    strict_cfg = {
        "pass_sr_threshold": 0.5,
        "pass_ic_threshold": 0.1,
        "warn_sr_threshold": 0.2,
        "warn_ic_threshold": 0.0,
        "severity_none_sr_threshold": 0.5,
        "severity_low_sr_threshold": 0.0,
        "severity_medium_sr_threshold": -0.5,
    }

    with _mode_b_env():
        # config_load 호출 시 deploy_decision_gate 키만 주입
        def _config_side_effect(file_name, key=None):
            if file_name == "risk_config.yaml" and key == "backtest_agent.deploy_decision_gate":
                return strict_cfg
            if file_name == "universe_config.yaml":
                # universe_config.yaml은 실 로드 (20종목)
                from src.utils.config_loader import load
                return load("universe_config.yaml")
            return {}

        with patch("src.agents.mode_b.backtest.config_load", side_effect=_config_side_effect):
            result = agent.run("BUNDLE-YAML-VERDICT-TEST")

    # SR 0.3 < pass_sr 0.5 → pass 아님
    # SR 0.3 >= warn_sr 0.2 → warn
    assert result["verdict"] == "warn", (
        f"yaml strict 임계값에서 SR 0.3 / IC 0.02 → expected warn, got {result['verdict']}"
    )


# ────────────────────────────────────────────────────────────────────────
# 12. SHIP-fix NEW-2 (D 보강): default vs strict 임계값 분기 검증
# ────────────────────────────────────────────────────────────────────────

def test_backtest_agent_verdict_default_vs_strict_thresholds():
    """SHIP-fix NEW-2 보강 (D 옵션): 같은 SR/IC, default 임계값 → 'pass', strict 임계값 → 'warn'.

    yaml SSOT가 진짜 verdict 분기를 제어하는지 비교 검증.
    """
    # 같은 metrics: SR 0.3, IC 0.02
    mock_engine_default = MagicMock()
    mock_engine_default.run.return_value = _make_engine_result(sr=0.3, ic=0.02)

    mock_engine_strict = MagicMock()
    mock_engine_strict.run.return_value = _make_engine_result(sr=0.3, ic=0.02)

    # ── default 임계값 (gate_cfg = {}) → SR 0.3 >= 0.0 AND IC 0.02 >= 0.0 → "pass"
    agent_d = _make_agent(engine_mock=mock_engine_default)
    with _mode_b_env():
        with patch(
            "src.agents.mode_b.backtest.config_load",
            side_effect=_config_load_with_real_universe(),
        ):
            result_default = agent_d.run("BUNDLE-DEFAULT")

    assert result_default["verdict"] == "pass", (
        f"default 임계값 (SR>=0.0 AND IC>=0.0) 에서 SR 0.3 / IC 0.02 → expected pass, got {result_default['verdict']}"
    )

    # ── strict 임계값 → SR 0.3 < 0.5 → not pass. SR 0.3 >= 0.2 → "warn"
    strict_cfg = {
        "pass_sr_threshold": 0.5,
        "pass_ic_threshold": 0.1,
        "warn_sr_threshold": 0.2,
        "warn_ic_threshold": 0.0,
    }
    agent_s = _make_agent(engine_mock=mock_engine_strict)
    with _mode_b_env():
        def _strict_side_effect(file_name, key=None):
            if file_name == "risk_config.yaml" and key == "backtest_agent.deploy_decision_gate":
                return strict_cfg
            if file_name == "universe_config.yaml":
                from src.utils.config_loader import load
                return load("universe_config.yaml")  # 실 SSOT 로드
            return {}

        with patch("src.agents.mode_b.backtest.config_load", side_effect=_strict_side_effect):
            result_strict = agent_s.run("BUNDLE-STRICT")

    assert result_strict["verdict"] == "warn", (
        f"strict 임계값 (SR>=0.5 fail, SR>=0.2 warn) 에서 SR 0.3 / IC 0.02 → expected warn, got {result_strict['verdict']}"
    )

    # 핵심 검증: 같은 metrics 인데 yaml 값에 따라 verdict 다르게 분기
    assert result_default["verdict"] != result_strict["verdict"], (
        "yaml SSOT 가 verdict 분기를 제어하지 못함. 두 케이스 모두 같은 verdict 반환."
    )

"""S2-8 RiskFastAgent unit tests.

비LLM 100% 규칙 기반. 8개 테스트.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from src.agents.hot.risk_fast import RiskFastAgent
from src.utils.config_loader import load as config_load, reload as config_reload


# ====================================================================== #
# Fixtures
# ====================================================================== #


@pytest.fixture
def agent() -> RiskFastAgent:
    return RiskFastAgent()


@pytest.fixture
def ts() -> datetime:
    return datetime(2026, 4, 25, 10, 0, 0, tzinfo=timezone.utc)


def _make_bars(
    ticker: str,
    closes: list[float],
    volumes: list[float] | None = None,
) -> dict[str, list[dict]]:
    """테스트용 bars dict 생성 헬퍼."""
    if volumes is None:
        volumes = [1000.0] * len(closes)
    bars = [
        {"close": c, "volume": v}
        for c, v in zip(closes, volumes)
    ]
    return {ticker: bars}


# ====================================================================== #
# 1. recent_bars=None → risk_level='low'
# ====================================================================== #


def test_low_risk_no_bars(agent: RiskFastAgent, ts: datetime) -> None:
    """recent_bars=None이면 규칙 1~3 skip → risk_level='low'."""
    result = agent.evaluate(
        snapshot={"ranking": {}, "portfolio_patch": {}, "recent_bars": None},
        ts=ts,
    )
    assert result["risk_level"] == "low"
    assert result["fast_rule_match"] is False
    assert result["triggered_rules"] == []
    assert result["recommended_action"] == "pass"


# ====================================================================== #
# 2. 급락 → rule_intraday_drop, risk_level='high'
# ====================================================================== #


def test_intraday_drop_triggers_high(agent: RiskFastAgent, ts: datetime) -> None:
    """5% 급락 종목 → triggered_rules에 rule_intraday_drop, risk_level='high'."""
    # close 100 → 94: 6% 하락 (임계 5% 초과)
    bars = _make_bars("005930", [100.0, 99.0, 97.0, 95.0, 94.0])
    result = agent.evaluate(
        snapshot={"ranking": {}, "portfolio_patch": {}, "recent_bars": bars},
        ts=ts,
    )
    assert "rule_intraday_drop" in result["triggered_rules"]
    assert result["risk_level"] == "high"
    assert "005930" in result["affected_tickers"]
    assert result["fast_rule_match"] is True
    assert result["recommended_action"] == "reduce"


# ====================================================================== #
# 3. 거래량 spike → rule_volume_spike, risk_level='medium'
# ====================================================================== #


def test_volume_spike_triggers_medium(agent: RiskFastAgent, ts: datetime) -> None:
    """거래량 최신값이 이전 평균의 3배 이상 → rule_volume_spike, risk_level='medium'."""
    # close는 변동 없음 (급락 규칙 미트리거). volume: 이전 1000 × 20봉 → 마지막 4000 (4배)
    closes = [100.0] * 22
    volumes = [1000.0] * 21 + [4000.0]
    bars = {
        "000660": [{"close": c, "volume": v} for c, v in zip(closes, volumes)]
    }
    result = agent.evaluate(
        snapshot={"ranking": {}, "portfolio_patch": {}, "recent_bars": bars},
        ts=ts,
    )
    assert "rule_volume_spike" in result["triggered_rules"]
    assert result["risk_level"] == "medium"
    assert "000660" in result["affected_tickers"]
    assert result["recommended_action"] == "reduce"


# ====================================================================== #
# 4. 변동성 → rule_volatility, risk_level='medium'
# ====================================================================== #


def test_volatility_triggers_medium(agent: RiskFastAgent, ts: datetime) -> None:
    """1분 return std > 30bp → rule_volatility, risk_level='medium'.

    30bp = 0.003. 100 → 110 → 95 → 108 → 92 → 105 : 큰 등락 반복 → std >> 30bp.
    """
    bars = _make_bars("035720", [100.0, 110.0, 95.0, 108.0, 92.0, 105.0])
    result = agent.evaluate(
        snapshot={"ranking": {}, "portfolio_patch": {}, "recent_bars": bars},
        ts=ts,
    )
    assert "rule_volatility" in result["triggered_rules"]
    assert result["risk_level"] in ("medium", "high", "critical")
    assert "035720" in result["affected_tickers"]


# ====================================================================== #
# 5. top10 붕괴 → rule_top10_collapse, risk_level='critical'
# ====================================================================== #


def test_top10_collapse_critical(agent: RiskFastAgent, ts: datetime) -> None:
    """Top10 중 high 트리거 3개 이상 → rule_top10_collapse, risk_level='critical'."""
    # 5% 이상 급락 종목 3개
    drop_bars: dict[str, list[dict]] = {}
    tickers_drop = ["005930", "000660", "035720"]
    for t in tickers_drop:
        # 100 → 93: 7% 하락
        drop_bars[t] = [{"close": float(c), "volume": 1000.0} for c in [100, 99, 97, 95, 93]]

    # ranking: tickers_drop를 top3으로 설정 (나머지 7개도 포함)
    scores = {t: float(10 - i) for i, t in enumerate(tickers_drop)}
    for i, t in enumerate(["051910", "006400", "028260", "096770", "003550", "066570", "017670"]):
        scores[t] = float(i * 0.5)

    result = agent.evaluate(
        snapshot={
            "ranking": scores,
            "portfolio_patch": {},
            "recent_bars": drop_bars,
        },
        ts=ts,
    )
    assert "rule_top10_collapse" in result["triggered_rules"]
    assert result["risk_level"] == "critical"
    assert result["recommended_action"] == "halt"


# ====================================================================== #
# 6. 레이턴시 < 50ms
# ====================================================================== #


def test_latency_under_50ms(agent: RiskFastAgent, ts: datetime) -> None:
    """recent_bars 포함 evaluate() < 50ms SLA."""
    bars: dict[str, list[dict]] = {}
    # 20 종목 × 60 bar
    for i in range(20):
        ticker = str(i).zfill(6)
        closes = [100.0 + j * 0.1 for j in range(60)]
        volumes = [1000.0] * 60
        bars[ticker] = [{"close": c, "volume": v} for c, v in zip(closes, volumes)]

    t0 = time.perf_counter()
    result = agent.evaluate(
        snapshot={"ranking": {}, "portfolio_patch": {}, "recent_bars": bars},
        ts=ts,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert elapsed_ms < 50.0, f"레이턴시 SLA 초과: {elapsed_ms:.2f}ms"
    assert result["latency_ms"] < 50.0


# ====================================================================== #
# 7. report() payload schema
# ====================================================================== #


def test_report_payload_schema(agent: RiskFastAgent, ts: datetime) -> None:
    """report() 결과에 필수 키 존재 확인."""
    bars = _make_bars("005930", [100.0, 94.0])
    eval_result = agent.evaluate(
        snapshot={"ranking": {}, "portfolio_patch": {}, "recent_bars": bars},
        ts=ts,
    )
    payload = {
        "stance": "veto_recommendation",
        "risk_level": eval_result["risk_level"],
        "macro_note_ref": None,
        "micro_note_ref": None,
        "fast_rule_match": eval_result["fast_rule_match_details"],
    }
    report = agent.report("risk_warning", payload)

    assert report["report_type"] == "risk_warning"
    assert report["agent"] == "RiskFastAgent"
    assert "payload" in report

    # payload 키 검증 (C5 risk_warning schema)
    p = report["payload"]
    assert "risk_level" in p
    assert "fast_rule_match" in p
    assert "stance" in p

    # eval_result 필수 키 검증
    for key in ("risk_level", "fast_rule_match", "triggered_rules", "affected_tickers"):
        assert key in eval_result, f"eval_result에 '{key}' 키 없음"


# ====================================================================== #
# 8. 임계값 yaml 반영 확인
# ====================================================================== #


def test_thresholds_from_yaml(ts: datetime) -> None:
    """risk_config.yaml risk_fast 값이 코드에 반영되는지 확인.

    실제 yaml 값을 읽어서 에이전트 내부 값과 비교.
    """
    config_reload("risk_config.yaml")
    rf_cfg = config_load("risk_config.yaml", "risk_fast")
    agent = RiskFastAgent()

    assert agent._intraday_drop_pct == float(rf_cfg["intraday_drop_pct"])
    assert agent._volume_spike_multiplier == float(rf_cfg["volume_spike_multiplier"])
    assert agent._volatility_bp_threshold == float(rf_cfg["volatility_bp_threshold"])
    assert agent._top10_collapse_count == int(rf_cfg["top10_collapse_count"])

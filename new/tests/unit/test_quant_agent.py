"""S1-1 QuantAgent unit tests.

커버:
  - 초기화 (passive mode vs with model)
  - on_bar (BarBuffer 저장, 필드 검증)
  - score_cross_section (active/passive/warmup 3 경로, latency SLA)
  - compute_features (feat_ prefix 4 피처, warmup 부족 시 None)
  - detect_anomalies (intraday drop z-score)
  - latency_percentiles
  - report C5 (허용 3종 vs invalid)
  - reload_model
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from src.agents.hot.quant import QuantAgent
from src.data.bar_buffer import BarBuffer
from src.models.registry import ModelRegistry


# ====================================================================== #
# Fixtures
# ====================================================================== #


class MockBooster:
    """LightGBM Booster 대체. predict(X) 호출 시 주입된 scores 반환."""

    def __init__(self, scores=None):
        self._scores = scores
        self.predict_calls = 0

    def predict(self, X):
        self.predict_calls += 1
        n = len(X)
        if self._scores is None:
            return np.linspace(0.1, 0.9, n).astype(float)
        if callable(self._scores):
            return self._scores(X)
        return np.asarray(self._scores[:n], dtype=float)


def _make_bars(
    ticker: str,
    n: int = 65,
    start_price: float = 50000.0,
    seed: int = 0,
    drift: float = 0.0,
) -> list[dict[str, Any]]:
    """합성 1분봉. warmup 이상 길이 기본 65."""
    rng = np.random.default_rng(seed)
    bars = []
    price = start_price
    for i in range(n):
        delta = rng.normal(drift, 50)
        price = max(1.0, price + float(delta))
        hour = 9 + (i // 60)
        minute = i % 60
        bars.append({
            "ticker": ticker,
            "ts_close": f"2026-04-20T{hour:02d}:{minute:02d}:00+09:00",
            "open": price,
            "high": price + 10.0,
            "low": price - 10.0,
            "close": price,
            "volume": 1000.0,
        })
    return bars


@pytest.fixture
def empty_registry(tmp_path: Path) -> ModelRegistry:
    """모델 미저장 registry."""
    return ModelRegistry(artifacts_dir=tmp_path / "lgbm")


@pytest.fixture
def populated_registry(tmp_path: Path) -> ModelRegistry:
    """Mock booster가 baseline으로 저장된 registry."""
    reg = ModelRegistry(artifacts_dir=tmp_path / "lgbm")
    mock = MockBooster(scores=None)   # default linspace
    metadata = {
        "version": "baseline",
        "bundle_id": None,
        "train_start": "2026-01-01",
        "train_end": "2026-04-19",
        "feature_cols": [
            "feat_1m_close_robust_z",
            "feat_5m_ret",
            "feat_30m_vol",
            "feat_60m_trend",
        ],
        "label_horizon_bars": 5,
        "metrics": {"ic": 0.01, "icir": 0.5, "rank_ic": 0.012,
                    "arr": 0.1, "ir": 1.0, "mdd": -0.08, "sr": 1.0},
        "data_version": "v1",
    }
    reg.save(mock, metadata, is_latest=True)
    return reg


@pytest.fixture
def agent_passive(empty_registry: ModelRegistry) -> QuantAgent:
    return QuantAgent(registry=empty_registry, bar_buffer=BarBuffer())


@pytest.fixture
def agent_active(populated_registry: ModelRegistry) -> QuantAgent:
    return QuantAgent(registry=populated_registry, bar_buffer=BarBuffer())


# ====================================================================== #
# 1. 초기화
# ====================================================================== #


def test_init_passive_mode_no_model(agent_passive: QuantAgent) -> None:
    assert agent_passive.has_model is False
    assert agent_passive.model_metadata is None


def test_init_active_mode_loads_model(agent_active: QuantAgent) -> None:
    assert agent_active.has_model is True
    assert agent_active.model_metadata is not None
    assert agent_active.model_metadata["version"] == "baseline"


def test_init_config_values(agent_passive: QuantAgent) -> None:
    assert agent_passive._warmup_bars == 60
    assert agent_passive._anomaly_zscore_threshold == 3.0
    assert agent_passive._latency_window == 1000
    assert agent_passive._multi_scale_windows == [1, 5, 30, 60]
    assert agent_passive._feature_cols == [
        "feat_1m_close_robust_z",
        "feat_5m_ret",
        "feat_30m_vol",
        "feat_60m_trend",
    ]


# ====================================================================== #
# 2. on_bar
# ====================================================================== #


def test_on_bar_appends_to_buffer(agent_passive: QuantAgent) -> None:
    bar = {
        "ticker": "005930",
        "ts_close": "2026-04-20T09:00:00+09:00",
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000.0,
    }
    agent_passive.on_bar(bar)
    latest = agent_passive._bar_buffer.get_latest("005930", n=10)
    assert len(latest) == 1
    assert latest[0]["close"] == 100.5


def test_on_bar_missing_field_raises(agent_passive: QuantAgent) -> None:
    bar = {"ticker": "005930", "open": 100.0}   # 대부분 누락
    with pytest.raises(ValueError, match="필수 필드 누락"):
        agent_passive.on_bar(bar)


def test_on_bar_pads_ticker(agent_passive: QuantAgent) -> None:
    bar = {
        "ticker": "5930",   # pad_ticker가 "005930"으로 전환
        "ts_close": "2026-04-20T09:00:00+09:00",
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000.0,
    }
    agent_passive.on_bar(bar)
    # 6자리 padding 된 ticker로 저장됨
    assert "005930" in agent_passive._bar_buffer.tickers


# ====================================================================== #
# 3. score_cross_section
# ====================================================================== #


def test_score_cross_section_passive_no_model(agent_passive: QuantAgent) -> None:
    for bar in _make_bars("005930", n=65):
        agent_passive.on_bar(bar)
    result = agent_passive.score_cross_section(["005930"], asof="2026-04-20T10:00:00+09:00")
    assert result["mode"] == "passive"
    assert result["scores"] == {}
    assert result["n_tickers"] == 0
    assert "latency_ms" in result


def test_score_cross_section_warmup_insufficient(agent_active: QuantAgent) -> None:
    # 60 bars 미만 주입
    for bar in _make_bars("005930", n=30):
        agent_active.on_bar(bar)
    result = agent_active.score_cross_section(["005930"], asof="2026-04-20T10:00:00+09:00")
    assert result["mode"] == "warmup"
    assert result["n_tickers"] == 0


def test_score_cross_section_happy_path(agent_active: QuantAgent) -> None:
    tickers = ["005930", "000660", "035420", "051910"]
    for t in tickers:
        for bar in _make_bars(t, n=65, start_price=50000 + int(t) % 1000, seed=int(t[-1])):
            agent_active.on_bar(bar)
    result = agent_active.score_cross_section(tickers, asof="2026-04-20T10:00:00+09:00")
    assert result["mode"] == "active"
    assert result["n_tickers"] == 4
    assert set(result["scores"].keys()) == set(tickers)
    for s in result["scores"].values():
        assert isinstance(s, float)
    assert result["latency_ms"] > 0


def test_score_cross_section_mixed_warmup(agent_active: QuantAgent) -> None:
    # 일부 ticker만 warmup 달성
    for bar in _make_bars("005930", n=65):
        agent_active.on_bar(bar)
    for bar in _make_bars("000660", n=30):   # 부족
        agent_active.on_bar(bar)
    result = agent_active.score_cross_section(
        ["005930", "000660"], asof="2026-04-20T10:00:00+09:00"
    )
    assert result["n_tickers"] == 1
    assert "005930" in result["scores"]
    assert "000660" not in result["scores"]


def test_score_cross_section_latency_under_100ms(agent_active: QuantAgent) -> None:
    """Hot Path SLA: 단일 20종목 추론 p95 < 100ms 목표.

    Mock booster라 실제 LightGBM 부하 없음. 코드 오버헤드만 측정 (실측 ~5ms 예상).
    50ms threshold는 CI 환경 변동성 고려한 tight 상한.
    """
    tickers = [f"00593{i}" for i in range(10)]
    for t in tickers:
        for bar in _make_bars(t, n=65, seed=int(t[-1])):
            agent_active.on_bar(bar)
    result = agent_active.score_cross_section(tickers, asof="2026-04-20T10:00:00+09:00")
    # Mock inference + feature 계산은 50ms 이내 (실측 ~5ms)
    assert result["latency_ms"] < 50.0, f"latency={result['latency_ms']}ms"


# ====================================================================== #
# 4. compute_features
# ====================================================================== #


def test_compute_features_returns_feat_prefix(agent_active: QuantAgent) -> None:
    bars = _make_bars("005930", n=65)
    feats = agent_active._compute_features(bars)
    assert feats is not None
    assert set(feats.keys()) == {
        "feat_1m_close_robust_z",
        "feat_5m_ret",
        "feat_30m_vol",
        "feat_60m_trend",
    }
    for v in feats.values():
        assert isinstance(v, float)
        assert np.isfinite(v)


def test_compute_features_none_on_short_bars(agent_active: QuantAgent) -> None:
    bars = _make_bars("005930", n=30)
    assert agent_active._compute_features(bars) is None


def test_compute_features_z_clip_on_extreme(agent_active: QuantAgent) -> None:
    """마지막 close가 극단값이면 outlier_cap_z로 clip."""
    bars = _make_bars("005930", n=65, seed=42)
    # 마지막 bar close를 10x spike
    bars[-1]["close"] = bars[-2]["close"] * 10.0
    feats = agent_active._compute_features(bars)
    assert feats is not None
    assert feats["feat_1m_close_robust_z"] == pytest.approx(
        agent_active._outlier_cap_z, abs=1e-6,
    )


# ====================================================================== #
# 5. detect_anomalies
# ====================================================================== #


def test_detect_anomalies_intraday_drop(agent_active: QuantAgent) -> None:
    # 잔잔한 시세 → 마지막 bar만 큰 하락 (z-score 크게 음수)
    bars = _make_bars("005930", n=65, seed=1)
    for b in bars[:-1]:
        b["close"] = 50000.0    # 아예 plat
    bars[-1]["close"] = 50000.0
    # 마지막 하락 삽입: 50000 → 40000 (-20% 단일 bar, 극단)
    for bar in bars:
        agent_active.on_bar(bar)
    # 한 번 더 drop bar 추가 (flat history 끝나고 현재만 drop)
    drop_bar = dict(bars[-1])
    drop_bar["ts_close"] = "2026-04-20T10:05:00+09:00"
    drop_bar["close"] = 40000.0
    agent_active.on_bar(drop_bar)

    anomalies = agent_active.detect_anomalies(["005930"], asof="2026-04-20T10:05:00+09:00")
    # flat history면 std=0이라 early-return. drop이라도 anomaly 아닐 수 있음.
    # → history에 약간의 noise 포함된 케이스로 재검사 필요
    # 여기서는 flat history 케이스는 no-anomaly.
    assert len(anomalies) == 0   # flat → sigma 0 → return (False, 0)


def test_detect_anomalies_noisy_history_detects_drop(agent_active: QuantAgent) -> None:
    bars = _make_bars("005930", n=65, seed=7)
    for bar in bars:
        agent_active.on_bar(bar)
    # 다음 bar에 큰 drop (-10σ 수준)
    last_close = bars[-1]["close"]
    drop_bar = {
        "ticker": "005930",
        "ts_close": "2026-04-20T10:05:00+09:00",
        "open": last_close,
        "high": last_close,
        "low": last_close * 0.9,
        "close": last_close * 0.9,   # -10% drop
        "volume": 1000.0,
    }
    agent_active.on_bar(drop_bar)

    anomalies = agent_active.detect_anomalies(["005930"], asof="2026-04-20T10:05:00+09:00")
    assert len(anomalies) == 1
    assert anomalies[0]["ticker"] == "005930"
    assert anomalies[0]["anomaly_type"] == "intraday_drop"
    assert anomalies[0]["z_score"] < -3.0


def test_detect_anomalies_warmup_insufficient(agent_active: QuantAgent) -> None:
    for bar in _make_bars("005930", n=30):
        agent_active.on_bar(bar)
    anomalies = agent_active.detect_anomalies(["005930"], asof="2026-04-20T09:29:00+09:00")
    assert anomalies == []


# ====================================================================== #
# 6. latency_percentiles
# ====================================================================== #


def test_latency_percentiles_empty(agent_active: QuantAgent) -> None:
    p = agent_active.latency_percentiles()
    assert p["n"] == 0
    assert p["p50"] == 0.0


def test_latency_percentiles_records(agent_active: QuantAgent) -> None:
    tickers = ["005930", "000660", "035420", "051910"]
    for t in tickers:
        for bar in _make_bars(t, n=65):
            agent_active.on_bar(bar)
    # score 여러번 호출 → 레이턴시 기록
    for _ in range(5):
        agent_active.score_cross_section(tickers, asof="2026-04-20T10:00:00+09:00")

    p = agent_active.latency_percentiles()
    assert p["n"] == 5
    assert p["p50"] >= 0.0
    assert p["p95"] >= p["p50"]
    assert p["p99"] >= p["p95"]


# ====================================================================== #
# 7. report (C5)
# ====================================================================== #


def test_report_quant_signal(agent_passive: QuantAgent) -> None:
    r = agent_passive.report("quant_signal", {"ticker": "005930", "score": 0.7})
    assert r["report_type"] == "quant_signal"
    assert r["agent"] == "QuantAgent"
    assert r["payload"]["score"] == 0.7


def test_report_quant_alert(agent_passive: QuantAgent) -> None:
    r = agent_passive.report("quant_alert", {"reason": "latency_exceeded"})
    assert r["report_type"] == "quant_alert"


def test_report_anomaly_detected(agent_passive: QuantAgent) -> None:
    r = agent_passive.report("anomaly_detected", {"ticker": "005930", "z": -4.5})
    assert r["report_type"] == "anomaly_detected"


def test_report_invalid_type_raises(agent_passive: QuantAgent) -> None:
    with pytest.raises(ValueError, match="invalid report_type"):
        agent_passive.report("news_signal", {})   # News는 QuantAgent publishes 아님


# ====================================================================== #
# 8. reload_model
# ====================================================================== #


def test_reload_model_success(
    agent_passive: QuantAgent,
    populated_registry: ModelRegistry,
) -> None:
    # 시작 시 passive. 이후 registry에 모델 있는 상태로 교체 reload.
    agent_passive._registry = populated_registry
    ok = agent_passive.reload_model()
    assert ok is True
    assert agent_passive.has_model is True


def test_reload_model_failure_keeps_passive(
    agent_passive: QuantAgent,
    tmp_path: Path,
) -> None:
    empty = ModelRegistry(artifacts_dir=tmp_path / "empty_lgbm")
    agent_passive._registry = empty
    ok = agent_passive.reload_model()
    assert ok is False
    assert agent_passive.has_model is False

"""S4-4 HotPathProfiler unit tests.

1. percentile 산출 정확성
2. SLA violation alert 생성
3. JSON 리포트 형식
4. multi-stage 측정 분리
5. end-to-end 합성 시뮬 (mock LGBM, p95 측정)
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest

from src.ops.profiler import HotPathProfiler, HOT_STAGES


# ====================================================================== #
# Fixtures
# ====================================================================== #

@pytest.fixture()
def profiler(tmp_path: Path) -> HotPathProfiler:
    """테스트용 profiler (tmp_path 사용, SLA p95=50ms로 낮춰서 위반 테스트 가능)."""
    p = HotPathProfiler(
        window_size=200,
        alerts_path=tmp_path / "ops_alerts.jsonl",
        profiling_dir=tmp_path / "profiling",
    )
    # SLA 강제 override (테스트 환경에서 50ms → 위반 유도 가능하게)
    p._sla = {"p50_ms": 50.0, "p95_ms": 100.0, "p99_ms": 150.0}
    return p


# ====================================================================== #
# 1. percentile 산출 정확성
# ====================================================================== #

def test_percentile_empty(profiler: HotPathProfiler) -> None:
    """기록 없을 때 percentiles 0 반환."""
    stats = profiler.percentiles("quant")
    assert stats["p50"] == 0.0
    assert stats["p95"] == 0.0
    assert stats["p99"] == 0.0
    assert stats["n"] == 0


def test_percentile_single_value(profiler: HotPathProfiler) -> None:
    """값 1개일 때 p50/p95/p99 모두 동일."""
    profiler.record("quant", 10.0)
    stats = profiler.percentiles("quant")
    assert stats["p50"] == pytest.approx(10.0, abs=1e-6)
    assert stats["p95"] == pytest.approx(10.0, abs=1e-6)
    assert stats["n"] == 1


def test_percentile_uniform_distribution(profiler: HotPathProfiler) -> None:
    """0~100ms 균일 분포 100개. p50≈50 확인. p95는 numpy 기준 ±10 허용."""
    rng = np.random.default_rng(42)
    values = rng.uniform(0, 100, 100)
    for v in values:
        profiler.record("quant", float(v))

    # numpy 직접 계산과 동일해야 함
    expected_p50 = float(np.percentile(values, 50))
    expected_p95 = float(np.percentile(values, 95))

    stats = profiler.percentiles("quant")
    assert stats["p50"] == pytest.approx(expected_p50, abs=0.01)
    assert stats["p95"] == pytest.approx(expected_p95, abs=0.01)
    assert stats["n"] == 100


def test_percentile_known_values(profiler: HotPathProfiler) -> None:
    """알려진 값 10개. p50/p95 정확성 검증."""
    for v in range(1, 11):  # 1, 2, ..., 10
        profiler.record("ppo", float(v))

    stats = profiler.percentiles("ppo")
    # numpy.percentile(1..10, 50) = 5.5
    assert stats["p50"] == pytest.approx(5.5, abs=0.1)
    # numpy.percentile(1..10, 95) = 9.55
    assert stats["p95"] == pytest.approx(9.55, abs=0.1)


# ====================================================================== #
# 2. SLA violation alert 생성
# ====================================================================== #

def test_sla_no_violation(profiler: HotPathProfiler) -> None:
    """모든 값이 SLA 이내 → violation 없음."""
    for _ in range(10):
        profiler.record("hot_loop", 30.0)  # p95 100ms 기준 이하

    violations = profiler.check_sla()
    assert violations == []


def test_sla_violation_detected(profiler: HotPathProfiler, tmp_path: Path) -> None:
    """p95 SLA(100ms) 초과 시 violation 검출 + alert 파일 기록.

    p95 > 100ms 확보 방법:
      값 20개 중 17개 = 10ms, 3개 = 200ms.
      np.percentile([10]*17+[200]*3, 95) 는 200ms → SLA(100ms) 확실 초과.
    """
    values = [10.0] * 17 + [200.0] * 3  # p95 ≈ 200ms > SLA 100ms
    for v in values:
        profiler.record("hot_loop", v)

    violations = profiler.check_sla(min_samples=5)
    stages_violated = [v["stage"] for v in violations]
    assert "hot_loop" in stages_violated, (
        f"hot_loop SLA 위반 미탐지. percentiles={profiler.percentiles('hot_loop')}"
    )

    # ops_alerts.jsonl 파일 생성 확인
    alerts_path = tmp_path / "ops_alerts.jsonl"
    assert alerts_path.exists(), "SLA 위반 시 ops_alerts.jsonl 기록 필수"

    with alerts_path.open(encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert len(lines) >= 1
    assert lines[-1]["type"] == "hot_path_sla_violation"


def test_sla_violation_min_samples_skip(profiler: HotPathProfiler) -> None:
    """min_samples 미만이면 SLA 검증 skip."""
    profiler.record("quant", 999.0)  # 1개만 기록
    violations = profiler.check_sla(min_samples=5)
    # n=1 < min_samples=5 → skip → violation 없음
    quant_violated = [v for v in violations if v["stage"] == "quant"]
    assert quant_violated == []


def test_sla_alert_content(profiler: HotPathProfiler, tmp_path: Path) -> None:
    """alert JSONL 내용 검증: type / violations 필드."""
    for _ in range(10):
        profiler.record("hot_loop", 200.0)  # p99 150ms 초과 확정

    profiler.check_sla(min_samples=5)

    alerts_path = tmp_path / "ops_alerts.jsonl"
    with alerts_path.open(encoding="utf-8") as f:
        alert = json.loads(f.readline())

    assert "ts" in alert
    assert alert["type"] == "hot_path_sla_violation"
    assert isinstance(alert["violations"], list)
    assert len(alert["violations"]) > 0

    first = alert["violations"][0]
    assert "stage" in first
    assert "violations" in first
    for vi in first["violations"]:
        assert "percentile" in vi
        assert "actual_ms" in vi
        assert "sla_ms" in vi


# ====================================================================== #
# 3. JSON 리포트 형식
# ====================================================================== #

def test_write_report_creates_file(profiler: HotPathProfiler, tmp_path: Path) -> None:
    """write_report() 파일 생성 + 필수 키 존재."""
    for stage in ("quant", "ppo", "hot_loop"):
        for _ in range(5):
            profiler.record(stage, 10.0)

    out = tmp_path / "report.json"
    result_path = profiler.write_report(out)

    assert result_path == out
    assert out.exists()

    with out.open(encoding="utf-8") as f:
        data = json.load(f)

    assert "generated_at" in data
    assert "sla_thresholds" in data
    assert "stages" in data
    assert "sla_violations" in data
    assert "tick_count" in data
    assert "window_size" in data


def test_write_report_stages_structure(profiler: HotPathProfiler, tmp_path: Path) -> None:
    """각 stage 데이터에 p50/p95/p99/max/n 키 존재."""
    for _ in range(5):
        profiler.record("quant", 20.0)

    out = tmp_path / "stages_report.json"
    profiler.write_report(out)

    with out.open(encoding="utf-8") as f:
        data = json.load(f)

    assert "quant" in data["stages"]
    quant_stats = data["stages"]["quant"]
    for key in ("p50", "p95", "p99", "max", "n"):
        assert key in quant_stats, f"'{key}' 키 누락"


def test_write_report_default_path(profiler: HotPathProfiler, tmp_path: Path) -> None:
    """output=None → profiling_dir/hotpath_YYYYMMDD.json 자동 생성."""
    for _ in range(3):
        profiler.record("hot_loop", 5.0)

    path = profiler.write_report()

    assert path.exists()
    assert "hotpath_" in path.name
    assert path.suffix == ".json"


# ====================================================================== #
# 4. multi-stage 측정 분리
# ====================================================================== #

def test_multi_stage_independent(profiler: HotPathProfiler) -> None:
    """여러 stage 독립 측정. 서로 영향 없음."""
    for _ in range(20):
        profiler.record("quant", 10.0)
    for _ in range(20):
        profiler.record("ppo", 50.0)
    for _ in range(20):
        profiler.record("fda", 30.0)

    quant_stats = profiler.percentiles("quant")
    ppo_stats = profiler.percentiles("ppo")
    fda_stats = profiler.percentiles("fda")

    assert quant_stats["p50"] == pytest.approx(10.0, abs=1.0)
    assert ppo_stats["p50"] == pytest.approx(50.0, abs=1.0)
    assert fda_stats["p50"] == pytest.approx(30.0, abs=1.0)

    # quant/ppo/fda 간 p50 서로 다름
    assert quant_stats["p50"] != ppo_stats["p50"]


def test_all_stages_in_percentiles(profiler: HotPathProfiler) -> None:
    """percentiles(None) → 기록된 모든 stage 반환."""
    for stage in ("quant", "ppo", "pm"):
        profiler.record(stage, 5.0)

    all_stats = profiler.percentiles()
    assert "quant" in all_stats
    assert "ppo" in all_stats
    assert "pm" in all_stats


def test_start_end_stage_timing(profiler: HotPathProfiler) -> None:
    """start_stage/end_stage 패턴으로 실시간 측정."""
    t0 = profiler.start_stage("quant")
    time.sleep(0.01)  # 10ms
    ms = profiler.end_stage("quant", t0)

    assert ms >= 8.0, f"측정값 {ms:.2f}ms < 8ms (sleep 10ms)"
    stats = profiler.percentiles("quant")
    assert stats["n"] == 1
    assert stats["p50"] >= 8.0


def test_record_tick_meta(profiler: HotPathProfiler) -> None:
    """record_tick() 호출 후 tick_count 증가."""
    for i in range(5):
        profiler.record_tick(n_tickers=20, ts=f"2026-01-02T09:0{i}:00+09:00")

    assert len(profiler._tick_meta) == 5


def test_reset_clears_records(profiler: HotPathProfiler) -> None:
    """reset() 후 모든 기록 제거."""
    for _ in range(10):
        profiler.record("quant", 5.0)

    profiler.reset()

    stats = profiler.percentiles("quant")
    assert stats["n"] == 0
    assert len(profiler._tick_meta) == 0


# ====================================================================== #
# 5. end-to-end 합성 시뮬
# ====================================================================== #

def test_e2e_synthetic_sla_pass(profiler: HotPathProfiler) -> None:
    """합성 20종목 100 tick 시뮬. hot_loop p95 < 100ms 확인."""
    import numpy as np
    import random

    n_tickers = 20
    n_ticks = 100
    warmup = 70

    # 합성 BarBuffer (deque)
    from collections import deque
    bars_store: dict[str, deque] = {
        str(i).zfill(6): deque(maxlen=200) for i in range(n_tickers)
    }

    class MockBooster:
        def predict(self, X: np.ndarray) -> np.ndarray:
            return np.dot(X, np.random.randn(X.shape[1]) * 0.1)

    booster = MockBooster()
    tickers = list(bars_store.keys())
    prices = {t: 50000.0 for t in tickers}

    # warmup
    for _ in range(warmup):
        for t in tickers:
            prices[t] *= (1 + random.gauss(0, 0.002))
            bars_store[t].append({
                "close": prices[t],
                "open": prices[t],
                "high": prices[t],
                "low": prices[t],
                "volume": 1000,
            })

    # 실 측정
    mad_constant = 1.4826
    outlier_cap_z = 5.0

    for _ in range(n_ticks):
        t_loop = profiler.start_stage("hot_loop")
        t_quant = profiler.start_stage("quant")

        feature_matrix = []
        valid = []
        for t in tickers:
            bars = list(bars_store[t])
            if len(bars) < 60:
                continue
            closes = np.array([b["close"] for b in bars], dtype=float)
            last = closes[-1]

            feat_5m = last / closes[-6] - 1.0 if closes[-6] > 1e-8 else 0.0
            feat_30v = float(closes[-30:].std() / closes[-30:].mean()) if closes[-30:].mean() > 1e-8 else 0.0
            w = min(60, len(closes))
            y = closes[-w:]
            x = np.arange(w, dtype=float)
            xm = x.mean()
            xv = float(np.sum((x - xm) ** 2))
            ym = float(y.mean())
            feat_trend = (float(np.sum((x - xm) * (y - ym))) / xv) / ym if xv > 1e-12 and ym > 1e-8 else 0.0
            block = closes[-w:]
            med = float(np.median(block))
            mad = float(np.median(np.abs(block - med)))
            denom = max(mad * mad_constant, 1e-8)
            feat_z = float(np.clip((last - med) / denom, -outlier_cap_z, outlier_cap_z))

            feature_matrix.append([feat_z, feat_5m, feat_30v, feat_trend])
            valid.append(t)

        if valid:
            X = np.asarray(feature_matrix, dtype=float)
            preds = booster.predict(X)
            _ = np.argsort(preds)[::-1]

        profiler.end_stage("quant", t_quant)
        profiler.end_stage("hot_loop", t_loop)

        for t in tickers:
            prices[t] *= (1 + random.gauss(0, 0.002))
            bars_store[t].append({
                "close": prices[t],
                "open": prices[t],
                "high": prices[t],
                "low": prices[t],
                "volume": 1000,
            })

    stats = profiler.percentiles("hot_loop")
    assert stats["n"] == n_ticks
    assert stats["p95"] < 100.0, (
        f"합성 Hot Path p95={stats['p95']:.2f}ms >= 100ms. 성능 문제 가능."
    )

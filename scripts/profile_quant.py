#!/usr/bin/env python3
"""S4-4 Hot Path 성능 프로파일 스크립트.

20 종목 x N ticks 합성 시뮬레이션으로 각 단계 레이턴시 측정.
실 booster 없이 mock LGBM으로 HotRunner 전체 파이프라인 측정.

사용:
    python scripts/profile_quant.py
    python scripts/profile_quant.py --tickers 20 --ticks 1000
    python scripts/profile_quant.py --tickers 20 --ticks 500 --output artifacts/profiling/custom.json

출력:
    콘솔: 단계별 p50/p95/p99 요약
    파일: artifacts/profiling/hotpath_YYYYMMDD.json
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가 (new/ 하위 import 경로)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_NEW_DIR = _PROJECT_ROOT / "new"
if str(_NEW_DIR) not in sys.path:
    sys.path.insert(0, str(_NEW_DIR))

import numpy as np

from src.ops.profiler import HotPathProfiler, HOT_STAGES
from src.utils.ticker_utils import pad_ticker
from src.utils.logger import get_logger

logger = get_logger("profile_quant")


# ====================================================================== #
# Mock 컴포넌트 (실 데이터 없이 레이턴시 측정용)
# ====================================================================== #

class _MockBooster:
    """lightgbm.Booster 최소 mock. predict() 레이턴시만 측정."""

    def predict(self, X: np.ndarray) -> np.ndarray:
        # 실 LGBM은 leaf 탐색 오버헤드 존재. mock은 pure numpy dot으로 근사.
        n = X.shape[0]
        weights = np.random.randn(X.shape[1]) * 0.1
        return np.dot(X, weights) + np.random.randn(n) * 0.01


def _make_bar(ticker: str, price: float) -> dict:
    """합성 1분봉 생성."""
    return {
        "ticker": ticker,
        "open": price * (1 + random.gauss(0, 0.001)),
        "high": price * (1 + abs(random.gauss(0, 0.002))),
        "low": price * (1 - abs(random.gauss(0, 0.002))),
        "close": price * (1 + random.gauss(0, 0.001)),
        "volume": random.randint(1000, 50000),
        "vwap": price * (1 + random.gauss(0, 0.0005)),
        "ts_close": f"2026-01-02T{random.randint(9,15):02d}:{random.randint(0,59):02d}:00+09:00",
    }


# ====================================================================== #
# 시뮬레이션 실행
# ====================================================================== #

def run_simulation(
    n_tickers: int = 20,
    n_ticks: int = 1000,
    warmup_ticks: int = 70,
    output: Path | None = None,
) -> dict:
    """Hot Path 단계별 레이턴시 시뮬레이션.

    실 HotRunner 대신 각 단계를 개별 측정. HotRunner는 외부 의존 많아서
    mock으로 대체하고 실제 compute-bound 코드 경로(feature 계산 + numpy)만 실행.

    단계:
      quant_feature: BarBuffer batch load + _compute_features x n_tickers
      quant_predict: MockBooster.predict (n_tickers x 4 features)
      quant_rank: cross-sectional rank (argsort)
      hot_loop: 위 3단계 합산 (ppo/pm/fda는 trivial cost로 제외)
    """
    profiler = HotPathProfiler(window_size=n_ticks, profiling_dir=output.parent if output else None)

    # 합성 ticker 목록 (20개)
    tickers = [pad_ticker(str(6000 + i)) for i in range(n_tickers)]

    # 각 ticker 가격 초기화
    prices = {t: 50000 + random.randint(-10000, 10000) for t in tickers}

    # BarBuffer 내부 구조를 mock으로 대체 (단순 deque)
    from collections import deque
    bars_store: dict[str, deque] = {t: deque(maxlen=200) for t in tickers}

    # mock booster
    booster = _MockBooster()
    n_features = 4  # feat_1m_close_robust_z / feat_5m_ret / feat_30m_vol / feat_60m_trend

    # warmup: warmup_ticks 분 데이터 채우기 (레이턴시 미측정)
    print(f"[profile_quant] warmup {warmup_ticks} ticks ...")
    for _ in range(warmup_ticks):
        for t in tickers:
            prices[t] = prices[t] * (1 + random.gauss(0, 0.002))
            bars_store[t].append(_make_bar(t, prices[t]))

    # 본 시뮬
    print(f"[profile_quant] 시뮬 시작: {n_tickers} tickers x {n_ticks} ticks ...")

    for tick_i in range(n_ticks):
        t_loop = time.perf_counter()

        # bar 갱신
        for t in tickers:
            prices[t] = prices[t] * (1 + random.gauss(0, 0.002))
            bars_store[t].append(_make_bar(t, prices[t]))

        # --- Stage: quant (feature 계산 + predict + rank) ---
        t_quant = profiler.start_stage("quant")

        # feature 계산 (QuantAgent._compute_features 동일 로직)
        feature_matrix: list[list[float]] = []
        valid_tickers: list[str] = []
        mad_constant = 1.4826
        outlier_cap_z = 5.0

        for t in tickers:
            bars = list(bars_store[t])
            if len(bars) < 60:
                continue
            closes = np.array([float(b["close"]) for b in bars], dtype=float)
            last = float(closes[-1])

            # feat_5m_ret
            feat_5m_ret = last / float(closes[-6]) - 1.0 if closes[-6] > 1e-8 else 0.0

            # feat_30m_vol
            feat_30m_vol = (
                float(closes[-30:].std(ddof=0) / closes[-30:].mean())
                if closes[-30:].mean() > 1e-8 else 0.0
            )

            # feat_60m_trend (linear slope)
            w = min(60, len(closes))
            y = closes[-w:]
            x = np.arange(w, dtype=float)
            mean_y = float(y.mean())
            if mean_y > 1e-8 and w >= 2:
                x_mean = x.mean()
                x_var = float(np.sum((x - x_mean) ** 2))
                cov = float(np.sum((x - x_mean) * (y - mean_y)))
                feat_60m_trend = (cov / x_var) / mean_y if x_var > 1e-12 else 0.0
            else:
                feat_60m_trend = 0.0

            # feat_1m_close_robust_z (MAD)
            block = closes[-w:]
            med = float(np.median(block))
            mad = float(np.median(np.abs(block - med)))
            denom = mad * mad_constant
            if denom < 1e-8:
                denom = 1e-8
            z_raw = (last - med) / denom
            feat_robust_z = float(np.clip(z_raw, -outlier_cap_z, outlier_cap_z))

            feature_matrix.append([feat_robust_z, feat_5m_ret, feat_30m_vol, feat_60m_trend])
            valid_tickers.append(t)

        # predict
        if valid_tickers:
            X = np.asarray(feature_matrix, dtype=float)
            preds = booster.predict(X)

            # cross-sectional rank
            _ = np.argsort(preds)[::-1]

        quant_ms = profiler.end_stage("quant", t_quant)

        # ppo/pm/fda: dict lookup + 간단 계산만 (실 비용 무시 가능 수준)
        t_ppo = profiler.start_stage("ppo")
        dummy_weights = {t: 1.0 / max(len(valid_tickers), 1) for t in valid_tickers}
        ppo_ms = profiler.end_stage("ppo", t_ppo)

        t_pm = profiler.start_stage("pm")
        _ = {t: dummy_weights.get(t, 0.0) for t in tickers}
        pm_ms = profiler.end_stage("pm", t_pm)

        t_rf = profiler.start_stage("risk_fast")
        # risk_fast: 6 규칙 평가 비용 근사 (비LLM, dict access)
        _ = [t for t in tickers if abs(random.gauss(0, 1)) > 3.0]
        risk_fast_ms = profiler.end_stage("risk_fast", t_rf)

        t_fda = profiler.start_stage("fda")
        # fda: approve/veto 판정만 (비LLM hot path)
        _ = "approve" if random.random() > 0.1 else "veto"
        fda_ms = profiler.end_stage("fda", t_fda)

        loop_ms = (time.perf_counter() - t_loop) * 1000.0
        profiler.record("hot_loop", loop_ms)

        profiler.record_tick(
            n_tickers=len(valid_tickers),
            ts=f"2026-01-02T09:{tick_i % 60:02d}:00+09:00",
            stage_ms={
                "quant": quant_ms,
                "ppo": ppo_ms,
                "pm": pm_ms,
                "risk_fast": risk_fast_ms,
                "fda": fda_ms,
                "hot_loop": loop_ms,
            },
        )

    # 결과 출력
    print()
    print(profiler.summary_text())
    print()

    # SLA 위반 체크
    violations = profiler.check_sla()
    if violations:
        print("[profile_quant] SLA 위반:")
        for v in violations:
            for vi in v["violations"]:
                print(
                    f"  {v['stage']} {vi['percentile']}: "
                    f"{vi['actual_ms']:.2f}ms > {vi['sla_ms']:.0f}ms "
                    f"(+{vi['excess_ms']:.2f}ms)"
                )
    else:
        print("[profile_quant] SLA 전체 PASS.")

    # 리포트 저장
    report_path = profiler.write_report(output)
    print(f"[profile_quant] 리포트 저장: {report_path}")

    return profiler.percentiles()


# ====================================================================== #
# CLI
# ====================================================================== #

def main() -> None:
    parser = argparse.ArgumentParser(description="Hot Path 레이턴시 시뮬")
    parser.add_argument("--tickers", type=int, default=20, help="종목 수 (default=20)")
    parser.add_argument("--ticks", type=int, default=1000, help="시뮬 tick 수 (default=1000)")
    parser.add_argument("--warmup", type=int, default=70, help="warmup tick 수 (default=70)")
    parser.add_argument("--output", type=str, default=None, help="리포트 저장 경로")
    args = parser.parse_args()

    output = Path(args.output) if args.output else None
    run_simulation(
        n_tickers=args.tickers,
        n_ticks=args.ticks,
        warmup_ticks=args.warmup,
        output=output,
    )


if __name__ == "__main__":
    main()

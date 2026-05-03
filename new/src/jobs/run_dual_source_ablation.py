"""S4-2 Dual-Source Ablation 실행 스크립트.

Configuration A (w/o dual_source, baseline 4피처) vs
Configuration B (w/ dual_source, baseline + 5피처) 비교.

## 실행 흐름

    1. risk_config.yaml 기준값 로드
    2. Configuration A: enabled_for_lgbm=False 환경으로 LGBMTrainer 실행
    3. Configuration B: enabled_for_lgbm=True 환경으로 LGBMTrainer 실행
    4. IC / Rank IC / Sharpe / MDD / NDCG@5 delta 계산
    5. verdict 판정 (improvement / regression / neutral)
    6. new/artifacts/ablation/dual_source_YYYYMMDD.json 저장

## 합성 데이터 한계

    baseline.pkl은 합성 1분봉 (mock bar 6 ticker × 108일).
    실 IC/Sharpe 개선은 S4-6 paper trading 이후 측정.
    본 스크립트는 ablation framework 동작 검증 목적.

## PIT-Safety

    Dual-Source 점수 파일 없으면 기본값 0.0 (DatasetBuilder join 단계에서 처리).
    학습 데이터 자체는 DatasetBuilder PIT-safe 보장 (leakage_guard=True).

불변 원칙:
    - 모든 임계값: risk_config.yaml
    - 하드코딩 금지
    - Mode A 비개입 (학습 스크립트는 오프라인 전용)
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# 프로젝트 루트를 sys.path에 추가 (scripts 직접 실행 시)
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "new"))

from src.data.dataset_builder import DatasetBuilder
from src.models.lgbm_trainer import LGBMTrainer, _load_feature_cols
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger

logger = get_logger("dual_source_ablation")

_KST = ZoneInfo("Asia/Seoul")
_ABLATION_DIR = _PROJECT_ROOT / "new" / "artifacts" / "ablation"

# 합성 데이터 ticker 목록 (S1-0에서 생성한 mock bar 6종목)
_DEFAULT_TICKERS = ["005930", "000660", "035420", "005380", "051910", "035720"]
_DEFAULT_START = "20260101"
_DEFAULT_END = "20260419"


def _compute_ndcg5(y_true_by_group: dict, y_pred_by_group: dict) -> float:
    """NDCG@5 계산. 그룹별 평균 반환.

    top_k=5 기준. 그룹 크기 < 5면 min(5, len(group)) 사용.
    """
    import numpy as np
    import math as _math

    ndcg_vals: list[float] = []
    for ts in y_true_by_group:
        y_true = y_true_by_group[ts]
        y_pred = y_pred_by_group[ts]
        n = len(y_true)
        if n < 2:
            continue

        k = min(5, n)

        # Ideal DCG: true label 내림차순 정렬
        ideal_order = np.argsort(y_true)[::-1]
        idcg = sum(
            (2 ** float(y_true[ideal_order[i]]) - 1) / _math.log2(i + 2)
            for i in range(k)
        )

        # Predicted order: pred 내림차순으로 top-k 선택
        pred_order = np.argsort(y_pred)[::-1]
        dcg = sum(
            (2 ** float(y_true[pred_order[i]]) - 1) / _math.log2(i + 2)
            for i in range(k)
        )

        if idcg > 1e-12:
            ndcg_vals.append(dcg / idcg)

    if not ndcg_vals:
        return 0.0
    return float(sum(ndcg_vals) / len(ndcg_vals))


def _run_config(
    config_label: str,
    tickers: list[str],
    start_date: str,
    end_date: str,
    enabled_for_lgbm: bool,
    version: str,
) -> dict[str, Any]:
    """단일 Configuration 학습 + 메트릭 반환.

    Args:
        config_label: "baseline" 또는 "with_dual_source"
        enabled_for_lgbm: Dual-Source 5피처 포함 여부
        version: ModelRegistry에 저장할 버전명

    Returns:
        {
          "ic": float, "rank_ic": float, "sharpe": float,
          "mdd": float, "ndcg5": float,
          "version": str, "model_path": str, "feature_cols": list
        }
    """
    logger.info(
        "[ablation] Config %s 실행: enabled_for_lgbm=%s version=%s",
        config_label, enabled_for_lgbm, version,
    )

    # 환경 변수로 enabled_for_lgbm 주입 (risk_config.yaml 우선 원칙 유지,
    # DatasetBuilder/LGBMTrainer는 yaml 로드. 단 테스트 환경에서 override 필요.)
    # 실제 환경에서는 risk_config.yaml을 수정 후 실행하는 것이 정석.
    # 여기서는 DatasetBuilder를 직접 설정 주입해 우회.
    builder = DatasetBuilder()
    builder._ds_enabled_for_lgbm = enabled_for_lgbm

    # feature_cols: enabled_for_lgbm에 따라 분기
    if enabled_for_lgbm:
        feature_cols = _load_feature_cols()  # dual_source_feature_cols 포함
    else:
        # base 4피처만
        pre_cfg = config_load("risk_config.yaml", "preprocessor")
        feature_cols = list(pre_cfg["feature_cols"])

    from src.models.splitter import WalkForwardSplitter
    from src.models.registry import ModelRegistry

    trainer = LGBMTrainer(dataset_builder=builder)
    # feature_cols override (enabled_for_lgbm=false 시 4피처만)
    trainer.feature_cols = feature_cols

    result = trainer.train(
        tickers=tickers,
        start_date=start_date,
        end_date=end_date,
        version=version,
    )

    metrics = result.get("metrics", {})

    logger.info(
        "[ablation] Config %s 완료: IC=%.4f, RankIC=%.4f, SR=%.4f, MDD=%.4f",
        config_label,
        metrics.get("ic", 0.0),
        metrics.get("rank_ic", 0.0),
        metrics.get("sr", 0.0),
        metrics.get("mdd", 0.0),
    )

    return {
        "ic": float(metrics.get("ic", 0.0)),
        "rank_ic": float(metrics.get("rank_ic", 0.0)),
        "sharpe": float(metrics.get("sr", 0.0)),
        "mdd": float(metrics.get("mdd", 0.0)),
        "ndcg5": 0.0,  # LGBMTrainer.train() 경유 시 ndcg5 직접 미계산. 추후 확장.
        "version": result.get("version", version),
        "model_path": result.get("model_path", ""),
        "feature_cols": list(feature_cols),
        "n_folds": result.get("n_folds", 0),
    }


def _compute_delta(baseline: dict[str, Any], with_ds: dict[str, Any]) -> dict[str, str]:
    """두 Configuration의 주요 지표 delta 계산."""
    def fmt(val: float) -> str:
        sign = "+" if val >= 0 else ""
        return f"{sign}{val:.4f}"

    return {
        "ic": fmt(with_ds["ic"] - baseline["ic"]),
        "rank_ic": fmt(with_ds["rank_ic"] - baseline["rank_ic"]),
        "sharpe": fmt(with_ds["sharpe"] - baseline["sharpe"]),
        "mdd": fmt(with_ds["mdd"] - baseline["mdd"]),
        "ndcg5": fmt(with_ds["ndcg5"] - baseline["ndcg5"]),
    }


def _verdict(delta: dict[str, str], threshold_sharpe: float) -> str:
    """verdict 판정: improvement / regression / neutral.

    기준: risk_config.yaml validation_tools.performance_analyzer.verdict_improve_sharpe_threshold
    """
    sharpe_delta = float(delta["sharpe"])
    if sharpe_delta >= threshold_sharpe:
        return "improvement"
    if sharpe_delta <= -threshold_sharpe:
        return "regression"
    return "neutral"


def run_ablation(
    tickers: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Dual-Source Ablation 실행 진입점.

    Returns:
        ablation 결과 딕셔너리 (dual_source_YYYYMMDD.json 저장).
    """
    today = datetime.now(_KST).strftime("%Y%m%d")
    tickers = tickers or _DEFAULT_TICKERS
    start_date = start_date or _DEFAULT_START
    end_date = end_date or _DEFAULT_END

    # verdict 임계값 yaml 로드 (하드코딩 금지)
    pa_cfg = config_load("risk_config.yaml", "validation_tools.performance_analyzer") or {}
    threshold_sharpe = float(pa_cfg.get("verdict_improve_sharpe_threshold", 0.1))

    logger.info(
        "[ablation] S4-2 Dual-Source Ablation 시작: tickers=%d, %s~%s",
        len(tickers), start_date, end_date,
    )

    # Configuration A: w/o dual_source (baseline)
    baseline_result = _run_config(
        config_label="baseline",
        tickers=tickers,
        start_date=start_date,
        end_date=end_date,
        enabled_for_lgbm=False,
        version="baseline",
    )

    # Configuration B: w/ dual_source
    with_ds_result = _run_config(
        config_label="with_dual_source",
        tickers=tickers,
        start_date=start_date,
        end_date=end_date,
        enabled_for_lgbm=True,
        version="baseline_with_dual_source",
    )

    # delta 계산
    delta = _compute_delta(baseline_result, with_ds_result)
    verdict = _verdict(delta, threshold_sharpe)

    payload: dict[str, Any] = {
        "ablation_name": "dual_source",
        "date": today,
        "tickers": tickers,
        "train_range": {"start": start_date, "end": end_date},
        "data_note": (
            "합성 데이터 (mock bar 6 ticker x 108일). "
            "실 IC/Sharpe 개선은 S4-6 paper trading 이후 측정."
        ),
        "baseline": {
            "ic": baseline_result["ic"],
            "rank_ic": baseline_result["rank_ic"],
            "sharpe": baseline_result["sharpe"],
            "mdd": baseline_result["mdd"],
            "ndcg5": baseline_result["ndcg5"],
            "model_path": baseline_result["model_path"],
            "feature_cols": baseline_result["feature_cols"],
        },
        "with_dual_source": {
            "ic": with_ds_result["ic"],
            "rank_ic": with_ds_result["rank_ic"],
            "sharpe": with_ds_result["sharpe"],
            "mdd": with_ds_result["mdd"],
            "ndcg5": with_ds_result["ndcg5"],
            "model_path": with_ds_result["model_path"],
            "feature_cols": with_ds_result["feature_cols"],
        },
        "delta": delta,
        "verdict": verdict,
        "threshold_sharpe_used": threshold_sharpe,
    }

    # 저장
    _ABLATION_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _ABLATION_DIR / f"dual_source_{today}.json"
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    logger.info(
        "[ablation] 완료. verdict=%s delta_sharpe=%s → %s",
        verdict, delta["sharpe"], out_path,
    )

    return payload


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_ablation()
    print(json.dumps(result, ensure_ascii=False, indent=2))

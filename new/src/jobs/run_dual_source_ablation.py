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

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

# 프로젝트 루트를 sys.path에 추가 (scripts 직접 실행 시)
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "new"))

from src.data.dataset_builder import DatasetBuildError, DatasetBuilder
from src.models.lgbm_trainer import LGBMTrainer, _load_feature_cols
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger

logger = get_logger("dual_source_ablation")

_KST = ZoneInfo("Asia/Seoul")
_ABLATION_DIR = _PROJECT_ROOT / "new" / "artifacts" / "ablation"

_FALLBACK_TICKERS = ["005930", "000660", "042700", "403870", "051910"]
_DEFAULT_START = "20260101"
_DEFAULT_END = "20260419"
_DATA_ROOT = _PROJECT_ROOT / "artifacts" / "data"


def _load_default_tickers() -> list[str]:
    """universe_config.yaml의 active 종목을 ablation 기본 universe로 사용."""
    cfg = config_load("universe_config.yaml") or {}
    sectors = cfg.get("sectors", {}) if isinstance(cfg, dict) else {}
    tickers: list[str] = []
    for sector in sectors.values():
        if not isinstance(sector, dict):
            continue
        for stock in sector.get("stocks", []) or []:
            if not isinstance(stock, dict) or stock.get("status") != "active":
                continue
            ticker = str(stock.get("ticker", "")).zfill(6)
            if ticker.isdigit() and len(ticker) == 6:
                tickers.append(ticker)
    return tickers or list(_FALLBACK_TICKERS)


def _infer_artifact_date_range(tickers: list[str]) -> tuple[str | None, str | None]:
    """저장된 1분봉 artifact에서 cross-sectional 학습 가능 날짜 범위를 추론."""
    date_counts: dict[str, int] = {}
    for ticker in tickers:
        ticker_dir = _DATA_ROOT / ticker
        if not ticker_dir.exists():
            continue
        for path in ticker_dir.glob("bars_1m_*.parquet"):
            date = path.stem.replace("bars_1m_", "")
            if date.isdigit() and len(date) == 8:
                date_counts[date] = date_counts.get(date, 0) + 1
        for path in ticker_dir.glob("bars_1m_*.jsonl"):
            date = path.stem.replace("bars_1m_", "")
            if date.isdigit() and len(date) == 8:
                date_counts[date] = date_counts.get(date, 0) + 1

    # DatasetBuilder cross-sectional rank는 최소 4종목 그룹을 요구한다.
    usable_dates = sorted(date for date, count in date_counts.items() if count >= 4)
    if not usable_dates:
        return None, None
    return usable_dates[0], usable_dates[-1]


def _resolve_output_dir(path: Path | str | None) -> Path:
    """CLI relative output-dir를 repo root 기준 절대 경로로 정규화."""
    if path is None:
        return _ABLATION_DIR
    resolved = Path(path)
    return resolved if resolved.is_absolute() else _PROJECT_ROOT / resolved


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
    registry_dir: Path,
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

    from src.models.registry import ModelRegistry

    trainer = LGBMTrainer(
        dataset_builder=builder,
        registry=ModelRegistry(artifacts_dir=registry_dir / config_label),
    )
    # feature_cols override (enabled_for_lgbm=false 시 4피처만)
    trainer.feature_cols = feature_cols

    result = trainer.train(
        tickers=tickers,
        start_date=start_date,
        end_date=end_date,
        version=version,
        is_latest=False,
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
    output_dir: Path | None = None,
    write_report: bool = True,
) -> dict[str, Any]:
    """Dual-Source Ablation 실행 진입점.

    Returns:
        ablation 결과 딕셔너리 (dual_source_YYYYMMDD.json 저장).
    """
    today = datetime.now(_KST).strftime("%Y%m%d")
    tickers = tickers or _load_default_tickers()
    inferred_start, inferred_end = _infer_artifact_date_range(tickers)
    start_date = start_date or inferred_start or _DEFAULT_START
    end_date = end_date or inferred_end or _DEFAULT_END
    output_dir = _resolve_output_dir(output_dir)
    scratch_registry_dir = output_dir / "models"

    # verdict 임계값 yaml 로드 (하드코딩 금지)
    pa_cfg = config_load("risk_config.yaml", "validation_tools.performance_analyzer") or {}
    threshold_sharpe = float(pa_cfg.get("verdict_improve_sharpe_threshold", 0.1))

    logger.info(
        "[ablation] S4-2 Dual-Source Ablation 시작: tickers=%d, %s~%s",
        len(tickers), start_date, end_date,
    )

    try:
        # Configuration A: w/o dual_source (baseline)
        baseline_result = _run_config(
            config_label="baseline",
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            enabled_for_lgbm=False,
            version="baseline",
            registry_dir=scratch_registry_dir,
        )

        # Configuration B: w/ dual_source
        with_ds_result = _run_config(
            config_label="with_dual_source",
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            enabled_for_lgbm=True,
            version="baseline_with_dual_source",
            registry_dir=scratch_registry_dir,
        )
    except (DatasetBuildError, RuntimeError) as exc:
        blocker = "dataset_build_error"
        if isinstance(exc, RuntimeError) and "fold 0개" in str(exc):
            blocker = "walk_forward_fold_unavailable"
        payload = {
            "status": "BLOCKED",
            "ablation_name": "dual_source",
            "date": today,
            "tickers": tickers,
            "train_range": {"start": start_date, "end": end_date},
            "blockers": [blocker],
            "error": str(exc),
            "registry_mutated": False,
            "scratch_registry_dir": str(scratch_registry_dir),
        }
        if write_report:
            output_dir.mkdir(parents=True, exist_ok=True)
            out_path = output_dir / f"dual_source_{today}_blocked.json"
            with out_path.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            payload["report_path"] = str(out_path)
            payload["report_path_relative"] = str(out_path.relative_to(_PROJECT_ROOT))
        logger.warning("[ablation] BLOCKED(%s): %s", blocker, exc)
        return payload

    # delta 계산
    delta = _compute_delta(baseline_result, with_ds_result)
    verdict = _verdict(delta, threshold_sharpe)

    payload: dict[str, Any] = {
        "ablation_name": "dual_source",
        "status": "PASS",
        "date": today,
        "tickers": tickers,
        "train_range": {"start": start_date, "end": end_date},
        "data_note": (
            "artifacts/data의 1분봉 active universe 기준 trainer_validation_proxy. "
            "C12 real backtest 또는 1주 paper 결과 전까지 deploy-quality 성능으로 해석하지 않는다."
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
        "registry_mutated": False,
        "scratch_registry_dir": str(scratch_registry_dir),
    }

    # 저장
    if write_report:
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / f"dual_source_{today}.json"
        with out_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        payload["report_path"] = str(out_path)
        payload["report_path_relative"] = str(out_path.relative_to(_PROJECT_ROOT))
    else:
        out_path = None

    logger.info(
        "[ablation] 완료. verdict=%s delta_sharpe=%s report=%s",
        verdict, delta["sharpe"], out_path or "no-write",
    )

    return payload


def _parse_tickers(raw: str | None) -> list[str] | None:
    if raw is None or raw.strip() == "":
        return None
    return [item.strip().zfill(6) for item in raw.split(",") if item.strip()]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run S4-2 Dual-Source ablation with trainable artifact data.",
    )
    parser.add_argument("--tickers", help="comma-separated tickers. 기본은 universe_config active 종목")
    parser.add_argument("--start-date", help="YYYYMMDD. 생략 시 artifacts/data에서 자동 추론")
    parser.add_argument("--end-date", help="YYYYMMDD. 생략 시 artifacts/data에서 자동 추론")
    parser.add_argument("--output-dir", default=str(_ABLATION_DIR))
    parser.add_argument("--no-write-report", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    result = run_ablation(
        tickers=_parse_tickers(args.tickers),
        start_date=args.start_date,
        end_date=args.end_date,
        output_dir=Path(args.output_dir),
        write_report=not args.no_write_report,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

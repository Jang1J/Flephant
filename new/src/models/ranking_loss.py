"""S1-0 Batch B LightGBM LambdaRank NDCG@5 Dataset helper.

LightGBM `objective='lambdarank'`의 입력 규약:
  - X: feature matrix (n_rows × n_features)
  - label: integer relevance grade (0 ~ n_grades-1)
  - group: 각 query(ts_close)별 row count list. sum(group) == len(X).

Cross-sectional ranking:
  매 1분 ts_close가 하나의 query. 그 안의 20종목(= 20 rows)이 ranking 대상.

모든 파라미터는 risk_config.yaml lightgbm 섹션에서 로드.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger

logger = get_logger("ranking_loss")


def _import_lightgbm():
    """lazy import. 학습 시에만 필요 (Quant Agent 추론은 pkl load만)."""
    try:
        import lightgbm as lgb  # type: ignore[import]

        return lgb
    except ImportError as e:
        raise RuntimeError(
            "lightgbm 미설치. pip install lightgbm으로 설치 필요."
        ) from e


get_lightgbm = _import_lightgbm  # public API


def _load_lgbm_cfg() -> dict[str, Any]:
    return config_load("risk_config.yaml", "lightgbm")


# ====================================================================== #
# Group 계산 (cross-sectional query)
# ====================================================================== #


def compute_group_sizes(panel, group_col: str = "ts_close") -> list[int]:
    """panel MultiIndex 또는 column에서 group_col별 row count list 반환.

    LightGBM `group` 파라미터 규약: sum == len(panel), 순서 = panel row 순서.

    panel은 group_col 오름차순 정렬 전제 (WalkForwardSplitter 결과 또는 DatasetBuilder 출력).
    """
    import pandas as pd_

    if group_col in (panel.index.names or []):
        level = panel.index.get_level_values(group_col)
    elif group_col in panel.columns:
        level = panel[group_col]
    else:
        raise KeyError(
            f"group_col='{group_col}' panel.index.names나 columns에 없음"
        )

    # 같은 group_col 값이 연속 블록으로 있는지 확인 후 count
    ser = pd_.Series(np.asarray(level))
    # value_counts를 정렬된 group 순서로 얻기 위해 순차 스캔
    groups: list[int] = []
    current_val = None
    current_count = 0
    for v in ser:
        if current_val is None:
            current_val = v
            current_count = 1
            continue
        if v == current_val:
            current_count += 1
        else:
            groups.append(current_count)
            current_val = v
            current_count = 1
    if current_count > 0:
        groups.append(current_count)

    if sum(groups) != len(panel):
        raise ValueError(
            f"group 합({sum(groups)}) != panel 길이({len(panel)}). "
            f"panel이 {group_col} 오름차순 정렬인지 확인."
        )

    return groups


# ====================================================================== #
# Dataset builder
# ====================================================================== #


def make_lgbm_dataset(
    panel,
    feature_cols: list[str],
    label_col: str = "relevance",
    group_col: str = "ts_close",
):
    """panel → lgb.Dataset (X, label, group).

    Args:
      panel: MultiIndex (ticker, ts_close) DataFrame. DatasetBuilder 출력.
      feature_cols: 학습에 쓸 피처 컬럼 list.
      label_col: integer relevance grade 컬럼 (default 'relevance').
      group_col: cross-sectional query 기준 (default 'ts_close').

    Returns: lgb.Dataset
    """
    lgb = _import_lightgbm()

    missing = [c for c in feature_cols if c not in panel.columns]
    if missing:
        raise KeyError(f"feature_cols에 panel 없는 컬럼 존재: {missing}")
    if label_col not in panel.columns:
        raise KeyError(f"label_col='{label_col}' panel에 없음")

    X = panel[feature_cols].to_numpy(dtype=float)
    y_raw = panel[label_col].to_numpy(dtype=float)
    # LambdaRank label은 정수 0~n-1
    y = y_raw.astype(int)
    group = compute_group_sizes(panel, group_col=group_col)

    ds = lgb.Dataset(X, label=y, group=group, free_raw_data=False)
    logger.info(
        "[ranking_loss] Dataset 생성: %d rows, %d groups, %d features",
        len(panel), len(group), len(feature_cols),
    )
    return ds


# ====================================================================== #
# LightGBM params builder (risk_config.yaml lightgbm → lgb.train params dict)
# ====================================================================== #


def build_lgbm_params(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """risk_config.yaml lightgbm 섹션 → lgb.train() params dict.

    overrides는 테스트 또는 Mode B 재학습 시 특정 값 덮어쓰기용.
    """
    cfg = _load_lgbm_cfg()

    params: dict[str, Any] = {
        "objective": cfg["objective"],
        "metric": cfg["metric"],
        "ndcg_eval_at": list(cfg["ndcg_eval_at"]),
        "label_gain": list(cfg["label_gain"]),
        "num_leaves": int(cfg["num_leaves"]),
        "learning_rate": float(cfg["learning_rate"]),
        "min_child_samples": int(cfg["min_child_samples"]),
        "subsample": float(cfg["subsample"]),
        "colsample_bytree": float(cfg["colsample_bytree"]),
        "random_state": int(cfg["random_state"]),
        "feature_fraction_seed": int(cfg["feature_fraction_seed"]),
        "verbose": -1,
    }
    if overrides:
        params.update(overrides)
    return params


def get_training_control() -> dict[str, int]:
    """n_estimators / early_stopping_rounds 등 학습 제어 파라미터."""
    cfg = _load_lgbm_cfg()
    return {
        "n_estimators": int(cfg["n_estimators"]),
        "early_stopping_rounds": int(cfg["early_stopping_rounds"]),
        "n_relevance_grades": int(cfg["n_relevance_grades"]),
    }

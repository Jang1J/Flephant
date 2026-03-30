"""
LightGBM Cross-Sectional Momentum Ranker (v2.0)
AI #2 — 유니버스 내 종목 상대 순위 예측 모델

설계 개요:
- DMP 히스토리(artifacts/daily_market_packet/)에서 피처 행렬 구축
- Manual factors + MLF-lite + UMI-lite 피처 조합
- 날짜별 sector-neutral excess return rank 레이블 (GPT Pro #8 권고)
- Walk-forward expanding window + purge(5d) + embargo(5d)
- LightGBM LambdaMART ranker (NDCG@5 최적화) — binary fallback 지원
- 모델, 피처 중요도, 예측 결과 저장

Usage:
    python models/strategy_model/lgbm_ranker.py
    python models/strategy_model/lgbm_ranker.py --config models/strategy_model/config.yaml
    python models/strategy_model/lgbm_ranker.py --predict 20250101
"""

import argparse
import json
import pickle
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_BASE_DIR))

from models.strategy_model.feature_factory import (
    build_cross_sectional_raw,
    extract_single_ticker_features,
)

MODEL_DIR = Path(__file__).resolve().parent
ARTIFACTS_DIR = _BASE_DIR / "artifacts" / "daily_market_packet"
OUTPUT_DIR = _BASE_DIR / "artifacts" / "strategy_model"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

UNIVERSE_CSV = _BASE_DIR / "config" / "universe_v1.csv"

# ──────────────────────────────────────────────────────────────────────────────
# 유틸리티
# ──────────────────────────────────────────────────────────────────────────────

def _load_universe() -> pd.DataFrame:
    """유니버스 26종목 로드. ticker를 6자리 zero-padded 문자열로 정규화."""
    df = pd.read_csv(UNIVERSE_CSV)
    df["ticker"] = df["ticker"].apply(lambda t: str(t).zfill(6))
    return df


def _load_dmp(date_str: str) -> dict | None:
    """지정 날짜의 DMP JSON 로드. 없으면 None 반환."""
    path = ARTIFACTS_DIR / f"DMP-{date_str}.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[LGBMRanker] DMP 로드 오류 ({date_str}): {e}")
        return None


def _available_dates() -> list[str]:
    """artifacts/daily_market_packet/ 에 존재하는 날짜 목록 (오름차순)."""
    files = sorted(ARTIFACTS_DIR.glob("DMP-*.json"))
    dates = []
    for f in files:
        stem = f.stem  # "DMP-20240731"
        parts = stem.split("-")
        if len(parts) == 2 and len(parts[1]) == 8:
            dates.append(parts[1])
    return dates


def _compute_macd(close_series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD 및 Signal line 계산. 짧은 시리즈는 NaN 반환."""
    if len(close_series) < slow + signal:
        return np.nan, np.nan
    ema_fast = close_series.ewm(span=fast, adjust=False).mean()
    ema_slow = close_series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line.iloc[-1], signal_line.iloc[-1]


def _compute_sma60_ratio(close_series: pd.Series) -> float:
    """close / sma_60 - 1. 60일치 미만이면 NaN."""
    if len(close_series) < 60:
        return np.nan
    sma60 = close_series.iloc[-60:].mean()
    if sma60 == 0:
        return np.nan
    return close_series.iloc[-1] / sma60 - 1.0


def _compute_return_60d(close_series: pd.Series) -> float:
    """60거래일 수익률 (%). 60일치 미만이면 NaN."""
    if len(close_series) < 61:
        return np.nan
    p0 = close_series.iloc[-61]
    p1 = close_series.iloc[-1]
    if p0 == 0:
        return np.nan
    return (p1 / p0 - 1.0) * 100.0


# ──────────────────────────────────────────────────────────────────────────────
# 피처 행렬 구축
# ──────────────────────────────────────────────────────────────────────────────

def build_feature_matrix(dates: list[str], dmp_dir: Path | None = None) -> pd.DataFrame:
    """
    DMP 히스토리를 순회하며 (date, ticker) 단위 피처 행렬을 구성한다.

    PIT-Safety:
    - t일 피처는 t일 DMP 데이터만 사용 (미래 데이터 사용 불가).
    - forward 수익률(fwd_ret_5d)은 레이블 생성 전용으로만 사용되며,
      예측 시점에서는 제공되지 않는다.

    Args:
        dates: 처리할 날짜 목록 (YYYYMMDD 문자열, 오름차순).
        dmp_dir: DMP 파일 디렉토리. None이면 기본 ARTIFACTS_DIR 사용.

    Returns:
        columns: date, ticker, {피처들}, fwd_ret_5d
        fwd_ret_5d는 5거래일 후 종가 기준 수익률(%). 마지막 5일은 NaN.
    """
    if dmp_dir is None:
        dmp_dir = ARTIFACTS_DIR

    universe_df = _load_universe()
    universe_tickers = list(universe_df["ticker"])
    sector_map = dict(zip(universe_df["ticker"], universe_df["wics_sector"]))

    print(f"[LGBMRanker] 피처 행렬 구축 시작: {len(dates)}개 날짜, 유니버스 {len(universe_tickers)}종목")

    # 날짜별 DMP 전체 로드 (반복 I/O 최소화)
    all_dmps: dict[str, dict] = {}
    for d in dates:
        dmp = _load_dmp(d)
        if dmp is not None:
            all_dmps[d] = dmp

    valid_dates = [d for d in dates if d in all_dmps]
    date_to_idx = {d: i for i, d in enumerate(valid_dates)}

    if len(valid_dates) == 0:
        print("[LGBMRanker] 유효한 DMP 없음. 빈 DataFrame 반환.")
        return pd.DataFrame()

    # 종목별 종가 시계열 구성 (MACD/SMA60/return_60d 계산용)
    close_history: dict[str, list[float]] = {t: [] for t in universe_tickers}
    for d in valid_dates:
        dmp = all_dmps[d]
        mdata = dmp.get("market_data", {})
        for ticker in universe_tickers:
            tdata = mdata.get(ticker, {})
            close = tdata.get("ohlcv", {}).get("close", np.nan)
            close_history[ticker].append(float(close) if close is not None else np.nan)

    rows = []

    for date_idx, date in enumerate(valid_dates):
        dmp = all_dmps[date]
        mdata = dmp.get("market_data", {})
        macro = dmp.get("macro_snapshot", {})

        # ── 날짜 단위 사전 계산 (feature_factory에 전달) ──────────────────────
        # UMI-lite: 유니버스 평균 수익률
        _urs: list[float] = []
        for _t in universe_tickers:
            _r5 = mdata.get(_t, {}).get("tech_features", {}).get("return_5d")
            if _r5 is not None:
                _urs.append(float(_r5))
        universe_mean_ret5 = float(np.nanmean(_urs)) if _urs else np.nan

        # 섹터별 close/sma20 평균 (rational_price_gap 기준)
        _scs: dict[str, list[float]] = {}
        for _t in universe_tickers:
            _td2 = mdata.get(_t, {})
            _cl2 = _td2.get("ohlcv", {}).get("close")
            _s20_2 = _td2.get("tech_features", {}).get("sma_20")
            _sec2 = sector_map.get(_t, "Unknown")
            if _cl2 is not None and _s20_2 and float(_s20_2) != 0:
                _scs.setdefault(_sec2, []).append(float(_cl2) / float(_s20_2) - 1.0)
        sector_mean_ratio = {
            sec: float(np.mean(vals)) for sec, vals in _scs.items() if vals
        }

        # Cross-sectional percentile rank 원시값 수집 (feature_factory)
        # PIT-safe: 동일 날짜 내 종목 간 비교만 수행
        _cs_raw = build_cross_sectional_raw(
            mdata, universe_tickers, close_history, date_idx
        )

        for ticker in universe_tickers:
            # 전일 종가 (overnight_gap 계산용)
            _prev_idx = date_idx - 1
            _prev_c = close_history[ticker][_prev_idx] if _prev_idx >= 0 else None

            feats = extract_single_ticker_features(
                dmp_market_data=mdata,
                ticker=ticker,
                close_history=close_history,
                date_idx=date_idx,
                sector_map=sector_map,
                universe_tickers=universe_tickers,
                macro=macro,
                cs_raw=_cs_raw,
                sector_mean_ratio=sector_mean_ratio,
                universe_mean_ret5=universe_mean_ret5,
                prev_close_val=_prev_c,
            )
            if feats is None:
                continue

            # forward 수익률 (레이블 생성 전용) — PIT-safe: 학습 시에만 사용
            close = float(mdata.get(ticker, {}).get("ohlcv", {}).get("close", 0))

            # O-I Decoupling v1: overnight(close→open) + intraday(open→close) 분리
            # t+1 시가와 종가를 각각 읽어 overnight/intraday 수익률 계산
            fwd_1_idx = date_idx + 1
            fwd_overnight_ret = np.nan
            fwd_intraday_ret = np.nan
            if fwd_1_idx < len(valid_dates):
                fwd1_date = valid_dates[fwd_1_idx]
                fwd1_dmp = all_dmps.get(fwd1_date, {})
                fwd1_td = fwd1_dmp.get("market_data", {}).get(ticker, {})
                fwd1_open = fwd1_td.get("ohlcv", {}).get("open")
                fwd1_close = fwd1_td.get("ohlcv", {}).get("close")
                if fwd1_open and close != 0:
                    fwd_overnight_ret = (float(fwd1_open) / close - 1.0) * 100.0
                if fwd1_open and fwd1_close and float(fwd1_open) != 0:
                    fwd_intraday_ret = (float(fwd1_close) / float(fwd1_open) - 1.0) * 100.0

            fwd_close_idx = date_idx + 5
            if fwd_close_idx < len(valid_dates):
                fwd_date = valid_dates[fwd_close_idx]
                fwd_dmp = all_dmps.get(fwd_date, {})
                fwd_close_val = (
                    fwd_dmp.get("market_data", {})
                    .get(ticker, {})
                    .get("ohlcv", {})
                    .get("close")
                )
                fwd_ret_5d = (
                    (float(fwd_close_val) / close - 1.0) * 100.0
                    if fwd_close_val and close != 0
                    else np.nan
                )
            else:
                fwd_ret_5d = np.nan

            row = {"date": date, "ticker": ticker}
            row.update(feats)
            row["fwd_ret_5d"] = float(fwd_ret_5d)
            row["fwd_overnight_ret"] = float(fwd_overnight_ret)
            row["fwd_intraday_ret"] = float(fwd_intraday_ret)
            rows.append(row)

    df = pd.DataFrame(rows)
    print(f"[LGBMRanker] 피처 행렬 완성: {len(df)}행 ({df['date'].nunique()}일 × 종목)")
    return df


def _build_labels(df: pd.DataFrame, top_quantile: float = 0.25, use_sector_neutral: bool = True) -> pd.DataFrame:
    """
    날짜별 cross-sectional 순위 레이블 생성.

    v2.0 (GPT Pro #8): sector-neutral excess return rank 사용.
    - 종목 수익률에서 동일 섹터 평균을 빼서 sector-neutral residual 계산
    - 해당 residual 기준 상위 top_quantile → y=1
    - ranking objective용 relevance label도 생성 (0~4 등급)

    fwd_ret_5d가 NaN인 행은 제거된다.
    """
    df = df.dropna(subset=["fwd_ret_5d"]).copy()

    # 유니버스 섹터 매핑 로드
    try:
        _univ = pd.read_csv(UNIVERSE_CSV, dtype={"ticker": str})
        _univ["ticker"] = _univ["ticker"].apply(lambda t: str(t).zfill(6))
        _sector_map = dict(zip(_univ["ticker"], _univ["wics_sector"]))
    except Exception:
        _sector_map = {}
        use_sector_neutral = False

    def _label(group: pd.DataFrame) -> pd.DataFrame:
        group = group.copy()

        if use_sector_neutral and _sector_map:
            # sector-neutral residual: 종목 수익률 - 섹터 평균
            group["_sector"] = group["ticker"].map(_sector_map).fillna("Unknown")
            sector_mean = group.groupby("_sector")["fwd_ret_5d"].transform("mean")
            group["_excess"] = group["fwd_ret_5d"] - sector_mean
            rank_col = "_excess"
        else:
            rank_col = "fwd_ret_5d"

        # binary label (backward compat)
        threshold = group[rank_col].quantile(1.0 - top_quantile)
        group["label"] = (group[rank_col] >= threshold).astype(int)

        # ordinal label: bottom 50% → 0, 50~75% → 1, top 25% → 2
        pct = group[rank_col].rank(pct=True)
        group["ordinal_label"] = 0
        group.loc[pct >= 0.50, "ordinal_label"] = 1
        group.loc[pct >= 0.75, "ordinal_label"] = 2

        # ranking relevance label (0~4): LambdaMART용
        # 상위 10% → 4, 상위 25% → 3, 상위 50% → 2, 상위 75% → 1, 하위 → 0
        group["rank_label"] = 0
        group.loc[pct >= 0.25, "rank_label"] = 1
        group.loc[pct >= 0.50, "rank_label"] = 2
        group.loc[pct >= 0.75, "rank_label"] = 3
        group.loc[pct >= 0.90, "rank_label"] = 4

        # O-I Decoupling v1: overnight/intraday direction labels
        # Momentum: overnight continuation (상승 지속), intraday follow-through (장중 유지)
        if "fwd_overnight_ret" in group.columns:
            group["overnight_direction"] = (group["fwd_overnight_ret"] > 0).astype(int)
        if "fwd_intraday_ret" in group.columns:
            group["intraday_direction"] = (group["fwd_intraday_ret"] > 0).astype(int)

        # cleanup
        group.drop(columns=["_sector", "_excess"], errors="ignore", inplace=True)
        return group

    result_parts = []
    for date_val, group in df.groupby("date"):
        labeled = _label(group)
        result_parts.append(labeled)
    df = pd.concat(result_parts, ignore_index=True)
    return df


# ──────────────────────────────────────────────────────────────────────────────
# Walk-Forward 학습
# ──────────────────────────────────────────────────────────────────────────────

def _get_feature_cols(config: dict) -> list[str]:
    """config.features 에서 중복 제거 후 피처 컬럼명 목록 반환."""
    feat_cfg = config.get("features", {})
    cols: list[str] = []
    seen: set[str] = set()
    for group in ["manual", "mlf_lite", "umi_lite", "cross_sectional_pct", "ohlcv_micro"]:
        for col in feat_cfg.get(group, []):
            if col not in seen:
                cols.append(col)
                seen.add(col)
    return cols


def train_walk_forward(df: pd.DataFrame, config: dict) -> list[dict[str, Any]]:
    """
    Walk-forward expanding window 학습.

    Args:
        df: build_feature_matrix + _build_labels 결과 DataFrame.
            columns: date, ticker, features..., fwd_ret_5d, label
        config: config.yaml 전체 dict.

    Returns:
        fold_results: 각 fold의 메타 + 학습된 모델 + 검증 AUC 리스트.
    """
    try:
        import lightgbm as lgb
        from sklearn.metrics import roc_auc_score
    except ImportError as e:
        print(f"[LGBMRanker] 의존성 미설치: {e}. pip install lightgbm scikit-learn")
        raise

    wf_cfg = config.get("walk_forward", {})
    lgbm_cfg = config.get("lgbm", {})
    label_cfg = config.get("label", {})
    top_quantile = label_cfg.get("top_quantile", 0.25)

    mode = wf_cfg.get("mode", "expanding")
    train_window = wf_cfg.get("train_window", 200)
    val_window = wf_cfg.get("val_window", 20)
    step_size = wf_cfg.get("step_size", 20)
    purge_days = wf_cfg.get("purge_days", 5)
    embargo_days = wf_cfg.get("embargo_days", 5)

    feature_cols = _get_feature_cols(config)

    # 레이블 생성
    df_labeled = _build_labels(df, top_quantile=top_quantile)

    # 날짜 정렬
    all_dates = sorted(df_labeled["date"].unique())
    n_dates = len(all_dates)

    if n_dates < train_window + val_window + purge_days + embargo_days:
        print(
            f"[LGBMRanker] 날짜 수 부족: {n_dates}일 < "
            f"최소 요구 {train_window + val_window + purge_days + embargo_days}일"
        )
        return []

    fold_results = []
    fold_idx = 0

    # expanding: 시작 고정 (0), val 끝점을 step_size씩 증가
    val_end = train_window + purge_days + embargo_days + val_window

    while val_end <= n_dates:
        train_end_idx = val_end - embargo_days - val_window - purge_days
        if mode == "expanding":
            train_start_idx = 0
        else:  # sliding
            train_start_idx = max(0, train_end_idx - train_window)

        train_dates = all_dates[train_start_idx:train_end_idx]
        # purge: train 끝 이후 purge_days 제외
        # embargo: val 시작 이전 embargo_days 제외 (이미 train_end_idx로 처리됨)
        val_start_idx = train_end_idx + purge_days + embargo_days
        val_dates = all_dates[val_start_idx : val_start_idx + val_window]

        if len(train_dates) < train_window // 2 or len(val_dates) == 0:
            val_end += step_size
            continue

        train_df = df_labeled[df_labeled["date"].isin(set(train_dates))]
        val_df = df_labeled[df_labeled["date"].isin(set(val_dates))]

        # 결측 처리: 피처 컬럼만 평균 대치
        available_cols = [c for c in feature_cols if c in train_df.columns]
        X_train = train_df[available_cols].copy()
        y_train = train_df["label"].values
        X_val = val_df[available_cols].copy()
        y_val = val_df["label"].values

        col_means = X_train.mean()
        X_train = X_train.fillna(col_means)
        X_val = X_val.fillna(col_means)

        if len(np.unique(y_train)) < 2:
            print(f"[LGBMRanker] Fold {fold_idx}: 학습 레이블 단일 클래스 — 건너뜀")
            val_end += step_size
            fold_idx += 1
            continue

        # LightGBM 학습 — LambdaMART ranker or binary classifier
        objective = lgbm_cfg.get("objective", "lambdarank")
        use_ranker = objective in ("lambdarank", "rank_xendcg")

        n_estimators = lgbm_cfg.get("n_estimators", 200)

        if use_ranker:
            # LambdaMART ranking mode (GPT Pro #8 권고)
            y_train_rank = train_df["rank_label"].values if "rank_label" in train_df.columns else y_train
            y_val_rank = val_df["rank_label"].values if "rank_label" in val_df.columns else y_val

            # group size: 날짜별 종목 수
            train_groups = train_df.groupby("date").size().values
            val_groups = val_df.groupby("date").size().values

            model = lgb.LGBMRanker(
                objective=objective,
                metric="ndcg",
                ndcg_eval_at=[5],
                n_estimators=n_estimators,
                num_leaves=lgbm_cfg.get("num_leaves", 31),
                learning_rate=lgbm_cfg.get("learning_rate", 0.05),
                min_child_samples=lgbm_cfg.get("min_child_samples", 10),
                subsample=lgbm_cfg.get("subsample", 0.8),
                colsample_bytree=lgbm_cfg.get("colsample_bytree", 0.8),
                reg_alpha=lgbm_cfg.get("reg_alpha", 0.1),
                reg_lambda=lgbm_cfg.get("reg_lambda", 0.1),
                verbose=-1,
                n_jobs=-1,
            )
            model.fit(
                X_train, y_train_rank,
                group=train_groups,
                eval_set=[(X_val, y_val_rank)],
                eval_group=[val_groups],
                callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False)],
            )

            # 평가: 날짜별 Precision@5 + RankIC
            val_scores = model.predict(X_val)
            val_df_eval = val_df[["date", "ticker"]].copy()
            val_df_eval["score"] = val_scores
            val_df_eval["fwd_ret"] = val_df["fwd_ret_5d"].values

            p5_list, ric_list = [], []
            for _, g in val_df_eval.groupby("date"):
                top5 = g.nlargest(5, "score")
                p5 = (top5["fwd_ret"] > g["fwd_ret"].median()).mean()
                p5_list.append(p5)
                if len(g) > 2:
                    ric_list.append(float(g["score"].corr(g["fwd_ret"], method="spearman")))
            val_p5 = float(np.mean(p5_list)) if p5_list else 0.0
            val_ric = float(np.nanmean(ric_list)) if ric_list else 0.0

            # AUC도 보조 지표로 계산
            try:
                val_auc = float(roc_auc_score(y_val, val_scores))
            except Exception:
                val_auc = np.nan

        else:
            # Binary classifier fallback (기존 방식)
            pos_count = int(y_train.sum())
            neg_count = len(y_train) - pos_count
            scale_pos_weight = neg_count / pos_count if pos_count > 0 else 1.0

            params = {
                "objective": "binary",
                "metric": lgbm_cfg.get("metric", "auc"),
                "num_leaves": lgbm_cfg.get("num_leaves", 31),
                "learning_rate": lgbm_cfg.get("learning_rate", 0.05),
                "min_child_samples": lgbm_cfg.get("min_child_samples", 10),
                "subsample": lgbm_cfg.get("subsample", 0.8),
                "colsample_bytree": lgbm_cfg.get("colsample_bytree", 0.8),
                "reg_alpha": lgbm_cfg.get("reg_alpha", 0.1),
                "reg_lambda": lgbm_cfg.get("reg_lambda", 0.1),
                "scale_pos_weight": scale_pos_weight,
                "verbose": -1,
                "n_jobs": -1,
            }
            model = lgb.LGBMClassifier(n_estimators=n_estimators, **params)
            model.fit(
                X_train, y_train,
                eval_set=[(X_val, y_val)],
                callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False)],
            )
            val_scores = model.predict_proba(X_val)[:, 1]
            try:
                val_auc = float(roc_auc_score(y_val, val_scores))
            except Exception:
                val_auc = np.nan

            # Binary mode에서도 P@5 + RankIC 계산 (Gemini Pro 피드백)
            val_df_eval_bin = val_df[["date", "ticker"]].copy()
            val_df_eval_bin["score"] = val_scores
            val_df_eval_bin["fwd_ret"] = val_df["fwd_ret_5d"].values

            p5_list_bin, ric_list_bin = [], []
            for _, g in val_df_eval_bin.groupby("date"):
                if len(g) >= 5:
                    top5 = g.nlargest(5, "score")
                    p5 = (top5["fwd_ret"] > g["fwd_ret"].median()).mean()
                    p5_list_bin.append(p5)
                if len(g) > 2:
                    ric_list_bin.append(float(g["score"].corr(g["fwd_ret"], method="spearman")))
            val_p5 = float(np.mean(p5_list_bin)) if p5_list_bin else 0.0
            val_ric = float(np.nanmean(ric_list_bin)) if ric_list_bin else 0.0

        # 피처 중요도
        importance = dict(zip(available_cols, model.feature_importances_.tolist()))

        # ordinal 모델 학습 (multiclass, num_class=3)
        ordinal_model = None
        ordinal_val_auc = np.nan
        if "ordinal_label" in train_df.columns and len(train_df["ordinal_label"].unique()) >= 2:
            try:
                y_train_ord = train_df["ordinal_label"].values
                y_val_ord = val_df["ordinal_label"].values
                ord_params = {
                    "objective": "multiclass",
                    "metric": "multi_logloss",
                    "num_class": 3,
                    "num_leaves": lgbm_cfg.get("num_leaves", 31),
                    "learning_rate": lgbm_cfg.get("learning_rate", 0.05),
                    "min_child_samples": lgbm_cfg.get("min_child_samples", 10),
                    "subsample": lgbm_cfg.get("subsample", 0.8),
                    "colsample_bytree": lgbm_cfg.get("colsample_bytree", 0.8),
                    "reg_alpha": lgbm_cfg.get("reg_alpha", 0.1),
                    "reg_lambda": lgbm_cfg.get("reg_lambda", 0.1),
                    "verbose": -1,
                    "n_jobs": -1,
                }
                ordinal_model = lgb.LGBMClassifier(n_estimators=n_estimators, **ord_params)
                ordinal_model.fit(
                    X_train, y_train_ord,
                    eval_set=[(X_val, y_val_ord)],
                    callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False)],
                )
                # one-vs-rest AUC: class2(top) 확률 기준
                ord_proba = ordinal_model.predict_proba(X_val)
                try:
                    from sklearn.metrics import roc_auc_score as _auc
                    # one-vs-rest: 각 클래스 binary AUC 평균
                    auc_list = []
                    for cls_idx in range(3):
                        y_bin = (y_val_ord == cls_idx).astype(int)
                        if y_bin.sum() > 0 and y_bin.sum() < len(y_bin):
                            auc_list.append(float(_auc(y_bin, ord_proba[:, cls_idx])))
                    ordinal_val_auc = float(np.mean(auc_list)) if auc_list else np.nan
                except Exception:
                    ordinal_val_auc = np.nan
                print(f"[LGBMRanker] Fold {fold_idx:02d} ordinal AUC(OVR)={ordinal_val_auc:.4f}")
            except Exception as e:
                print(f"[LGBMRanker] ordinal 모델 학습 실패: {e}")
                ordinal_model = None
                ordinal_val_auc = np.nan

        # O-I Decoupling v1: overnight/intraday direction 보조 모델 학습
        oi_models = {}
        oi_aucs = {}
        for oi_target in ["overnight_direction", "intraday_direction"]:
            if oi_target not in df_labeled.columns:
                continue
            oi_train = df_labeled[df_labeled["date"].isin(set(train_dates))][oi_target].dropna()
            oi_val = df_labeled[df_labeled["date"].isin(set(val_dates))][oi_target].dropna()
            if len(oi_train) < 50 or len(np.unique(oi_train)) < 2:
                continue
            try:
                oi_X_train = X_train.loc[oi_train.index]
                oi_X_val = X_val.loc[oi_val.index] if len(oi_val) > 0 else oi_X_train[:10]
                oi_y_val = oi_val.values if len(oi_val) > 0 else oi_train.values[:10]

                oi_mdl = lgb.LGBMClassifier(
                    n_estimators=min(n_estimators, 100), objective="binary",
                    num_leaves=15, learning_rate=0.05, verbose=-1, n_jobs=-1,
                )
                oi_mdl.fit(oi_X_train, oi_train.values,
                           eval_set=[(oi_X_val, oi_y_val)],
                           callbacks=[lgb.early_stopping(stopping_rounds=10, verbose=False)])
                oi_pred = oi_mdl.predict_proba(oi_X_val)[:, 1]
                oi_auc = float(roc_auc_score(oi_y_val, oi_pred)) if len(np.unique(oi_y_val)) >= 2 else 0.5
                oi_models[oi_target] = oi_mdl
                oi_aucs[oi_target] = oi_auc
                print(f"[LGBMRanker] O-I {oi_target} AUC={oi_auc:.4f}")
            except Exception as e:
                print(f"[LGBMRanker] O-I {oi_target} 학습 실패: {e}")

        fold_result: dict[str, Any] = {
            "fold": fold_idx,
            "train_start": train_dates[0],
            "train_end": train_dates[-1],
            "val_start": val_dates[0] if val_dates else None,
            "val_end": val_dates[-1] if val_dates else None,
            "n_train": len(train_df),
            "n_val": len(val_df),
            "val_auc": val_auc,
            "ordinal_val_auc": ordinal_val_auc,
            "val_precision_at_5": val_p5,
            "val_rank_ic": val_ric,
            "objective": objective,
            "feature_importance": importance,
            "model": model,
            "ordinal_model": ordinal_model,
            "oi_models": oi_models,
            "oi_aucs": oi_aucs,
            "feature_cols": available_cols,
            "col_means": col_means.to_dict(),
        }
        fold_results.append(fold_result)

        metric_str = f"AUC={val_auc:.4f}"
        if use_ranker:
            metric_str += f", P@5={val_p5:.3f}, RankIC={val_ric:.3f}"
        print(
            f"[LGBMRanker] Fold {fold_idx:02d} | "
            f"train {train_dates[0]}~{train_dates[-1]} ({len(train_df)}행) | "
            f"val {val_dates[0]}~{val_dates[-1] if val_dates else 'N/A'} | "
            f"{metric_str}"
        )

        val_end += step_size
        fold_idx += 1

    print(f"[LGBMRanker] Walk-forward 완료: {len(fold_results)}개 fold")
    return fold_results


# ──────────────────────────────────────────────────────────────────────────────
# 예측
# ──────────────────────────────────────────────────────────────────────────────

def predict(
    model: Any,
    features: pd.DataFrame,
    feature_cols: list[str],
    col_means: dict,
    ordinal_model: Any = None,
) -> pd.Series:
    """
    학습된 모델로 예측 확률을 반환한다.

    Args:
        model: 학습된 LGBMClassifier(binary) 인스턴스.
        features: 피처 DataFrame (date, ticker 포함 가능).
        feature_cols: 학습 시 사용된 피처 컬럼 목록.
        col_means: 결측 대치에 사용할 학습셋 평균값 dict.
        ordinal_model: ordinal(multiclass) 모델. 있으면 class2 확률을 0.4 가중 혼합.

    Returns:
        pd.Series: 최종 score. index는 features의 index.
    """
    available = [c for c in feature_cols if c in features.columns]
    X = features[available].copy()
    means_series = pd.Series(col_means)
    X = X.fillna(means_series)
    binary_proba = model.predict_proba(X)[:, 1]

    if ordinal_model is not None:
        try:
            ord_proba = ordinal_model.predict_proba(X)
            # class2(top 25%) 확률을 보조 점수로 사용
            ord_top_proba = ord_proba[:, 2]
            final_score = 0.6 * binary_proba + 0.4 * ord_top_proba
        except Exception as e:
            print(f"[LGBMRanker] ordinal 예측 실패, binary만 사용: {e}")
            final_score = binary_proba
    else:
        final_score = binary_proba

    return pd.Series(final_score, index=features.index, name="score")


# ──────────────────────────────────────────────────────────────────────────────
# 저장 / 로드 헬퍼
# ──────────────────────────────────────────────────────────────────────────────

def _save_model(fold_results: list[dict], run_id: str) -> None:
    """마지막 fold 모델 + 전체 fold 메타 저장."""
    if not fold_results:
        print("[LGBMRanker] 저장할 fold 결과 없음.")
        return

    last_fold = fold_results[-1]
    model_path = OUTPUT_DIR / f"lgbm_ranker_{run_id}.pkl"
    with open(model_path, "wb") as f:
        pickle.dump(
            {
                "model": last_fold["model"],
                "ordinal_model": last_fold.get("ordinal_model"),
                "oi_models": last_fold.get("oi_models", {}),
                "feature_cols": last_fold["feature_cols"],
                "col_means": last_fold["col_means"],
            },
            f,
        )
    print(f"[LGBMRanker] 모델 저장: {model_path}")

    # canonical path에도 복사 (추론 시 latest_model.pkl로 탐색)
    latest_path = OUTPUT_DIR / "latest_model.pkl"
    import shutil as _shutil
    _shutil.copy2(model_path, latest_path)
    print(f"[LGBMRanker] Latest 모델 복사: {latest_path}")

    # 피처 중요도 (전체 fold 평균)
    importance_accum: dict[str, list[float]] = {}
    for fold in fold_results:
        for col, imp in fold["feature_importance"].items():
            importance_accum.setdefault(col, []).append(imp)
    avg_importance = {
        col: float(np.mean(vals)) for col, vals in importance_accum.items()
    }
    sorted_importance = dict(
        sorted(avg_importance.items(), key=lambda x: x[1], reverse=True)
    )

    importance_path = OUTPUT_DIR / f"feature_importance_{run_id}.json"
    with open(importance_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "run_id": run_id,
                "n_folds": len(fold_results),
                "feature_importance_avg": sorted_importance,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"[LGBMRanker] 피처 중요도 저장: {importance_path}")

    # fold 메타 (모델 객체 제외)
    meta_path = OUTPUT_DIR / f"fold_meta_{run_id}.json"
    meta = [
        {k: v for k, v in fold.items() if k not in ("model",)}
        for fold in fold_results
    ]
    # col_means의 NaN 처리
    for fold_meta in meta:
        fold_meta["col_means"] = {
            k: (None if (isinstance(v, float) and np.isnan(v)) else v)
            for k, v in fold_meta.get("col_means", {}).items()
        }
        fold_meta["val_auc"] = (
            None
            if isinstance(fold_meta.get("val_auc"), float) and np.isnan(fold_meta["val_auc"])
            else fold_meta.get("val_auc")
        )

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[LGBMRanker] Fold 메타 저장: {meta_path}")


def _save_predictions(pred_df: pd.DataFrame, run_id: str, pred_date: str) -> None:
    """예측 결과 저장."""
    pred_path = OUTPUT_DIR / f"predictions_{pred_date}_{run_id}.json"
    records = pred_df.to_dict(orient="records")
    with open(pred_path, "w", encoding="utf-8") as f:
        json.dump(
            {"run_id": run_id, "pred_date": pred_date, "predictions": records},
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"[LGBMRanker] 예측 결과 저장: {pred_path}")


def load_model(run_id: str) -> dict:
    """저장된 모델 로드."""
    model_path = OUTPUT_DIR / f"lgbm_ranker_{run_id}.pkl"
    if not model_path.exists():
        raise FileNotFoundError(f"[LGBMRanker] 모델 파일 없음: {model_path}")
    with open(model_path, "rb") as f:
        return pickle.load(f)


# ──────────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="LightGBM Cross-Sectional Momentum Ranker")
    parser.add_argument(
        "--config",
        type=str,
        default=str(MODEL_DIR / "config.yaml"),
        help="config.yaml 경로",
    )
    parser.add_argument(
        "--predict",
        type=str,
        default=None,
        metavar="YYYYMMDD",
        help="지정 날짜에 대한 예측만 수행 (기학습 모델 사용)",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="모델 저장/로드 시 사용할 run_id (기본: 현재 날짜)",
    )
    args = parser.parse_args()

    # config 로드
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"[LGBMRanker] config 파일 없음: {config_path}")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")

    if args.predict:
        # 예측 전용 모드: 기학습 모델로 지정 날짜 예측
        pred_date = args.predict
        print(f"[LGBMRanker] 예측 모드: {pred_date}")

        try:
            bundle = load_model(run_id)
        except FileNotFoundError as e:
            print(f"[LGBMRanker] {e}")
            print("[LGBMRanker] 먼저 학습을 실행하세요: python lgbm_ranker.py --run-id <ID>")
            sys.exit(1)

        all_dates = _available_dates()
        if pred_date not in all_dates:
            print(f"[LGBMRanker] {pred_date}의 DMP 없음.")
            sys.exit(1)

        # 예측 피처 구축 (pred_date 포함 이전 데이터로)
        hist_cutoff = all_dates[: all_dates.index(pred_date) + 1]
        feat_df = build_feature_matrix(hist_cutoff)
        if feat_df.empty:
            print("[LGBMRanker] 피처 행렬이 비어 있습니다.")
            sys.exit(1)

        today_feat = feat_df[feat_df["date"] == pred_date].copy()
        if today_feat.empty:
            print(f"[LGBMRanker] {pred_date} 피처 행 없음.")
            sys.exit(1)

        scores = predict(
            bundle["model"],
            today_feat,
            bundle["feature_cols"],
            bundle["col_means"],
            ordinal_model=bundle.get("ordinal_model"),
        )
        today_feat = today_feat.copy()
        today_feat["score"] = scores.values
        today_feat = today_feat.sort_values("score", ascending=False)
        _save_predictions(today_feat[["date", "ticker", "score"]], run_id, pred_date)

    else:
        # 학습 모드
        print(f"[LGBMRanker] 학습 모드 시작 (run_id={run_id})")

        all_dates = _available_dates()
        if not all_dates:
            print("[LGBMRanker] DMP 파일이 없습니다. artifacts/daily_market_packet/ 를 확인하세요.")
            sys.exit(1)

        print(f"[LGBMRanker] 사용 가능한 DMP: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)}일)")

        feat_df = build_feature_matrix(all_dates)
        if feat_df.empty:
            print("[LGBMRanker] 피처 행렬이 비어 있습니다. 학습을 중단합니다.")
            sys.exit(1)

        # 피처 행렬 저장 (재사용 가능)
        feat_path = OUTPUT_DIR / f"feature_matrix_{run_id}.parquet"
        try:
            feat_df.to_parquet(feat_path, index=False)
            print(f"[LGBMRanker] 피처 행렬 저장: {feat_path}")
        except Exception as e:
            print(f"[LGBMRanker] parquet 저장 실패 (pyarrow/fastparquet 필요): {e}")
            feat_csv_path = OUTPUT_DIR / f"feature_matrix_{run_id}.csv"
            feat_df.to_csv(feat_csv_path, index=False)
            print(f"[LGBMRanker] 피처 행렬 CSV로 저장: {feat_csv_path}")

        fold_results = train_walk_forward(feat_df, config)

        if not fold_results:
            print("[LGBMRanker] 학습된 fold가 없습니다. 데이터 범위를 확인하세요.")
            sys.exit(1)

        _save_model(fold_results, run_id)

        # 요약 출력
        aucs = [f["val_auc"] for f in fold_results if f["val_auc"] is not None and not np.isnan(f["val_auc"])]
        if aucs:
            print(
                f"[LGBMRanker] 검증 AUC 요약 — "
                f"평균: {np.mean(aucs):.4f}, "
                f"최소: {np.min(aucs):.4f}, "
                f"최대: {np.max(aucs):.4f}"
            )
        print(f"[LGBMRanker] 학습 완료. run_id={run_id}")


if __name__ == "__main__":
    main()

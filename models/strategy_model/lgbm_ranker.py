"""
LightGBM Cross-Sectional Momentum Ranker (v1.0)
AI #2 — 유니버스 내 종목 상대 순위 예측 모델

설계 개요:
- DMP 히스토리(artifacts/daily_market_packet/)에서 피처 행렬 구축
- Manual factors + MLF-lite + UMI-lite 피처 조합
- 날짜별 forward 5일 수익률 기준 상위 25% → y=1 레이블
- Walk-forward expanding window + purge(5d) + embargo(5d)
- LightGBM binary classifier (AUC 최적화)
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

        # 매크로 피처
        vix_proxy = macro.get("vix_proxy") or np.nan
        market_breadth = macro.get("market_breadth") or np.nan
        base_rate = macro.get("base_rate") or np.nan
        usd_krw = macro.get("usd_krw") or np.nan

        # UMI-lite: 유니버스 평균 수익률 (market_synchronism 계산 기준)
        universe_ret5 = []
        universe_close = []
        universe_sma20 = []
        for ticker in universe_tickers:
            td = mdata.get(ticker, {})
            tech = td.get("tech_features", {})
            r5 = tech.get("return_5d")
            cl = td.get("ohlcv", {}).get("close")
            s20 = tech.get("sma_20")
            if r5 is not None:
                universe_ret5.append(r5)
            if cl is not None:
                universe_close.append(cl)
            if s20 is not None:
                universe_sma20.append(s20)

        universe_mean_ret5 = float(np.nanmean(universe_ret5)) if universe_ret5 else np.nan
        universe_mean_close_sma20 = (
            float(np.nanmean([c / s - 1.0 for c, s in zip(universe_close, universe_sma20) if s and s != 0]))
            if universe_close and universe_sma20
            else np.nan
        )

        # 섹터별 종가 평균 close/sma20 비율 (rational_price_gap 기준)
        sector_close_sma20: dict[str, list[float]] = {}
        for ticker in universe_tickers:
            td = mdata.get(ticker, {})
            cl = td.get("ohlcv", {}).get("close")
            s20 = td.get("tech_features", {}).get("sma_20")
            sec = sector_map.get(ticker, "Unknown")
            if cl is not None and s20 and s20 != 0:
                sector_close_sma20.setdefault(sec, []).append(cl / s20 - 1.0)
        sector_mean_ratio = {
            sec: float(np.mean(vals)) for sec, vals in sector_close_sma20.items() if vals
        }

        for ticker in universe_tickers:
            tdata = mdata.get(ticker, {})
            if not tdata:
                continue

            ohlcv = tdata.get("ohlcv", {})
            tech = tdata.get("tech_features", {})
            close = ohlcv.get("close")
            if close is None or close == 0:
                continue
            close = float(close)

            # ── Manual factors ──
            return_5d = tech.get("return_5d", np.nan)
            return_20d = tech.get("return_20d", np.nan)
            rsi_14 = tech.get("rsi_14", np.nan)
            volume_ratio_20 = tech.get("volume_ratio_20", np.nan)
            atr_14 = tech.get("atr_14", np.nan)

            sma_5 = tech.get("sma_5")
            sma_20 = tech.get("sma_20")
            sma5_ratio = (close / sma_5 - 1.0) if sma_5 and sma_5 != 0 else np.nan
            sma20_ratio = (close / sma_20 - 1.0) if sma_20 and sma_20 != 0 else np.nan

            # sma60_ratio 및 MACD는 누적 종가 시계열 사용 (PIT-safe: date_idx+1까지만)
            hist_close = pd.Series(close_history[ticker][: date_idx + 1])
            hist_close = hist_close.dropna()

            sma60_ratio = _compute_sma60_ratio(hist_close)
            macd_val, macd_signal_val = _compute_macd(hist_close)

            # ── MLF-lite ──
            return_60d = _compute_return_60d(hist_close)

            # period_agreement_score: 5d, 20d, 60d 수익률 부호 일치 비율
            signs = []
            for r in [return_5d, return_20d, return_60d]:
                if not (r is None or (isinstance(r, float) and np.isnan(r))):
                    signs.append(np.sign(float(r)))
            if len(signs) >= 2:
                dominant_sign = np.sign(np.sum(signs))
                agree_count = sum(1 for s in signs if s == dominant_sign)
                period_agreement_score = agree_count / len(signs)
            else:
                period_agreement_score = np.nan

            # ── UMI-lite ──
            # stock_sync_score: 해당 종목 ret_5d와 유니버스 평균 ret_5d의 부호 일치 여부
            if (
                return_5d is not None
                and not np.isnan(float(return_5d))
                and not np.isnan(universe_mean_ret5)
            ):
                stock_sync_score = 1.0 if np.sign(float(return_5d)) == np.sign(universe_mean_ret5) else 0.0
            else:
                stock_sync_score = np.nan

            # market_synchronism: breadth 가중 유니버스 동행 지수 (market_breadth 활용)
            # breadth가 없으면 단순 universe_mean_ret5 사용
            if not np.isnan(universe_mean_ret5):
                breadth_w = float(market_breadth) if not np.isnan(market_breadth) else 0.5
                market_synchronism = breadth_w * universe_mean_ret5
            else:
                market_synchronism = np.nan

            # rational_price_gap: 섹터 평균 close/sma20 대비 해당 종목 편차
            sec = sector_map.get(ticker, "Unknown")
            tick_ratio = (close / sma_20 - 1.0) if sma_20 and sma_20 != 0 else np.nan
            sec_mean = sector_mean_ratio.get(sec, np.nan)
            if not (isinstance(tick_ratio, float) and np.isnan(tick_ratio)) and not np.isnan(sec_mean):
                rational_price_gap = tick_ratio - sec_mean
            else:
                rational_price_gap = np.nan

            # forward 수익률 (레이블 생성 전용, 5거래일 후)
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

            rows.append({
                "date": date,
                "ticker": ticker,
                # Manual
                "return_5d": float(return_5d) if return_5d is not None else np.nan,
                "return_20d": float(return_20d) if return_20d is not None else np.nan,
                "rsi_14": float(rsi_14) if rsi_14 is not None else np.nan,
                "volume_ratio_20": float(volume_ratio_20) if volume_ratio_20 is not None else np.nan,
                "macd": float(macd_val) if not (isinstance(macd_val, float) and np.isnan(macd_val)) else np.nan,
                "macd_signal": float(macd_signal_val) if not (isinstance(macd_signal_val, float) and np.isnan(macd_signal_val)) else np.nan,
                "atr_14": float(atr_14) if atr_14 is not None else np.nan,
                "sma5_ratio": float(sma5_ratio) if sma5_ratio is not None else np.nan,
                "sma20_ratio": float(sma20_ratio) if sma20_ratio is not None else np.nan,
                "sma60_ratio": float(sma60_ratio),
                # MLF-lite
                "return_60d": float(return_60d),
                "period_agreement_score": float(period_agreement_score) if not (isinstance(period_agreement_score, float) and np.isnan(period_agreement_score)) else np.nan,
                # UMI-lite
                "stock_sync_score": float(stock_sync_score) if not (isinstance(stock_sync_score, float) and np.isnan(stock_sync_score)) else np.nan,
                "market_synchronism": float(market_synchronism) if not (isinstance(market_synchronism, float) and np.isnan(market_synchronism)) else np.nan,
                "rational_price_gap": float(rational_price_gap) if not (isinstance(rational_price_gap, float) and np.isnan(rational_price_gap)) else np.nan,
                # 매크로 (보조)
                "vix_proxy": float(vix_proxy),
                "market_breadth": float(market_breadth),
                "base_rate": float(base_rate),
                "usd_krw": float(usd_krw),
                # 레이블 전용
                "fwd_ret_5d": float(fwd_ret_5d),
            })

    df = pd.DataFrame(rows)
    print(f"[LGBMRanker] 피처 행렬 완성: {len(df)}행 ({df['date'].nunique()}일 × 종목)")
    return df


def _build_labels(df: pd.DataFrame, top_quantile: float = 0.25) -> pd.DataFrame:
    """
    날짜별 cross-sectional 순위 레이블 생성.
    fwd_ret_5d 기준 상위 top_quantile → y=1, 나머지 → y=0.
    fwd_ret_5d가 NaN인 행은 제거된다.
    """
    df = df.dropna(subset=["fwd_ret_5d"]).copy()

    def _label(group: pd.DataFrame) -> pd.DataFrame:
        threshold = group["fwd_ret_5d"].quantile(1.0 - top_quantile)
        group = group.copy()
        group["label"] = (group["fwd_ret_5d"] >= threshold).astype(int)
        return group

    df = df.groupby("date", group_keys=False).apply(_label)
    return df


# ──────────────────────────────────────────────────────────────────────────────
# Walk-Forward 학습
# ──────────────────────────────────────────────────────────────────────────────

def _get_feature_cols(config: dict) -> list[str]:
    """config.features 에서 중복 제거 후 피처 컬럼명 목록 반환."""
    feat_cfg = config.get("features", {})
    cols: list[str] = []
    seen: set[str] = set()
    for group in ["manual", "mlf_lite", "umi_lite"]:
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

        # LightGBM 학습
        pos_count = int(y_train.sum())
        neg_count = len(y_train) - pos_count
        scale_pos_weight = neg_count / pos_count if pos_count > 0 else 1.0

        params = {
            "objective": lgbm_cfg.get("objective", "binary"),
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
        n_estimators = lgbm_cfg.get("n_estimators", 200)

        model = lgb.LGBMClassifier(n_estimators=n_estimators, **params)
        model.fit(
            X_train,
            y_train,
            eval_set=[(X_val, y_val)],
            callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False)],
        )

        val_proba = model.predict_proba(X_val)[:, 1]
        try:
            val_auc = float(roc_auc_score(y_val, val_proba))
        except Exception:
            val_auc = np.nan

        # 피처 중요도
        importance = dict(zip(available_cols, model.feature_importances_.tolist()))

        fold_result: dict[str, Any] = {
            "fold": fold_idx,
            "train_start": train_dates[0],
            "train_end": train_dates[-1],
            "val_start": val_dates[0] if val_dates else None,
            "val_end": val_dates[-1] if val_dates else None,
            "n_train": len(train_df),
            "n_val": len(val_df),
            "val_auc": val_auc,
            "feature_importance": importance,
            "model": model,
            "feature_cols": available_cols,
            "col_means": col_means.to_dict(),
        }
        fold_results.append(fold_result)

        print(
            f"[LGBMRanker] Fold {fold_idx:02d} | "
            f"train {train_dates[0]}~{train_dates[-1]} ({len(train_df)}행) | "
            f"val {val_dates[0]}~{val_dates[-1] if val_dates else 'N/A'} | "
            f"AUC={val_auc:.4f}"
        )

        val_end += step_size
        fold_idx += 1

    print(f"[LGBMRanker] Walk-forward 완료: {len(fold_results)}개 fold")
    return fold_results


# ──────────────────────────────────────────────────────────────────────────────
# 예측
# ──────────────────────────────────────────────────────────────────────────────

def predict(model: Any, features: pd.DataFrame, feature_cols: list[str], col_means: dict) -> pd.Series:
    """
    학습된 모델로 예측 확률을 반환한다.

    Args:
        model: 학습된 LGBMClassifier 인스턴스.
        features: 피처 DataFrame (date, ticker 포함 가능).
        feature_cols: 학습 시 사용된 피처 컬럼 목록.
        col_means: 결측 대치에 사용할 학습셋 평균값 dict.

    Returns:
        pd.Series: y=1(상위 quartile) 확률값. index는 features의 index.
    """
    available = [c for c in feature_cols if c in features.columns]
    X = features[available].copy()
    means_series = pd.Series(col_means)
    X = X.fillna(means_series)
    proba = model.predict_proba(X)[:, 1]
    return pd.Series(proba, index=features.index, name="score")


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
                "feature_cols": last_fold["feature_cols"],
                "col_means": last_fold["col_means"],
            },
            f,
        )
    print(f"[LGBMRanker] 모델 저장: {model_path}")

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

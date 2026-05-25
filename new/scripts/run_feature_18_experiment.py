#!/usr/bin/env python
"""14피처 vs 18피처 LambdaRank cross-sectional OOF Sharpe 비교 실험 러너.

기존 운영 14피처 모델과, regression IC에서 유효했던 OHLCV 파생피처 4개를
추가한 18피처 모델을 동일 panel/fold에서 학습시켜 OOF Sharpe를 비교한다.

추가 4피처:
  - feat_volume_ratio: 현재 거래량 / Nm 평균 거래량 (config volume_ratio_window)
  - feat_price_range: (고가 - 저가) / 저가
  - feat_30m_ret: DatasetBuilder intraday_feature_windows 기존 계산 재사용
  - feat_vwap_dev: DatasetBuilder feat_session_vwap_gap 재사용

실행 (Mode B 시간창 18-22 KST):
    python new/scripts/run_feature_18_experiment.py

산출물:
    artifacts/feature_18_experiment/feature_18_exp_<timestamp>.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
_NEW_SRC = _ROOT / "new"
if str(_NEW_SRC) not in sys.path:
    sys.path.insert(0, str(_NEW_SRC))

os.environ.setdefault("ELEPHANT_MODE", "mode_b")

from src.data.dataset_builder import DatasetBuilder  # noqa: E402
from src.models.lgbm_trainer import _load_feature_cols  # noqa: E402
from src.models.splitter import WalkForwardSplitter  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.logger import get_logger  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402

logger = get_logger("feature_18_experiment")
_KST = ZoneInfo("Asia/Seoul")

_COMMUNITY_FEATURES_EXCLUDED: frozenset[str] = frozenset({
    "comm_score_t_1",
    "comm_score_t_2",
    "news_comm_divergence",
    "community_noise_multiplier",
})


def _load_universe_tickers(max_tickers: int) -> list[str]:
    cfg = config_load("universe_config.yaml") or {}
    tickers: list[str] = []
    allowed_statuses = {"active", "pending"}
    for sector in (cfg.get("sectors") or {}).values():
        for stock in sector.get("stocks", []):
            status = str(stock.get("status", "")).lower()
            if status in allowed_statuses and stock.get("ticker"):
                tickers.append(pad_ticker(str(stock["ticker"])))
    return tickers[:max_tickers]


def _build_baseline_feature_cols() -> list[str]:
    return [
        col for col in _load_feature_cols(
            include_dual_source=True,
            include_exogenous=True,
        )
        if col not in _COMMUNITY_FEATURES_EXCLUDED
    ]


def _build_extra_feature_names() -> list[str]:
    return [
        "feat_volume_ratio",
        "feat_price_range",
        "feat_30m_ret",
        "feat_vwap_dev",
    ]


def _compute_extra_features(panel):
    """panel에 추가 피처 생성. DatasetBuilder 기존 계산이 있으면 재사용."""
    import pandas as pd

    cfg_exp = config_load("risk_config.yaml", "feature_18_experiment") or {}
    vol_ratio_window = int(cfg_exp["volume_ratio_window"])
    cfg_preprocessor = config_load("risk_config.yaml", "preprocessor") or {}
    rolling_min_periods = int(cfg_preprocessor.get("rolling_min_periods", 5))

    closes = panel["close"].astype(float)
    volumes = panel["volume"].astype(float)
    highs = panel["high"].astype(float)
    lows = panel["low"].astype(float)

    # feat_volume_ratio (ticker별 grouped rolling — MultiIndex 경계 오염 방지)
    roll_vol_mean = volumes.groupby(level="ticker").transform(
        lambda s: s.rolling(window=vol_ratio_window, min_periods=rolling_min_periods).mean()
    )
    panel["feat_volume_ratio"] = (volumes / roll_vol_mean.replace(0, np.nan)).fillna(1.0)

    # feat_price_range
    panel["feat_price_range"] = ((highs - lows) / lows.replace(0, np.nan)).fillna(0.0)

    # feat_30m_ret: DatasetBuilder intraday_feature_windows가 이미 계산
    if "feat_30m_ret" not in panel.columns:
        ret_window = int(cfg_exp["ret_window"])
        prev_n = closes.groupby(level="ticker").shift(ret_window)
        panel["feat_30m_ret"] = ((closes / prev_n.replace(0, np.nan)) - 1.0).fillna(0.0)
        logger.info("[실험] feat_30m_ret 직접 계산 (window=%d)", ret_window)
    else:
        logger.info("[실험] feat_30m_ret DatasetBuilder 기존 계산 재사용")

    # feat_vwap_dev: DatasetBuilder feat_session_vwap_gap 재사용
    if "feat_session_vwap_gap" in panel.columns:
        panel["feat_vwap_dev"] = panel["feat_session_vwap_gap"]
        logger.info("[실험] feat_vwap_dev = feat_session_vwap_gap 재사용")
    else:
        ticker_level = panel.index.get_level_values("ticker")
        ts_level = panel.index.get_level_values("ts_close")
        session_key = pd.to_datetime(ts_level).strftime("%Y%m%d")
        pv = closes * volumes
        cumulative_pv = pv.groupby([ticker_level, session_key]).cumsum()
        cumulative_volume = volumes.groupby([ticker_level, session_key]).cumsum()
        session_vwap = cumulative_pv / cumulative_volume.replace(0, np.nan)
        panel["feat_vwap_dev"] = ((closes / session_vwap.replace(0, np.nan)) - 1.0).fillna(0.0)
        logger.info("[실험] feat_vwap_dev 직접 계산")

    for col in _build_extra_feature_names():
        panel[col] = panel[col].replace([np.inf, -np.inf], np.nan).fillna(0.0)

    return panel


def _cross_sectional_oof_sharpe(panel, oof_scores, sharpe_label_col, top_k_fraction, annualization_factor):
    """OOF scores -> cross-sectional top-K daily PnL -> annualized Sharpe."""
    import pandas as pd

    if sharpe_label_col not in panel.columns:
        logger.warning("[실험] sharpe_label_col '%s' 없음. 0.0 반환.", sharpe_label_col)
        return 0.0

    ts_level = panel.index.get_level_values("ts_close")
    df = pd.DataFrame({
        "ts_close": ts_level,
        "day_key": pd.to_datetime(ts_level).strftime("%Y%m%d"),
        "label": panel[sharpe_label_col].to_numpy(dtype=float),
        "score": oof_scores,
    })
    df = df.dropna(subset=["label", "score"])

    pnl_records = []
    for _, group in df.groupby("ts_close"):
        if len(group) < 2:
            continue
        k = max(1, int(len(group) * top_k_fraction))
        top_k = group.nlargest(k, "score")
        pnl_records.append({"day_key": group["day_key"].iloc[0], "pnl": top_k["label"].mean()})

    if not pnl_records:
        return 0.0

    pnl_df = pd.DataFrame(pnl_records)
    daily_pnl = pnl_df.groupby("day_key")["pnl"].mean().to_numpy()

    if len(daily_pnl) < 2 or daily_pnl.std() < 1e-12:
        return 0.0

    return float(daily_pnl.mean() / daily_pnl.std() * np.sqrt(annualization_factor))


def _train_lambdarank_oof(panel, feature_cols, label_col):
    """WalkForward split -> LambdaRank -> OOF prediction."""
    import pandas as pd
    try:
        import lightgbm as lgb
    except ImportError as e:
        raise RuntimeError("lightgbm 미설치") from e

    cfg_wf = config_load("risk_config.yaml", "walk_forward") or {}
    cfg_lgbm = config_load("risk_config.yaml", "lightgbm") or {}

    splitter = WalkForwardSplitter()

    params = {
        "objective": "lambdarank",
        "metric": "ndcg",
        "ndcg_eval_at": [5],
        "learning_rate": cfg_lgbm.get("learning_rate", 0.05),
        "num_leaves": cfg_lgbm.get("num_leaves", 31),
        "min_child_samples": cfg_lgbm.get("min_child_samples", 50),
        "verbosity": -1,
        "seed": int(cfg_lgbm.get("random_state", 42)),
    }
    n_estimators = int(cfg_lgbm.get("n_estimators", 300))
    early_stopping_rounds = int(cfg_lgbm.get("early_stopping_rounds", 30))

    oof_scores = np.full(len(panel), np.nan, dtype=float)
    fold_info = []
    booster = None

    for fold_idx, (train_idx, val_idx) in enumerate(splitter.split(panel), 1):
        # LambdaRank: 동일 ts_close 행이 연속해야 group 대응이 맞음
        train_panel = panel.iloc[train_idx].sort_index(level="ts_close")
        val_panel = panel.iloc[val_idx].sort_index(level="ts_close")

        X_train = train_panel[feature_cols].to_numpy(dtype=np.float32)
        y_train = train_panel[label_col].to_numpy(dtype=float)
        X_val = val_panel[feature_cols].to_numpy(dtype=np.float32)
        y_val = val_panel[label_col].to_numpy(dtype=float)

        train_groups = train_panel.index.get_level_values("ts_close")
        val_groups = val_panel.index.get_level_values("ts_close")
        train_group_counts = pd.Series(train_groups).value_counts().sort_index().values
        val_group_counts = pd.Series(val_groups).value_counts().sort_index().values

        dtrain = lgb.Dataset(X_train, label=y_train, group=train_group_counts)
        dval = lgb.Dataset(X_val, label=y_val, group=val_group_counts, reference=dtrain)

        booster = lgb.train(
            params, dtrain,
            num_boost_round=n_estimators,
            valid_sets=[dval],
            callbacks=[lgb.early_stopping(early_stopping_rounds, verbose=False), lgb.log_evaluation(0)],
        )

        # val_panel은 ts_close 기준 재정렬됐으므로 원본 panel 순서로 매핑
        preds = booster.predict(X_val)
        oof_scores[val_idx] = pd.Series(preds, index=val_panel.index).loc[panel.index[val_idx]].values
        fold_info.append({
            "fold": fold_idx,
            "train_size": len(train_idx),
            "val_size": len(val_idx),
            "best_iteration": booster.best_iteration,
        })
        logger.info(
            "[실험] fold %d 완료. train=%d val=%d best_iter=%d",
            fold_idx, len(train_idx), len(val_idx), booster.best_iteration,
        )

    if booster is None or not fold_info:
        raise RuntimeError(
            "insufficient_walk_forward_folds: no validation folds generated. "
            "Increase the date range or adjust risk_config.yaml walk_forward train/test/step settings."
        )

    return oof_scores, fold_info, booster


def _default_max_tickers() -> int:
    cfg = config_load("risk_config.yaml", "paper_auto_trading") or {}
    if "max_tickers" not in cfg:
        raise RuntimeError("risk_config.yaml paper_auto_trading.max_tickers 설정 누락")
    return int(cfg["max_tickers"])


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start-date", default="20251001")
    p.add_argument("--end-date", default="20260515")
    p.add_argument("--max-tickers", type=int, default=None)
    p.add_argument("--label-col", default="relevance")
    p.add_argument("--output-dir", default="artifacts/feature_18_experiment")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    max_tickers = int(args.max_tickers) if args.max_tickers is not None else _default_max_tickers()
    tickers = _load_universe_tickers(max_tickers)

    baseline_feature_cols = _build_baseline_feature_cols()
    extra_features = _build_extra_feature_names()
    extended_feature_cols = baseline_feature_cols + extra_features

    cfg_committee = config_load("risk_config.yaml", "committee") or {}
    cfg_eval = config_load("risk_config.yaml", "evaluation") or {}
    cfg_exp = config_load("risk_config.yaml", "feature_18_experiment") or {}
    sharpe_label_col = cfg_committee.get("sharpe_label_col", "label_5m_net_ret")
    top_k_fraction = float(cfg_eval.get("top_k_fraction", 0.25))
    annualization_factor = int(cfg_eval.get("annualization_factor", 252))

    started_at = datetime.now(_KST)
    logger.info(
        "[실험] 14 vs 18 피처 시작. window=%s~%s tickers=%d",
        args.start_date, args.end_date, len(tickers),
    )

    builder = DatasetBuilder(dual_source_enabled_for_lgbm=True, exogenous_enabled_for_lgbm=True)
    panel = builder.build_training_frame(tickers=tickers, start_date=args.start_date, end_date=args.end_date)
    logger.info("[실험] panel 빌드 완료. rows=%d cols=%d", len(panel), len(panel.columns))

    panel = _compute_extra_features(panel)

    missing = [c for c in extended_feature_cols if c not in panel.columns]
    if missing:
        raise RuntimeError(f"extended feature 누락: {missing}")

    logger.info("[실험] === Baseline %d피처 LambdaRank 학습 ===", len(baseline_feature_cols))
    oof_14, folds_14, _booster_14 = _train_lambdarank_oof(panel, baseline_feature_cols, args.label_col)
    sharpe_14 = _cross_sectional_oof_sharpe(panel, oof_14, sharpe_label_col, top_k_fraction, annualization_factor)

    logger.info("[실험] === Extended %d피처 LambdaRank 학습 ===", len(extended_feature_cols))
    oof_18, folds_18, booster_18 = _train_lambdarank_oof(panel, extended_feature_cols, args.label_col)
    sharpe_18 = _cross_sectional_oof_sharpe(panel, oof_18, sharpe_label_col, top_k_fraction, annualization_factor)

    delta = sharpe_18 - sharpe_14
    improved = delta > 0

    # feature importance (18피처 모델)
    importance_gain = booster_18.feature_importance(importance_type="gain").tolist()

    finished_at = datetime.now(_KST)
    output_dir = _ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_sec": (finished_at - started_at).total_seconds(),
        "args": {
            "start_date": args.start_date,
            "end_date": args.end_date,
            "n_tickers": len(tickers),
            "tickers": list(tickers),
            "label_col": args.label_col,
            "sharpe_label_col": sharpe_label_col,
            "top_k_fraction": top_k_fraction,
            "annualization_factor": annualization_factor,
            "baseline_feature_cols": baseline_feature_cols,
            "extended_feature_cols": extended_feature_cols,
            "extra_4_features": extra_features,
            "feature_cols_excluded_community": sorted(_COMMUNITY_FEATURES_EXCLUDED),
            "volume_ratio_window": int(cfg_exp["volume_ratio_window"]),
            "ret_window": int(cfg_exp["ret_window"]),
        },
        "panel": {
            "rows": int(len(panel)),
            "cols": int(len(panel.columns)),
            "loaded_tickers": panel.attrs.get("loaded_tickers", []),
            "missing_tickers": panel.attrs.get("missing_tickers", []),
        },
        "result": {
            "baseline_14_sharpe": sharpe_14,
            "extended_18_sharpe": sharpe_18,
            "delta_sharpe": delta,
            "improved": improved,
            "n_folds": len(folds_18),
            "n_samples": len(panel),
        },
        "folds_14": folds_14,
        "folds_18": folds_18,
        "importance": {
            "feature_names": extended_feature_cols,
            "gain": importance_gain,
        },
    }

    report_path = output_dir / f"feature_18_exp_{started_at.strftime('%Y%m%d_%H%M%S')}.json"
    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    print()
    print("=" * 60)
    print("14피처 vs 18피처 LambdaRank OOF Sharpe 비교")
    print("=" * 60)
    print(f"  window           : {args.start_date} ~ {args.end_date}")
    print(f"  n_tickers        : {len(tickers)}")
    print(f"  panel rows       : {len(panel):,}")
    print(f"  sharpe_label     : {sharpe_label_col}")
    print(f"  top_k_fraction   : {top_k_fraction}")
    print(f"  annualization    : {annualization_factor}")
    print()
    print(f"  [14피처] OOF Sharpe : {sharpe_14:+.4f}")
    print(f"  [18피처] OOF Sharpe : {sharpe_18:+.4f}")
    print(f"  delta_sharpe       : {delta:+.4f}")
    print(f"  improved           : {improved}")
    print()
    print(f"  report : {report_path}")
    print(f"  elapsed: {report['elapsed_sec']:.1f}s")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

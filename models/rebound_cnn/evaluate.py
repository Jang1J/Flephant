"""
KR-Rebound-CNN Evaluation Pipeline
- 3층 평가: Signal / Deployment / Portfolio

Usage:
    python models/rebound_cnn/evaluate.py
    python models/rebound_cnn/evaluate.py --date 20260325
"""

import argparse
import json
import pickle
import sys
from pathlib import Path

import numpy as np
import yaml

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_BASE_DIR))

MODEL_DIR = Path(__file__).resolve().parent
REPORT_DIR = _BASE_DIR / "reports" / "model_evaluation"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

UNIVERSE_CSV = _BASE_DIR / "config" / "universe_v1.csv"
RISK_POLICY = _BASE_DIR / "config" / "risk_policy_v0.yaml"


def _get_device():
    try:
        import torch
        if torch.backends.mps.is_available():
            return torch.device("mps")
        if torch.cuda.is_available():
            return torch.device("cuda")
        return torch.device("cpu")
    except ImportError:
        return None


def _load_config() -> dict:
    config_path = MODEL_DIR / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_risk_policy() -> dict:
    if RISK_POLICY.exists():
        with open(RISK_POLICY, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    return {}


# ---------------------------------------------------------------------------
# Signal Layer
# ---------------------------------------------------------------------------

def evaluate_signal_layer(
    probs: np.ndarray,
    labels: np.ndarray,
    config: dict,
    calibrator=None,
) -> dict:
    """
    Signal layer 평가.
    - Precision@5, Precision@10
    - Hit rate (buy/strong_buy 신호 중 실제 rebound 비율)
    - Brier score
    - Calibration curve (10-bin)
    - Uncertainty bucket별 성능
    """
    signal_map = config["inference"]["signal_map"]
    threshold = config["inference"]["confidence_threshold"]

    # Platt calibration 적용
    cal_probs = probs.copy()
    if calibrator is not None:
        try:
            cal_probs = calibrator.predict(probs)
        except Exception as e:
            print(f"[Modeler] calibrator 적용 실패: {e}")

    # Precision@K
    def precision_at_k(k: int) -> float:
        if len(probs) < k:
            return float("nan")
        top_k_idx = np.argsort(cal_probs)[-k:]
        return float(np.mean(labels[top_k_idx]))

    p_at_5 = precision_at_k(5)
    p_at_10 = precision_at_k(10)

    # 신호 분류 (프로덕션 map_signal_and_direction과 동일한 3단계)
    def classify_signal(p: float) -> str:
        if p >= 0.70:
            return "strong_buy"
        elif p >= 0.55:
            return "buy"
        else:
            return "hold"

    signals = [classify_signal(p) for p in cal_probs]

    # Hit rate (buy + strong_buy)
    buy_mask = np.array([s in ("buy", "strong_buy") for s in signals])
    hit_rate = float(np.mean(labels[buy_mask])) if buy_mask.sum() > 0 else float("nan")
    n_buy_signals = int(buy_mask.sum())

    # Brier score
    brier = float(np.mean((cal_probs - labels) ** 2))
    brier_raw = float(np.mean((probs - labels) ** 2))

    # Calibration curve (10-bin)
    n_bins = 10
    bin_edges = np.linspace(0, 1, n_bins + 1)
    cal_curve = []
    for i in range(n_bins):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        mask = (cal_probs >= lo) & (cal_probs < hi)
        if mask.sum() == 0:
            continue
        mean_pred = float(cal_probs[mask].mean())
        mean_actual = float(labels[mask].mean())
        cal_curve.append({
            "bin_low": round(lo, 2),
            "bin_high": round(hi, 2),
            "n_samples": int(mask.sum()),
            "mean_pred": round(mean_pred, 4),
            "mean_actual": round(mean_actual, 4),
        })

    # Uncertainty bucket별 성능 (low/mid/high confidence)
    uncertainty_buckets = {
        "low_confidence": (0.0, 0.35),
        "mid_confidence": (0.35, 0.65),
        "high_confidence": (0.65, 1.0),
    }
    unc_results = {}
    for bname, (lo, hi) in uncertainty_buckets.items():
        mask = (cal_probs >= lo) & (cal_probs < hi)
        if mask.sum() == 0:
            unc_results[bname] = {"n": 0, "hit_rate": None, "brier": None}
            continue
        hit = float(np.mean(labels[mask]))
        b = float(np.mean((cal_probs[mask] - labels[mask]) ** 2))
        unc_results[bname] = {
            "n": int(mask.sum()),
            "hit_rate": round(hit, 4),
            "brier": round(b, 6),
        }

    # AUC
    auc = 0.5
    try:
        from sklearn.metrics import roc_auc_score
        if len(np.unique(labels)) >= 2:
            auc = float(roc_auc_score(labels, cal_probs))
    except Exception:
        pass

    signal_counts = {}
    for s in ("strong_buy", "buy", "hold"):
        signal_counts[s] = int(sum(1 for sig in signals if sig == s))

    return {
        "n_samples": len(probs),
        "positive_rate": round(float(labels.mean()), 4),
        "precision_at_5": round(p_at_5, 4) if not np.isnan(p_at_5) else None,
        "precision_at_10": round(p_at_10, 4) if not np.isnan(p_at_10) else None,
        "hit_rate": round(hit_rate, 4) if not np.isnan(hit_rate) else None,
        "n_buy_signals": n_buy_signals,
        "brier_score": round(brier, 6),
        "brier_score_raw": round(brier_raw, 6),
        "auc_roc": round(auc, 4),
        "signal_counts": signal_counts,
        "calibration_curve": cal_curve,
        "uncertainty_buckets": unc_results,
    }


# ---------------------------------------------------------------------------
# Deployment Layer
# ---------------------------------------------------------------------------

def evaluate_deployment_layer(
    probs: np.ndarray,
    labels: np.ndarray,
    tickers: list,
    dates: list,
    config: dict,
    risk_policy: dict,
) -> dict:
    """
    Deployment layer 평가.
    - Risk/FDA approval rate 시뮬레이션
    - Average turnover
    - Sector concentration
    """
    import pandas as pd

    universe = pd.read_csv(UNIVERSE_CSV, dtype={"ticker": str})
    universe["ticker"] = universe["ticker"].apply(lambda x: str(x).zfill(6))
    sector_map = dict(zip(universe["ticker"], universe["wics_sector"]))

    signal_map = config["inference"]["signal_map"]
    position_policy = risk_policy.get("position_constraints", {})
    max_positions = position_policy.get("max_position_count", 10)
    max_sector_pct = position_policy.get("max_sector_weight", 40.0) / 100.0  # percent → ratio
    min_confidence = position_policy.get("min_confidence", 0.3)

    # 날짜별로 그룹화
    date_groups: dict = {}
    for i, (t, d, p) in enumerate(zip(tickers, dates, probs)):
        if d not in date_groups:
            date_groups[d] = []
        date_groups[d].append({"ticker": t, "prob": p, "label": labels[i]})

    total_signals = 0
    approved_signals = 0
    turnover_list = []
    sector_conc_list = []
    prev_holdings: set = set()

    for date_str in sorted(date_groups.keys()):
        day_data = date_groups[date_str]

        # buy/strong_buy 신호만 추출
        buy_candidates = [
            d for d in day_data
            if d["prob"] >= signal_map["buy"] and d["prob"] >= min_confidence
        ]
        buy_candidates.sort(key=lambda x: x["prob"], reverse=True)
        total_signals += len(buy_candidates)

        # 포지션 수 제한
        selected = buy_candidates[:max_positions]

        # 섹터 집중도 체크
        sector_counts: dict = {}
        approved = []
        for cand in selected:
            sec = sector_map.get(cand["ticker"], "Unknown")
            sector_counts[sec] = sector_counts.get(sec, 0) + 1

        n_selected = len(selected)
        final_approved = []
        if n_selected > 0:
            for cand in selected:
                sec = sector_map.get(cand["ticker"], "Unknown")
                sec_weight = sector_counts.get(sec, 0) / n_selected
                if sec_weight <= max_sector_pct:
                    final_approved.append(cand)

        approved_signals += len(final_approved)
        cur_holdings = {c["ticker"] for c in final_approved}

        # 턴오버: 변경된 포지션 / max(현재, 이전) 포지션 수
        if prev_holdings or cur_holdings:
            changed = len(prev_holdings.symmetric_difference(cur_holdings))
            denom = max(len(prev_holdings), len(cur_holdings), 1)
            turnover_list.append(changed / denom)
        prev_holdings = cur_holdings

        # 섹터 집중도
        if cur_holdings:
            sec_dist: dict = {}
            for t in cur_holdings:
                sec = sector_map.get(t, "Unknown")
                sec_dist[sec] = sec_dist.get(sec, 0) + 1
            max_conc = max(sec_dist.values()) / len(cur_holdings)
            sector_conc_list.append(max_conc)

    approval_rate = approved_signals / total_signals if total_signals > 0 else float("nan")
    avg_turnover = float(np.mean(turnover_list)) if turnover_list else float("nan")
    avg_sector_conc = float(np.mean(sector_conc_list)) if sector_conc_list else float("nan")

    return {
        "total_buy_signals": total_signals,
        "approved_signals": approved_signals,
        "approval_rate": round(approval_rate, 4) if not np.isnan(approval_rate) else None,
        "avg_daily_turnover": round(avg_turnover, 4) if not np.isnan(avg_turnover) else None,
        "avg_max_sector_concentration": round(avg_sector_conc, 4) if not np.isnan(avg_sector_conc) else None,
        "n_trading_days": len(date_groups),
    }


# ---------------------------------------------------------------------------
# Portfolio Layer
# ---------------------------------------------------------------------------

def evaluate_portfolio_layer(
    probs: np.ndarray,
    labels: np.ndarray,
    tickers: list,
    dates: list,
    config: dict,
    risk_policy: dict = None,
) -> dict:
    """
    Portfolio layer 평가 (가상 포트폴리오).
    - Cumulative return
    - Sharpe ratio
    - Maximum Drawdown
    - Average cash ratio
    """
    signal_map = config["inference"]["signal_map"]
    position_policy = risk_policy.get("position_constraints", {}) if risk_policy else {}
    min_confidence = position_policy.get("min_confidence", 0.3)
    max_positions = position_policy.get("max_position_count", 10)

    # 날짜별 그룹화
    date_groups: dict = {}
    for i, (t, d, p) in enumerate(zip(tickers, dates, probs)):
        if d not in date_groups:
            date_groups[d] = []
        date_groups[d].append({"ticker": t, "prob": float(p), "label": float(labels[i])})

    sorted_dates = sorted(date_groups.keys())
    daily_returns = []
    cash_ratios = []

    for date_str in sorted_dates:
        day_data = date_groups[date_str]
        buy_candidates = [
            d for d in day_data
            if d["prob"] >= signal_map["buy"] and d["prob"] >= min_confidence
        ]
        buy_candidates.sort(key=lambda x: x["prob"], reverse=True)
        top_10 = buy_candidates[:max_positions]

        n_pos = len(top_10)
        if n_pos == 0:
            daily_returns.append(0.0)
            cash_ratios.append(1.0)
            continue

        # 균등 배분 가상 수익률: label=1이면 assumed_positive_return, label=0이면 assumed_negative_return
        pos_ret = config.get("evaluation", {}).get("assumed_positive_return", 0.02)
        neg_ret = config.get("evaluation", {}).get("assumed_negative_return", -0.005)

        day_ret = 0.0
        for cand in top_10:
            weight = 1.0 / n_pos
            ret = pos_ret if cand["label"] == 1.0 else neg_ret
            day_ret += weight * ret

        # 잔여 현금 비율 (risk_policy의 min_cash_ratio 정책 반영)
        min_cash = risk_policy.get("core_rules", {}).get("min_cash_ratio", {}).get("ratio", 10.0) / 100.0 if risk_policy else 0.10
        invested = min(n_pos / max_positions, 1.0 - min_cash)
        cash_ratio = 1.0 - invested

        daily_returns.append(day_ret * invested)
        cash_ratios.append(cash_ratio)

    n_days = len(daily_returns)
    daily_returns_arr = np.array(daily_returns)

    # Cumulative return
    cumulative_ret = float(np.prod(1 + daily_returns_arr) - 1) if n_days > 0 else 0.0

    # Sharpe ratio (연율화, 252 거래일 기준)
    if n_days > 1 and daily_returns_arr.std() > 1e-8:
        sharpe = float(
            daily_returns_arr.mean() / daily_returns_arr.std() * np.sqrt(252)
        )
    else:
        sharpe = float("nan")

    # Maximum Drawdown
    cumulative_curve = np.cumprod(1 + daily_returns_arr)
    running_max = np.maximum.accumulate(cumulative_curve)
    drawdowns = (cumulative_curve - running_max) / (running_max + 1e-10)
    max_drawdown = float(drawdowns.min()) if len(drawdowns) > 0 else 0.0

    avg_cash = float(np.mean(cash_ratios)) if cash_ratios else float("nan")

    return {
        "n_trading_days": n_days,
        "cumulative_return": round(cumulative_ret, 6),
        "sharpe_ratio": round(sharpe, 4) if not np.isnan(sharpe) else None,
        "max_drawdown": round(max_drawdown, 6),
        "avg_cash_ratio": round(avg_cash, 4) if not np.isnan(avg_cash) else None,
        "daily_return_mean": round(float(daily_returns_arr.mean()), 6) if n_days > 0 else None,
        "daily_return_std": round(float(daily_returns_arr.std()), 6) if n_days > 1 else None,
    }


# ---------------------------------------------------------------------------
# Style Orthogonality Layer
# ---------------------------------------------------------------------------

def evaluate_style_orthogonality(
    probs: np.ndarray,
    labels: np.ndarray,
    tickers: list,
    dates: list,
    config: dict,
    calibrator=None,
    eval_ds=None,
) -> dict:
    """
    Style Orthogonality 평가.
    - corr(rebound_score, momentum_score): 낮을수록 momentum과 독립적
    - corr(rebound_score, market_return): 시장 수익률과의 상관
    - sector별 rebound score 분포
    - 목표: rebound alpha와 momentum alpha의 스타일 직교성 확인

    momentum proxy: 해당 종목의 ret_5d (dataset의 sector_feats에서 추출)
    market_return proxy: labels 분포 (유니버스 공통 시장 수익률 근사)
    """
    import pandas as pd

    universe = pd.read_csv(UNIVERSE_CSV, dtype={"ticker": str})
    universe["ticker"] = universe["ticker"].apply(lambda x: str(x).zfill(6))
    sector_map = dict(zip(universe["ticker"], universe["wics_sector"]))

    # calibrated rebound score
    rebound_scores = probs.copy()
    if calibrator is not None:
        try:
            rebound_scores = calibrator.predict(probs)
        except Exception as e:
            print(f"[Modeler] style_orthogonality calibrator 적용 실패: {e}")

    # ------------------------------------------------------------------
    # corr(rebound_score, momentum_score)
    # momentum proxy: dataset samples의 ret_5d raw 값 사용 (설계서 의도).
    # ret_5d raw 값은 ReboundDataset.samples[i]["ret_5d"]에 저장됨.
    # ret_5d_sector_z(z-score 변환값) 대신 raw ret_5d를 사용하여 설계서 정합성 확보.
    # ------------------------------------------------------------------

    # 날짜별 그룹화 + dataset samples에서 ret_5d raw 값 추출
    date_groups: dict = {}
    sample_ret5d_raw = {}  # (ticker, date) → ret_5d raw
    for s in (eval_ds.samples if hasattr(eval_ds, "samples") else []):
        key = (s["ticker"], s["date"])
        # ret_5d 메타 키가 있으면 raw 값 사용, 없으면 context_features[11] (구버전 호환)
        if "ret_5d" in s:
            sample_ret5d_raw[key] = float(s["ret_5d"])
        else:
            _CTX_SECTOR_REL_OFFSET = 11  # macro(4) + technical(5) + price_stretch(2)
            if hasattr(s["context_features"], "numpy"):
                ctx = s["context_features"].numpy()
            else:
                ctx = np.array(s["context_features"])
            sample_ret5d_raw[key] = float(ctx[_CTX_SECTOR_REL_OFFSET]) if len(ctx) > _CTX_SECTOR_REL_OFFSET else 0.0

    for i, (t, d) in enumerate(zip(tickers, dates)):
        if d not in date_groups:
            date_groups[d] = []
        key = (t, d)
        ret5d_raw = sample_ret5d_raw.get(key, 0.0)
        date_groups[d].append({
            "ticker": t,
            "score": rebound_scores[i],
            "label": labels[i],
            "ret_5d": ret5d_raw,
        })

    # sector별 rebound score 분포 수집
    sector_scores: dict = {}
    momentum_proxies = []
    rebound_scores_paired = []

    for date_str in sorted(date_groups.keys()):
        day_data = date_groups[date_str]

        for d in day_data:
            # momentum proxy: ret_5d raw (높을수록 모멘텀 강세, 설계서 의도)
            momentum_proxies.append(float(d["ret_5d"]))
            rebound_scores_paired.append(float(d["score"]))

            # sector별 수집
            sec = sector_map.get(d["ticker"], "Unknown")
            if sec not in sector_scores:
                sector_scores[sec] = []
            sector_scores[sec].append(float(d["score"]))

    # corr(rebound_score, momentum_proxy)
    corr_momentum = float("nan")
    if len(rebound_scores_paired) > 2:
        corr_arr = np.corrcoef(rebound_scores_paired, momentum_proxies)
        corr_momentum = float(corr_arr[0, 1]) if corr_arr.shape == (2, 2) else float("nan")

    # corr(rebound_score, market_return)
    # market_return proxy: 날짜별 universe 평균 ret_5d raw 값 사용 (가능하면).
    # ret_5d raw 값이 없는 샘플(구버전)이 있으면 labels 평균 fallback.
    date_mean_scores = []
    date_mean_market_rets = []
    has_raw_ret5d = any("ret_5d" in s for s in (eval_ds.samples if hasattr(eval_ds, "samples") else []))
    for date_str in sorted(date_groups.keys()):
        day_data = date_groups[date_str]
        day_score = np.mean([d["score"] for d in day_data])
        if has_raw_ret5d:
            # universe 평균 ret_5d (날짜별 시장 수익률 proxy)
            day_market_ret = np.mean([d["ret_5d"] for d in day_data])
        else:
            # fallback: label 평균으로 근사
            day_market_ret = np.mean([d["label"] for d in day_data])
        date_mean_scores.append(float(day_score))
        date_mean_market_rets.append(float(day_market_ret))

    corr_market = float("nan")
    if len(date_mean_scores) > 2:
        try:
            corr_arr2 = np.corrcoef(date_mean_scores, date_mean_market_rets)
            corr_market = float(corr_arr2[0, 1]) if corr_arr2.shape == (2, 2) else float("nan")
        except Exception as e:
            print(f"[Modeler] market correlation 계산 실패: {e}")

    # sector별 rebound score 분포 통계
    sector_distribution = {}
    for sec, scores in sector_scores.items():
        arr = np.array(scores)
        sector_distribution[sec] = {
            "n": len(arr),
            "mean": round(float(arr.mean()), 4),
            "std": round(float(arr.std()), 4),
            "median": round(float(np.median(arr)), 4),
            "q25": round(float(np.percentile(arr, 25)), 4),
            "q75": round(float(np.percentile(arr, 75)), 4),
        }

    # 전체 rebound score 통계
    all_scores_arr = np.array(rebound_scores)
    overall_stats = {
        "mean": round(float(all_scores_arr.mean()), 4),
        "std": round(float(all_scores_arr.std()), 4),
        "skewness": round(
            float(((all_scores_arr - all_scores_arr.mean()) ** 3).mean()
                  / (all_scores_arr.std() ** 3 + 1e-10)),
            4,
        ),
    }

    # style orthogonality 판정
    # 목표: |corr_momentum| < 0.3 (낮을수록 독립적)
    is_orthogonal = (
        not np.isnan(corr_momentum) and abs(corr_momentum) < 0.3
    )

    return {
        "corr_rebound_momentum": round(corr_momentum, 4) if not np.isnan(corr_momentum) else None,
        "corr_rebound_market": round(corr_market, 4) if not np.isnan(corr_market) else None,
        "is_style_orthogonal": is_orthogonal,
        "orthogonality_target": "|corr| < 0.3",
        "sector_distribution": sector_distribution,
        "overall_score_stats": overall_stats,
        "n_dates": len(date_groups),
        "n_samples": len(rebound_scores),
    }


# ---------------------------------------------------------------------------
# 메인 평가 실행
# ---------------------------------------------------------------------------

def run_evaluation(eval_date: str = None):
    """메인 평가 파이프라인."""
    import torch
    import torch.utils.data as td_utils

    from models.rebound_cnn.dataset import ReboundDataset
    from models.rebound_cnn.model import build_model

    config = _load_config()
    risk_policy = _load_risk_policy()
    device = _get_device()

    model_path = MODEL_DIR / "model.pt"
    if not model_path.exists():
        print(f"[Modeler] 모델 파일 없음: {model_path}. 먼저 train.py를 실행하세요.")
        return

    # training_log에서 n_context_features 동적 로드
    n_context_features = 26  # 기본값 (설계서 §10.2 명세)
    training_log_path = MODEL_DIR / "training_log.json"
    if training_log_path.exists():
        try:
            with open(training_log_path, "r", encoding="utf-8") as f:
                tlog = json.load(f)
            n_ctx_logged = tlog.get("n_context_features")
            if n_ctx_logged is not None:
                n_context_features = int(n_ctx_logged)
                print(f"[Modeler] training_log에서 n_context_features={n_context_features} 로드")
        except Exception as e:
            print(f"[Modeler] training_log 로드 실패 ({e}), 기본값 {n_context_features} 사용")

    model = build_model(n_context_features=n_context_features, device=device)
    model.load_state_dict(torch.load(model_path, map_location=device, weights_only=True))
    model.eval()
    print(f"[Modeler] 모델 로드: {model_path}")

    # context_scaler 로드 (StandardScaler, train fold 기준)
    scaler = None
    scaler_path = MODEL_DIR / "context_scaler.pkl"
    if scaler_path.exists():
        with open(scaler_path, "rb") as f:
            scaler = pickle.load(f)
        print(f"[Modeler] context_scaler.pkl 로드: {scaler_path}")
    else:
        print(f"[Modeler] context_scaler.pkl 없음 ({scaler_path}), scaler 미적용")

    # ensemble 모델 로드: seed 파일만 사용 (model.pt는 seed42와 중복되므로 제외)
    # model.pt는 seed 모델이 하나도 없을 때 fallback으로만 사용
    ensemble_models = []
    seeds = config["training"].get("ensemble_seeds", [42, 123, 456])
    for seed in seeds:
        seed_path = MODEL_DIR / f"model_seed{seed}.pt"
        if seed_path.exists():
            try:
                seed_model = build_model(n_context_features=n_context_features, device=device)
                seed_model.load_state_dict(
                    torch.load(seed_path, map_location=device, weights_only=True)
                )
                seed_model.eval()
                ensemble_models.append(seed_model)
                print(f"[Modeler] Ensemble seed {seed} 모델 로드: {seed_path}")
            except Exception as e:
                print(f"[Modeler] Seed {seed} 모델 로드 실패: {e}")

    if not ensemble_models:
        # fallback: seed 파일이 없으면 model.pt 단독 사용
        print(f"[Modeler] seed 모델 없음 → model.pt fallback 사용")
        ensemble_models = [model]

    # calibrator 로드 (isotonic regression)
    calibrator = None
    cal_path = MODEL_DIR / "calibrator.pkl"
    if cal_path.exists():
        with open(cal_path, "rb") as f:
            calibrator = pickle.load(f)
        print(f"[Modeler] Calibrator 로드: {cal_path}")

    # 평가 날짜 결정
    dmp_dir = _BASE_DIR / "artifacts" / "daily_market_packet"
    dmp_files = sorted(dmp_dir.glob("DMP-*.json"))
    all_dates = [p.stem.replace("DMP-", "") for p in dmp_files]

    if not all_dates:
        print("[Modeler] DMP 파일이 없습니다. 평가 중단.")
        return

    if eval_date:
        if eval_date not in all_dates:
            print(f"[Modeler] 경고: {eval_date} DMP 없음. 가장 최근 날짜 사용.")
            eval_date = all_dates[-1]
        eval_dates = [d for d in all_dates if d <= eval_date]
    else:
        eval_date = all_dates[-1]
        eval_dates = all_dates

    lookback = config["data"]["lookback_days"]
    horizon = config["data"]["forecast_horizon"]

    if len(eval_dates) <= lookback + horizon:
        print(
            f"[Modeler] 경고: 평가 날짜 {len(eval_dates)}일이 lookback+horizon({lookback+horizon})보다 적음. "
            "가용 전체 날짜로 평가 진행."
        )
        eval_dates = all_dates

    print(f"[Modeler] 평가 날짜 범위: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)}일)")

    eval_ds = ReboundDataset(dmp_dir, eval_dates, config)
    if len(eval_ds) == 0:
        print("[Modeler] 평가 샘플 0개. 평가 중단.")
        return

    # context_scaler 적용 (train scaler 기준, PIT-safe transform only)
    if scaler is not None:
        eval_ds.apply_scaler(scaler)

    eval_loader = td_utils.DataLoader(eval_ds, batch_size=32, shuffle=False)

    # 추론 (3-seed ensemble 평균 + uncertainty_score)
    all_labels = []
    all_tickers = []
    all_dates_list = []

    # 첫 번째 모델 순회 시 labels 수집, 이후 모델은 probs만 수집
    ensemble_probs_list = []
    for em_idx, em in enumerate(ensemble_models):
        em.eval()
        seed_probs = []
        with torch.no_grad():
            for chart, context, label in eval_loader:
                chart = chart.to(device)
                context = context.to(device)
                prob = em(chart, context)
                seed_probs.extend(prob.squeeze(1).cpu().numpy().tolist())
                if em_idx == 0:
                    all_labels.extend(label.numpy().tolist())
        ensemble_probs_list.append(np.array(seed_probs))

    # Ensemble 평균 + 분산(uncertainty)
    probs_matrix = np.stack(ensemble_probs_list, axis=0)  # (n_models, n_samples)
    probs_arr = probs_matrix.mean(axis=0)                  # (n_samples,) ensemble 평균
    uncertainty_arr = probs_matrix.var(axis=0)             # (n_samples,) ensemble variance

    labels_arr = np.array(all_labels)

    print(
        f"[Modeler] Ensemble 모델 수: {len(ensemble_models)}, "
        f"평균 uncertainty: {uncertainty_arr.mean():.6f}"
    )

    # Dataset에서 ticker/date 메타 추출
    for s in eval_ds.samples:
        all_tickers.append(s["ticker"])
        all_dates_list.append(s["date"])

    print(f"[Modeler] 평가 샘플: {len(probs_arr)}개, 양성 비율: {labels_arr.mean():.3f}")

    # 4층 평가
    signal_result = evaluate_signal_layer(probs_arr, labels_arr, config, calibrator)
    deployment_result = evaluate_deployment_layer(
        probs_arr, labels_arr, all_tickers, all_dates_list, config, risk_policy
    )
    portfolio_result = evaluate_portfolio_layer(
        probs_arr, labels_arr, all_tickers, all_dates_list, config, risk_policy
    )
    orthogonality_result = evaluate_style_orthogonality(
        probs_arr, labels_arr, all_tickers, all_dates_list, config, calibrator, eval_ds
    )

    print(
        f"[Modeler] Style Orthogonality: "
        f"corr_momentum={orthogonality_result.get('corr_rebound_momentum')}, "
        f"is_orthogonal={orthogonality_result.get('is_style_orthogonal')}"
    )

    # Committee v1.1 평가 (committee.enabled=true 시만 실행)
    committee_result = None
    if config.get("committee", {}).get("enabled", False):
        committee_result = evaluate_committee_layer(
            probs_arr, labels_arr, config, calibrator, eval_ds
        )
        print(
            f"[Modeler] Committee 평가 완료: "
            f"tab_auc={committee_result.get('tree_core_auc')}, "
            f"fused_auc={committee_result.get('fused_auc')}"
        )
    else:
        print("[Modeler] Committee 평가 스킵 (committee.enabled=false).")

    # Calibration 비교 메타 로드 (calibrator_meta.json)
    calibration_meta = None
    cal_meta_path = MODEL_DIR / "calibrator_meta.json"
    if cal_meta_path.exists():
        try:
            with open(cal_meta_path, "r", encoding="utf-8") as f:
                calibration_meta = json.load(f)
            print(
                f"[Modeler] Calibration 비교 메타 로드: "
                f"winner={calibration_meta.get('winner')}"
            )
        except Exception as e:
            print(f"[Modeler] calibrator_meta.json 로드 실패: {e}")

    # 결과 취합
    eval_result = {
        "eval_date": eval_date,
        "eval_date_range": {"start": eval_dates[0], "end": eval_dates[-1]},
        "model_path": str(model_path),
        "n_ensemble_models": len(ensemble_models),
        "n_samples": len(probs_arr),
        "mean_uncertainty": round(float(uncertainty_arr.mean()), 8),
        "signal_layer": signal_result,
        "deployment_layer": deployment_result,
        "portfolio_layer": portfolio_result,
        "style_orthogonality": orthogonality_result,
        "committee_layer": committee_result,
        "calibration_comparison": calibration_meta,
    }

    # JSON 저장
    json_path = REPORT_DIR / f"eval_{eval_date}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(eval_result, f, ensure_ascii=False, indent=2)
    print(f"[Modeler] 평가 결과 저장: {json_path}")

    # Markdown 저장
    md_path = REPORT_DIR / f"eval_{eval_date}.md"
    _write_eval_markdown(eval_result, md_path)
    print(f"[Modeler] 평가 리포트 저장: {md_path}")

    return eval_result


def evaluate_committee_layer(
    probs: np.ndarray,
    labels: np.ndarray,
    config: dict,
    calibrator=None,
    eval_ds=None,
) -> dict:
    """Committee v1.1 평가.

    Tree Core 단독 AUC, CNN 단독 AUC, Fusion AUC 비교.
    tree_core.pkl 없으면 Tree Core 관련 지표는 None으로 처리.
    """
    from sklearn.metrics import roc_auc_score, brier_score_loss

    # calibrated CNN probs
    cnn_probs = probs.copy()
    if calibrator is not None:
        try:
            if isinstance(calibrator, dict) and calibrator.get("type") == "temperature":
                T = calibrator["T"]
                eps = 1e-8
                logits = np.log(
                    np.clip(cnn_probs, eps, 1 - eps) / (1 - np.clip(cnn_probs, eps, 1 - eps))
                )
                cnn_probs = 1.0 / (1.0 + np.exp(-logits / T))
            else:
                cnn_probs = calibrator.predict(cnn_probs)
        except Exception as e:
            print(f"[Modeler] committee_layer calibrator 적용 실패: {e}")

    cnn_auc = 0.5
    cnn_brier = float("nan")
    if len(np.unique(labels)) >= 2:
        try:
            cnn_auc = float(roc_auc_score(labels, cnn_probs))
            cnn_brier = float(brier_score_loss(labels, cnn_probs))
        except Exception:
            pass

    # Tree Core 단독 평가
    tab_auc = None
    tab_brier = None
    fused_auc = None
    fused_brier = None
    n_agreement_high = None
    mean_agreement = None

    try:
        from models.rebound_cnn.committee import load_tree_core, fuse_scores, extract_context_arrays

        tree_core = load_tree_core()
        if tree_core is not None and eval_ds is not None:
            X, y = extract_context_arrays(eval_ds)

            # context_scaler 로드 후 transform (PIT-safe)
            scaler_path = MODEL_DIR / "context_scaler.pkl"
            if scaler_path.exists():
                import pickle as _pickle
                with open(scaler_path, "rb") as _f:
                    _scaler = _pickle.load(_f)
                try:
                    X = _scaler.transform(X)
                except Exception as _e:
                    print(f"[Modeler] committee scaler transform 실패: {_e}")

            tab_probs = tree_core.predict_proba(X)[:, 1]

            if len(np.unique(y)) >= 2:
                try:
                    tab_auc = round(float(roc_auc_score(y, tab_probs)), 4)
                    tab_brier = round(float(brier_score_loss(y, tab_probs)), 6)
                except Exception:
                    pass

            # Fusion 평가
            committee_cfg = config.get("committee", {})
            tab_w = float(committee_cfg.get("tab_weight", 0.65))
            cnn_w = float(committee_cfg.get("cnn_weight", 0.35))
            thr = float(committee_cfg.get("agreement_threshold", 0.55))

            fused_list = []
            agree_list = []
            for idx in range(len(tab_probs)):
                fp, agr, _ = fuse_scores(
                    float(tab_probs[idx]),
                    float(cnn_probs[idx]),
                    tab_weight=tab_w,
                    cnn_weight=cnn_w,
                    agreement_threshold=thr,
                )
                fused_list.append(fp)
                agree_list.append(agr)

            fused_arr = np.array(fused_list)
            agree_arr = np.array(agree_list)
            mean_agreement = round(float(agree_arr.mean()), 4)
            n_agreement_high = int((agree_arr >= thr).sum())

            if len(np.unique(labels)) >= 2:
                try:
                    fused_auc = round(float(roc_auc_score(labels, fused_arr)), 4)
                    fused_brier = round(float(brier_score_loss(labels, fused_arr)), 6)
                except Exception:
                    pass

            print(
                f"[Modeler] Committee 평가: "
                f"tab_auc={tab_auc}, cnn_auc={round(cnn_auc, 4)}, fused_auc={fused_auc}, "
                f"mean_agreement={mean_agreement}"
            )
        else:
            print("[Modeler] Committee 평가: tree_core.pkl 없음 또는 eval_ds 없음 → 스킵")
    except Exception as e:
        print(f"[Modeler] Committee 평가 실패: {e}")

    return {
        "committee_enabled": config.get("committee", {}).get("enabled", False),
        "cnn_auc": round(cnn_auc, 4),
        "cnn_brier": round(cnn_brier, 6) if not (cnn_brier != cnn_brier) else None,
        "tree_core_auc": tab_auc,
        "tree_core_brier": tab_brier,
        "fused_auc": fused_auc,
        "fused_brier": fused_brier,
        "mean_agreement": mean_agreement,
        "n_high_agreement": n_agreement_high,
    }


def _write_eval_markdown(result: dict, path: Path):
    """평가 결과를 Markdown 형식으로 저장."""
    sig = result["signal_layer"]
    dep = result["deployment_layer"]
    port = result["portfolio_layer"]
    orth = result.get("style_orthogonality", {})
    comm = result.get("committee_layer") or {}
    cal_cmp = result.get("calibration_comparison") or {}

    lines = [
        f"# KR-Rebound-CNN 평가 리포트",
        f"",
        f"- **평가 날짜**: {result['eval_date']}",
        f"- **평가 범위**: {result['eval_date_range']['start']} ~ {result['eval_date_range']['end']}",
        f"- **총 샘플**: {result['n_samples']}",
        f"- **Ensemble 모델 수**: {result.get('n_ensemble_models', 1)}",
        f"- **평균 Uncertainty**: {result.get('mean_uncertainty')}",
        f"",
        f"---",
        f"",
        f"## 1. Signal Layer",
        f"",
        f"| 지표 | 값 |",
        f"|------|-----|",
        f"| Precision@5 | {sig.get('precision_at_5')} |",
        f"| Precision@10 | {sig.get('precision_at_10')} |",
        f"| Hit Rate (buy 신호) | {sig.get('hit_rate')} |",
        f"| Buy 신호 수 | {sig.get('n_buy_signals')} |",
        f"| Brier Score (calibrated) | {sig.get('brier_score')} |",
        f"| Brier Score (raw) | {sig.get('brier_score_raw')} |",
        f"| AUC-ROC | {sig.get('auc_roc')} |",
        f"| 양성 비율 | {sig.get('positive_rate')} |",
        f"",
        f"### 신호 분포",
        f"",
        f"| 신호 | 건수 |",
        f"|------|------|",
    ]

    for sig_name, cnt in (sig.get("signal_counts") or {}).items():
        lines.append(f"| {sig_name} | {cnt} |")

    lines += [
        f"",
        f"### Uncertainty Bucket별 성능",
        f"",
        f"| Bucket | N | Hit Rate | Brier |",
        f"|--------|---|----------|-------|",
    ]
    for bname, bdata in (sig.get("uncertainty_buckets") or {}).items():
        lines.append(
            f"| {bname} | {bdata.get('n', 0)} | {bdata.get('hit_rate')} | {bdata.get('brier')} |"
        )

    lines += [
        f"",
        f"---",
        f"",
        f"## 2. Deployment Layer",
        f"",
        f"| 지표 | 값 |",
        f"|------|-----|",
        f"| 총 매수 신호 | {dep.get('total_buy_signals')} |",
        f"| 승인된 신호 | {dep.get('approved_signals')} |",
        f"| 승인율 | {dep.get('approval_rate')} |",
        f"| 평균 일간 턴오버 | {dep.get('avg_daily_turnover')} |",
        f"| 평균 섹터 최대 집중도 | {dep.get('avg_max_sector_concentration')} |",
        f"| 평가 거래일 수 | {dep.get('n_trading_days')} |",
        f"",
        f"---",
        f"",
        f"## 3. Portfolio Layer (가상)",
        f"",
        f"| 지표 | 값 |",
        f"|------|-----|",
        f"| 누적 수익률 | {port.get('cumulative_return')} |",
        f"| Sharpe Ratio (연율) | {port.get('sharpe_ratio')} |",
        f"| Max Drawdown | {port.get('max_drawdown')} |",
        f"| 평균 현금 비율 | {port.get('avg_cash_ratio')} |",
        f"| 일간 수익률 평균 | {port.get('daily_return_mean')} |",
        f"| 일간 수익률 표준편차 | {port.get('daily_return_std')} |",
        f"| 평가 거래일 수 | {port.get('n_trading_days')} |",
        f"",
        f"---",
        f"",
        f"## 4. Style Orthogonality",
        f"",
        f"| 지표 | 값 |",
        f"|------|-----|",
        f"| corr(rebound, momentum) | {orth.get('corr_rebound_momentum')} |",
        f"| corr(rebound, market) | {orth.get('corr_rebound_market')} |",
        f"| 스타일 직교성 판정 | {orth.get('is_style_orthogonal')} |",
        f"| 직교성 기준 | {orth.get('orthogonality_target')} |",
        f"| Score 평균 | {orth.get('overall_score_stats', {}).get('mean')} |",
        f"| Score 표준편차 | {orth.get('overall_score_stats', {}).get('std')} |",
        f"| Score 왜도 | {orth.get('overall_score_stats', {}).get('skewness')} |",
        f"",
        f"### Sector별 Rebound Score 분포",
        f"",
        f"| Sector | N | Mean | Std | Median |",
        f"|--------|---|------|-----|--------|",
    ]

    for sec, dist in (orth.get("sector_distribution") or {}).items():
        lines.append(
            f"| {sec} | {dist.get('n', 0)} | {dist.get('mean')} "
            f"| {dist.get('std')} | {dist.get('median')} |"
        )

    # Committee v1.1 섹션
    lines += [
        f"",
        f"---",
        f"",
        f"## 5. Committee v1.1 (Tree Core + CNN)",
        f"",
        f"| 지표 | 값 |",
        f"|------|-----|",
        f"| Committee 활성화 | {comm.get('committee_enabled', False)} |",
        f"| CNN AUC | {comm.get('cnn_auc')} |",
        f"| CNN Brier | {comm.get('cnn_brier')} |",
        f"| Tree Core AUC | {comm.get('tree_core_auc')} |",
        f"| Tree Core Brier | {comm.get('tree_core_brier')} |",
        f"| Fused AUC | {comm.get('fused_auc')} |",
        f"| Fused Brier | {comm.get('fused_brier')} |",
        f"| 평균 Agreement | {comm.get('mean_agreement')} |",
        f"| High-Agreement 건수 | {comm.get('n_high_agreement')} |",
        f"",
        f"---",
        f"",
        f"## 6. Calibration 비교 (Temperature vs Isotonic)",
        f"",
        f"| 방법 | Brier | ECE | 비고 |",
        f"|------|-------|-----|------|",
        f"| Raw | {cal_cmp.get('raw', {}).get('brier')} "
        f"| {cal_cmp.get('raw', {}).get('ece')} | 보정 없음 |",
        f"| Temperature | {cal_cmp.get('temperature', {}).get('brier')} "
        f"| {cal_cmp.get('temperature', {}).get('ece')} "
        f"| T={cal_cmp.get('temperature', {}).get('T')} |",
        f"| Isotonic | {cal_cmp.get('isotonic', {}).get('brier')} "
        f"| {cal_cmp.get('isotonic', {}).get('ece')} | |",
        f"| **Winner** | **{cal_cmp.get('winner', '-')}** | | |",
        f"",
    ]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KR-Rebound-CNN Evaluation")
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="평가 기준 날짜 (YYYYMMDD). 미지정 시 가장 최근 날짜.",
    )
    args = parser.parse_args()
    run_evaluation(eval_date=args.date)

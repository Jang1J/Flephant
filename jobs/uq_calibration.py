"""
UQ Calibration v0 — Execution Uncertainty Proxy
- StrategyCard의 confidence, pre_risk_score, 기술적 지표로부터
  "실행 불확실성"을 추정하는 logistic regression 모델
- 출력: uncertainty_score (0~1), P85 threshold 산출

Usage:
    python jobs/uq_calibration.py --train             # 모델 학습
    python jobs/uq_calibration.py --predict YYYYMMDD  # 예측 테스트
    python jobs/uq_calibration.py --recalibrate --days 60  # threshold 재검증

Online Inference 경로:
    run_risk_engine.py (Step 5.5) →
        from jobs.uq_calibration import predict_uncertainty
        predict_uncertainty(features_dict, ticker) →
        반환: {"uncertainty_score": float|None, ...}

    모델 파일(models/uq_model_v0.pkl) 존재 여부에 따른 동작:
    - 모델 없음 → uncertainty_score = None (graceful fallback, UQ 비활성 동작 유지)
    - 모델 있음 → logistic regression으로 실제 예측값 반환 (0~1)
"""

import sys
import json
import pickle
import numpy as np
import pandas as pd
from datetime import datetime
from pathlib import Path
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_val_score
from sklearn.preprocessing import StandardScaler

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

from connectors import now_kst_iso

MODEL_DIR = _BASE_DIR / "models"
DMP_DIR = _BASE_DIR / "artifacts" / "daily_market_packet"


def generate_training_data(n_samples: int = 500) -> pd.DataFrame:
    """
    Mock 학습 데이터 생성
    실제로는 AI #2의 StrategyCard + 실현 수익률로 만들지만,
    Phase 1에서는 synthetic 데이터로 UQ 파이프라인 검증

    Features:
    - confidence: StrategyCard confidence
    - pre_risk_score: pre_risk_score
    - rsi_14: RSI
    - volume_ratio_20: 거래량 비율
    - return_5d: 5일 수익률

    Target:
    - execution_miss: 실행 결과가 기대와 크게 다른 경우 (1=miss, 0=hit)
    """
    np.random.seed(42)

    confidence = np.random.beta(5, 2, n_samples)  # 높은 confidence 쪽으로 치우침
    pre_risk_score = np.random.normal(0, 0.5, n_samples).clip(-1, 1)
    rsi = np.random.normal(50, 15, n_samples).clip(10, 90)
    vol_ratio = np.random.lognormal(0, 0.5, n_samples).clip(0.1, 5)
    ret_5d = np.random.normal(0, 3, n_samples)

    # execution_miss 확률: confidence 낮을수록, vol_ratio 높을수록, rsi 극단값일수록 miss 확률 높음
    logit = (
        -2.0 * confidence
        + 0.3 * np.abs(pre_risk_score)
        + 0.02 * np.abs(rsi - 50)
        + 0.5 * np.log(vol_ratio)
        - 0.05 * ret_5d
        + np.random.normal(0, 0.3, n_samples)
    )

    miss_prob = 1 / (1 + np.exp(-logit))
    execution_miss = (np.random.random(n_samples) < miss_prob).astype(int)

    df = pd.DataFrame({
        "confidence": confidence,
        "pre_risk_score": pre_risk_score,
        "rsi_14": rsi,
        "volume_ratio_20": vol_ratio,
        "return_5d": ret_5d,
        "execution_miss": execution_miss,
    })

    return df


def train_uq_model():
    """UQ 모델 학습 + P85 threshold 산출"""
    print(f"\n{'='*60}")
    print(f"  UQ Calibration v0 — 모델 학습")
    print(f"{'='*60}\n")

    # 데이터
    df = generate_training_data(500)
    print(f"학습 데이터: {len(df)}건 (miss rate: {df['execution_miss'].mean():.2%})")

    features = ["confidence", "pre_risk_score", "rsi_14", "volume_ratio_20", "return_5d"]
    X = df[features].values
    y = df["execution_miss"].values

    # Scaling
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Logistic Regression
    model = LogisticRegression(random_state=42, max_iter=1000)
    model.fit(X_scaled, y)

    # Cross-validation
    scores = cross_val_score(model, X_scaled, y, cv=5, scoring="roc_auc")
    print(f"CV AUC: {scores.mean():.4f} ± {scores.std():.4f}")

    # P85 threshold 산출
    probs = model.predict_proba(X_scaled)[:, 1]
    p85 = float(np.percentile(probs, 85))
    print(f"P85 threshold: {p85:.4f}")

    # Feature importance
    print(f"\nFeature coefficients:")
    for feat, coef in zip(features, model.coef_[0]):
        print(f"  {feat}: {coef:.4f}")

    # 저장
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    model_info = {
        "model_type": "logistic_regression",
        "features": features,
        "p85_threshold": p85,
        "cv_auc_mean": round(float(scores.mean()), 4),
        "cv_auc_std": round(float(scores.std()), 4),
        "coefficients": {f: round(float(c), 4) for f, c in zip(features, model.coef_[0])},
        "intercept": round(float(model.intercept_[0]), 4),
        "miss_rate": round(float(df["execution_miss"].mean()), 4),
        "trained_at": now_kst_iso(),
        "note": "Phase 1 — synthetic data, 실제 StrategyCard + 실현수익률 연결 전",
    }

    # pickle 모델
    with open(MODEL_DIR / "uq_model_v0.pkl", "wb") as f:
        pickle.dump({"model": model, "scaler": scaler, "info": model_info}, f)

    # JSON 메타
    with open(MODEL_DIR / "uq_model_v0_meta.json", "w", encoding="utf-8") as f:
        json.dump(model_info, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 모델 저장: models/uq_model_v0.pkl")
    print(f"✅ 메타 저장: models/uq_model_v0_meta.json")

    return model_info


def predict_uncertainty(features_dict: dict, ticker: str = "") -> dict:
    """
    단일 종목 uncertainty 예측 (Online Inference)

    모델 파일(models/uq_model_v0.pkl) 존재 여부:
    - 없음: uncertainty_score = None 반환 (graceful fallback, 기존 UQ 비활성 동작 유지)
    - 있음: logistic regression으로 실제 예측값 반환 (0~1)

    Args:
        features_dict: confidence, pre_risk_score, rsi_14, volume_ratio_20, return_5d
        ticker: 종목코드 (로그 추적용, zfill 불필요 — 호출자가 책임)

    Returns:
        dict with keys: ticker, uncertainty_score, p85_value, exceeds_p85, calibration_info
        모델 없을 때: {"uncertainty_score": None, "error": "모델 없음"}
    """
    model_path = MODEL_DIR / "uq_model_v0.pkl"
    if not model_path.exists():
        return {"uncertainty_score": None, "error": "모델 없음"}

    with open(model_path, "rb") as f:
        bundle = pickle.load(f)

    model = bundle["model"]
    scaler = bundle["scaler"]
    info = bundle["info"]
    feat_names = info["features"]

    X = np.array([[features_dict.get(f, 0) for f in feat_names]])
    X_scaled = scaler.transform(X)
    prob = float(model.predict_proba(X_scaled)[0, 1])

    return {
        "ticker": ticker,
        "uncertainty_score": round(prob, 4),
        "p85_value": info["p85_threshold"],
        "exceeds_p85": prob >= info["p85_threshold"],
        "calibration_info": {
            "model_version": "uq_model_v0",
            "model_type": info.get("model_type", "logistic_regression"),
            "trained_at": info.get("trained_at", ""),
            "train_end_date": info.get("trained_at", "")[:10] if info.get("trained_at") else None,
            "cv_auc_mean": info.get("cv_auc_mean"),
            "brier_score": None,  # Phase 2에서 실데이터 기반 산출
            "ece": None,          # Phase 2에서 실데이터 기반 산출
        },
    }


def batch_predict(target_date: str):
    """DMP의 기술적 지표로 전종목 uncertainty 예측"""
    print(f"\n{'='*60}")
    print(f"  UQ Prediction: {target_date}")
    print(f"{'='*60}\n")

    dmp_path = DMP_DIR / f"DMP-{target_date}.json"
    if not dmp_path.exists():
        print(f"❌ DMP 없음: {dmp_path}")
        return

    with open(dmp_path) as f:
        dmp = json.load(f)

    results = []
    exceed_count = 0

    for ticker, md in dmp.get("market_data", {}).items():
        tech = md.get("tech_features", {})
        feat = {
            "confidence": 0.7,  # mock — 실제는 StrategyCard에서
            "pre_risk_score": 0.0,
            "rsi_14": tech.get("rsi_14", 50),
            "volume_ratio_20": tech.get("volume_ratio_20", 1.0),
            "return_5d": tech.get("return_5d", 0),
        }

        pred = predict_uncertainty(feat, ticker=ticker)
        results.append(pred)

        if pred.get("exceeds_p85"):
            exceed_count += 1

    print(f"총 {len(results)}종목 예측")
    print(f"P85 초과: {exceed_count}종목")

    # 상위 5개 출력
    results.sort(key=lambda x: x.get("uncertainty_score", 0), reverse=True)
    print(f"\n[가장 불확실한 Top 5]")
    for r in results[:5]:
        flag = "⚠️" if r.get("exceeds_p85") else "  "
        print(f"  {flag} {r['ticker']}: uncertainty={r['uncertainty_score']}")

    return results


def recalibrate_threshold(historical_scores: list, historical_outcomes: list) -> float:
    """
    실제 SC uncertainty_score와 실현 수익률을 비교하여
    최적 P85 threshold를 재계산한다.
    Phase 2: Backtest 결과 기반 자동 재검증.
    현재: 수동 호출용 유틸리티.

    Args:
        historical_scores: 과거 uncertainty_score 리스트 (0~1)
        historical_outcomes: 실현 수익률 부호 일치 여부 (1=miss/loss, 0=hit/win)
                             len(historical_scores) == len(historical_outcomes) 필수

    Returns:
        최적 P85 threshold (float). 데이터 부족 시 현재 모델의 p85_threshold 반환.

    Side effects:
        models/uq_threshold_report.json에 결과 저장
    """
    print(f"\n[UQ] threshold 재검증 시작")
    print(f"  samples: {len(historical_scores)}")

    yaml_path = _BASE_DIR / "config" / "risk_policy_v0.yaml"
    with open(yaml_path) as f:
        import yaml as _yaml
        policy = _yaml.safe_load(f)

    uq_cfg = policy.get("position_constraints", {}).get("uq_tail_cap", {})
    recal_cfg = uq_cfg.get("auto_recalibrate_settings", {})
    min_samples = recal_cfg.get("recalibrate_min_samples", 30)
    current_threshold = uq_cfg.get("p85_threshold", 0.7)

    if len(historical_scores) < min_samples:
        print(f"  [UQ] 샘플 부족 ({len(historical_scores)} < {min_samples}) → 현재 threshold={current_threshold} 유지")
        report = {
            "status": "insufficient_samples",
            "required_min_samples": min_samples,
            "actual_samples": len(historical_scores),
            "threshold_unchanged": current_threshold,
            "generated_at": now_kst_iso(),
        }
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        with open(MODEL_DIR / "uq_threshold_report.json", "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return current_threshold

    scores = np.array(historical_scores)
    outcomes = np.array(historical_outcomes)

    # 최적 threshold = P85 percentile of scores where outcome == loss(1)
    loss_scores = scores[outcomes == 1]
    if len(loss_scores) == 0:
        print(f"  [UQ] loss 샘플 없음 → 현재 threshold={current_threshold} 유지")
        new_threshold = current_threshold
    else:
        new_threshold = float(np.percentile(loss_scores, 85))
        print(f"  [UQ] loss 샘플 {len(loss_scores)}건 기반 P85 threshold={new_threshold:.4f}")

    report = {
        "status": "recalibrated",
        "total_samples": len(historical_scores),
        "loss_samples": int(len(loss_scores)),
        "win_samples": int(np.sum(outcomes == 0)),
        "previous_threshold": current_threshold,
        "new_threshold": round(new_threshold, 4),
        "p50_loss_score": round(float(np.percentile(loss_scores, 50)), 4) if len(loss_scores) > 0 else None,
        "p85_all_scores": round(float(np.percentile(scores, 85)), 4),
        "note": "Phase 2에서 자동 적용. Phase 1에서는 참고용.",
        "generated_at": now_kst_iso(),
    }

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    with open(MODEL_DIR / "uq_threshold_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"  [UQ] threshold 재검증 완료: {current_threshold} → {new_threshold:.4f}")
    print(f"  [UQ] 보고서 저장: models/uq_threshold_report.json")
    return new_threshold


if __name__ == "__main__":
    import argparse
    _parser = argparse.ArgumentParser(description="UQ Calibration v0")
    _parser.add_argument("--train", action="store_true", help="UQ 모델 학습")
    _parser.add_argument("--predict", metavar="YYYYMMDD", help="전종목 uncertainty 예측")
    _parser.add_argument("--recalibrate", action="store_true", help="threshold 재검증 (--days 필요)")
    _parser.add_argument("--days", type=int, default=60, help="재검증 look-back 기간 (일)")
    _args = _parser.parse_args()

    if _args.train:
        train_uq_model()
    elif _args.predict:
        batch_predict(_args.predict)
    elif _args.recalibrate:
        print(f"[UQ] --recalibrate: look-back {_args.days}일 기반 synthetic 데이터로 재검증")
        # Phase 1: synthetic 데이터 사용 (Phase 2에서 실제 Backtest 데이터로 교체)
        np.random.seed(0)
        n = _args.days * 5  # 종목 수 근사
        s_scores = np.random.beta(2, 5, n).tolist()
        s_outcomes = (np.random.random(n) < 0.3).astype(int).tolist()
        recalibrate_threshold(s_scores, s_outcomes)
    else:
        # 기본: 학습 + 예측
        train_uq_model()
        print()
        batch_predict("20260320")

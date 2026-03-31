"""
LightGBM Momentum StrategyCard Builder (AI #2)

학습된 LightGBM 모델로 종목별 quant_score를 예측하고,
TTP에서 news_signal을 수집한 뒤 synthesizer로 결합하여
StrategyCard를 생성한다.

Branch 출력:
  artifacts/strategy_card_variants/momentum/SC-{date}.json  (synthesized)
  artifacts/strategy_card_variants/quant_only/SC-{date}.json
  artifacts/strategy_card_variants/news_only/SC-{date}.json

Usage:
    python jobs/build_strategy_card_momentum.py YYYYMMDD
    python jobs/build_strategy_card_momentum.py YYYYMMDD --publish
"""

import sys
import json
import argparse
import shutil
from pathlib import Path
from datetime import datetime

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

import numpy as np
import pandas as pd
import yaml

from connectors import make_snapshot_dt, now_kst_iso
from models.strategy_model.feature_factory import (
    build_cross_sectional_raw,
    extract_single_ticker_features,
)

# ── 경로 설정 ──────────────────────────────────────────────────
UNIVERSE_PATH     = _BASE_DIR / "config" / "universe_v1.csv"
RISK_POLICY_PATH  = _BASE_DIR / "config" / "risk_policy_v0.yaml"
MODEL_DIR         = _BASE_DIR / "models" / "strategy_model"
MODEL_CONFIG_PATH = MODEL_DIR / "config.yaml"
MODEL_PKL_PATH    = MODEL_DIR / "model.pkl"                              # legacy fallback
ARTIFACT_MODEL_DIR = _BASE_DIR / "artifacts" / "strategy_model"
CANONICAL_MODEL_PATH = ARTIFACT_MODEL_DIR / "latest_model.pkl"           # canonical path
DMP_DIR           = _BASE_DIR / "artifacts" / "daily_market_packet"
TTP_DIR           = _BASE_DIR / "artifacts" / "ticker_text_pack"
SC_SCHEMA_PATH    = _BASE_DIR / "schemas" / "strategy_card.json"

OUT_MOMENTUM  = _BASE_DIR / "artifacts" / "strategy_card_variants" / "momentum"
OUT_QUANT     = _BASE_DIR / "artifacts" / "strategy_card_variants" / "quant_only"
OUT_NEWS      = _BASE_DIR / "artifacts" / "strategy_card_variants" / "news_only"
CANONICAL_DIR = _BASE_DIR / "artifacts" / "strategy_card"

for _d in (OUT_MOMENTUM, OUT_QUANT, OUT_NEWS, CANONICAL_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ── 유틸 ──────────────────────────────────────────────────────

def load_model_config() -> dict:
    """models/strategy_model/config.yaml 로드"""
    with open(MODEL_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_risk_policy() -> dict:
    """config/risk_policy_v0.yaml 로드"""
    with open(RISK_POLICY_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_universe() -> pd.DataFrame:
    """유니버스 CSV 로드. ticker 6자리 보장."""
    uni = pd.read_csv(UNIVERSE_PATH, dtype={"ticker": str})
    uni["ticker"] = uni["ticker"].apply(lambda x: str(x).zfill(6))
    return uni


def load_dmp(target_date: str) -> dict:
    """DMP 로드. PIT-Safety: 파일명 기준 target_date 이하만 허용."""
    dmp_path = DMP_DIR / f"DMP-{target_date}.json"
    if not dmp_path.exists():
        raise FileNotFoundError(f"[SCMomentum] DMP 파일 없음: {dmp_path}")
    with open(dmp_path, encoding="utf-8") as f:
        dmp = json.load(f)
    # PIT-Safety: snapshot_dt가 target_date 18:00 이하인지 확인
    snap = dmp.get("snapshot_dt", "")
    cutoff = make_snapshot_dt(target_date)
    if snap and snap > cutoff:
        raise ValueError(
            f"[SCMomentum] PIT 위반: DMP snapshot_dt={snap} > cutoff={cutoff}"
        )
    return dmp


def load_ttp_news_signal(target_date: str, ticker: str) -> tuple:
    """
    TTP에서 ticker의 news_signal을 추출한다.
    TTP 파일이 없거나 뉴스 0건이면 (None, False) 반환.

    Returns:
        (news_signal, news_available): news_available=False이면 뉴스 없음(masked).
    news_signal 계산: target_company_docs의 sentiment_score 평균 (없으면 None).
    """
    ttp_path = TTP_DIR / f"TTP-{target_date}-{ticker}.json"
    if not ttp_path.exists():
        return None, False
    try:
        with open(ttp_path, encoding="utf-8") as f:
            ttp = json.load(f)
    except Exception as e:
        print(f"[SCMomentum] TTP 로드 실패 ({ticker}): {e}")
        return None, False

    # TTP에 news_signal 필드가 있으면 직접 사용
    if "news_signal" in ttp:
        val = ttp["news_signal"]
        if val is None:
            return None, False
        return float(val), True

    # target_company_docs의 sentiment_score 평균
    scores = []
    for doc in ttp.get("target_company_docs", []):
        s = doc.get("sentiment_score")
        if s is not None:
            try:
                scores.append(float(s))
            except (TypeError, ValueError):
                pass
    if scores:
        return round(float(np.mean(scores)), 4), True

    # macro_docs에서 fallback 추출 시도
    macro_scores = []
    for doc in ttp.get("macro_docs", []):
        s = doc.get("sentiment_score")
        if s is not None:
            try:
                macro_scores.append(float(s))
            except (TypeError, ValueError):
                pass
    if macro_scores:
        return round(float(np.mean(macro_scores)) * 0.5, 4), True

    return None, False


# ── 피처 추출 ─────────────────────────────────────────────────

def build_feature_matrix(dmp: dict, tickers: list, cfg: dict, target_date_hint: str = "") -> pd.DataFrame:
    """
    모든 종목의 feature matrix를 구성한다.

    feature_factory.extract_single_ticker_features()를 통해 Train과 동일한
    피처 계산 로직을 사용한다 (Train-Serve Skew 방지).

    Serve 경로 특이사항:
    - close_history=None: sma60_ratio/return_60d/macd는 DMP tech_features에서 직접 읽음
    - date_idx=None: 전체 시계열 사용 (단일 날짜이므로 항상 None)
    - cross_sectional_pct: 당일 DMP 전체 종목 데이터로 계산
    """
    mdata = dmp.get("market_data", {})
    macro = dmp.get("macro_snapshot", {})

    # 유니버스 정보 로드
    uni = load_universe()
    universe_tickers = uni["ticker"].tolist()
    sector_map = dict(zip(uni["ticker"], uni["wics_sector"]))

    # config에서 사용 피처 목록 조립
    feat_cfg = cfg.get("features", {})
    all_feats = (
        feat_cfg.get("manual", [])
        + feat_cfg.get("mlf_lite", [])
        + feat_cfg.get("umi_lite", [])
        + feat_cfg.get("cross_sectional_pct", [])
        + feat_cfg.get("ohlcv_micro", [])
    )
    # 중복 제거 (순서 보존)
    seen: set = set()
    feature_cols: list = []
    for f in all_feats:
        if f not in seen:
            feature_cols.append(f)
            seen.add(f)

    # P0-1 GPT Pro: 이전 날짜 DMP에서 prev_close 로드 (overnight_gap serve skew 제거)
    prev_close_map: dict = {}
    try:
        dmp_files = sorted(DMP_DIR.glob("DMP-*.json"))
        prev_files = [f for f in dmp_files if f.stem.replace("DMP-", "") < target_date_hint]
        if prev_files:
            with open(prev_files[-1], encoding="utf-8") as _pf:
                _prev_dmp = json.load(_pf)
            for _t in universe_tickers:
                _pc = _prev_dmp.get("market_data", {}).get(_t, {}).get("ohlcv", {}).get("close")
                if _pc is not None:
                    prev_close_map[_t] = float(_pc)
            print(f"[SCMomentum] 전일 DMP 로드 완료: {prev_files[-1].name} ({len(prev_close_map)}종목)")
    except Exception as _e:
        print(f"[SCMomentum] 전일 DMP 로드 실패 ({_e}), overnight_gap=0.0 fallback")

    # Cross-sectional percentile rank 원시값 — 당일 DMP 전체 종목으로 계산
    cs_raw = build_cross_sectional_raw(
        mdata, universe_tickers, close_history=None, date_idx=None
    )

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

    # 유니버스 평균 return_5d
    _urs: list[float] = []
    for _t in universe_tickers:
        _r5 = mdata.get(_t, {}).get("tech_features", {}).get("return_5d")
        if _r5 is not None:
            _urs.append(float(_r5))
    universe_mean_ret5 = float(np.nanmean(_urs)) if _urs else np.nan

    rows = []
    for ticker in tickers:
        feats = extract_single_ticker_features(
            dmp_market_data=mdata,
            ticker=ticker,
            close_history=None,      # Serve 경로: 단일 DMP만 보유
            date_idx=None,
            sector_map=sector_map,
            universe_tickers=universe_tickers,
            macro=macro,
            cs_raw=cs_raw,
            sector_mean_ratio=sector_mean_ratio,
            universe_mean_ret5=universe_mean_ret5,
            prev_close_val=prev_close_map.get(ticker),  # P0-1: 전일 종가 전달
        )
        if feats is None:
            feats = {}
        rows.append({col: feats.get(col, 0.0) for col in feature_cols})

    df = pd.DataFrame(rows, index=tickers, columns=feature_cols)
    return df


# ── 모델 예측 ─────────────────────────────────────────────────

def load_or_mock_model(cfg: dict):
    """
    학습된 LightGBM 모델을 로드한다.
    탐색 순서:
      1. artifacts/strategy_model/latest_model.pkl (canonical path)
      2. models/strategy_model/model.pkl (legacy fallback)
    둘 다 없으면 None 반환 (호출부 heuristic fallback 사용).
    """
    import pickle

    # 1. canonical path
    if CANONICAL_MODEL_PATH.exists():
        try:
            with open(CANONICAL_MODEL_PATH, "rb") as f:
                model = pickle.load(f)
            print(f"[SCMomentum] 모델 로드 완료 (canonical): {CANONICAL_MODEL_PATH}")
            return model
        except Exception as e:
            print(f"[SCMomentum] canonical 모델 로드 실패: {e} — legacy 경로 시도")

    # 2. legacy fallback
    if MODEL_PKL_PATH.exists():
        try:
            with open(MODEL_PKL_PATH, "rb") as f:
                model = pickle.load(f)
            print(f"[SCMomentum] 모델 로드 완료 (legacy): {MODEL_PKL_PATH}")
            return model
        except Exception as e:
            print(f"[SCMomentum] legacy 모델 로드 실패: {e} — heuristic fallback 사용")

    # 3. heuristic fallback
    print(f"[SCMomentum] 모델 파일 없음 (탐색: {CANONICAL_MODEL_PATH}, {MODEL_PKL_PATH}) — 점수 fallback 사용")
    return None


def predict_quant_scores(model, X: pd.DataFrame) -> np.ndarray:
    """
    LightGBM 모델로 quant_score 예측.
    모델이 None이면 feature 기반 휴리스틱 점수를 반환 (개발/테스트 용도).
    """
    if model is not None:
        try:
            # lgbm_ranker.py는 {'model': LGBMClassifier, 'feature_cols': [...], 'col_means': {...}} 형태로 저장
            estimator = model["model"] if isinstance(model, dict) else model
            feature_cols = model.get("feature_cols") if isinstance(model, dict) else None
            col_means = model.get("col_means", {}) if isinstance(model, dict) else {}
            # feature_cols가 있으면 정렬 + 누락 컬럼 채움
            if feature_cols is not None:
                for c in feature_cols:
                    if c not in X.columns:
                        X[c] = col_means.get(c, 0.0)
                X = X[feature_cols]
            if hasattr(estimator, "predict_proba"):
                # Binary classifier mode
                proba = estimator.predict_proba(X)
                if proba.ndim == 2 and proba.shape[1] == 2:
                    binary_scores = proba[:, 1]
                else:
                    binary_scores = proba.ravel()

                # P0-3 GPT Pro: ordinal_model이 있으면 class2 확률로 보조 점수 혼합
                ordinal_model = model.get("ordinal_model") if isinstance(model, dict) else None
                if ordinal_model is not None and hasattr(ordinal_model, "predict_proba"):
                    try:
                        ord_proba = ordinal_model.predict_proba(X)
                        ord_top = ord_proba[:, -1]  # class2 (top 25%) 확률
                        scores = binary_scores * 0.6 + ord_top * 0.4
                        print(f"[SCMomentum] ordinal 혼합 적용: binary(0.6)+ordinal(0.4)")
                    except Exception as _oe:
                        print(f"[SCMomentum] ordinal 예측 실패 ({_oe}), binary only")
                        scores = binary_scores
                else:
                    scores = binary_scores
                scores = scores * 2.0 - 1.0
            else:
                # LambdaMART ranker mode — predict() returns relevance scores
                raw = estimator.predict(X)
                # cross-sectional rank normalize to [-1, 1]
                mn, mx = raw.min(), raw.max()
                rng = mx - mn if mx > mn else 1.0
                scores = (raw - mn) / rng * 2.0 - 1.0
            return np.clip(scores, -1.0, 1.0)
        except Exception as e:
            print(f"[SCMomentum] 모델 예측 실패: {e} — fallback 사용")

    # Fallback: momentum 기반 휴리스틱
    scores = np.zeros(len(X))
    for i, (_, row) in enumerate(X.iterrows()):
        mom = (
            row.get("return_5d", 0) * 0.4
            + row.get("return_20d", 0) * 0.3
            + row.get("return_60d", 0) * 0.2
            + (row.get("rsi_14", 50) - 50) / 100 * 0.1
        )
        scores[i] = float(np.clip(mom * 3, -1.0, 1.0))
    return scores


# ── 신호 변환 ─────────────────────────────────────────────────

def score_to_signal_direction(score: float, min_confidence: float) -> tuple:
    """
    pre_risk_score → (signal, direction, confidence).

    signal 분류 기준 (long-bias: sell/strong_sell → hold 통일, GPT Pro 권고):
      score >= 0.5  → strong_buy / long
      score >= 0.2  → buy / long
      score < 0.2   → hold / neutral
    """
    abs_score = abs(score)
    if score >= 0.5:
        signal, direction = "strong_buy", "long"
    elif score >= 0.2:
        signal, direction = "buy", "long"
    else:
        signal, direction = "hold", "neutral"

    confidence = round(min(abs_score + 0.1, 1.0), 3)
    confidence = max(confidence, min_confidence)
    return signal, direction, confidence


def synthesize_scores(quant_score: float, news_signal: float,
                      quant_weight: float = 0.7) -> float:
    """
    quant_score와 news_signal을 가중 결합하여 pre_risk_score를 반환한다.
    quant_weight: quant 비중 (기본 0.7, config.yaml synthesis.quant_weight와 동일).
    """
    news_weight = 1.0 - quant_weight
    combined = quant_score * quant_weight + news_signal * news_weight
    return round(float(np.clip(combined, -1.0, 1.0)), 4)


# ── StrategyCard 생성 ─────────────────────────────────────────

def make_strategy_card(
    target_date: str,
    ticker: str,
    name: str,
    quant_score: float,
    news_signal,
    pre_risk_score: float,
    source_strategy: str,
    feature_names: list,
    min_confidence: float,
    news_available: bool = True,
) -> dict:
    """단일 종목 StrategyCard dict 생성."""
    signal, direction, confidence = score_to_signal_direction(
        pre_risk_score, min_confidence
    )
    news_signal_val = None if news_signal is None else round(float(news_signal), 4)
    news_display = "N/A" if news_signal is None else f"{news_signal:.3f}"
    return {
        "card_id":          f"SC-{target_date}-{ticker}",
        "snapshot_dt":      make_snapshot_dt(target_date),
        "artifact_version": "v1.0",
        "ticker":           ticker,
        "name":             name,
        "direction":        direction,
        "signal":           signal,
        "confidence":       confidence,
        "pre_risk_score":   round(pre_risk_score, 4),
        "quant_score":      round(quant_score, 4),
        "news_signal":      news_signal_val,
        "news_available":   news_available,
        "rationale":        (
            f"[Momentum] {name} — quant={quant_score:.3f}, "
            f"news={news_display}, pre_risk={pre_risk_score:.3f}, "
            f"signal={signal}, confidence={confidence:.2f}"
        ),
        "source_strategy":  source_strategy,
        "evidence_ids":     [
            f"DMP-{target_date}",
            f"TTP-{target_date}-{ticker}",
        ],
        "features_used":    feature_names,
        "uncertainty_score": None,
        "conformal_interval": None,
    }


def save_branch(cards: list, out_dir: Path, target_date: str) -> Path:
    """StrategyCard 리스트를 branch 디렉토리에 저장한다."""
    out_path = out_dir / f"SC-{target_date}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cards, f, ensure_ascii=False, indent=2)
    return out_path


# ── 스키마 검증 ───────────────────────────────────────────────

def validate_schema(cards: list) -> list:
    """스키마 필수 필드 검증. jsonschema 있으면 전체 검증."""
    required_fields = [
        "card_id", "snapshot_dt", "artifact_version", "ticker",
        "direction", "signal", "confidence", "pre_risk_score",
        "rationale", "source_strategy", "evidence_ids",
    ]
    issues = []
    try:
        import jsonschema
        with open(SC_SCHEMA_PATH, encoding="utf-8") as f:
            schema = json.load(f)
        for card in cards:
            try:
                jsonschema.validate(instance=card, schema=schema)
            except jsonschema.ValidationError as e:
                issues.append(
                    f"[SCMomentum] 스키마 오류 ({card.get('ticker')}): {e.message}"
                )
    except ImportError:
        for card in cards:
            for field in required_fields:
                if field not in card:
                    issues.append(
                        f"[SCMomentum] 필수 필드 누락 ({card.get('ticker')}): {field}"
                    )
    return issues


# ── 메인 ─────────────────────────────────────────────────────

def build_momentum_strategy_cards(target_date: str) -> dict:
    """
    전체 파이프라인:
    DMP → features → LightGBM 예측 → TTP news_signal → 3-branch SC 생성.

    반환: {"momentum": [...], "quant_only": [...], "news_only": [...]}
    """
    print(f"[SCMomentum] 시작: {target_date}")

    # 설정 로드
    cfg = load_model_config()
    policy = load_risk_policy()
    min_conf = policy["position_constraints"]["min_confidence"]
    print(f"[SCMomentum] 정책 로드 완료 — min_confidence={min_conf}")

    # 유니버스
    uni = load_universe()
    ticker_name_map = {row["ticker"]: row["name"] for _, row in uni.iterrows()}
    tickers = uni["ticker"].tolist()

    # DMP 로드
    dmp = load_dmp(target_date)
    available_tickers = list(dmp.get("market_data", {}).keys())
    # 유니버스와 교집합 (6자리 정규화)
    available_tickers = [str(t).zfill(6) for t in available_tickers]
    tickers = [t for t in tickers if t in available_tickers]
    print(f"[SCMomentum] 대상 종목: {len(tickers)}개")

    # Feature matrix 구성
    feat_cfg = cfg.get("features", {})
    all_feats = (
        feat_cfg.get("manual", [])
        + feat_cfg.get("mlf_lite", [])
        + feat_cfg.get("umi_lite", [])
        + feat_cfg.get("cross_sectional_pct", [])
        + feat_cfg.get("ohlcv_micro", [])
    )
    seen_f: set = set()
    feature_names: list = []
    for f in all_feats:
        if f not in seen_f:
            feature_names.append(f)
            seen_f.add(f)

    X = build_feature_matrix(dmp, tickers, cfg, target_date_hint=target_date)
    print(f"[SCMomentum] Feature matrix: {X.shape}")

    # 모델 로드 및 예측
    model = load_or_mock_model(cfg)
    quant_scores_arr = predict_quant_scores(model, X)
    print(f"[SCMomentum] Quant scores 예측 완료")

    # Conformal Predictor 로드 (한 번만, 없으면 graceful skip)
    conformal_predictor = None
    try:
        from models.strategy_model.conformal import ConformalPredictor
        conformal_predictor = ConformalPredictor.load()
        print("[SCMomentum] Conformal predictor 로드 완료")
    except Exception as e:
        print(f"[SCMomentum] Conformal predictor 없음, interval 생성 skip: {e}")

    # TTP에서 news_signal 수집 (뉴스 없으면 None + news_available=False)
    news_signals: dict = {}
    news_available_map: dict = {}
    for ticker in tickers:
        ns, na = load_ttp_news_signal(target_date, ticker)
        news_signals[ticker] = ns
        news_available_map[ticker] = na

    # 3-branch StrategyCard 생성
    momentum_cards = []
    quant_only_cards = []
    news_only_cards = []

    # Cross-sectional rank normalization (GPT Pro #8)
    # 모델이 단일 날짜에서 절대 확률이 낮더라도, 상대 rank로 top-K 선별
    n = len(tickers)
    synth_scores = []
    for i in range(n):
        qs = float(quant_scores_arr[i])
        ns_raw = news_signals.get(tickers[i])
        # news_available=False이면 quant only (quant_weight=1.0)
        if ns_raw is None:
            synth_scores.append(synthesize_scores(qs, 0.0, quant_weight=1.0))
        else:
            synth_scores.append(synthesize_scores(qs, float(ns_raw), quant_weight=0.7))

    # rank normalize: synth_scores를 유니버스 내 순위로 [-1, 1] 재매핑
    ranked = np.argsort(np.argsort(synth_scores)).astype(float)  # 0 ~ n-1
    ranked_norm = ranked / max(n - 1, 1) * 2.0 - 1.0  # [-1, 1]

    # Conformal interval 사전 계산 (전체 quant_scores 벡터에 일괄 적용)
    conformal_intervals = None
    if conformal_predictor is not None and conformal_predictor.q_hat is not None:
        try:
            lower_arr, upper_arr, q_hat_val = conformal_predictor.predict_interval(
                np.array(quant_scores_arr, dtype=np.float64)
            )
            conformal_intervals = {
                tickers[i]: {
                    "lower": round(float(lower_arr[i]), 4),
                    "upper": round(float(upper_arr[i]), 4),
                    "q_hat": round(float(q_hat_val), 4),
                    "alpha": conformal_predictor.alpha,
                }
                for i in range(n)
            }
        except Exception as e:
            print(f"[SCMomentum] Conformal interval 계산 실패 (skip): {e}")

    # quant rank normalization 사전 계산 (루프 밖으로)
    q_ranked = np.argsort(np.argsort(quant_scores_arr)).astype(float)
    q_norm = q_ranked / max(n - 1, 1) * 2.0 - 1.0

    for i, ticker in enumerate(tickers):
        name = ticker_name_map.get(ticker, ticker)
        qs   = float(quant_scores_arr[i])
        ns   = news_signals.get(ticker)
        na   = news_available_map.get(ticker, False)

        # Branch 1: synthesized — rank-normalized score 사용
        pre_synth = round(float(ranked_norm[i]), 4)
        card_synth = make_strategy_card(
            target_date, ticker, name, qs, ns,
            pre_synth, "synthesized", feature_names, min_conf,
            news_available=na,
        )
        if conformal_intervals and ticker in conformal_intervals:
            ci = conformal_intervals[ticker]
            card_synth["conformal_interval"] = ci
            interval_width = ci["upper"] - ci["lower"]
            card_synth["uncertainty_score"] = round(min(max(interval_width, 0.0), 1.0), 4)
        momentum_cards.append(card_synth)

        # Branch 2: quant only — quant rank-normalized (news_available 무관하게 quant만)
        card_quant = make_strategy_card(
            target_date, ticker, name, qs, ns,
            round(float(q_norm[i]), 4), "quant", feature_names, min_conf,
            news_available=na,
        )
        if conformal_intervals and ticker in conformal_intervals:
            ci = conformal_intervals[ticker]
            card_quant["conformal_interval"] = ci
            interval_width = ci["upper"] - ci["lower"]
            card_quant["uncertainty_score"] = round(min(max(interval_width, 0.0), 1.0), 4)
        quant_only_cards.append(card_quant)

        # Branch 3: news only (뉴스 없으면 pre_risk_score=0.0)
        news_pre_score = round(float(ns), 4) if ns is not None else 0.0
        news_only_cards.append(make_strategy_card(
            target_date, ticker, name, qs, ns,
            news_pre_score, "news", feature_names, min_conf,
            news_available=na,
        ))

    # Top-K shortlist: risk_policy max_position_count까지만 BUY 허용
    max_positions = policy["position_constraints"]["max_position_count"]
    print(f"[SCMomentum] Top-K shortlist 적용: 상위 {max_positions}종목만 BUY 허용")

    # synth_scores 내림차순 정렬 → 상위 K개 인덱스
    synth_arr = np.array(synth_scores)
    ranked_indices = np.argsort(synth_arr)[::-1]
    top_k_set = set(ranked_indices[:max_positions])

    for i, card in enumerate(momentum_cards):
        if i not in top_k_set and card["signal"] in ("buy", "strong_buy"):
            card["signal"] = "hold"
            card["direction"] = "neutral"

    # quant_only_cards에도 동일 로직 적용 (quant_scores_arr 기준)
    quant_ranked_indices = np.argsort(quant_scores_arr)[::-1]
    quant_top_k_set = set(quant_ranked_indices[:max_positions])

    for i, card in enumerate(quant_only_cards):
        if i not in quant_top_k_set and card["signal"] in ("buy", "strong_buy"):
            card["signal"] = "hold"
            card["direction"] = "neutral"

    return {
        "momentum":   momentum_cards,
        "quant_only": quant_only_cards,
        "news_only":  news_only_cards,
    }


def main():
    parser = argparse.ArgumentParser(
        description="LightGBM Momentum StrategyCard Builder (AI #2)"
    )
    parser.add_argument(
        "date",
        help="대상 날짜 YYYYMMDD (t일 장마감 스냅샷 기준)"
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="생성 후 artifacts/strategy_card/ 에 바로 publish"
    )
    args = parser.parse_args()
    target_date = args.date

    # 날짜 형식 검증
    try:
        datetime.strptime(target_date, "%Y%m%d")
    except ValueError:
        print(f"[SCMomentum] 날짜 형식 오류: {target_date} (YYYYMMDD 필요)")
        sys.exit(1)

    try:
        branches = build_momentum_strategy_cards(target_date)
    except FileNotFoundError as e:
        print(f"[SCMomentum] 파일 없음: {e}")
        print(f"  먼저 python jobs/build_daily_market_packet.py {target_date} 를 실행하세요.")
        sys.exit(1)
    except Exception as e:
        print(f"[SCMomentum] 빌드 실패: {e}")
        sys.exit(1)

    # 스키마 검증
    print(f"[SCMomentum] 스키마 검증 중...")
    all_issues = validate_schema(branches["momentum"])
    if all_issues:
        for iss in all_issues:
            print(f"[SCMomentum] 경고: {iss}")
        print(f"[SCMomentum] 스키마 검증 실패 ({len(all_issues)}개 이슈). 저장 중단.")
        sys.exit(1)
    print(f"[SCMomentum] 스키마 검증 통과")

    # 3-branch 저장
    branch_dirs = {
        "momentum":   OUT_MOMENTUM,
        "quant_only": OUT_QUANT,
        "news_only":  OUT_NEWS,
    }
    saved_paths = []
    for branch_name, cards in branches.items():
        out_path = save_branch(cards, branch_dirs[branch_name], target_date)
        saved_paths.append(out_path)
        buy_count = sum(
            1 for c in cards if c["signal"] in ("buy", "strong_buy")
        )
        print(
            f"[SCMomentum] [{branch_name}] 저장: {out_path} "
            f"({len(cards)}개 카드, BUY {buy_count}개)"
        )

    # Publish (선택)
    if args.publish:
        canonical_path = CANONICAL_DIR / f"SC-{target_date}.json"
        shutil.copy2(branch_dirs["momentum"] / f"SC-{target_date}.json", canonical_path)
        print(f"[SCMomentum] Publish 완료: {canonical_path}")
        print(f"[SCMomentum] strategy_loader.has_real_sc('{target_date}') = True 상태")

    print(f"\n[SCMomentum] 완료: {target_date}")
    print(f"  카드 수: {len(branches['momentum'])}개")
    print(f"  BUY 신호: {sum(1 for c in branches['momentum'] if c['signal'] in ('buy','strong_buy'))}개")
    print(f"  HOLD 신호: {sum(1 for c in branches['momentum'] if c['signal'] == 'hold')}개")
    print(f"  SELL 신호: {sum(1 for c in branches['momentum'] if c['signal'] in ('sell','strong_sell'))}개")


if __name__ == "__main__":
    main()

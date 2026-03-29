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

# ── 경로 설정 ──────────────────────────────────────────────────
UNIVERSE_PATH     = _BASE_DIR / "config" / "universe_v1.csv"
RISK_POLICY_PATH  = _BASE_DIR / "config" / "risk_policy_v0.yaml"
MODEL_DIR         = _BASE_DIR / "models" / "strategy_model"
MODEL_CONFIG_PATH = MODEL_DIR / "config.yaml"
MODEL_PKL_PATH    = MODEL_DIR / "model.pkl"
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


def load_ttp_news_signal(target_date: str, ticker: str) -> float:
    """
    TTP에서 ticker의 news_signal을 추출한다.
    TTP 파일이 없거나 필드가 없으면 0.0 반환.

    news_signal 계산: ticker_docs의 sentiment_score 평균 (없으면 0.0).
    """
    ttp_path = TTP_DIR / f"TTP-{target_date}-{ticker}.json"
    if not ttp_path.exists():
        return 0.0
    try:
        with open(ttp_path, encoding="utf-8") as f:
            ttp = json.load(f)
    except Exception as e:
        print(f"[SCMomentum] TTP 로드 실패 ({ticker}): {e}")
        return 0.0

    # TTP에 news_signal 필드가 있으면 직접 사용
    if "news_signal" in ttp:
        return float(ttp["news_signal"])

    # ticker_docs의 sentiment_score 평균
    scores = []
    for doc in ttp.get("ticker_docs", []):
        s = doc.get("sentiment_score")
        if s is not None:
            try:
                scores.append(float(s))
            except (TypeError, ValueError):
                pass
    if scores:
        return round(float(np.mean(scores)), 4)

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
        return round(float(np.mean(macro_scores)) * 0.5, 4)  # 매크로 신호는 절반 가중

    return 0.0


# ── 피처 추출 ─────────────────────────────────────────────────

def build_feature_row(dmp: dict, ticker: str) -> dict:
    """
    DMP에서 단일 종목의 feature row를 추출한다.
    models/strategy_model/config.yaml features.manual + mlf_lite + umi_lite 기반.
    """
    market_data = dmp.get("market_data", {})
    td = market_data.get(ticker, {})
    ohlcv = td.get("ohlcv", {})
    tech = td.get("tech_features", {})

    close = float(ohlcv.get("close") or 0)
    sma_5  = float(tech.get("sma_5", close) or close or 1)
    sma_20 = float(tech.get("sma_20", close) or close or 1)
    sma_60 = float(tech.get("sma_60", close) or close or 1)

    # return_Xd 직접 추출 (없으면 sma 비율로 대체)
    return_5d  = float(tech.get("return_5d")  or (close / sma_5  - 1 if sma_5  > 0 else 0))
    return_20d = float(tech.get("return_20d") or (close / sma_20 - 1 if sma_20 > 0 else 0))
    return_60d = float(tech.get("return_60d") or (close / sma_60 - 1 if sma_60 > 0 else 0))

    sma5_ratio  = (close / sma_5  - 1.0) if sma_5  > 0 else 0.0
    sma20_ratio = (close / sma_20 - 1.0) if sma_20 > 0 else 0.0
    sma60_ratio = (close / sma_60 - 1.0) if sma_60 > 0 else 0.0

    return {
        # manual features
        "return_5d":        return_5d,
        "return_20d":       return_20d,
        "rsi_14":           float(tech.get("rsi_14", 50.0)),
        "volume_ratio_20":  float(tech.get("volume_ratio_20", 1.0)),
        "macd":             float(tech.get("macd", 0.0)),
        "macd_signal":      float(tech.get("macd_signal", 0.0)),
        "atr_14":           float(tech.get("atr_14", 0.0)),
        "sma5_ratio":       sma5_ratio,
        "sma20_ratio":      sma20_ratio,
        "sma60_ratio":      sma60_ratio,
        # mlf_lite
        "return_60d":                float(return_60d),
        "period_agreement_score":    float(tech.get("period_agreement_score", 0.0)),
        # umi_lite
        "stock_sync_score":          float(tech.get("stock_sync_score", 0.0)),
        "market_synchronism":        float(tech.get("market_synchronism", 0.0)),
        "rational_price_gap":        float(tech.get("rational_price_gap", 0.0)),
    }


def build_feature_matrix(dmp: dict, tickers: list, cfg: dict) -> pd.DataFrame:
    """모든 종목의 feature matrix를 구성한다."""
    # config에서 사용 피처 목록 조립
    feat_cfg = cfg.get("features", {})
    all_feats = (
        feat_cfg.get("manual", [])
        + feat_cfg.get("mlf_lite", [])
        + feat_cfg.get("umi_lite", [])
    )
    # 중복 제거 (순서 보존)
    seen = set()
    feature_cols = []
    for f in all_feats:
        if f not in seen:
            feature_cols.append(f)
            seen.add(f)

    rows = []
    for ticker in tickers:
        row = build_feature_row(dmp, ticker)
        rows.append({col: row.get(col, 0.0) for col in feature_cols})

    df = pd.DataFrame(rows, index=tickers, columns=feature_cols)
    return df


# ── 모델 예측 ─────────────────────────────────────────────────

def load_or_mock_model(cfg: dict):
    """
    학습된 LightGBM 모델을 로드한다.
    모델 파일이 없으면 None 반환 (호출부에서 fallback 처리).
    """
    if not MODEL_PKL_PATH.exists():
        print(f"[SCMomentum] 모델 파일 없음: {MODEL_PKL_PATH} — 점수 fallback 사용")
        return None
    try:
        import pickle
        with open(MODEL_PKL_PATH, "rb") as f:
            model = pickle.load(f)
        print(f"[SCMomentum] 모델 로드 완료: {MODEL_PKL_PATH}")
        return model
    except Exception as e:
        print(f"[SCMomentum] 모델 로드 실패: {e} — fallback 사용")
        return None


def predict_quant_scores(model, X: pd.DataFrame) -> np.ndarray:
    """
    LightGBM 모델로 quant_score 예측.
    모델이 None이면 feature 기반 휴리스틱 점수를 반환 (개발/테스트 용도).
    """
    if model is not None:
        try:
            proba = model.predict_proba(X)
            # binary classifier: positive class probability
            if proba.ndim == 2 and proba.shape[1] == 2:
                scores = proba[:, 1]
            else:
                scores = proba.ravel()
            # [0, 1] → [-1, 1] 정규화
            scores = scores * 2.0 - 1.0
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

    signal 분류 기준:
      score >= 0.5  → strong_buy / long
      score >= 0.2  → buy / long
      score >= -0.2 → hold / neutral
      score >= -0.5 → sell / short
      score < -0.5  → strong_sell / short
    """
    abs_score = abs(score)
    if score >= 0.5:
        signal, direction = "strong_buy", "long"
    elif score >= 0.2:
        signal, direction = "buy", "long"
    elif score >= -0.2:
        signal, direction = "hold", "neutral"
    elif score >= -0.5:
        signal, direction = "sell", "short"
    else:
        signal, direction = "strong_sell", "short"

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
    news_signal: float,
    pre_risk_score: float,
    source_strategy: str,
    feature_names: list,
    min_confidence: float,
) -> dict:
    """단일 종목 StrategyCard dict 생성."""
    signal, direction, confidence = score_to_signal_direction(
        pre_risk_score, min_confidence
    )
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
        "news_signal":      round(news_signal, 4),
        "rationale":        (
            f"[Momentum] {name} — quant={quant_score:.3f}, "
            f"news={news_signal:.3f}, pre_risk={pre_risk_score:.3f}, "
            f"signal={signal}, confidence={confidence:.2f}"
        ),
        "source_strategy":  source_strategy,
        "evidence_ids":     [
            f"DMP-{target_date}",
            f"TTP-{target_date}-{ticker}",
        ],
        "features_used":    feature_names,
        "uncertainty_score": None,
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
    )
    seen_f: set = set()
    feature_names: list = []
    for f in all_feats:
        if f not in seen_f:
            feature_names.append(f)
            seen_f.add(f)

    X = build_feature_matrix(dmp, tickers, cfg)
    print(f"[SCMomentum] Feature matrix: {X.shape}")

    # 모델 로드 및 예측
    model = load_or_mock_model(cfg)
    quant_scores_arr = predict_quant_scores(model, X)
    print(f"[SCMomentum] Quant scores 예측 완료")

    # TTP에서 news_signal 수집
    news_signals: dict = {}
    for ticker in tickers:
        news_signals[ticker] = load_ttp_news_signal(target_date, ticker)

    # 3-branch StrategyCard 생성
    momentum_cards = []
    quant_only_cards = []
    news_only_cards = []

    for i, ticker in enumerate(tickers):
        name = ticker_name_map.get(ticker, ticker)
        qs   = float(quant_scores_arr[i])
        ns   = news_signals.get(ticker, 0.0)

        # Branch 1: synthesized (quant 0.7 + news 0.3)
        pre_synth = synthesize_scores(qs, ns, quant_weight=0.7)
        momentum_cards.append(make_strategy_card(
            target_date, ticker, name, qs, ns,
            pre_synth, "synthesized", feature_names, min_conf
        ))

        # Branch 2: quant only
        quant_only_cards.append(make_strategy_card(
            target_date, ticker, name, qs, ns,
            round(qs, 4), "quant", feature_names, min_conf
        ))

        # Branch 3: news only
        news_only_cards.append(make_strategy_card(
            target_date, ticker, name, qs, ns,
            round(ns, 4), "news", feature_names, min_conf
        ))

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

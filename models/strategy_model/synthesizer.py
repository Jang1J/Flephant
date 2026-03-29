"""
GeneralSynthesizer — AI #2 전략 카드 합성 모듈

quant_scores(LightGBM)와 news_signals(NewsStrategy)를 결합해
StrategyCard 스키마 호환 dict 리스트를 생성한다.

결합 공식:
  combined = quant_weight * quant_score
           + news_weight * (news_signal + 1) / 2

신호 매핑:
  combined >= 0.70 → strong_buy  / long
  combined >= 0.55 → buy         / long
  combined >= 0.30 → hold        / long
  combined <  0.30 → sell        / neutral

PIT-Safety: 입력 데이터의 PIT-Safety는 상위 모듈(NewsStrategy, QuantStrategy)이
            보장한다고 가정하며, synthesizer 자체는 snapshot_dt를 기록만 한다.
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

KST = timezone(timedelta(hours=9))

# ---------------------------------------------------------------------------
# 신호 임계값 — models/strategy_model/config.yaml에서 로드
# ---------------------------------------------------------------------------
import yaml as _yaml

def _load_signal_thresholds() -> dict:
    _cfg_path = Path(__file__).resolve().parent / "config.yaml"
    try:
        with open(_cfg_path, encoding="utf-8") as _f:
            _cfg = _yaml.safe_load(_f)
        _sig = _cfg.get("signal_thresholds", {})
        return {
            "strong_buy": _sig.get("strong_buy", 0.70),
            "buy": _sig.get("buy", 0.55),
            "hold": _sig.get("hold", 0.30),
        }
    except Exception:
        return {"strong_buy": 0.70, "buy": 0.55, "hold": 0.30}

_THRESHOLDS = _load_signal_thresholds()
THRESHOLD_STRONG_BUY = _THRESHOLDS["strong_buy"]
THRESHOLD_BUY = _THRESHOLDS["buy"]
THRESHOLD_HOLD = _THRESHOLDS["hold"]


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _classify_signal(combined: float) -> tuple[str, str]:
    """
    combined 점수에서 (signal, direction) 튜플을 반환한다.

    Returns
    -------
    tuple[str, str]
        signal   : "strong_buy" | "buy" | "hold" | "sell"
        direction: "long" | "neutral"
    """
    if combined >= THRESHOLD_STRONG_BUY:
        return "strong_buy", "long"
    if combined >= THRESHOLD_BUY:
        return "buy", "long"
    if combined >= THRESHOLD_HOLD:
        return "hold", "long"
    return "sell", "neutral"


def _determine_source(has_quant: bool, has_news: bool) -> str:
    """
    quant/news 가용 여부에 따라 source_strategy 값을 결정한다.
    스키마 enum: "quant" | "news" | "synthesized"
    """
    if has_quant and has_news:
        return "synthesized"
    if has_quant:
        return "quant"
    if has_news:
        return "news"
    return "quant"  # fallback: 양쪽 모두 없으면 quant 기본값


def _build_rationale(
    ticker: str,
    quant_score: Optional[float],
    news_signal: Optional[float],
    combined: float,
    signal: str,
    quant_weight: float,
    news_weight: float,
) -> str:
    """종목별 투자 근거 요약 문자열을 생성한다."""
    parts: list[str] = []

    if quant_score is not None:
        parts.append(f"퀀트 점수 {quant_score:.3f}")
    if news_signal is not None:
        sentiment_label = (
            "긍정적" if news_signal > 0.1
            else "부정적" if news_signal < -0.1
            else "중립적"
        )
        parts.append(f"뉴스 신호 {news_signal:+.3f}({sentiment_label})")

    signal_map = {
        "strong_buy": "강력 매수",
        "buy": "매수",
        "hold": "보유",
        "sell": "매도",
    }
    signal_kr = signal_map.get(signal, signal)
    weight_desc = f"(퀀트 {quant_weight:.0%} / 뉴스 {news_weight:.0%})"

    base = " · ".join(parts) if parts else "입력 신호 없음"
    return (
        f"[{ticker}] {base} → 결합 점수 {combined:.3f} {weight_desc} → {signal_kr}"
    )


# ---------------------------------------------------------------------------
# 공개 인터페이스
# ---------------------------------------------------------------------------

def synthesize(
    target_date: str,
    quant_scores: dict,
    news_signals: dict,
    quant_weight: float = 0.7,
    news_weight: float = 0.3,
) -> list:
    """
    quant_scores와 news_signals를 결합해 StrategyCard 호환 dict 리스트를 반환한다.

    Parameters
    ----------
    target_date : str
        기준 날짜 (YYYYMMDD)
    quant_scores : dict
        {ticker: float}  — LightGBM ranker 출력, 범위 0~1
    news_signals : dict
        {ticker: float}  — NewsStrategy 출력, 범위 -1~+1
    quant_weight : float
        퀀트 가중치 (default 0.7)
    news_weight : float
        뉴스 가중치 (default 0.3)

    Returns
    -------
    list[dict]
        StrategyCard 스키마 호환 dict 리스트.
        필드: card_id, snapshot_dt, artifact_version, ticker,
              direction, signal, confidence, pre_risk_score,
              quant_score, news_signal, rationale, source_strategy,
              evidence_ids, features_used, uncertainty_score

    Notes
    -----
    - combined = quant_weight * quant_score + news_weight * (news_signal + 1) / 2
    - 가중치 합이 1이 아닐 경우 정규화(normalize)하지 않는다.
      호출자가 quant_weight + news_weight == 1.0을 보장해야 한다.
    - uncertainty_score: Phase 1에서는 null.
    """
    if abs(quant_weight + news_weight - 1.0) > 1e-6:
        print(
            f"[Synthesizer] 경고: quant_weight({quant_weight}) + "
            f"news_weight({news_weight}) != 1.0 — 결합 점수가 0~1 범위를 벗어날 수 있습니다."
        )

    # 두 입력에서 전체 종목 집합 추출
    all_tickers = sorted(set(list(quant_scores.keys()) + list(news_signals.keys())))
    snapshot_dt = (
        datetime.strptime(target_date, "%Y%m%d")
        .replace(hour=18, minute=0, second=0, tzinfo=KST)
        .isoformat()
    )

    print(
        f"[Synthesizer] {target_date} 합성 시작 "
        f"(종목 수: {len(all_tickers)}, "
        f"quant_weight={quant_weight}, news_weight={news_weight})"
    )

    cards: list[dict] = []

    for raw_ticker in all_tickers:
        ticker = str(raw_ticker).zfill(6)

        # 입력값 추출 — 없는 경우 None 으로 명시
        raw_quant = quant_scores.get(ticker) or quant_scores.get(raw_ticker)
        raw_news = news_signals.get(ticker) or news_signals.get(raw_ticker)

        has_quant = raw_quant is not None
        has_news = raw_news is not None

        # 없는 입력은 중립값으로 대체 후 가중치를 재조정
        if has_quant and has_news:
            quant_score: float = float(raw_quant)
            news_score: float = float(raw_news)
            effective_quant_w = quant_weight
            effective_news_w = news_weight
        elif has_quant:
            quant_score = float(raw_quant)
            news_score = 0.0
            effective_quant_w = 1.0
            effective_news_w = 0.0
        elif has_news:
            quant_score = 0.5  # 중립 기본값
            news_score = float(raw_news)
            effective_quant_w = 0.0
            effective_news_w = 1.0
        else:
            # 양쪽 모두 없음 — 중립 카드 생성
            quant_score = 0.5
            news_score = 0.0
            effective_quant_w = 1.0
            effective_news_w = 0.0
            print(f"[Synthesizer]  {ticker}: 퀀트·뉴스 모두 없음 → 중립 처리")

        # 결합 점수 계산
        news_normalized = (news_score + 1.0) / 2.0   # -1~+1 → 0~1
        combined = (
            effective_quant_w * quant_score
            + effective_news_w * news_normalized
        )
        # 부동소수점 오차 보정
        combined = max(0.0, min(1.0, combined))

        signal, direction = _classify_signal(combined)
        confidence = combined
        pre_risk_score = 2.0 * confidence - 1.0       # 0~1 → -1~+1
        source_strategy = _determine_source(has_quant, has_news)

        rationale = _build_rationale(
            ticker=ticker,
            quant_score=raw_quant,
            news_signal=raw_news,
            combined=combined,
            signal=signal,
            quant_weight=effective_quant_w,
            news_weight=effective_news_w,
        )

        card = {
            "card_id": f"SC-{target_date}-{ticker}",
            "snapshot_dt": snapshot_dt,
            "artifact_version": "v1.0",
            "ticker": ticker,
            "direction": direction,
            "signal": signal,
            "confidence": round(confidence, 4),
            "pre_risk_score": round(pre_risk_score, 4),
            "quant_score": round(quant_score, 4) if has_quant else None,
            "news_signal": round(news_score, 4) if has_news else None,
            "rationale": rationale,
            "source_strategy": source_strategy,
            "evidence_ids": [],      # 상위 파이프라인에서 주입
            "features_used": [],     # 상위 파이프라인에서 주입
            "uncertainty_score": None,  # Phase 2에서 활성화
        }
        cards.append(card)

        print(
            f"[Synthesizer]  {ticker}: combined={combined:.4f} "
            f"signal={signal} direction={direction} source={source_strategy}"
        )

    print(f"[Synthesizer] {target_date} 합성 완료 — {len(cards)}개 카드 생성")
    return cards


# ---------------------------------------------------------------------------
# 단독 실행 (smoke test)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys as _sys

    _date = _sys.argv[1] if len(_sys.argv) > 1 else "20260320"

    _quant = {
        "005930": 0.82,
        "000660": 0.45,
        "009150": 0.60,
        "005380": 0.30,
        "000270": 0.75,
    }
    _news = {
        "005930": 0.50,
        "000660": -0.30,
        "009150": 0.10,
        "005380": 0.00,
        # 000270: 뉴스 없음
    }

    result = synthesize(_date, _quant, _news)
    print("\n--- 결과 요약 ---")
    for card in result:
        print(
            f"  {card['ticker']}: {card['signal']:<10} "
            f"conf={card['confidence']:.4f} "
            f"pre_risk={card['pre_risk_score']:+.4f} "
            f"src={card['source_strategy']}"
        )
    print("\n--- 첫 번째 카드 전체 ---")
    print(json.dumps(result[0], ensure_ascii=False, indent=2))

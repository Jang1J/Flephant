"""
StrategyCard 로더 모듈
- artifacts/strategy_card/ 에서 SC 파일을 로드하거나 Mock SC를 생성한다.
- run_risk_engine.py, run_e2e_pipeline.py, run_intraday_cycle.py에서 공통 사용.

Usage:
    from jobs.strategy_loader import load_strategy_cards, generate_mock_strategy_cards, has_real_sc, validate_sc_pit
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors import make_snapshot_dt

_BASE_DIR = Path(__file__).resolve().parent.parent
SC_DIR = _BASE_DIR / "artifacts" / "strategy_card"
UNIVERSE_PATH = _BASE_DIR / "config" / "universe_v1.csv"


def has_real_sc(target_date: str) -> bool:
    """artifacts/strategy_card/ 에 해당 날짜 SC 파일이 존재하는지 확인"""
    if not SC_DIR.exists():
        return False
    if (SC_DIR / f"SC-{target_date}.json").exists():
        return True
    return any(SC_DIR.glob(f"SC-{target_date}-*.json"))


def load_strategy_cards(target_date: str) -> list:
    """StrategyCard 로드 (artifacts/strategy_card/ 에서)"""
    cards = []
    sc_path = SC_DIR / f"SC-{target_date}.json"
    if sc_path.exists():
        with open(sc_path) as f:
            data = json.load(f)
            if isinstance(data, list):
                cards = data
            else:
                cards = [data]
    else:
        # 개별 파일 탐색
        for p in sorted(SC_DIR.glob(f"SC-{target_date}-*.json")):
            with open(p) as f:
                cards.append(json.load(f))
    return cards


def validate_sc_pit(cards: list, target_date: str, dmp: dict = None) -> list:
    """
    StrategyCard PIT semantic validation:
    1. snapshot_dt <= target_date 18:00 KST
    2. ticker가 universe에 존재
    3. ticker 중복 없음
    4. evidence_ids가 있으면 DMP에 존재하는지 (선택적)

    Returns: list of validation issues (빈 리스트면 PASS)
    """
    import pandas as pd

    issues = []

    # 유니버스 로드
    try:
        uni_df = pd.read_csv(UNIVERSE_PATH)
        valid_tickers = {str(r["ticker"]).zfill(6) for _, r in uni_df.iterrows()}
    except Exception as e:
        issues.append(f"[validate_sc_pit] 유니버스 로드 실패: {e}")
        valid_tickers = set()

    # DMP evidence_ids 인덱스
    dmp_evidence_ids: set = set()
    if dmp:
        for item in dmp.get("news_index", []):
            eid = item.get("evidence_id")
            if eid:
                dmp_evidence_ids.add(eid)
        for item in dmp.get("disclosure_index", []):
            eid = item.get("evidence_id")
            if eid:
                dmp_evidence_ids.add(eid)

    # snapshot 기준: target_date 18:00 KST
    cutoff_dt = make_snapshot_dt(target_date)

    seen_tickers: set = set()
    for i, card in enumerate(cards):
        ticker = str(card.get("ticker", "")).zfill(6)
        card_id = card.get("card_id", f"card[{i}]")

        # 1. snapshot_dt PIT 검증
        snap = card.get("snapshot_dt", "")
        if snap and snap > cutoff_dt:
            issues.append(
                f"[PIT] {card_id}: snapshot_dt={snap} > cutoff={cutoff_dt} (미래 데이터)"
            )

        # 2. ticker 유니버스 확인
        if valid_tickers and ticker not in valid_tickers:
            issues.append(
                f"[UNIVERSE] {card_id}: ticker={ticker} 유니버스에 없음"
            )

        # 3. ticker 중복 확인
        if ticker in seen_tickers:
            issues.append(f"[DUPLICATE] ticker={ticker} 중복")
        seen_tickers.add(ticker)

        # 4. evidence_ids DMP 존재 확인 (선택적 — MOCK evidence는 건너뜀)
        if dmp_evidence_ids:
            for eid in card.get("evidence_ids", []):
                if eid and not eid.startswith("MOCK-") and eid not in dmp_evidence_ids:
                    issues.append(
                        f"[EVIDENCE] {card_id}: evidence_id={eid} DMP에 없음"
                    )

    return issues


def generate_mock_strategy_cards(target_date: str, dmp: dict) -> list:
    """Mock StrategyCard 생성 (AI #2 연결 전 테스트용)"""
    import random
    random.seed(42)

    import pandas as pd
    uni = pd.read_csv(UNIVERSE_PATH)
    ticker_name_map = {str(r["ticker"]).zfill(6): r["name"] for _, r in uni.iterrows()}

    cards = []
    tickers = dmp.get("tickers", [])[:10]  # 상위 10종목만

    signals = ["strong_buy", "buy", "hold"]
    signal_weights = [0.2, 0.5, 0.3]

    for ticker in tickers:
        signal = random.choices(signals, weights=signal_weights, k=1)[0]
        confidence = round(random.uniform(0.2, 0.95), 2)

        # signal에 따른 direction (Long-bias: sell/strong_sell 미생성)
        if signal in ["strong_buy", "buy"]:
            direction = "long"
        else:
            direction = "neutral"

        cards.append({
            "card_id": f"SC-{target_date}-{ticker}",
            "snapshot_dt": make_snapshot_dt(target_date),
            "artifact_version": "v1.0",
            "ticker": ticker,
            "direction": direction,
            "signal": signal,
            "confidence": confidence,
            "pre_risk_score": round(random.uniform(-1, 1), 3),
            "quant_score": round(random.uniform(-1, 1), 3),
            "news_signal": round(random.uniform(-1, 1), 3),
            "name": ticker_name_map.get(ticker, ticker),
            "rationale": f"[MOCK] {ticker_name_map.get(ticker, ticker)} 자동 생성 전략 카드",
            "source_strategy": "synthesized",
            "evidence_ids": [f"MOCK-{ticker}"],
            "features_used": ["sma_5", "rsi_14", "volume_ratio_20"],
            "uncertainty_score": None,
        })

    return cards

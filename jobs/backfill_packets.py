"""
Backfill 파이프라인
- 과거 N거래일에 대해 DailyMarketPacket을 일괄 생성
- 뉴스/공시는 historical 재현 불가하므로 OHLCV + tech_features + macro만 채움
- backtest_eligible=true (OHLCV/macro는 PIT-safe)

Usage:
    python jobs/backfill_packets.py --days 20
    python jobs/backfill_packets.py --start 20260101 --end 20260320
"""

import sys
import json
import argparse
import time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

from connectors import now_kst, now_kst_iso, make_snapshot_dt
from connectors.krx import get_ohlcv, get_ticker_name
from connectors.ecos import get_base_rate, get_treasury_yield, get_fx_rate

UNIVERSE_PATH = _BASE_DIR / "config" / "universe_v1.csv"
OUTPUT_DIR = _BASE_DIR / "artifacts" / "daily_market_packet"


def load_universe() -> pd.DataFrame:
    return pd.read_csv(UNIVERSE_PATH)


def get_trading_days(start: str, end: str) -> list:
    """KRX 거래일 목록 조회 (삼성전자 기준)"""
    from pykrx import stock
    df = stock.get_market_ohlcv(start, end, "005930")
    if df.empty:
        return []
    dates = [d.strftime("%Y%m%d") for d in df.index]
    return dates


def compute_tech_features(df: pd.DataFrame) -> dict:
    """OHLCV에서 기술적 지표 계산"""
    if df.empty or len(df) < 5:
        return {}

    close = df["종가"]
    high = df["고가"]
    low = df["저가"]
    volume = df["거래량"]

    features = {}

    # SMA
    for w in [5, 20, 60]:
        if len(close) >= w:
            features[f"sma_{w}"] = round(float(close.rolling(w).mean().iloc[-1]), 2)

    # RSI 14
    if len(close) >= 15:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        features["rsi_14"] = round(float(rsi.iloc[-1]), 2) if not pd.isna(rsi.iloc[-1]) else None

    # Volume ratio
    if len(volume) >= 20:
        avg_vol = volume.rolling(20).mean().iloc[-1]
        features["volume_ratio_20"] = round(float(volume.iloc[-1] / avg_vol), 4) if avg_vol > 0 else None

    # ATR 14
    if len(close) >= 15:
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        features["atr_14"] = round(float(atr.iloc[-1]), 2) if not pd.isna(atr.iloc[-1]) else None

    # Return 5d / 20d
    for w in [5, 20]:
        if len(close) > w:
            ret = (close.iloc[-1] / close.iloc[-w-1] - 1) * 100
            features[f"return_{w}d"] = round(float(ret), 4)

    return features


def collect_macro(target_date: str) -> dict:
    """ECOS 매크로 수집 (최근 30일 범위)"""
    macro = {}
    start = (datetime.strptime(target_date, "%Y%m%d") - timedelta(days=60)).strftime("%Y%m%d")

    try:
        df = get_base_rate(start, target_date)
        if not df.empty:
            macro["base_rate"] = float(df["value"].iloc[-1])
    except Exception:
        pass

    try:
        df = get_treasury_yield("3Y", start, target_date)
        if not df.empty:
            macro["treasury_3y"] = float(df["value"].iloc[-1])
    except Exception:
        pass

    try:
        df = get_fx_rate(start, target_date)
        if not df.empty:
            macro["usd_krw"] = float(df["value"].iloc[-1])
    except Exception:
        pass

    macro["vix_proxy"] = None
    macro["market_breadth"] = None

    return macro


def build_backfill_packet(target_date: str, universe_df: pd.DataFrame, lookback: int = 70) -> dict:
    """단일 날짜의 backfill DMP 생성 (OHLCV + tech + macro only, 뉴스/공시 없음)"""
    from pykrx import stock

    snapshot_dt = make_snapshot_dt(target_date)
    now_str = now_kst_iso()

    tickers = [str(t).zfill(6) for t in universe_df["ticker"]]
    start_date = (datetime.strptime(target_date, "%Y%m%d") - timedelta(days=lookback)).strftime("%Y%m%d")

    market_data = {}
    missing = []

    for ticker in tickers:
        try:
            df = stock.get_market_ohlcv(start_date, target_date, ticker)
            if df.empty or len(df) < 3:
                missing.append(ticker)
                continue

            last = df.iloc[-1]
            market_data[ticker] = {
                "ohlcv": {
                    "open": int(last["시가"]),
                    "high": int(last["고가"]),
                    "low": int(last["저가"]),
                    "close": int(last["종가"]),
                },
                "volume": int(last["거래량"]),
                "mktcap": None,
                "tech_features": compute_tech_features(df),
            }
        except Exception as e:
            print(f"[backfill] {ticker} 수집 실패: {e}")
            missing.append(ticker)

    # macro
    macro = collect_macro(target_date)

    packet = {
        "snapshot_id": f"DMP-{target_date}-180000",
        "snapshot_dt": snapshot_dt,
        "available_at": now_str,
        "artifact_version": "v1.0",
        "backfill": True,
        "tickers": tickers,
        "market_data": market_data,
        "macro_snapshot": macro,
        "news_index": [],
        "disclosure_index": [],
        "meta": {
            "as_of_dt": snapshot_dt,
            "universe_version": "v1",
            "backfill_note": "historical backfill — OHLCV/tech/macro only, news/disclosure excluded for PIT-safety",
            "data_quality": {
                "missing_tickers": missing,
                "stale_data_tickers": [],
            }
        }
    }

    return packet


def run_backfill(start_date: str = None, end_date: str = None, days: int = None):
    """메인: backfill 실행"""
    universe_df = load_universe()

    if days and not start_date:
        end_date = end_date or now_kst().strftime("%Y%m%d")
        start_date = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=days * 2)).strftime("%Y%m%d")

    if not start_date or not end_date:
        print("❌ --start/--end 또는 --days 필요")
        return

    print(f"\n{'='*60}")
    print(f"  Backfill: {start_date} ~ {end_date}")
    print(f"{'='*60}\n")

    trading_days = get_trading_days(start_date, end_date)
    if days:
        trading_days = trading_days[-days:]

    print(f"거래일 {len(trading_days)}일 확인\n")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    success = 0
    skip = 0
    fail = 0

    for i, td in enumerate(trading_days):
        out_path = OUTPUT_DIR / f"DMP-{td}.json"
        if out_path.exists():
            print(f"  [{i+1}/{len(trading_days)}] {td} — 이미 존재, skip")
            skip += 1
            continue

        try:
            packet = build_backfill_packet(td, universe_df)
            collected = len(packet["market_data"])
            missing = len(packet["meta"]["data_quality"]["missing_tickers"])

            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(packet, f, ensure_ascii=False, indent=2)

            print(f"  [{i+1}/{len(trading_days)}] {td} — ✅ {collected}종목 (missing: {missing})")
            success += 1
        except Exception as e:
            print(f"  [{i+1}/{len(trading_days)}] {td} — ❌ {e}")
            fail += 1

        # rate limit 방지
        time.sleep(0.5)

    print(f"\n{'─'*40}")
    print(f"  완료: {success}일 성공, {skip}일 skip, {fail}일 실패")
    print(f"  총 파일: {len(list(OUTPUT_DIR.glob('DMP-*.json')))}개")
    print(f"{'─'*40}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DailyMarketPacket backfill")
    parser.add_argument("--start", type=str, help="시작일 YYYYMMDD")
    parser.add_argument("--end", type=str, help="종료일 YYYYMMDD")
    parser.add_argument("--days", type=int, default=20, help="최근 N거래일 (기본 20)")
    args = parser.parse_args()

    run_backfill(
        start_date=args.start,
        end_date=args.end or now_kst().strftime("%Y%m%d"),
        days=args.days if not args.start else None,
    )

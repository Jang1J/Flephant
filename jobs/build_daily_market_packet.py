"""
DailyMarketPacket 생성 파이프라인
- 특정 일자를 입력하면 유니버스 전체의 시장 데이터 + 뉴스/공시 인덱스 + 매크로 스냅샷을 수집
- artifacts/ 폴더에 JSON 파일로 저장
- PIT-safe tagging 적용 (available_at, as_of_dt, evidence_id)

Usage:
    python jobs/build_daily_market_packet.py 20260320
    python jobs/build_daily_market_packet.py  # 기본값: 오늘
"""

import sys
import os
import json
import hashlib
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path

# 프로젝트 루트를 path에 추가
_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

from connectors import now_kst_iso, make_snapshot_dt
from connectors.krx import get_ohlcv, get_universe_ohlcv, get_ticker_name, get_market_cap
from connectors.naver_news import search_stock_news
from connectors.dart import get_disclosure_list
from connectors.ecos import get_base_rate, get_treasury_yield, get_fx_rate
from connectors.llm_router import call_llm


# ── 설정 ──────────────────────────────────────────────────────
UNIVERSE_PATH = _BASE_DIR / "config" / "universe_v1.csv"
OUTPUT_DIR = _BASE_DIR / "artifacts" / "daily_market_packet"
LOOKBACK_DAYS = 70  # 기술적 지표 계산용 과거 데이터


def load_universe() -> pd.DataFrame:
    """유니버스 CSV 로드"""
    return pd.read_csv(UNIVERSE_PATH)


def compute_tech_features(df: pd.DataFrame) -> dict:
    """
    OHLCV DataFrame에서 기술적 지표 계산
    - SMA 5/20/60, RSI 14, BB, MACD, ATR, Volume Ratio
    """
    if df.empty or len(df) < 5:
        return {}

    close = df["종가"]
    high = df["고가"]
    low = df["저가"]
    volume = df["거래량"]

    features = {}

    # SMA
    features["sma_5"] = round(float(close.rolling(5).mean().iloc[-1]), 2) if len(close) >= 5 else None
    features["sma_20"] = round(float(close.rolling(20).mean().iloc[-1]), 2) if len(close) >= 20 else None
    features["sma_60"] = round(float(close.rolling(60).mean().iloc[-1]), 2) if len(close) >= 60 else None

    # RSI 14
    if len(close) >= 15:
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        features["rsi_14"] = round(float(rsi.iloc[-1]), 2) if not pd.isna(rsi.iloc[-1]) else None
    else:
        features["rsi_14"] = None

    # Bollinger Bands (20일)
    if len(close) >= 20:
        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        features["bb_upper"] = round(float((sma20 + 2 * std20).iloc[-1]), 2)
        features["bb_lower"] = round(float((sma20 - 2 * std20).iloc[-1]), 2)
    else:
        features["bb_upper"] = None
        features["bb_lower"] = None

    # MACD (12, 26, 9)
    if len(close) >= 26:
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        macd_signal = macd_line.ewm(span=9, adjust=False).mean()
        features["macd"] = round(float(macd_line.iloc[-1]), 2)
        features["macd_signal"] = round(float(macd_signal.iloc[-1]), 2)
    else:
        features["macd"] = None
        features["macd_signal"] = None

    # ATR 14
    if len(close) >= 15:
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        features["atr_14"] = round(float(atr.iloc[-1]), 2) if not pd.isna(atr.iloc[-1]) else None
    else:
        features["atr_14"] = None

    # Volume Ratio (당일 / 20일 평균)
    if len(volume) >= 20:
        avg_vol_20 = volume.rolling(20).mean().iloc[-1]
        if avg_vol_20 > 0:
            features["volume_ratio_20"] = round(float(volume.iloc[-1] / avg_vol_20), 4)
        else:
            features["volume_ratio_20"] = None
    else:
        features["volume_ratio_20"] = None

    # return_5d / return_20d (PIT-safe: 과거 종가만 사용)
    # 데이터 부족(< 6일) 또는 분모=0인 경우 0.0 fallback으로 UQ 입력 보장
    current_close = float(close.iloc[-1])
    if len(close) >= 6:
        close_5d_ago = float(close.iloc[-6])
        if close_5d_ago != 0:
            features["return_5d"] = round((current_close - close_5d_ago) / close_5d_ago * 100, 4)
        else:
            features["return_5d"] = 0.0
    else:
        features["return_5d"] = 0.0
    if len(close) >= 21:
        close_20d_ago = float(close.iloc[-21])
        if close_20d_ago != 0:
            features["return_20d"] = round((current_close - close_20d_ago) / close_20d_ago * 100, 4)
        else:
            features["return_20d"] = 0.0
    else:
        features["return_20d"] = 0.0

    # None 제거
    return {k: v for k, v in features.items() if v is not None}


def make_evidence_id(prefix: str, url_or_id: str) -> str:
    """URL 또는 ID에서 evidence_id 생성"""
    h = hashlib.md5(url_or_id.encode()).hexdigest()[:8]
    return f"{prefix}-{h}"


def collect_news_index(universe_df: pd.DataFrame, target_date: str) -> list:
    """유니버스 종목별 뉴스 수집 → news_index 생성 (PIT-safe: snapshot 이후 뉴스 제거)"""
    now_str = now_kst_iso()
    # snapshot 기준: target_date 18:00 KST
    snapshot_ts = pd.Timestamp(make_snapshot_dt(target_date))
    news_index = []
    filtered_count = 0

    for _, row in universe_df.iterrows():
        try:
            df = search_stock_news(row["name"], display=3)
            if df.empty:
                continue

            for _, n in df.iterrows():
                # pubDate → ISO 8601 KST 변환 (ZoneInfo 기반)
                try:
                    _raw = pd.to_datetime(n["pubDate"])
                    if _raw.tzinfo is None:
                        from zoneinfo import ZoneInfo as _ZI
                        _raw = _raw.tz_localize(_ZI("Asia/Seoul"))
                    pub_dt = _raw.isoformat(timespec="seconds")
                    pub_ts = pd.Timestamp(pub_dt)
                except Exception:
                    # pubDate 파싱 실패 → pub_ts를 신뢰할 수 없으므로 보수적으로 제외 (PIT-Safety)
                    filtered_count += 1
                    continue

                # ── PIT-safe 필터: snapshot 이후 뉴스 제거 ──
                # (파싱 실패 뉴스는 위 except에서 이미 제외됨 — None 통과 없음)
                if pub_ts > snapshot_ts:
                    filtered_count += 1
                    continue

                url = n.get("link", "")
                news_index.append({
                    "evidence_id": make_evidence_id("NEWS", url),
                    "ticker": str(row["ticker"]).zfill(6),
                    "headline": n["title"],
                    "source": "naver_news",
                    "published_at": pub_dt,
                    "available_at": now_str,
                    "url": url,
                })
        except Exception as e:
            print(f"  [WARN] {row['name']} 뉴스 수집 실패: {e}")

    if filtered_count > 0:
        print(f"  [PIT-SAFE] 미래 뉴스 {filtered_count}건 제거됨 (snapshot: {snapshot_ts})")

    return news_index


def collect_disclosure_index(target_date: str, universe_tickers: list = None) -> list:
    """DART 공시 수집 → disclosure_index 생성 (유니버스 필터링 적용)
    PIT-Safety: target_date 당일 공시는 보수적으로 제외 (전일까지만 수집).
    """
    now_str = now_kst_iso()
    disclosures = []

    # target_date 전일까지만 수집 (당일 공시는 PIT-safe하지 않으므로 제외)
    prev_date = (datetime.strptime(target_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")

    try:
        df = get_disclosure_list(bgn_de=prev_date, end_de=prev_date, page_count=100)
        if df.empty:
            return []

        total_before = len(df)
        for _, row in df.iterrows():
            rcept_no = str(row.get("rcept_no", ""))
            stock_code = str(row.get("stock_code", "")).strip()

            # 유니버스 필터: stock_code가 유니버스에 포함된 것만
            if universe_tickers and stock_code and stock_code not in universe_tickers:
                continue

            disclosures.append({
                "evidence_id": rcept_no if rcept_no else make_evidence_id("DART", str(row)),
                "ticker": stock_code,
                "corp_name": str(row.get("corp_name", "")),
                "report_nm": str(row.get("report_nm", "")),
                "rcept_dt": str(row.get("rcept_dt", "")),
                "rcept_no": rcept_no,
                "available_at": now_str,
            })

        if universe_tickers:
            print(f"  [FILTER] DART 공시 유니버스 필터: {total_before}건 → {len(disclosures)}건")
    except Exception as e:
        print(f"  [WARN] DART 공시 수집 실패: {e}")

    return disclosures


def collect_macro_snapshot(target_date: str) -> dict:
    """ECOS에서 매크로 데이터 수집"""
    macro = {}

    # 최근 30일 범위에서 마지막 값 가져오기
    start = (datetime.strptime(target_date, "%Y%m%d") - timedelta(days=30)).strftime("%Y%m%d")

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
        df = get_treasury_yield("10Y", start, target_date)
        if not df.empty:
            macro["treasury_10y"] = float(df["value"].iloc[-1])
    except Exception:
        pass

    try:
        df = get_fx_rate(start, target_date)
        if not df.empty:
            macro["usd_krw"] = float(df["value"].iloc[-1])
    except Exception:
        pass

    # VIX proxy: 유니버스 종목들의 평균 20일 변동성 (연환산)
    # pykrx index API가 pandas 2.3에서 호환 이슈 → 개별 종목 변동성 평균으로 우회
    import pandas as _pd
    try:
        uni = _pd.read_csv(UNIVERSE_PATH)
    except Exception as e:
        print(f"  [WARN] 유니버스 로드 실패: {e}")
        uni = _pd.DataFrame()
    try:
        vol_list = []
        for _, row in uni.iterrows():
            ticker = str(row["ticker"]).zfill(6)
            try:
                ohlcv = get_ohlcv(ticker, start, target_date)
                if len(ohlcv) >= 20:
                    rets = ohlcv["종가"].pct_change().dropna()
                    vol = float(rets.tail(20).std() * (252 ** 0.5) * 100)
                    vol_list.append(vol)
            except Exception:
                pass
        if vol_list:
            macro["vix_proxy"] = round(sum(vol_list) / len(vol_list), 2)
        else:
            macro["vix_proxy"] = None
    except Exception as e:
        print(f"  [WARN] VIX proxy 계산 실패: {e}")
        macro.setdefault("vix_proxy", None)

    # Market breadth: 유니버스 종목 중 당일 양의 수익률 비율
    try:
        advancing = 0
        total_checked = 0
        for _, row in uni.iterrows():
            ticker = str(row["ticker"]).zfill(6)
            try:
                ohlcv = get_ohlcv(ticker, target_date, target_date)
                if not ohlcv.empty:
                    total_checked += 1
                    chg = ohlcv["등락률"].iloc[-1] if "등락률" in ohlcv.columns else 0
                    if chg > 0:
                        advancing += 1
            except Exception:
                pass
        if total_checked > 0:
            macro["market_breadth"] = round(advancing / total_checked, 4)
        else:
            macro["market_breadth"] = None
    except Exception as e:
        print(f"  [WARN] Market breadth 계산 실패: {e}")
        macro.setdefault("market_breadth", None)

    return macro


def build_packet(target_date: str) -> dict:
    """
    메인: 특정 일자의 DailyMarketPacket 생성

    Args:
        target_date: YYYYMMDD 형식
    """
    print(f"\n{'='*60}")
    print(f"  DailyMarketPacket 생성: {target_date}")
    print(f"{'='*60}\n")

    now_str = now_kst_iso()
    snapshot_dt = make_snapshot_dt(target_date)

    # 1. 유니버스 로드
    universe_df = load_universe()
    tickers = [str(t).zfill(6) for t in universe_df["ticker"]]
    all_tickers = list(tickers)  # 고정 유니버스 (수집 실패와 무관하게 유지)
    print(f"[1/6] 유니버스 로드: {len(tickers)}종목")

    # 2. OHLCV + 시가총액 수집 (lookback 포함)
    start = (datetime.strptime(target_date, "%Y%m%d") - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    print(f"[2/6] OHLCV 수집 ({start} ~ {target_date})...")
    ohlcv_data = get_universe_ohlcv(tickers, start, target_date)

    # 시가총액 수집 (target_date 당일)
    mktcap_data = {}
    for ticker in tickers:
        try:
            cap_df = get_market_cap(ticker, target_date, target_date)
            if not cap_df.empty:
                # pykrx: 시가총액 컬럼명, KRX API fallback: MKTCAP_AMT 등
                if "시가총액" in cap_df.columns:
                    mktcap_data[ticker] = int(cap_df["시가총액"].iloc[-1])
                elif "MKTCAP_AMT" in cap_df.columns:
                    mktcap_data[ticker] = int(str(cap_df["MKTCAP_AMT"].iloc[-1]).replace(",", ""))
        except Exception as e:
            print(f"  [WARN] {ticker} 시가총액 수집 실패: {e}")

    # 3. 종목별 market_data 구성
    print(f"[3/6] 기술적 지표 계산...")
    market_data = {}
    missing_tickers = []
    stale_tickers = []

    for ticker in tickers:
        if ticker not in ohlcv_data or ohlcv_data[ticker].empty:
            missing_tickers.append(ticker)
            continue

        df = ohlcv_data[ticker]
        last_row = df.iloc[-1]

        # 최근 데이터가 target_date와 너무 다르면 stale
        last_date = df.index[-1]
        if hasattr(last_date, 'strftime'):
            last_date_str = last_date.strftime("%Y%m%d")
        else:
            last_date_str = str(last_date).replace("-", "")[:8]

        if last_date_str < (datetime.strptime(target_date, "%Y%m%d") - timedelta(days=5)).strftime("%Y%m%d"):
            stale_tickers.append(ticker)

        tech = compute_tech_features(df)

        market_data[ticker] = {
            "ohlcv": {
                "open": int(last_row["시가"]),
                "high": int(last_row["고가"]),
                "low": int(last_row["저가"]),
                "close": int(last_row["종가"]),
            },
            "volume": int(last_row["거래량"]),
            "mktcap": mktcap_data.get(ticker),  # pykrx 또는 KRX Open API, 실패 시 None
            "tech_features": tech,
        }

    print(f"  → 수집 성공: {len(market_data)}종목, 실패: {len(missing_tickers)}종목")

    # 4. 뉴스 수집
    print(f"[4/6] 뉴스 인덱스 수집...")
    news_index = collect_news_index(universe_df, target_date)
    print(f"  → {len(news_index)}건 수집")

    # 5. 공시 수집 (유니버스 필터링 적용)
    print(f"[5/6] DART 공시 수집...")
    universe_ticker_set = set(tickers)
    disclosure_index = collect_disclosure_index(target_date, universe_tickers=universe_ticker_set)
    print(f"  → {len(disclosure_index)}건 수집 (유니버스 필터 적용)")

    # 6. 매크로 스냅샷
    print(f"[6/6] 매크로 스냅샷 수집...")
    macro_snapshot = collect_macro_snapshot(target_date)
    print(f"  → {macro_snapshot}")

    # LLM 시장 코멘터리 생성 (선택적 — 실패해도 패킷 생성 영향 없음)
    llm_market_analysis = None
    try:
        print(f"[LLM] 시장 코멘터리 생성 중...")
        vix_val = macro_snapshot.get("vix_proxy")
        breadth_val = macro_snapshot.get("market_breadth")
        base_rate_val = macro_snapshot.get("base_rate")
        usd_krw_val = macro_snapshot.get("usd_krw")
        news_count = len(news_index)

        vix_str = f"{vix_val:.2f}" if vix_val is not None else "N/A"
        breadth_str = f"{breadth_val:.2%}" if breadth_val is not None else "N/A"
        base_rate_str = f"{base_rate_val:.2f}%" if base_rate_val is not None else "N/A"
        usd_krw_str = f"{usd_krw_val:.2f}" if usd_krw_val is not None else "N/A"

        market_date = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:8]}"
        prompt_messages = [
            {
                "role": "system",
                "content": "너는 한국 주식시장 전문 애널리스트야. 제공된 데이터를 바탕으로 투자자 관점의 시장 분석을 제공해줘.",
            },
            {
                "role": "user",
                "content": (
                    f"오늘({market_date}) KOSPI 시장 데이터 요약:\n"
                    f"- VIX proxy (변동성): {vix_str}\n"
                    f"- 상승/하락 비율 (Market breadth): {breadth_str}\n"
                    f"- 주요 매크로: 기준금리 {base_rate_str}, 환율 {usd_krw_str}\n"
                    f"- 주요 뉴스 {news_count}건\n\n"
                    "투자자 관점에서 오늘 시장 상황을 3~4문장으로 분석하라.\n"
                    "반드시 아래 JSON 형식으로만 답하라:\n"
                    '{"market_commentary": "시장 분석 3~4문장", '
                    '"market_mood": "bullish" 또는 "bearish" 또는 "neutral" 또는 "uncertain", '
                    '"key_drivers": ["키워드1", "키워드2", "키워드3"]}'
                ),
            },
        ]

        llm_result = call_llm(prompt_messages, temperature=0.3, max_tokens=512)
        raw_content = llm_result.get("content", "")
        model_used = llm_result.get("model", "unknown")

        # JSON 파싱 시도
        import re
        json_match = re.search(r'\{.*\}', raw_content, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group())
            llm_market_analysis = {
                "market_commentary": parsed.get("market_commentary", ""),
                "market_mood": parsed.get("market_mood", "uncertain"),
                "key_drivers": parsed.get("key_drivers", []),
                "model_used": model_used,
            }
            # market_mood enum 검증
            valid_moods = ["bullish", "bearish", "neutral", "uncertain"]
            if llm_market_analysis["market_mood"] not in valid_moods:
                llm_market_analysis["market_mood"] = "uncertain"
            print(f"  → 시장 코멘터리 생성 완료 (mood={llm_market_analysis['market_mood']}, model={model_used})")
        else:
            print(f"  [WARN] LLM 응답 JSON 파싱 실패 → llm_market_analysis=null")
    except Exception as e:
        print(f"  [DMP] 시장 코멘터리 LLM 호출 실패 (skip): {e}")

    # 패킷 조립
    packet = {
        "snapshot_id": f"DMP-{target_date}-180000",
        "snapshot_dt": snapshot_dt,
        "available_at": now_str,
        "artifact_version": "v1.0",
        "tickers": all_tickers,  # 고정 유니버스 전체 (수집 실패 포함)
        "market_data": market_data,
        "macro_snapshot": macro_snapshot,
        "news_index": news_index,
        "disclosure_index": disclosure_index,
        "llm_market_analysis": llm_market_analysis,
        "meta": {
            "as_of_dt": snapshot_dt,
            "universe_version": "v1",
            "data_quality": {
                "missing_tickers": missing_tickers,
                "stale_data_tickers": stale_tickers,
            }
        }
    }

    return packet


def save_packet(packet: dict, target_date: str):
    """패킷을 JSON 파일로 저장"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"DMP-{target_date}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(packet, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 저장: {path} ({path.stat().st_size / 1024:.1f} KB)")
    return path


def validate_packet(packet: dict) -> bool:
    """schema 검증"""
    try:
        from jsonschema import validate as jvalidate
        with open(_BASE_DIR / "schemas" / "daily_market_packet.json") as f:
            schema = json.load(f)
        jvalidate(instance=packet, schema=schema)
        print("✅ Schema validation PASS")
        return True
    except Exception as e:
        print(f"❌ Schema validation FAIL: {e}")
        return False


# ── main ──
if __name__ == "__main__":
    from connectors import now_kst
    target = sys.argv[1] if len(sys.argv) > 1 else now_kst().strftime("%Y%m%d")

    packet = build_packet(target)
    save_packet(packet, target)
    validate_packet(packet)

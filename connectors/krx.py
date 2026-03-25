"""
KRX 주식 데이터 커넥터
- pykrx: OHLCV, 종목명 (primary)
- KRX Open API: 시가총액, 기본지표 (fallback — pykrx 호환 이슈 보완)
- universe_v1.csv 기반 종목 대상
"""

import os
import requests as _requests
import pandas as pd
from pykrx import stock
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path
from dotenv import load_dotenv

# .env 로드
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

KRX_API_KEY = os.getenv("KRX_API_KEY", "")
KRX_API_BASE = "https://data-dbg.krx.co.kr/svc/apis"


def get_ohlcv(ticker: str, start: str, end: str) -> pd.DataFrame:
    """
    종목별 OHLCV 데이터 조회

    Args:
        ticker: 종목코드 (e.g., "005930")
        start: 시작일 (e.g., "20240101")
        end: 종료일 (e.g., "20240331")

    Returns:
        DataFrame with columns: 시가, 고가, 저가, 종가, 거래량
    """
    df = stock.get_market_ohlcv(start, end, ticker)
    df.index.name = "date"
    return df


def _krx_api_get(endpoint: str, params: dict) -> dict:
    """KRX Open API 호출 (GET, AUTH_KEY 헤더)"""
    if not KRX_API_KEY:
        raise RuntimeError("KRX_API_KEY가 .env에 설정되지 않았습니다")
    url = KRX_API_BASE + endpoint
    headers = {"AUTH_KEY": KRX_API_KEY}
    r = _requests.get(url, params=params, headers=headers, timeout=15)
    if r.status_code == 401:
        raise RuntimeError(f"KRX API 401: 서비스 이용신청이 필요합니다 (endpoint={endpoint})")
    if r.status_code != 200:
        raise RuntimeError(f"KRX API {r.status_code}: {r.text[:200]}")
    return r.json()


def get_market_cap(ticker: str, start: str, end: str) -> pd.DataFrame:
    """
    종목별 시가총액 데이터 조회
    1차: pykrx 시도 → 실패/빈 결과 시 KRX Open API fallback

    Returns:
        DataFrame with columns: 시가총액, 거래량, 거래대금, 상장주식수
    """
    # 1차: pykrx
    try:
        df = stock.get_market_cap(start, end, ticker)
        df.index.name = "date"
        if not df.empty:
            return df
    except Exception as e:
        print(f"[KRX] pykrx 시가총액 실패: {e}")

    # 2차: KRX Open API (유가증권 일별매매정보)
    try:
        data = _krx_api_get("/sto/stk_bydd_trd", {"basDd": end})
        rows = data.get("OutBlock_1", [])
        if not rows:
            return pd.DataFrame()
        all_df = pd.DataFrame(rows)
        # ISU_SRT_CD가 종목코드 (6자리)
        if "ISU_SRT_CD" in all_df.columns:
            matched = all_df[all_df["ISU_SRT_CD"] == ticker]
        elif "ISU_CD" in all_df.columns:
            matched = all_df[all_df["ISU_CD"].str.contains(ticker)]
        else:
            matched = pd.DataFrame()
        if not matched.empty:
            print(f"  [KRX API fallback] {ticker} 시가총액 조회 성공")
        return matched
    except Exception as e:
        print(f"  [KRX API fallback] 시가총액 조회 실패: {e}")
        return pd.DataFrame()


def get_fundamentals(ticker: str, start: str, end: str) -> pd.DataFrame:
    """
    종목별 기본 지표 (PER, PBR, 배당수익률)
    1차: pykrx 시도 → 실패/빈 결과 시 KRX Open API fallback

    Returns:
        DataFrame with columns: BPS, PER, PBR, EPS, DIV, DPS
    """
    # 1차: pykrx
    try:
        df = stock.get_market_fundamental(start, end, ticker)
        df.index.name = "date"
        if not df.empty:
            return df
    except Exception as e:
        print(f"[KRX] pykrx 기본지표 실패: {e}")

    # 2차: KRX Open API (유가증권 종목기본정보)
    try:
        data = _krx_api_get("/sto/stk_isu_base_info", {"basDd": end})
        rows = data.get("OutBlock_1", [])
        if not rows:
            return pd.DataFrame()
        all_df = pd.DataFrame(rows)
        if "ISU_SRT_CD" in all_df.columns:
            matched = all_df[all_df["ISU_SRT_CD"] == ticker]
        elif "ISU_CD" in all_df.columns:
            matched = all_df[all_df["ISU_CD"].str.contains(ticker)]
        else:
            matched = pd.DataFrame()
        if not matched.empty:
            print(f"  [KRX API fallback] {ticker} 기본지표 조회 성공")
        return matched
    except Exception as e:
        print(f"  [KRX API fallback] 기본지표 조회 실패: {e}")
        return pd.DataFrame()


def get_universe_ohlcv(tickers: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """
    유니버스 전체 종목의 OHLCV 일괄 조회

    Returns:
        {ticker: DataFrame} 딕셔너리
    """
    result = {}
    for t in tickers:
        try:
            df = get_ohlcv(t, start, end)
            if not df.empty:
                result[t] = df
        except Exception as e:
            print(f"[WARN] {t} OHLCV 조회 실패: {e}")
    return result


def get_ticker_name(ticker: str, date: Optional[str] = None) -> str:
    """종목코드 → 종목명 변환"""
    if date is None:
        date = datetime.now().strftime("%Y%m%d")
    name = stock.get_market_ticker_name(ticker)
    return name


# ── smoke test ──
if __name__ == "__main__":
    print("=== KRX Connector Smoke Test ===\n")

    # 삼성전자 최근 5거래일
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")

    print(f"[1] 삼성전자(005930) OHLCV — pykrx ({start}~{end})")
    ohlcv = get_ohlcv("005930", start, end)
    print(ohlcv.tail())
    print()

    print("[2] 삼성전자 시가총액 — pykrx → KRX Open API fallback")
    cap = get_market_cap("005930", start, end)
    if cap.empty:
        print("  ⚠️ 빈 결과 (서비스 이용신청 필요할 수 있음)")
    else:
        print(cap.tail())
    print()

    print("[3] 삼성전자 기본지표 — pykrx → KRX Open API fallback")
    fund = get_fundamentals("005930", start, end)
    if fund.empty:
        print("  ⚠️ 빈 결과 (서비스 이용신청 필요할 수 있음)")
    else:
        print(fund.tail())
    print()

    print("[4] 종목명 확인 — pykrx")
    name = get_ticker_name("005930")
    print(f"  005930 → {name}")
    print()

    # 유니버스 일부 테스트
    test_tickers = ["005930", "000660", "005380"]
    print(f"[5] 유니버스 일괄 조회 ({test_tickers})")
    batch = get_universe_ohlcv(test_tickers, start, end)
    for t, df in batch.items():
        print(f"  {t}: {len(df)}일치 데이터")

    # KRX Open API 직접 테스트
    print(f"\n[6] KRX Open API 직접 호출 테스트")
    print(f"  API Key: {KRX_API_KEY[:8]}...{KRX_API_KEY[-4:]}" if KRX_API_KEY else "  ❌ API Key 없음")
    if KRX_API_KEY:
        try:
            data = _krx_api_get("/sto/stk_bydd_trd", {"basDd": end})
            rows = data.get("OutBlock_1", [])
            print(f"  ✅ 응답 성공! {len(rows)}개 종목 데이터")
            if rows:
                sample = rows[0]
                print(f"  샘플 필드: {list(sample.keys())[:8]}...")
        except Exception as e:
            print(f"  ❌ 호출 실패: {e}")

    print("\n✅ KRX smoke test 완료!")

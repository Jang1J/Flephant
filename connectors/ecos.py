"""
ECOS (한국은행 경제통계) 커넥터
- 금리, 환율, 경기지표 등 매크로 데이터
- Tier 1 Regime Gate 입력 데이터 소스
"""

import requests
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
ECOS_API_KEY = os.getenv("ECOS_API_KEY", "")
BASE_URL = "https://ecos.bok.or.kr/api/StatisticSearch"


# 자주 쓰는 통계표 코드
STAT_CODES = {
    "기준금리": ("722Y001", "0101000"),       # 한국은행 기준금리
    "국고채3Y": ("817Y002", "010200000"),     # 국고채 3년
    "국고채10Y": ("817Y002", "010210000"),    # 국고채 10년
    "원달러환율": ("731Y001", "0000001"),     # 원/달러 환율 (매매기준율)
    "KOSPI": ("802Y001", "0001000"),          # KOSPI 지수
}


def get_stat(
    stat_code: str,
    item_code: str,
    start: str,
    end: str,
    cycle: str = "D",
) -> pd.DataFrame:
    """
    ECOS 통계 조회

    Args:
        stat_code: 통계표코드
        item_code: 통계항목코드
        start: 시작일 (YYYYMMDD for D, YYYYMM for M)
        end: 종료일
        cycle: D=일, M=월, Q=분기, A=연

    Returns:
        DataFrame with columns: date, value
    """
    if not ECOS_API_KEY:
        print("[ECOS] API 키 미설정, 빈 결과 반환")
        return pd.DataFrame()

    url = f"{BASE_URL}/{ECOS_API_KEY}/json/kr/1/100/{stat_code}/{cycle}/{start}/{end}/{item_code}"

    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if "StatisticSearch" not in data:
        msg = data.get("RESULT", {}).get("MESSAGE", "unknown error")
        print(f"[ECOS] 에러: {msg}")
        return pd.DataFrame()

    rows = data["StatisticSearch"]["row"]
    df = pd.DataFrame(rows)

    # 정리
    result = pd.DataFrame({
        "date": df["TIME"],
        "value": pd.to_numeric(df["DATA_VALUE"], errors="coerce"),
        "stat_name": df["STAT_NAME"],
        "item_name": df["ITEM_NAME1"],
    })

    return result


def get_base_rate(start: str, end: str) -> pd.DataFrame:
    """한국은행 기준금리 조회"""
    code, item = STAT_CODES["기준금리"]
    return get_stat(code, item, start, end, cycle="D")


def get_treasury_yield(maturity: str, start: str, end: str) -> pd.DataFrame:
    """국고채 금리 조회 (3Y / 10Y)"""
    key = f"국고채{maturity}"
    if key not in STAT_CODES:
        print(f"[ECOS] 지원하지 않는 만기: {maturity}")
        return pd.DataFrame()
    code, item = STAT_CODES[key]
    return get_stat(code, item, start, end, cycle="D")


def get_fx_rate(start: str, end: str) -> pd.DataFrame:
    """원/달러 환율 조회"""
    code, item = STAT_CODES["원달러환율"]
    return get_stat(code, item, start, end, cycle="D")


# ── smoke test ──
if __name__ == "__main__":
    print("=== ECOS Connector Smoke Test ===\n")

    if not ECOS_API_KEY:
        print("⚠️  ECOS_API_KEY가 .env에 없습니다!")
        print("   https://ecos.bok.or.kr/api/ 에서 발급 후 .env에 설정하세요.")
        exit(1)

    print("[1] 기준금리 (최근)")
    df = get_base_rate("20260101", "20260320")
    if not df.empty:
        print(df.tail())
    else:
        print("  (결과 없음)")
    print()

    print("[2] 원/달러 환율")
    df = get_fx_rate("20260301", "20260320")
    if not df.empty:
        print(df.tail())
    else:
        print("  (결과 없음)")

    print("\n✅ ECOS smoke test 완료!")

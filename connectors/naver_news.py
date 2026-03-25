"""
Naver 검색 API 커넥터
- 뉴스 검색 (종목명 / 섹터 키워드)
- TickerTextPack의 news source
"""

import requests
import pandas as pd
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")
SEARCH_URL = "https://openapi.naver.com/v1/search/news.json"


def search_news(
    query: str,
    display: int = 10,
    sort: str = "date",
) -> pd.DataFrame:
    """
    Naver 뉴스 검색

    Args:
        query: 검색어 (e.g., "삼성전자 실적")
        display: 결과 수 (max 100)
        sort: 정렬 (date=최신순, sim=정확도순)

    Returns:
        DataFrame with columns: title, description, link, pubDate
    """
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        print("[NaverNews] API 키 미설정, 빈 결과 반환")
        return pd.DataFrame()

    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {
        "query": query,
        "display": display,
        "sort": sort,
    }

    resp = requests.get(SEARCH_URL, headers=headers, params=params, timeout=10)
    resp.raise_for_status()

    data = resp.json()
    items = data.get("items", [])

    if not items:
        return pd.DataFrame()

    df = pd.DataFrame(items)
    # HTML 태그 제거
    for col in ["title", "description"]:
        if col in df.columns:
            df[col] = df[col].str.replace(r"<.*?>", "", regex=True)

    return df[["title", "description", "link", "pubDate"]]


def search_stock_news(
    stock_name: str,
    extra_keywords: list[str] | None = None,
    display: int = 10,
) -> pd.DataFrame:
    """
    종목 관련 뉴스 검색

    Args:
        stock_name: 종목명 (e.g., "삼성전자")
        extra_keywords: 추가 키워드 (e.g., ["실적", "반도체"])
        display: 결과 수

    Returns:
        뉴스 DataFrame
    """
    query = stock_name
    if extra_keywords:
        query += " " + " ".join(extra_keywords)

    return search_news(query, display=display)


# ── smoke test ──
if __name__ == "__main__":
    print("=== Naver News Connector Smoke Test ===\n")

    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        print("⚠️  NAVER_CLIENT_ID / NAVER_CLIENT_SECRET가 .env에 없습니다!")
        print("   https://developers.naver.com 에서 발급 후 .env에 설정하세요.")
        exit(1)

    print("[1] '삼성전자' 뉴스 검색")
    df = search_stock_news("삼성전자", display=5)
    if not df.empty:
        for _, row in df.iterrows():
            print(f"  • {row['title']}")
            print(f"    {row['pubDate']}")
        print(f"\n  총 {len(df)}건")
    else:
        print("  (결과 없음)")

    print("\n✅ Naver News smoke test 완료!")

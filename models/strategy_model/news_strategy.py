"""
NewsStrategy — AI #2 뉴스 신호 생성 모듈

TTP(TickerTextPack)의 llm_news_analysis 또는 헤드라인 휴리스틱을 이용해
종목별 뉴스 신호 점수(-1 ~ +1)를 산출한다.

우선순위:
  1. TTP.llm_news_analysis.news_sentiment  (Kanana-o 분석 결과)
  2. TTP target_company_docs / sector_docs 헤드라인 키워드 휴리스틱
  3. DMP.news_index 헤드라인 키워드 휴리스틱
  4. 신호 없음 → 0.0 (중립)

PIT-Safety: snapshot_dt 기준 18:00 KST 이전에 available_at 이 찍힌 문서만 사용.
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

# jobs/ 와 동일한 패턴으로 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# ---------------------------------------------------------------------------
# 상수 — 한국어 감성 키워드
# ---------------------------------------------------------------------------
POSITIVE_KEYWORDS = [
    "호실적", "상향", "매수", "신고가", "증가", "성장", "수주", "배당", "기대",
]
NEGATIVE_KEYWORDS = [
    "적자", "하향", "매도", "급락", "감소", "하락", "리스크", "손실", "우려",
]

KST = timezone(timedelta(hours=9))

# ---------------------------------------------------------------------------
# 경로 설정
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TTP_DIR = PROJECT_ROOT / "artifacts" / "ticker_text_pack"
DMP_DIR = PROJECT_ROOT / "artifacts" / "daily_market_packet"


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _snapshot_cutoff(target_date: str) -> datetime:
    """target_date(YYYYMMDD) 기준 18:00 KST 반환 (PIT-Safety 기준선)."""
    dt = datetime.strptime(target_date, "%Y%m%d").replace(
        hour=18, minute=0, second=0, microsecond=0, tzinfo=KST
    )
    return dt


def _is_pit_safe(available_at_str: str, cutoff: datetime) -> bool:
    """available_at 문자열이 cutoff 이전인지 확인한다."""
    try:
        available_at = datetime.fromisoformat(available_at_str)
        if available_at.tzinfo is None:
            available_at = available_at.replace(tzinfo=KST)
        return available_at <= cutoff
    except Exception:
        # 파싱 실패 시 보수적으로 PIT-unsafe 처리
        return False


def _keyword_score(text: str) -> float:
    """
    텍스트에서 긍정/부정 키워드 출현 횟수를 집계해
    -1 ~ +1 사이 점수를 반환한다.
    """
    pos = sum(text.count(kw) for kw in POSITIVE_KEYWORDS)
    neg = sum(text.count(kw) for kw in NEGATIVE_KEYWORDS)
    total = pos + neg
    if total == 0:
        return 0.0
    raw = (pos - neg) / total          # -1 ~ +1
    # 절대값을 0.8로 클리핑해 극단값 방지
    return max(-0.8, min(0.8, raw))


def _load_ttp(target_date: str, ticker: str) -> Optional[dict]:
    """해당 날짜/종목의 TTP 파일을 로드한다. 없으면 None 반환."""
    path = TTP_DIR / f"TTP-{target_date}-{ticker}.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[NewsStrategy] TTP 로드 실패 ({ticker}): {e}")
        return None


def _load_dmp(target_date: str) -> Optional[dict]:
    """해당 날짜의 DMP 파일을 로드한다. 없으면 None 반환."""
    path = DMP_DIR / f"DMP-{target_date}.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[NewsStrategy] DMP 로드 실패 ({target_date}): {e}")
        return None


def _score_from_ttp(ttp: dict, cutoff: datetime) -> Optional[float]:
    """
    TTP에서 뉴스 신호 점수를 추출한다.

    우선순위:
      1. llm_news_analysis.news_sentiment (Kanana-o 분석 결과, 이미 -1~+1)
      2. target_company_docs + sector_docs 헤드라인 키워드 휴리스틱
    """
    # 1. LLM 분석 결과 우선 사용
    llm_analysis = ttp.get("llm_news_analysis")
    if llm_analysis and isinstance(llm_analysis, dict):
        sentiment = llm_analysis.get("news_sentiment")
        if sentiment is not None:
            try:
                score = float(sentiment)
                # 스키마 범위(-1~+1) 재보정
                return max(-1.0, min(1.0, score))
            except (TypeError, ValueError):
                pass

    # 2. 헤드라인 키워드 휴리스틱 (PIT-safe 문서만 대상)
    all_docs = ttp.get("target_company_docs", []) + ttp.get("sector_docs", [])
    texts: list[str] = []
    for doc in all_docs:
        available_at = doc.get("available_at") or ttp.get("meta", {}).get("available_at", "")
        if available_at and not _is_pit_safe(available_at, cutoff):
            continue
        title = doc.get("title", "")
        content = doc.get("content", "")
        texts.append(title + " " + content)

    if not texts:
        return None

    scores = [_keyword_score(t) for t in texts]
    return sum(scores) / len(scores)


def _score_from_dmp(dmp: dict, ticker: str, cutoff: datetime) -> Optional[float]:
    """
    DMP.news_index에서 해당 종목의 헤드라인 키워드 점수를 추출한다.
    PIT-safe (available_at <= cutoff) 기사만 대상으로 한다.
    """
    news_index = dmp.get("news_index", [])
    if not isinstance(news_index, list) or len(news_index) == 0:
        return None

    ticker_news = [
        item for item in news_index
        if str(item.get("ticker", "")).zfill(6) == ticker
        and _is_pit_safe(item.get("available_at", ""), cutoff)
    ]

    if not ticker_news:
        return None

    scores = [_keyword_score(item.get("headline", "")) for item in ticker_news]
    return sum(scores) / len(scores)


# ---------------------------------------------------------------------------
# 공개 인터페이스
# ---------------------------------------------------------------------------

def compute_news_signals(target_date: str, universe_tickers: list) -> dict:
    """
    target_date 기준 종목별 뉴스 신호 점수를 반환한다.

    Parameters
    ----------
    target_date : str
        기준 날짜 (YYYYMMDD 형식)
    universe_tickers : list[str]
        신호를 산출할 종목코드 리스트 (6자리 zero-padded)

    Returns
    -------
    dict
        {ticker: news_signal_score}
        - score 범위: -1.0 ~ +1.0
        - 데이터 없을 경우 0.0 (중립)

    Notes
    -----
    PIT-Safety: snapshot_dt 기준 18:00 KST 이전 available_at 데이터만 사용.
    """
    print(f"[NewsStrategy] {target_date} 뉴스 신호 산출 시작 (종목 수: {len(universe_tickers)})")

    cutoff = _snapshot_cutoff(target_date)
    dmp = _load_dmp(target_date)  # DMP는 전체에서 1회만 로드

    results: dict[str, float] = {}

    for raw_ticker in universe_tickers:
        ticker = str(raw_ticker).zfill(6)
        score: Optional[float] = None
        source_used = "none"

        # 1. TTP 우선 시도
        ttp = _load_ttp(target_date, ticker)
        if ttp is not None:
            score = _score_from_ttp(ttp, cutoff)
            if score is not None:
                source_used = (
                    "ttp_llm"
                    if ttp.get("llm_news_analysis") and ttp["llm_news_analysis"].get("news_sentiment") is not None
                    else "ttp_heuristic"
                )

        # 2. TTP 없거나 TTP에서 신호를 추출하지 못한 경우 DMP fallback
        if score is None and dmp is not None:
            score = _score_from_dmp(dmp, ticker, cutoff)
            if score is not None:
                source_used = "dmp_heuristic"

        # 3. 어떤 소스에서도 신호 없음 → 중립
        if score is None:
            score = 0.0
            source_used = "neutral_fallback"

        results[ticker] = round(score, 4)
        print(f"[NewsStrategy]  {ticker}: {results[ticker]:+.4f} ({source_used})")

    print(f"[NewsStrategy] {target_date} 뉴스 신호 산출 완료")
    return results


# ---------------------------------------------------------------------------
# 단독 실행 (smoke test)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys as _sys

    _date = _sys.argv[1] if len(_sys.argv) > 1 else "20260320"
    _tickers = [
        "005930", "000660", "009150", "005380", "000270",
        "373220", "006400", "051910", "105560", "055550",
    ]
    signals = compute_news_signals(_date, _tickers)
    print("\n--- 결과 요약 ---")
    for t, s in signals.items():
        bar = "#" * int((s + 1) * 10)
        print(f"  {t}: {s:+.4f}  {bar}")

"""
TickerTextPack 생성 파이프라인
- 종목별 3-level 텍스트 패키지: macro_docs / sector_docs / target_company_docs
- DailyMarketPacket의 news_index + DART 공시 + ECOS 매크로를 종목별로 재구성
- alias normalization + headline dedup 적용

Usage:
    python jobs/build_ticker_text_pack.py 20260320
    python jobs/build_ticker_text_pack.py 20260320 005930   # 특정 종목만
"""

import sys
import os
import json
import hashlib
import re
import html
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

from connectors import now_kst_iso, make_snapshot_dt
from connectors.naver_news import search_news, search_stock_news
from connectors.ecos import get_base_rate, get_treasury_yield
from connectors.dart import get_disclosure_list
from connectors.llm_router import call_llm

# ── 설정 ──────────────────────────────────────────────────────
UNIVERSE_PATH = _BASE_DIR / "config" / "universe_v1.csv"
OUTPUT_DIR = _BASE_DIR / "artifacts" / "ticker_text_pack"
DEDUP_SIMILARITY_THRESHOLD = 0.8  # 제목 유사도 기준


# ── Alias Normalization ──────────────────────────────────────
# 종목명 변형을 정규화 (뉴스에서 다양한 이름으로 등장)
ALIAS_MAP = {
    "삼성전자": ["삼성전자", "삼전", "Samsung Electronics"],
    "SK하이닉스": ["SK하이닉스", "하이닉스", "SK Hynix"],
    "현대차": ["현대차", "현대자동차", "현대모터", "Hyundai Motor"],
    "기아": ["기아", "기아차", "기아자동차", "Kia"],
    "LG에너지솔루션": ["LG에너지솔루션", "LG에너솔", "LGES"],
    "삼성SDI": ["삼성SDI", "삼성에스디아이"],
    "LG화학": ["LG화학", "엘지화학"],
    "KB금융": ["KB금융", "KB금융지주", "국민은행"],
    "신한지주": ["신한지주", "신한은행", "신한금융"],
    "하나금융지주": ["하나금융지주", "하나금융", "하나은행"],
    "DB손해보험": ["DB손해보험", "DB손보"],
    "삼성바이오로직스": ["삼성바이오로직스", "삼성바이오", "삼바"],
    "셀트리온": ["셀트리온"],
    "SK바이오팜": ["SK바이오팜"],
    "유한양행": ["유한양행"],
    "NAVER": ["NAVER", "네이버"],
    "카카오": ["카카오", "Kakao"],
    "크래프톤": ["크래프톤", "KRAFTON", "배그"],
    "HD한국조선해양": ["HD한국조선해양", "한국조선해양", "HD현대"],
    "한화에어로스페이스": ["한화에어로스페이스", "한화에어로", "한화방산"],
    "한화오션": ["한화오션", "대우조선"],
    "SK텔레콤": ["SK텔레콤", "SKT"],
    "KT": ["KT", "케이티"],
    "한국전력": ["한국전력", "한전", "KEPCO"],
    "대한항공": ["대한항공", "Korean Air"],
}

# 섹터별 키워드 (sector_docs 수집용)
SECTOR_KEYWORDS = {
    "반도체": ["반도체", "HBM", "메모리", "파운드리", "DRAM", "NAND"],
    "IT하드웨어": ["IT부품", "MLCC", "전자부품"],
    "자동차": ["자동차", "전기차", "EV", "완성차"],
    "2차전지": ["2차전지", "배터리", "리튬", "양극재"],
    "화학": ["화학", "석유화학", "소재"],
    "은행": ["은행", "금리", "대출", "금융"],
    "보험": ["보험", "손해보험"],
    "바이오": ["바이오", "바이오시밀러", "신약", "임상"],
    "제약": ["제약", "의약품"],
    "인터넷": ["인터넷", "플랫폼", "검색", "AI"],
    "게임": ["게임", "e스포츠"],
    "조선": ["조선", "LNG선", "선박"],
    "방산": ["방산", "무기", "방위"],
    "통신": ["통신", "5G", "6G"],
    "유틸리티": ["전력", "에너지", "유틸리티"],
    "운송": ["항공", "운송"],
}


def load_universe() -> pd.DataFrame:
    return pd.read_csv(UNIVERSE_PATH)


def normalize_headline(text: str) -> str:
    """뉴스 제목 정규화: HTML entity unescape + 태그 제거 + 공백 정리"""
    text = html.unescape(text)
    text = re.sub(r"<.*?>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def headline_fingerprint(text: str) -> str:
    """중복 판별용 fingerprint: 특수문자/공백 제거 후 hash"""
    cleaned = re.sub(r"[^가-힣a-zA-Z0-9]", "", text)
    return hashlib.md5(cleaned.encode()).hexdigest()


def dedup_docs(docs: list) -> tuple[list, float]:
    """
    headline 기반 중복 제거
    Returns: (deduped_docs, dedup_ratio)
    """
    if not docs:
        return [], 1.0

    seen = set()
    unique = []
    for doc in docs:
        fp = headline_fingerprint(doc.get("title", ""))
        if fp not in seen:
            seen.add(fp)
            unique.append(doc)

    original_count = len(docs)
    dedup_ratio = len(unique) / original_count if original_count > 0 else 1.0
    return unique, round(dedup_ratio, 4)


def is_within_snapshot(pub_dt: str, target_date: str) -> bool:
    """PIT-safe 체크: published_at이 snapshot(18:00 KST) 이전인지"""
    try:
        snapshot_ts = pd.Timestamp(make_snapshot_dt(target_date))
        pub_ts = pd.Timestamp(pub_dt)
        return pub_ts <= snapshot_ts
    except Exception:
        return False  # 파싱 실패 시 미래 데이터 포함 방지


def make_evidence_id(prefix: str, url_or_id: str) -> str:
    h = hashlib.md5(url_or_id.encode()).hexdigest()[:8]
    return f"{prefix}-{h}"


def collect_macro_docs(target_date: str) -> list:
    """매크로 문서 수집: ECOS 데이터 + 매크로 뉴스"""
    docs = []
    now_str = now_kst_iso()
    start = (datetime.strptime(target_date, "%Y%m%d") - timedelta(days=30)).strftime("%Y%m%d")

    # ECOS 기준금리
    try:
        df = get_base_rate(start, target_date)
        if not df.empty:
            last = df.iloc[-1]
            docs.append({
                "doc_id": f"MACRO-BASERATE-{target_date}",
                "source": "ecos",
                "title": f"한국은행 기준금리 {last['value']}%",
                "content": f"한국은행 기준금리가 {last['value']}%로 유지되고 있습니다. (기준일: {last['date']})",
                "published_at": f"{last['date'][:4]}-{last['date'][4:6]}-{last['date'][6:8]}T10:00:00+09:00",
                "evidence_id": f"ECOS-BASERATE-{last['date']}",
            })
    except Exception:
        pass

    # 매크로 뉴스 (경제 전반)
    try:
        news_df = search_news("경제 금리 시장 전망", display=5, sort="date")
        if not news_df.empty:
            for _, row in news_df.iterrows():
                title = normalize_headline(row["title"])
                desc = normalize_headline(row.get("description", ""))
                try:
                    from zoneinfo import ZoneInfo as _ZI
                    _raw = pd.to_datetime(row["pubDate"])
                    if _raw.tzinfo is None:
                        _raw = _raw.tz_localize(_ZI("Asia/Seoul"))
                    pub_dt = _raw.isoformat(timespec="seconds")
                except Exception:
                    # pubDate 파싱 실패 → 보수적으로 제외 (PIT-Safety)
                    continue

                # PIT-safe 필터
                if not is_within_snapshot(pub_dt, target_date):
                    continue

                url = row.get("link", "")
                docs.append({
                    "doc_id": make_evidence_id("MACRO-NEWS", url),
                    "source": "naver_news",
                    "title": title,
                    "content": desc[:2000],
                    "published_at": pub_dt,
                    "evidence_id": make_evidence_id("NEWS", url),
                })
    except Exception:
        pass

    return docs


def collect_sector_docs(sector: str, target_date: str) -> list:
    """섹터 관련 뉴스 수집"""
    docs = []
    keywords = SECTOR_KEYWORDS.get(sector, [sector])

    try:
        query = " ".join(keywords[:3])
        news_df = search_news(query, display=5, sort="date")
        if not news_df.empty:
            for _, row in news_df.iterrows():
                title = normalize_headline(row["title"])
                desc = normalize_headline(row.get("description", ""))
                try:
                    from zoneinfo import ZoneInfo as _ZI
                    _raw = pd.to_datetime(row["pubDate"])
                    if _raw.tzinfo is None:
                        _raw = _raw.tz_localize(_ZI("Asia/Seoul"))
                    pub_dt = _raw.isoformat(timespec="seconds")
                except Exception:
                    # pubDate 파싱 실패 → 보수적으로 제외 (PIT-Safety)
                    continue

                # PIT-safe 필터
                if not is_within_snapshot(pub_dt, target_date):
                    continue

                url = row.get("link", "")
                docs.append({
                    "doc_id": make_evidence_id(f"SECTOR-{sector}", url),
                    "source": "naver_news",
                    "title": title,
                    "content": desc[:2000],
                    "published_at": pub_dt,
                    "evidence_id": make_evidence_id("NEWS", url),
                })
    except Exception:
        pass

    return docs


def collect_target_docs(name: str, ticker: str, target_date: str) -> list:
    """종목 직접 관련 뉴스/공시 수집"""
    docs = []

    # 뉴스
    try:
        news_df = search_stock_news(name, display=5)
        if not news_df.empty:
            for _, row in news_df.iterrows():
                title = normalize_headline(row["title"])
                desc = normalize_headline(row.get("description", ""))
                try:
                    from zoneinfo import ZoneInfo as _ZI
                    _raw = pd.to_datetime(row["pubDate"])
                    if _raw.tzinfo is None:
                        _raw = _raw.tz_localize(_ZI("Asia/Seoul"))
                    pub_dt = _raw.isoformat(timespec="seconds")
                except Exception:
                    # pubDate 파싱 실패 → 보수적으로 제외 (PIT-Safety)
                    continue

                # PIT-safe 필터
                if not is_within_snapshot(pub_dt, target_date):
                    continue

                url = row.get("link", "")
                docs.append({
                    "doc_id": make_evidence_id(f"TARGET-{ticker}", url),
                    "source": "naver_news",
                    "title": title,
                    "content": desc[:2000],
                    "published_at": pub_dt,
                    "evidence_id": make_evidence_id("NEWS", url),
                })
    except Exception:
        pass

    # DART 공시
    # PIT-Safety: 공시 접수 시각(rcept_tm)이 없으면 당일(target_date) 공시는 제외.
    # 이유: same-day 공시는 장중/장후 접수일 수 있어 정확한 시각 없이 09:00 가정 시
    #       미래 데이터 leakage 발생 위험. 전일(target_date - 1) 이전 공시만 포함.
    try:
        start_dt = (datetime.strptime(target_date, "%Y%m%d") - timedelta(days=7)).strftime("%Y%m%d")
        # end_de를 target_date 전일까지로 제한하여 same-day 공시 전체 제외
        end_dt = (datetime.strptime(target_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
        disc_df = get_disclosure_list(bgn_de=start_dt, end_de=end_dt, page_count=20)
        if not disc_df.empty:
            # 종목코드 기준 필터
            disc_df["stock_code"] = disc_df["stock_code"].astype(str).str.strip()
            ticker_discs = disc_df[disc_df["stock_code"] == ticker]
            dart_excluded = 0
            for _, row in ticker_discs.iterrows():
                rcept_no = str(row.get("rcept_no", ""))
                rcept_dt = str(row.get("rcept_dt", ""))
                report_nm = str(row.get("report_nm", ""))
                corp_name = str(row.get("corp_name", ""))

                # rcept_dt 누락/형식 오류 → 보수적으로 제외
                if len(rcept_dt) != 8:
                    dart_excluded += 1
                    continue

                # same-day 공시 이중 방어: end_de 설정에도 불구하고 혹시 포함된 경우 제거
                if rcept_dt >= target_date:
                    dart_excluded += 1
                    continue

                pub_dt = f"{rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:8]}T18:00:00+09:00"

                docs.append({
                    "doc_id": make_evidence_id(f"DART-{ticker}", rcept_no),
                    "source": "dart",
                    "title": f"[공시] {corp_name}: {report_nm}",
                    "content": f"{corp_name}이(가) '{report_nm}' 공시를 제출했습니다. (접수번호: {rcept_no}, 접수일: {rcept_dt})",
                    "published_at": pub_dt,
                    "evidence_id": rcept_no if rcept_no else make_evidence_id("DART", str(row)),
                })

            if dart_excluded > 0:
                print(f"[TTP] {ticker} DART same-day/형식오류 공시 {dart_excluded}건 제외 (PIT-Safety)")
    except Exception as e:
        print(f"[TTP] {ticker} DART 조회 실패: {e}")  # DART API 실패 시 뉴스만으로 진행

    return docs


def analyze_news_with_llm(name: str, ticker: str, news_docs: list) -> dict:
    """
    수집된 뉴스를 Kanana-o로 요약/감성 분석.
    LLM 호출 실패 시 기본값 반환.
    """
    default = {
        "news_summary": "LLM 분석 불가",
        "news_sentiment": 0.0,
        "key_topics": [],
        "model_used": "none",
    }

    if not news_docs:
        default["news_summary"] = "수집된 뉴스 없음"
        return default

    news_entries = []
    for doc in news_docs[:10]:
        title = doc.get("title", "")
        if not title:
            continue
        desc = doc.get("description", "")
        if desc:
            news_entries.append(f"- {title}\n  요약: {desc}")
        else:
            news_entries.append(f"- {title}")

    if not news_entries:
        return default

    news_list_str = "\n".join(news_entries)
    prompt = f"""아래는 {name}({ticker})에 대한 최근 뉴스 목록이다. 각 뉴스의 제목과 기사 요약문을 참고하라.

{news_list_str}

다음을 JSON으로 답하라:
1. news_summary: 투자 관점에서 핵심 이슈 2~3문장 요약
2. news_sentiment: -1.0(극부정)~+1.0(극긍정) 감성 점수
3. key_topics: 주요 토픽 키워드 3~5개 배열

반드시 아래 형식의 JSON만 출력하라:
{{
  "news_summary": "...",
  "news_sentiment": 0.0,
  "key_topics": ["키워드1", "키워드2"]
}}"""

    try:
        messages = [
            {"role": "system", "content": "너는 한국 주식시장 전문 애널리스트다. 요청한 JSON 형식만 출력하라."},
            {"role": "user", "content": prompt},
        ]
        result = call_llm(messages, temperature=0.0, max_tokens=512)
        content = result["content"].strip()

        # JSON 파싱 (마크다운 코드블록 처리)
        if content.startswith("```"):
            content = re.sub(r"```(?:json)?\n?", "", content).strip().rstrip("```").strip()

        parsed = json.loads(content)
        return {
            "news_summary": str(parsed.get("news_summary", default["news_summary"])),
            "news_sentiment": max(-1.0, min(1.0, float(parsed.get("news_sentiment", 0.0)))),
            "key_topics": list(parsed.get("key_topics", [])),
            "model_used": result.get("model", "unknown"),
        }
    except Exception as e:
        print(f"[TTP] {ticker} 뉴스 LLM 분석 실패: {e}")
        return default


def analyze_disclosure_with_llm(name: str, ticker: str, dart_docs: list) -> dict:
    """
    수집된 DART 공시를 Kanana-o로 해석.
    LLM 호출 실패 시 기본값 반환.
    """
    default = {
        "disclosure_summary": "LLM 분석 불가",
        "disclosure_impact": "neutral",
        "risk_flags": [],
        "model_used": "none",
    }

    if not dart_docs:
        default["disclosure_summary"] = "공시 없음"
        return default

    disc_items = []
    for doc in dart_docs[:10]:
        title = doc.get("title", "")
        disc_items.append(title)

    if not disc_items:
        return default

    disc_list_str = "\n".join(f"- {t}" for t in disc_items)
    prompt = f"""아래는 {name}({ticker})의 최근 DART 공시 목록이다.

{disc_list_str}

다음을 JSON으로 답하라:
1. disclosure_summary: 투자자 관점에서 주요 공시 해석 (2~3문장)
2. disclosure_impact: "positive" / "negative" / "neutral" 중 하나
3. risk_flags: 주의 필요 키워드 배열 (예: "유상증자", "대표이사변경"). 해당 없으면 빈 배열.

반드시 아래 형식의 JSON만 출력하라:
{{
  "disclosure_summary": "...",
  "disclosure_impact": "neutral",
  "risk_flags": []
}}"""

    try:
        messages = [
            {"role": "system", "content": "너는 한국 주식시장 전문 애널리스트다. 요청한 JSON 형식만 출력하라."},
            {"role": "user", "content": prompt},
        ]
        result = call_llm(messages, temperature=0.0, max_tokens=512)
        content = result["content"].strip()

        # JSON 파싱 (마크다운 코드블록 처리)
        if content.startswith("```"):
            content = re.sub(r"```(?:json)?\n?", "", content).strip().rstrip("```").strip()

        parsed = json.loads(content)
        impact = parsed.get("disclosure_impact", "neutral")
        if impact not in ("positive", "negative", "neutral"):
            impact = "neutral"

        return {
            "disclosure_summary": str(parsed.get("disclosure_summary", default["disclosure_summary"])),
            "disclosure_impact": impact,
            "risk_flags": list(parsed.get("risk_flags", [])),
            "model_used": result.get("model", "unknown"),
        }
    except Exception as e:
        print(f"[TTP] {ticker} 공시 LLM 분석 실패: {e}")
        return default


def build_pack(ticker: str, name: str, sector: str, target_date: str) -> dict:
    """단일 종목의 TickerTextPack 생성"""
    now_str = now_kst_iso()
    snapshot_dt = make_snapshot_dt(target_date)

    # 수집
    macro_docs = collect_macro_docs(target_date)
    sector_docs = collect_sector_docs(sector, target_date)
    target_docs = collect_target_docs(name, ticker, target_date)

    # 중복 제거
    macro_docs, macro_dedup = dedup_docs(macro_docs)
    sector_docs, sector_dedup = dedup_docs(sector_docs)
    target_docs, target_dedup = dedup_docs(target_docs)

    total_before = len(macro_docs) + len(sector_docs) + len(target_docs)
    overall_dedup = 1.0  # 이미 dedup된 상태이므로

    # LLM 분석 (선택적 — API 키 없거나 실패해도 TTP 생성은 정상 동작)
    news_docs = [d for d in target_docs if d.get("source") == "naver_news"]
    dart_docs = [d for d in target_docs if d.get("source") == "dart"]

    print(f"  [TTP] {name}({ticker}) LLM 뉴스 분석 시작 (뉴스 {len(news_docs)}건)")
    llm_news_analysis = analyze_news_with_llm(name, ticker, news_docs)

    print(f"  [TTP] {name}({ticker}) LLM 공시 분석 시작 (공시 {len(dart_docs)}건)")
    llm_disclosure_analysis = analyze_disclosure_with_llm(name, ticker, dart_docs)

    pack = {
        "pack_id": f"TTP-{target_date}-{ticker}",
        "snapshot_dt": snapshot_dt,
        "artifact_version": "v1.0",
        "ticker": ticker,
        "sector_name": sector,
        "macro_docs": macro_docs,
        "sector_docs": sector_docs,
        "target_company_docs": target_docs,
        "llm_news_analysis": llm_news_analysis,
        "llm_disclosure_analysis": llm_disclosure_analysis,
        "meta": {
            "as_of_dt": snapshot_dt,
            "available_at": now_str,
            "doc_count": {
                "macro": len(macro_docs),
                "sector": len(sector_docs),
                "target": len(target_docs),
            },
            "dedup_ratio": round((macro_dedup + sector_dedup + target_dedup) / 3, 4),
        }
    }

    return pack


def build_all_packs(target_date: str, single_ticker: str = None) -> list:
    """유니버스 전체 또는 특정 종목의 TickerTextPack 생성"""
    print(f"\n{'='*60}")
    print(f"  TickerTextPack 생성: {target_date}")
    print(f"{'='*60}\n")

    universe_df = load_universe()

    if single_ticker:
        universe_df = universe_df[universe_df["ticker"].astype(str).str.zfill(6) == single_ticker]

    packs = []
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for i, (_, row) in enumerate(universe_df.iterrows()):
        ticker = str(row["ticker"]).zfill(6)
        name = row["name"]
        sector = row["wics_sector"]

        print(f"[{i+1}/{len(universe_df)}] {name}({ticker}) — {sector}")

        pack = build_pack(ticker, name, sector, target_date)
        packs.append(pack)

        # 개별 저장
        path = OUTPUT_DIR / f"TTP-{target_date}-{ticker}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(pack, f, ensure_ascii=False, indent=2)

        doc_total = pack["meta"]["doc_count"]
        print(f"  → macro:{doc_total['macro']} sector:{doc_total['sector']} target:{doc_total['target']} dedup:{pack['meta']['dedup_ratio']}")

    print(f"\n✅ 총 {len(packs)}개 TickerTextPack 생성 완료")
    return packs


def validate_packs(packs: list) -> bool:
    """schema 검증"""
    try:
        from jsonschema import validate as jvalidate
        with open(_BASE_DIR / "schemas" / "ticker_text_pack.json") as f:
            schema = json.load(f)

        fail_count = 0
        for pack in packs:
            try:
                jvalidate(instance=pack, schema=schema)
            except Exception as e:
                print(f"  ❌ {pack['pack_id']}: {e.message}")
                fail_count += 1

        if fail_count == 0:
            print(f"✅ Schema validation: {len(packs)}개 전부 PASS")
            return True
        else:
            print(f"❌ Schema validation: {fail_count}개 FAIL")
            return False
    except Exception as e:
        print(f"❌ Validation 에러: {e}")
        return False


if __name__ == "__main__":
    from connectors import now_kst
    target = sys.argv[1] if len(sys.argv) > 1 else now_kst().strftime("%Y%m%d")
    single = sys.argv[2] if len(sys.argv) > 2 else None

    packs = build_all_packs(target, single)
    validate_packs(packs)

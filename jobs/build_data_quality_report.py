"""
DataQualityReport v0
- DailyMarketPacket과 TickerTextPack의 데이터 품질 점검
- missing / stale / dedup 결과 요약

Usage:
    python jobs/build_data_quality_report.py 20260320
"""

import sys
import json
from datetime import datetime
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

from connectors import now_kst, now_kst_iso

DMP_DIR = _BASE_DIR / "artifacts" / "daily_market_packet"
TTP_DIR = _BASE_DIR / "artifacts" / "ticker_text_pack"
OUTPUT_DIR = _BASE_DIR / "reports"


def load_dmp(target_date: str) -> dict | None:
    path = DMP_DIR / f"DMP-{target_date}.json"
    if not path.exists():
        print(f"❌ DMP 파일 없음: {path}")
        return None
    with open(path) as f:
        return json.load(f)


def load_ttps(target_date: str) -> list:
    packs = []
    for path in sorted(TTP_DIR.glob(f"TTP-{target_date}-*.json")):
        with open(path) as f:
            packs.append(json.load(f))
    return packs


def check_dmp_quality(dmp: dict) -> dict:
    """DailyMarketPacket 품질 체크"""
    issues = []
    stats = {}

    tickers = dmp.get("tickers", [])
    market_data = dmp.get("market_data", {})
    news = dmp.get("news_index", [])
    disclosures = dmp.get("disclosure_index", [])
    macro = dmp.get("macro_snapshot", {})
    meta = dmp.get("meta", {})

    stats["ticker_count"] = len(tickers)
    stats["market_data_count"] = len(market_data)
    stats["news_count"] = len(news)
    stats["disclosure_count"] = len(disclosures)

    # 1. missing tickers
    missing = meta.get("data_quality", {}).get("missing_tickers", [])
    if missing:
        issues.append(f"OHLCV 수집 실패 종목: {missing}")
    stats["missing_tickers"] = missing

    # 2. stale data
    stale = meta.get("data_quality", {}).get("stale_data_tickers", [])
    if stale:
        issues.append(f"오래된 데이터 종목: {stale}")
    stats["stale_tickers"] = stale

    # 3. mktcap null 체크
    null_mktcap = [t for t, d in market_data.items() if d.get("mktcap") is None]
    if null_mktcap:
        issues.append(f"시가총액 null 종목: {len(null_mktcap)}개 (pykrx 호환 이슈)")
    stats["null_mktcap_count"] = len(null_mktcap)

    # 4. tech_features 완성도
    empty_tech = [t for t, d in market_data.items() if not d.get("tech_features")]
    if empty_tech:
        issues.append(f"기술적 지표 없는 종목: {empty_tech}")
    stats["empty_tech_count"] = len(empty_tech)

    # 5. macro snapshot 완성도
    null_macro = [k for k, v in macro.items() if v is None]
    if null_macro:
        issues.append(f"매크로 데이터 null: {null_macro}")
    stats["null_macro_fields"] = null_macro

    # 6. 뉴스 evidence_id 중복 체크 — (ticker, evidence_id) 쌍 기준
    #    같은 뉴스가 여러 종목에 등장하는 건 정상 (매크로 뉴스 등)
    #    같은 종목에 같은 evidence_id가 2번 이상이면 실제 중복
    news_pairs = [(n.get("ticker", ""), n.get("evidence_id", "")) for n in news]
    dup_news = len(news_pairs) - len(set(news_pairs))
    if dup_news > 0:
        issues.append(f"뉴스 (ticker, evidence_id) 중복: {dup_news}건")
    stats["duplicate_news"] = dup_news

    return {
        "stats": stats,
        "issues": issues,
        "pass": len(issues) == 0,
    }


def check_ttp_quality(packs: list) -> dict:
    """TickerTextPack 전체 품질 체크"""
    issues = []
    stats = {
        "pack_count": len(packs),
        "total_docs": 0,
        "avg_dedup_ratio": 0,
        "empty_packs": [],
    }

    dedup_ratios = []
    for pack in packs:
        meta = pack.get("meta", {})
        doc_count = meta.get("doc_count", {})
        total = doc_count.get("macro", 0) + doc_count.get("sector", 0) + doc_count.get("target", 0)
        stats["total_docs"] += total

        dedup = meta.get("dedup_ratio", 1.0)
        dedup_ratios.append(dedup)

        if total == 0:
            stats["empty_packs"].append(pack.get("ticker", "?"))

    if dedup_ratios:
        stats["avg_dedup_ratio"] = round(sum(dedup_ratios) / len(dedup_ratios), 4)

    if stats["empty_packs"]:
        issues.append(f"텍스트 없는 종목: {stats['empty_packs']}")

    if stats["avg_dedup_ratio"] < 0.7:
        issues.append(f"전체 평균 dedup_ratio가 낮음: {stats['avg_dedup_ratio']}")

    return {
        "stats": stats,
        "issues": issues,
        "pass": len(issues) == 0,
    }


def build_report(target_date: str) -> dict:
    """전체 DataQualityReport 생성"""
    print(f"\n{'='*60}")
    print(f"  DataQualityReport: {target_date}")
    print(f"{'='*60}\n")

    report = {
        "report_id": f"DQR-{target_date}",
        "target_date": target_date,
        "generated_at": now_kst_iso(),
        "dmp": None,
        "ttp": None,
        "overall_pass": False,
    }

    # DMP 체크
    dmp = load_dmp(target_date)
    if dmp:
        dmp_result = check_dmp_quality(dmp)
        report["dmp"] = dmp_result
        print("[DMP 품질]")
        print(f"  종목: {dmp_result['stats']['ticker_count']}개")
        print(f"  뉴스: {dmp_result['stats']['news_count']}건")
        print(f"  공시: {dmp_result['stats']['disclosure_count']}건")
        if dmp_result["issues"]:
            for issue in dmp_result["issues"]:
                print(f"  ⚠️ {issue}")
        else:
            print("  ✅ 이슈 없음")
    else:
        report["dmp"] = {"stats": {}, "issues": ["DMP 파일 없음"], "pass": False}

    print()

    # TTP 체크
    packs = load_ttps(target_date)
    if packs:
        ttp_result = check_ttp_quality(packs)
        report["ttp"] = ttp_result
        print("[TTP 품질]")
        print(f"  팩 수: {ttp_result['stats']['pack_count']}개")
        print(f"  총 문서: {ttp_result['stats']['total_docs']}건")
        print(f"  평균 dedup: {ttp_result['stats']['avg_dedup_ratio']}")
        if ttp_result["issues"]:
            for issue in ttp_result["issues"]:
                print(f"  ⚠️ {issue}")
        else:
            print("  ✅ 이슈 없음")
    else:
        report["ttp"] = {"stats": {}, "issues": ["TTP 파일 없음"], "pass": False}

    # 종합
    dmp_pass = report["dmp"].get("pass", False) if report["dmp"] else False
    ttp_pass = report["ttp"].get("pass", False) if report["ttp"] else False
    report["overall_pass"] = dmp_pass and ttp_pass

    # 저장
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"DQR-{target_date}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 리포트 저장: {path}")

    return report


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else now_kst().strftime("%Y%m%d")
    build_report(target)

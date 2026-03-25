"""
BE Handoff Payload Generator — BE 팀 대시보드용 데이터 패키징

4종 payload 생성:
1. recommendation_history.json — 종목별 추천 이력 (FDC 시계열)
2. portfolio_nav.json — 포트폴리오 순자산 추이 (PFS 시계열)
3. paper_trading_log_summary.json — paper trading 요약
4. risk_trace.json — 리스크 추적 (RC 시계열)

Usage:
    python jobs/build_be_handoff_payload.py --days 5
    python jobs/build_be_handoff_payload.py --dates 20260316 20260317 20260318 20260319 20260320
"""

import sys
import json
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

FDC_DIR = _BASE_DIR / "artifacts" / "final_decision_card"
PFS_DIR = _BASE_DIR / "artifacts" / "portfolio_state"
PTL_DIR = _BASE_DIR / "artifacts" / "paper_trading_log"
RC_DIR  = _BASE_DIR / "artifacts" / "risk_card"
OUT_DIR = _BASE_DIR / "artifacts" / "be_handoff"

KST = timezone(timedelta(hours=9))


def _now_kst() -> str:
    return datetime.now(KST).isoformat()


def _load_json(path: Path):
    """JSON 파일 로드. 없으면 None 반환."""
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _find_artifact(directory: Path, prefix: str, date: str):
    """날짜 기준으로 아티팩트 파일 탐색. 18:00 suffix 우선, 없으면 임의 suffix."""
    p = directory / f"{prefix}-{date}-180000.json"
    if p.exists():
        return _load_json(p)
    # suffix 없는 형식
    p2 = directory / f"{prefix}-{date}.json"
    if p2.exists():
        return _load_json(p2)
    # 패턴 매칭 (ex. PFS-20260320-103000.json)
    candidates = sorted(directory.glob(f"{prefix}-{date}-*.json"))
    if candidates:
        return _load_json(candidates[-1])  # 가장 마지막 파일 사용
    return None


def build_recommendation_history(dates: list) -> dict:
    """FDC 시계열 → recommendation_history payload"""
    history = []
    for date in dates:
        fdc = _find_artifact(FDC_DIR, "FDC", date)
        if fdc is None:
            print(f"[BEHandoff] FDC {date} 없음, skip")
            continue

        recommendations = []
        for d in fdc.get("decisions", []):
            recommendations.append({
                "ticker": str(d["ticker"]).zfill(6),
                "name": d.get("name", ""),
                "action": d.get("action", ""),
                "weight": d.get("weight", 0.0),
                "decision": d.get("decision", ""),
            })

        history.append({
            "date": date,
            "recommendations": recommendations,
        })

    start = dates[0] if dates else ""
    end = dates[-1] if dates else ""
    return {
        "payload_id": f"RH-{start}-{end}",
        "generated_at": _now_kst(),
        "history": history,
    }


def build_portfolio_nav(dates: list) -> dict:
    """PFS 시계열 → portfolio_nav payload"""
    nav_series = []
    for date in dates:
        pfs = _find_artifact(PFS_DIR, "PFS", date)
        if pfs is None:
            print(f"[BEHandoff] PFS {date} 없음, skip")
            continue

        nav_series.append({
            "date": date,
            "total_exposure": pfs.get("total_exposure", 0.0),
            "cash_ratio": pfs.get("cash_ratio", 0.0),
            "position_count": len(pfs.get("positions", [])),
            "daily_turnover": pfs.get("daily_turnover", 0.0),
        })

    start = dates[0] if dates else ""
    end = dates[-1] if dates else ""
    return {
        "payload_id": f"NAV-{start}-{end}",
        "generated_at": _now_kst(),
        "nav_series": nav_series,
    }


def build_paper_trading_summary(dates: list) -> dict:
    """PTL 시계열 → paper_trading_log_summary payload"""
    total_orders = 0
    executed = 0
    rejected = 0
    cancelled = 0
    total_notional = 0.0
    daily_breakdown = []

    for date in dates:
        ptl = _find_artifact(PTL_DIR, "PTL", date)
        if ptl is None:
            print(f"[BEHandoff] PTL {date} 없음, skip")
            continue

        summary = ptl.get("summary", {})
        day_orders      = summary.get("total_orders", 0)
        day_executed    = summary.get("executed_count", 0)
        day_rejected    = summary.get("rejected_count", 0)
        day_cancelled   = summary.get("cancelled_count", 0)
        day_notional    = summary.get("total_notional", 0.0)

        # 개별 주문에서 직접 집계 (summary에 cancelled_count가 없는 경우 대비)
        orders = ptl.get("orders", [])
        if not day_cancelled:
            day_cancelled = sum(1 for o in orders if o.get("status") == "cancelled")
        if not day_executed:
            day_executed = sum(1 for o in orders if o.get("status") == "executed")

        total_orders  += day_orders
        executed      += day_executed
        rejected      += day_rejected
        cancelled     += day_cancelled
        total_notional += day_notional

        daily_breakdown.append({
            "date": date,
            "total_orders": day_orders,
            "executed": day_executed,
            "rejected": day_rejected,
            "cancelled": day_cancelled,
            "total_notional": day_notional,
        })

    start = dates[0] if dates else ""
    end = dates[-1] if dates else ""
    return {
        "payload_id": f"PTS-{start}-{end}",
        "generated_at": _now_kst(),
        "trading_summary": {
            "total_orders": total_orders,
            "executed": executed,
            "rejected": rejected,
            "cancelled": cancelled,
            "total_notional": total_notional,
            "daily_breakdown": daily_breakdown,
        },
    }


def build_risk_trace(dates: list) -> dict:
    """RC 시계열 → risk_trace payload"""
    risk_trace = []
    for date in dates:
        rc = _find_artifact(RC_DIR, "RC", date)
        if rc is None:
            print(f"[BEHandoff] RC {date} 없음, skip")
            continue

        regime_info = rc.get("regime", {})
        position_risks = rc.get("position_risks", [])
        uq_enabled = any(
            pr.get("uncertainty_p85") is not None
            for pr in position_risks
        )

        risk_trace.append({
            "date": date,
            "regime": regime_info.get("label") or None,
            "vix_proxy": regime_info.get("vix_proxy"),
            "market_breadth": regime_info.get("market_breadth"),
            "position_count": len(position_risks),
            "uq_enabled": uq_enabled,
        })

    start = dates[0] if dates else ""
    end = dates[-1] if dates else ""
    return {
        "payload_id": f"RT-{start}-{end}",
        "generated_at": _now_kst(),
        "risk_trace": risk_trace,
    }


def get_recent_dates(n: int) -> list:
    """DMP 기준 최근 N거래일 날짜 추출"""
    dmp_dir = _BASE_DIR / "artifacts" / "daily_market_packet"
    dates = []
    for p in sorted(dmp_dir.glob("DMP-*.json")):
        date_str = p.stem.replace("DMP-", "")
        if len(date_str) == 8:
            dt = datetime.strptime(date_str, "%Y%m%d")
            if dt.weekday() < 5:  # 평일만
                dates.append(date_str)
    return dates[-n:]


def run_build(dates: list):
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[BEHandoff] 대상 날짜: {dates}")
    print(f"[BEHandoff] 출력 디렉토리: {OUT_DIR}")

    # payload 1: recommendation_history
    rh = build_recommendation_history(dates)
    rh_path = OUT_DIR / "recommendation_history.json"
    with open(rh_path, "w", encoding="utf-8") as f:
        json.dump(rh, f, ensure_ascii=False, indent=2)
    print(f"[BEHandoff] 저장: {rh_path} (history={len(rh['history'])}일)")

    # payload 2: portfolio_nav
    nav = build_portfolio_nav(dates)
    nav_path = OUT_DIR / "portfolio_nav.json"
    with open(nav_path, "w", encoding="utf-8") as f:
        json.dump(nav, f, ensure_ascii=False, indent=2)
    print(f"[BEHandoff] 저장: {nav_path} (nav_series={len(nav['nav_series'])}일)")

    # payload 3: paper_trading_log_summary
    pts = build_paper_trading_summary(dates)
    pts_path = OUT_DIR / "paper_trading_log_summary.json"
    with open(pts_path, "w", encoding="utf-8") as f:
        json.dump(pts, f, ensure_ascii=False, indent=2)
    total_days = len(pts["trading_summary"]["daily_breakdown"])
    print(f"[BEHandoff] 저장: {pts_path} (daily_breakdown={total_days}일)")

    # payload 4: risk_trace
    rt = build_risk_trace(dates)
    rt_path = OUT_DIR / "risk_trace.json"
    with open(rt_path, "w", encoding="utf-8") as f:
        json.dump(rt, f, ensure_ascii=False, indent=2)
    print(f"[BEHandoff] 저장: {rt_path} (risk_trace={len(rt['risk_trace'])}일)")

    print(f"[BEHandoff] 완료")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BE Handoff Payload 생성")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--days", type=int, help="최근 N거래일")
    group.add_argument("--dates", nargs="+", help="날짜 목록 (YYYYMMDD ...)")
    args = parser.parse_args()

    if args.days:
        target_dates = get_recent_dates(args.days)
        if not target_dates:
            print("[BEHandoff] 오류: DMP 아티팩트가 없습니다.")
            sys.exit(1)
    else:
        target_dates = sorted(set(args.dates))

    run_build(target_dates)

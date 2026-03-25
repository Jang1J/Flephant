"""
장중 1시간 Intraday Cycle Runner
- DailyMarketPacket(backbone) + HourlyMarketPatch(delta) → merged DMP
- PortfolioState(current) → Risk Engine → FDA → FDC → PortfolioState update

Usage:
    python jobs/run_intraday_cycle.py 20260322 1030
    python jobs/run_intraday_cycle.py 20260322 1130
    python jobs/run_intraday_cycle.py 20260322 --all   # 09:30~15:00 전체 시뮬레이션
"""

import sys
import json
import argparse
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

from connectors import now_kst, make_snapshot_dt

DMP_DIR = _BASE_DIR / "artifacts" / "daily_market_packet"
HMP_DIR = _BASE_DIR / "artifacts" / "hourly_market_patch"

# 장중 시간대 (KRX: 09:00~15:30)
INTRADAY_HOURS = ["0930", "1030", "1130", "1330", "1430"]


def merge_dmp_with_patch(dmp: dict, patch: dict) -> dict:
    """DMP backbone에 HourlyMarketPatch delta를 overlay"""
    import copy
    merged = copy.deepcopy(dmp)

    price_patch = patch.get("price_patch", {})
    for ticker, update in price_patch.items():
        if ticker in merged.get("market_data", {}):
            md = merged["market_data"][ticker]
            ohlcv = md.get("ohlcv", {})
            # 현재가를 close에 반영
            if "last" in update:
                ohlcv["close"] = update["last"]
            if "volume_intraday" in update:
                ohlcv["volume"] = update.get("volume_intraday", ohlcv.get("volume", 0))
            md["ohlcv"] = ohlcv

    # market stress 업데이트
    stress = patch.get("market_stress_update")
    if stress:
        macro = merged.get("macro_snapshot", {})
        if stress.get("vix_proxy") is not None:
            macro["vix_proxy"] = stress["vix_proxy"]
        if stress.get("market_breadth") is not None:
            macro["market_breadth"] = stress["market_breadth"]
        merged["macro_snapshot"] = macro

    return merged


def run_intraday_cycle(target_date: str, hour_str: str, use_mock_hmp: bool = True):
    """장중 1시간 사이클 1회 실행"""
    from jobs.run_risk_engine import run_risk_engine
    from jobs.portfolio_manager import PortfolioManager
    from agents.final_decision_agent import FinalDecisionAgent
    from jobs.build_hourly_patch import build_hourly_patch, save_patch

    snapshot_dt = make_snapshot_dt(target_date, int(hour_str[:2]))

    print(f"\n{'='*60}")
    print(f"  Intraday Cycle: {target_date} {hour_str}")
    print(f"{'='*60}")

    # 1. DMP backbone 로드
    dmp_path = DMP_DIR / f"DMP-{target_date}.json"
    if not dmp_path.exists():
        print(f"❌ DMP 없음: {dmp_path}")
        return None
    with open(dmp_path) as f:
        dmp = json.load(f)
    print(f"[1/6] DMP backbone 로드 ({len(dmp['tickers'])}종목)")

    # 2. HourlyMarketPatch 생성/로드
    hmp_path = HMP_DIR / f"HMP-{target_date}-{hour_str}00.json"
    if hmp_path.exists():
        with open(hmp_path) as f:
            patch = json.load(f)
        print(f"[2/6] HMP 기존 파일 사용")
    else:
        print(f"[2/6] HMP 생성 중...")
        patch = build_hourly_patch(target_date, hour_str, use_mock=use_mock_hmp)
        save_patch(patch)
    print(f"  → changed: {len(patch['changed_tickers'])}종목, evidence: {len(patch.get('new_evidence_ids', []))}건")

    # 3. DMP + HMP merge
    merged_dmp = merge_dmp_with_patch(dmp, patch)
    stress = patch.get("market_stress_update")
    if stress:
        print(f"  → stress update: vix={stress.get('vix_proxy')}, breadth={stress.get('market_breadth')}")

    # 4. PortfolioState 로드
    pm = PortfolioManager()
    # 장중에는 같은 날짜 내 최신 PFS를 찾거나, 당일 base를 로드
    pf_state = pm.load(target_date) or pm.load_or_init(target_date)
    print(f"[3/6] PortfolioState: {len(pf_state['positions'])}종목, 현금: {pf_state['cash_ratio']:.1f}%")

    # 5. Risk Engine (merged DMP 기반)
    print(f"[4/6] Risk Engine (intraday)...")

    # Risk Engine 실행 (SC auto-detect)
    from jobs.strategy_loader import load_strategy_cards, has_real_sc
    _has_real_sc = has_real_sc(target_date)
    _snapshot_hour = int(hour_str[:2])

    rc, cop = run_risk_engine(
        target_date, use_mock=not _has_real_sc,
        portfolio_state=pf_state,
        dmp_override=merged_dmp,
        snapshot_hour=_snapshot_hour,
    )

    # 6. FDA
    print(f"[5/6] Final Decision Agent...")
    strategy_cards = load_strategy_cards(target_date) if _has_real_sc else None
    fda = FinalDecisionAgent(use_llm=False)  # intraday는 deterministic
    fdc = fda.run(
        target_date=target_date,
        cop=cop,
        risk_card=rc,
        strategy_cards=strategy_cards,
        portfolio_state=pf_state,
        snapshot_hour=_snapshot_hour,
        backtest_summary={
            "status": "phase1_placeholder",
            "win_rate_hint": None,
            "baseline_delta": None,
            "failure_tags": [],
            "confidence_band": None,
            "recent_sharpe": None,
            "recent_mdd": None,
            "last_updated": None,
            "note": "Phase 1: Backtest Agent 미구현. W6에서 AI #2가 연결 예정.",
        },
    )

    # 7. PortfolioState 업데이트 (intraday version)
    print(f"[6/6] PortfolioState 업데이트...")
    pf_state = pm.apply_decisions(pf_state, fdc, cop, merged_dmp)

    # intraday PFS는 시간 구분하여 저장
    pf_state["state_id"] = f"PFS-{target_date}-{hour_str}00"
    pf_state["snapshot_dt"] = snapshot_dt
    pf_path = _BASE_DIR / "artifacts" / "portfolio_state" / f"PFS-{target_date}-{hour_str}00.json"
    with open(pf_path, "w", encoding="utf-8") as f:
        json.dump(pf_state, f, ensure_ascii=False, indent=2)

    print(f"\n  보유: {len(pf_state['positions'])}종목")
    print(f"  노출: {pf_state['total_exposure']:.1f}%, 현금: {pf_state['cash_ratio']:.1f}%")
    print(f"  turnover: {pf_state['daily_turnover']:.1f}%")
    print(f"  ✅ PFS: {pf_path}")

    return {
        "hour": hour_str,
        "regime": cop["regime"],
        "approved": fdc["execution_summary"]["approved_count"],
        "vetoed": fdc["execution_summary"]["vetoed_count"],
        "exposure": fdc["execution_summary"]["total_exposure"],
        "positions": len(pf_state["positions"]),
    }


def run_all_hours(target_date: str):
    """장중 전체 시간대 시뮬레이션"""
    print(f"\n{'='*60}")
    print(f"  장중 Intraday 시뮬레이션: {target_date}")
    print(f"  시간대: {INTRADAY_HOURS}")
    print(f"{'='*60}")

    results = {}
    for hour in INTRADAY_HOURS:
        result = run_intraday_cycle(target_date, hour)
        if result:
            results[hour] = result

    # 요약
    print(f"\n\n{'='*60}")
    print(f"  Intraday 시뮬레이션 결과 ({len(results)}시간대)")
    print(f"{'='*60}\n")

    for hour, r in results.items():
        print(
            f"  {hour} | regime={r['regime']} | "
            f"approve={r['approved']} veto={r['vetoed']} | "
            f"exposure={r['exposure']}% | positions={r['positions']}"
        )

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("date", nargs="?", default=None)
    parser.add_argument("hour", nargs="?", default=None)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    date = args.date or now_kst().strftime("%Y%m%d")

    if args.all:
        run_all_hours(date)
    elif args.hour:
        run_intraday_cycle(date, args.hour)
    else:
        print("Usage:")
        print("  python jobs/run_intraday_cycle.py 20260322 1030")
        print("  python jobs/run_intraday_cycle.py 20260322 --all")

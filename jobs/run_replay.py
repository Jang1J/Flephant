"""
연속 N거래일 Replay (Stateful)
- W3 종료 기준: 연속 5거래일 replay가 수동 수정 없이 돌아감
- DMP → StrategyCard → RiskCard → COP → FDA → FDC → PortfolioState
- PortfolioState가 날짜별로 이월되며 진입가 기반 stop-loss/turnover 추적

Usage:
    python jobs/run_replay.py --days 5
"""

import sys
import json
import argparse
from datetime import datetime
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

DMP_DIR = _BASE_DIR / "artifacts" / "daily_market_packet"
SC_DIR = _BASE_DIR / "artifacts" / "strategy_card"


def is_weekday(date_str: str) -> bool:
    """주말 여부 확인 (토=5, 일=6)"""
    return datetime.strptime(date_str, "%Y%m%d").weekday() < 5


def get_available_dates(n: int = 5) -> list:
    """backfill된 DMP 파일에서 거래일(평일) 날짜만 추출"""
    dates = []
    for p in sorted(DMP_DIR.glob("DMP-*.json")):
        date_str = p.stem.replace("DMP-", "")
        if len(date_str) == 8 and is_weekday(date_str):
            dates.append(date_str)
    return dates[-n:]


def run_replay(days: int = 5, disable_uq: bool = False):
    from jobs.run_risk_engine import run_risk_engine
    from jobs.portfolio_manager import PortfolioManager
    from agents.final_decision_agent import FinalDecisionAgent

    print(f"\n{'='*60}")
    print(f"  연속 {days}거래일 Replay (Stateful)")
    print(f"{'='*60}")

    dates = get_available_dates(days)
    if len(dates) < days:
        print(f"⚠️ 가용 날짜 {len(dates)}일 (요청: {days}일)")

    print(f"  대상: {dates}\n")

    pm = PortfolioManager()
    fda = FinalDecisionAgent(use_llm=False)  # replay에서는 deterministic
    results = {}

    for i, td in enumerate(dates):
        print(f"\n{'─'*60}")
        print(f"  [{i+1}/{len(dates)}] {td}")
        print(f"{'─'*60}")

        try:
            # DMP 로드
            dmp_path = DMP_DIR / f"DMP-{td}.json"
            with open(dmp_path) as f:
                dmp = json.load(f)

            # PortfolioState 로드 (전일 이월)
            pf_state = pm.load_or_init(td)

            # Risk Engine 실행
            has_real_sc = SC_DIR.exists() and (
                (SC_DIR / f"SC-{td}.json").exists()
                or any(SC_DIR.glob(f"SC-{td}-*.json"))
            )

            # prev_cop_path for turnover
            prev_cop_path = None
            if i > 0:
                prev_td = dates[i - 1]
                p = _BASE_DIR / "artifacts" / "candidate_order_plan" / f"COP-{prev_td}.json"
                if p.exists():
                    prev_cop_path = str(p)

            rc, cop = run_risk_engine(td, use_mock=not has_real_sc, prev_cop_path=prev_cop_path,
                                     portfolio_state=pf_state, disable_uq=disable_uq)

            # StrategyCard 로드 (real 또는 mock)
            from jobs.strategy_loader import load_strategy_cards, generate_mock_strategy_cards
            if has_real_sc:
                strategy_cards = load_strategy_cards(td)
            else:
                strategy_cards = generate_mock_strategy_cards(td, dmp)

            # FDA 실행
            fdc = fda.run(
                target_date=td,
                cop=cop,
                risk_card=rc,
                strategy_cards=strategy_cards,
                portfolio_state=pf_state,
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
                dmp=dmp,
            )

            # PortfolioState 업데이트
            pf_state = pm.apply_decisions(pf_state, fdc, cop, dmp)
            pm.save(pf_state, td)

            approved = fdc["execution_summary"]["approved_count"]
            vetoed = fdc["execution_summary"]["vetoed_count"]
            exposure = fdc["execution_summary"]["total_exposure"]

            results[td] = {
                "status": "✅",
                "regime": cop["regime"],
                "approved": approved,
                "vetoed": vetoed,
                "exposure": exposure,
                "positions": len(pf_state["positions"]),
                "turnover": pf_state["daily_turnover"],
                "realized": len(pf_state["realized_pnl"]),
            }

        except Exception as e:
            import traceback
            traceback.print_exc()
            results[td] = {"status": "❌", "error": str(e)}

    # ── 최종 리포트 ──
    print(f"\n\n{'='*60}")
    print(f"  Replay 결과 ({len(dates)}거래일)")
    print(f"{'='*60}\n")

    all_pass = True
    for td, r in results.items():
        if r["status"] == "✅":
            print(
                f"  ✅ {td} | regime={r['regime']} | "
                f"approve={r['approved']} veto={r['vetoed']} | "
                f"exposure={r['exposure']}% | "
                f"positions={r['positions']} | "
                f"turnover={r['turnover']}% | "
                f"realized={r['realized']}"
            )
        else:
            print(f"  ❌ {td} | {r.get('error', 'unknown')}")
            all_pass = False

    if all_pass:
        print(f"\n🎉 연속 {len(dates)}거래일 Stateful Replay 성공!")
    else:
        print(f"\n⚠️ 일부 날짜 실패")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--no-uq", action="store_true", help="UQ tail cap 비활성화 (ablation용)")
    args = parser.parse_args()
    run_replay(args.days, disable_uq=args.no_uq)

"""
E2E 파이프라인 공식 실행기
DailyMarketPacket → TickerTextPack → StrategyCard → RiskCard → COP → FDA → FDC → PortfolioState

real/mock SC 자동 감지:
  - artifacts/strategy_card/SC-{date}.json 또는 SC-{date}-*.json 이 존재하면 real SC 사용
  - 파일 없으면 mock SC 자동 생성 후 Risk Engine 및 FDA에 동일 cards 전달

Usage:
    python jobs/run_e2e_pipeline.py 20260320

Ablation 실행 예시:
    # UQ ON:  python jobs/run_e2e_pipeline.py 20260324
    # UQ OFF: python jobs/run_e2e_pipeline.py 20260324 --no-uq
    # Replay UQ OFF: python jobs/run_replay.py --days 5 --no-uq
"""

import sys
import json
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors import now_kst

_BASE_DIR = Path(__file__).resolve().parent.parent


def run_e2e(target_date: str, disable_uq: bool = False, use_mock: bool = False):
    print(f"\n{'='*60}")
    print(f"  E2E Pipeline: {target_date}")
    print(f"{'='*60}")

    results = {}

    # ── Step 1: DailyMarketPacket ──
    print(f"\n[Step 1/7] DailyMarketPacket 생성...")
    dmp_path = _BASE_DIR / "artifacts" / "daily_market_packet" / f"DMP-{target_date}.json"
    if dmp_path.exists():
        print(f"  → 기존 파일 사용: {dmp_path}")
    else:
        from jobs.build_daily_market_packet import build_packet, save_packet
        packet = build_packet(target_date)
        save_packet(packet, target_date)
    with open(dmp_path, encoding="utf-8") as f:
        dmp = json.load(f)
    results["dmp"] = "OK"

    # ── Step 2: TickerTextPack (샘플 3종목) ──
    # 전체 유니버스 TTP는 build_ticker_text_pack.py로 별도 실행:
    #   python jobs/build_ticker_text_pack.py YYYYMMDD
    print(f"\n[Step 2/7] TickerTextPack 생성 (샘플 3종목)...")
    sample_tickers = ["005930", "000660", "005380"]
    from jobs.build_ticker_text_pack import build_pack
    import pandas as pd
    uni = pd.read_csv(_BASE_DIR / "config" / "universe_v1.csv")

    ttp_dir = _BASE_DIR / "artifacts" / "ticker_text_pack"
    ttp_dir.mkdir(parents=True, exist_ok=True)

    for ticker in sample_tickers:
        ticker = str(ticker).zfill(6)
        ttp_path = ttp_dir / f"TTP-{target_date}-{ticker}.json"
        if ttp_path.exists():
            print(f"  → {ticker} 기존 파일 사용")
            continue
        row = uni[uni["ticker"].astype(str).str.zfill(6) == ticker].iloc[0]
        pack = build_pack(ticker, row["name"], row["wics_sector"], target_date)
        with open(ttp_path, "w", encoding="utf-8") as f:
            json.dump(pack, f, ensure_ascii=False, indent=2)
        print(f"  → {ticker} ({row['name']}) 생성 완료")
    results["ttp"] = "OK"

    # ── Step 3: PortfolioState 로드 ──
    print(f"\n[Step 3/7] PortfolioState 로드...")
    from jobs.portfolio_manager import PortfolioManager
    pm = PortfolioManager()
    pf_state = pm.load_or_init(target_date)
    prev_positions = len(pf_state.get("positions", []))
    print(f"  → 이월 포지션: {prev_positions}종목, 현금: {pf_state['cash_ratio']:.1f}%")
    results["pfs_load"] = "OK"

    # ── Step 4: Risk Engine (StrategyCard → RiskCard + COP) ──
    from jobs.run_risk_engine import run_risk_engine
    from jobs.strategy_loader import load_strategy_cards, generate_mock_strategy_cards, has_real_sc, validate_sc_pit
    if not use_mock:
        use_mock = not has_real_sc(target_date)

    # Phase 1 backtest_summary placeholder (W6에서 AI #2 Backtest Agent 연결 예정)
    backtest_summary = {
        "status": "phase1_placeholder",
        "win_rate_hint": None,
        "baseline_delta": None,
        "failure_tags": [],
        "confidence_band": None,
        "recent_sharpe": None,
        "recent_mdd": None,
        "last_updated": None,
        "note": "Phase 1: Backtest Agent 미구현. W6에서 AI #2가 연결 예정.",
    }

    print(f"\n[Step 4/7] Risk Engine ({'mock' if use_mock else 'real'} StrategyCard)...")
    risk_card, cop = run_risk_engine(
        target_date, use_mock=use_mock, portfolio_state=pf_state,
        backtest_summary=backtest_summary, disable_uq=disable_uq,
    )
    results["risk"] = "OK"

    # ── Step 5: Final Decision Agent ──
    # mock 모드에서도 Risk Engine이 내부에서 사용한 것과 동일한 SC를
    # FDA에 전달해 SC 정보(confidence, signal, rationale 등)를 활용할 수 있도록 한다.
    print(f"\n[Step 5/7] Final Decision Agent...")
    from agents.final_decision_agent import FinalDecisionAgent

    if use_mock:
        strategy_cards = generate_mock_strategy_cards(target_date, dmp)
        print(f"  → mock StrategyCard {len(strategy_cards)}개 FDA에 전달")
    else:
        strategy_cards = load_strategy_cards(target_date)
        print(f"  → real StrategyCard {len(strategy_cards)}개 FDA에 전달")
        pit_issues = validate_sc_pit(strategy_cards, target_date, dmp)
        if pit_issues:
            for issue in pit_issues:
                print(f"  [PIT WARN] {issue}")
        else:
            print(f"  → SC PIT 검증 PASS")

    fda = FinalDecisionAgent(use_llm=True)
    fdc = fda.run(
        target_date=target_date,
        cop=cop,
        risk_card=risk_card,
        strategy_cards=strategy_cards,
        portfolio_state=pf_state,
        backtest_summary=backtest_summary,
    )
    results["fdc"] = "OK"

    # ── Step 6: PortfolioState 업데이트 ──
    print(f"\n[Step 6/7] PortfolioState 업데이트...")
    pf_state = pm.apply_decisions(pf_state, fdc, cop, dmp)
    pf_path = pm.save(pf_state, target_date)
    print(f"  → 보유: {len(pf_state['positions'])}종목, 노출: {pf_state['total_exposure']:.1f}%, 현금: {pf_state['cash_ratio']:.1f}%")
    if pf_state["realized_pnl"]:
        for rp in pf_state["realized_pnl"]:
            print(f"  → 청산: {rp['ticker']} ({rp['pnl_pct']:+.2f}%) — {rp['reason']}")
    print(f"  → turnover: {pf_state['daily_turnover']:.1f}%")
    print(f"  OK PortfolioState: {pf_path}")
    results["portfolio"] = "OK"

    # ── Step 7: Schema 검증 ──
    print(f"\n[Step 7/7] 전체 Schema 검증...")
    from jsonschema import validate as jvalidate

    validations = [
        ("DMP",
         _BASE_DIR / "artifacts" / "daily_market_packet" / f"DMP-{target_date}.json",
         _BASE_DIR / "schemas" / "daily_market_packet.json"),
        ("RiskCard",
         _BASE_DIR / "artifacts" / "risk_card" / f"RC-{target_date}-180000.json",
         _BASE_DIR / "schemas" / "risk_card.json"),
        ("COP",
         _BASE_DIR / "artifacts" / "candidate_order_plan" / f"COP-{target_date}-180000.json",
         _BASE_DIR / "schemas" / "candidate_order_plan.json"),
        ("FDC",
         _BASE_DIR / "artifacts" / "final_decision_card" / f"FDC-{target_date}-180000.json",
         _BASE_DIR / "schemas" / "final_decision_card.json"),
        ("PortfolioState",
         _BASE_DIR / "artifacts" / "portfolio_state" / f"PFS-{target_date}-180000.json",
         _BASE_DIR / "schemas" / "portfolio_state.json"),
    ]

    all_pass = True
    for name, inst_path, schema_path in validations:
        try:
            with open(inst_path, encoding="utf-8") as f:
                inst = json.load(f)
            with open(schema_path, encoding="utf-8") as f:
                schema = json.load(f)
            jvalidate(instance=inst, schema=schema)
            print(f"  OK {name}")
        except Exception as e:
            msg = getattr(e, "message", str(e))
            print(f"  FAIL {name}: {msg}")
            all_pass = False

    results["validation"] = "OK" if all_pass else "FAIL"

    # ── 최종 요약 ──
    print(f"\n{'='*60}")
    print(f"  E2E Pipeline 결과")
    print(f"{'='*60}")
    for step, status in results.items():
        print(f"  {status} {step}")

    if all(v == "OK" for v in results.values()):
        print(f"\n[run_e2e_pipeline] E2E Pipeline 1회 성공!")
    else:
        print(f"\n[run_e2e_pipeline] 일부 단계 실패")

    print(f"\n생성된 artifacts:")
    print(f"  {_BASE_DIR}/artifacts/daily_market_packet/DMP-{target_date}.json")
    print(f"  {_BASE_DIR}/artifacts/ticker_text_pack/TTP-{target_date}-*.json")
    print(f"  {_BASE_DIR}/artifacts/risk_card/RC-{target_date}.json")
    print(f"  {_BASE_DIR}/artifacts/candidate_order_plan/COP-{target_date}.json")
    print(f"  {_BASE_DIR}/artifacts/final_decision_card/FDC-{target_date}.json")
    print(f"  {_BASE_DIR}/artifacts/portfolio_state/PFS-{target_date}-180000.json")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("date", nargs="?", default=now_kst().strftime("%Y%m%d"))
    parser.add_argument("--no-uq", action="store_true", help="UQ tail cap 비활성화 (ablation용)")
    parser.add_argument("--mock", action="store_true", help="mock StrategyCard 강제 사용")
    args = parser.parse_args()
    run_e2e(args.date, disable_uq=args.no_uq, use_mock=args.mock)

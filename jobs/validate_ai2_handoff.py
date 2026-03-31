"""
공식 통합 테스트: AI #2 StrategyCard Handoff 검증 Harness

- SC 파일 자동 감지 + schema validation
- 1회 E2E 실행 (real StrategyCard 사용)
- 결과 요약 출력
- JSON 리포트 저장: artifacts/validation_report/VR-{date}.json

Usage:
    python jobs/validate_ai2_handoff.py 20260322
"""

import sys
import json
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

from connectors import now_kst_iso

SC_DIR = _BASE_DIR / "artifacts" / "strategy_card"
SCHEMA_PATH = _BASE_DIR / "schemas" / "strategy_card.json"
REPORT_DIR = _BASE_DIR / "artifacts" / "validation_report"


def validate_semantic(cards: list, target_date: str) -> list:
    """SC의 의미론적 정합성을 검증한다.

    검증 항목:
      1. ticker unique
      2. ticker ∈ universe_v1.csv
      3. snapshot_dt <= target_date 18:00 KST (PIT-Safety)
      4. signal/direction 일관성
      5. confidence ∈ [0, 1]
      6. evidence_ids 비어 있지 않은지
    """
    import pandas as pd

    issues = []

    # 1. ticker unique 검증
    tickers = [str(c.get("ticker", "")).zfill(6) for c in cards]
    if len(tickers) != len(set(tickers)):
        dupes = [t for t in set(tickers) if tickers.count(t) > 1]
        issues.append(f"중복 ticker: {set(dupes)}")

    # 2. ticker ∈ universe 검증 + 26종목 coverage 체크
    universe = None
    try:
        universe = pd.read_csv(
            _BASE_DIR / "config" / "universe_v1.csv", dtype={"ticker": str}
        )
        universe["ticker"] = universe["ticker"].apply(lambda x: str(x).zfill(6))
        valid_tickers = set(universe["ticker"].tolist())
        for t in tickers:
            if t not in valid_tickers:
                issues.append(f"universe 미포함 ticker: {t}")
    except Exception as e:
        issues.append(f"universe_v1.csv 로드 실패 — ticker 검증 불가: {e}")

    # 3. snapshot_dt <= target_date 18:00 KST (PIT-Safety)
    expected_limit = (
        f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:8]}T18:00:00+09:00"
    )
    for c in cards:
        sdt = c.get("snapshot_dt", "")
        if sdt and sdt > expected_limit:
            issues.append(
                f"{c.get('ticker', '?')}: snapshot_dt({sdt}) > 허용 상한({expected_limit})"
            )

    # 4. signal/direction 일관성 검증
    for c in cards:
        sig = c.get("signal", "")
        dir_ = c.get("direction", "")
        t = c.get("ticker", "?")
        if sig in ("strong_buy", "buy") and dir_ != "long":
            issues.append(f"{t}: signal={sig} 이지만 direction={dir_} (long 이어야 함)")
        if sig == "hold" and dir_ not in ("neutral", "long"):
            issues.append(f"{t}: signal=hold 이지만 direction={dir_} (neutral 또는 long 이어야 함)")
        if sig in ("sell", "strong_sell") and dir_ not in ("neutral", "short"):
            issues.append(f"{t}: signal={sig} 이지만 direction={dir_} (neutral 또는 short 이어야 함)")

    # 5. confidence ∈ [0, 1] 검증
    for c in cards:
        conf = c.get("confidence")
        t = c.get("ticker", "?")
        if conf is not None and (conf < 0 or conf > 1):
            issues.append(f"{t}: confidence={conf} 범위 벗어남 (0~1 이어야 함)")

    # 6. evidence_ids 비어 있지 않은지 검증
    for c in cards:
        t = c.get("ticker", "?")
        if not c.get("evidence_ids"):
            issues.append(f"{t}: evidence_ids 비어 있음")

    # 7. 26종목 전체 카드 생성 여부 확인 (coverage 체크)
    if universe is not None:
        try:
            universe_tickers = set(universe["ticker"].tolist())
            card_tickers = set(str(c.get("ticker", "")).zfill(6) for c in cards)
            missing = universe_tickers - card_tickers
            if missing:
                issues.append(f"유니버스 미커버 종목 {len(missing)}개: {sorted(missing)}")
        except Exception as e:
            issues.append(f"coverage 체크 실패: {e}")

    return issues


def validate_handoff(target_date: str):
    print(f"\n{'='*60}")
    print(f"  AI #2 Handoff 검증: {target_date}")
    print(f"{'='*60}\n")

    steps = {"detect": False, "schema": False, "e2e": False}
    sc_count = 0
    summary_text = ""

    # ── 1. StrategyCard 파일 감지 ──
    print("[1/4] StrategyCard 파일 감지...")
    sc_files = []

    if not SC_DIR.exists():
        print(f"  [validate_ai2_handoff] 에러: SC 디렉토리 없음: {SC_DIR}")
        print(f"  artifacts/strategy_card/ 디렉토리를 생성하고 SC 파일을 배치하세요.")
        _save_report(target_date, steps, sc_count, "SC 디렉토리 없음")
        sys.exit(1)

    sc_single = SC_DIR / f"SC-{target_date}.json"
    if sc_single.exists():
        sc_files.append(sc_single)

    for p in sorted(SC_DIR.glob(f"SC-{target_date}-*.json")):
        sc_files.append(p)

    if not sc_files:
        print(f"  [validate_ai2_handoff] 에러: SC 파일 없음: {SC_DIR}/SC-{target_date}*.json")
        print(f"\n  AI #2에게 아래 형태로 파일을 요청하세요:")
        print(f"    - SC-{target_date}.json (단일 파일, 배열)")
        print(f"    - SC-{target_date}-005930.json (종목별 개별 파일)")
        _save_report(target_date, steps, sc_count, "SC 파일 없음")
        sys.exit(1)

    print(f"  → {len(sc_files)}개 파일 발견")
    for f in sc_files:
        print(f"    {f.name}")
    steps["detect"] = True

    # ── 2. Schema Validation ──
    print(f"\n[2/4] Schema Validation...")
    from jsonschema import validate as jvalidate

    with open(SCHEMA_PATH) as f:
        schema = json.load(f)

    cards = []
    all_valid = True
    for sc_path in sc_files:
        with open(sc_path) as f:
            data = json.load(f)

        items = data if isinstance(data, list) else [data]
        for item in items:
            try:
                jvalidate(instance=item, schema=schema)
                cards.append(item)
                print(f"  PASS {item.get('ticker', 'unknown')} — {item.get('signal', '?')} (conf={item.get('confidence', '?')})")
            except Exception as e:
                print(f"  FAIL {item.get('ticker', 'unknown')}: {e.message if hasattr(e, 'message') else e}")
                all_valid = False

    sc_count = len(cards)
    steps["schema"] = all_valid
    print(f"  → {sc_count}개 카드, {'전체 PASS' if all_valid else '일부 FAIL'}")

    if not all_valid:
        print("\n[validate_ai2_handoff] 경고: Schema 불일치 — AI #2에게 스키마 확인 요청 필요")
        print(f"   참조: {SCHEMA_PATH}")
        _save_report(target_date, steps, sc_count, "Schema FAIL")
        sys.exit(1)

    # ── 2b. SC PIT Semantic Validation ──
    print(f"\n[2b/4] SC PIT Semantic Validation...")
    from jobs.strategy_loader import validate_sc_pit
    pit_issues = validate_sc_pit(cards, target_date)
    if pit_issues:
        for issue in pit_issues:
            print(f"  WARN {issue}")
        print(f"  → PIT 검증 이슈 {len(pit_issues)}건 (E2E는 계속 진행)")
    else:
        print(f"  → PIT 검증 PASS")

    # ── 2c. SC Semantic Validation ──
    print(f"\n[2c/4] SC Semantic Validation...")
    sem_issues = validate_semantic(cards, target_date)
    if sem_issues:
        for issue in sem_issues:
            print(f"  WARN {issue}")
        print(f"  → Semantic 검증 이슈 {len(sem_issues)}건 (E2E는 계속 진행)")
    else:
        print(f"  → Semantic 검증 PASS")

    # ── 3. E2E 실행 (real StrategyCard) ──
    print(f"\n[3/4] E2E Pipeline (real StrategyCard)...")
    fdc = None
    cop = {}
    pf_state = {"positions": []}
    try:
        from jobs.run_risk_engine import run_risk_engine
        from jobs.portfolio_manager import PortfolioManager
        from agents.final_decision_agent import FinalDecisionAgent

        pm = PortfolioManager()
        pf_state = pm.load_or_init(target_date)

        rc, cop = run_risk_engine(target_date, use_mock=False, portfolio_state=pf_state)

        fda = FinalDecisionAgent(use_llm=False)
        fdc = fda.run(
            target_date=target_date,
            cop=cop,
            risk_card=rc,
            strategy_cards=cards,
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
        )

        dmp_path = _BASE_DIR / "artifacts" / "daily_market_packet" / f"DMP-{target_date}.json"
        with open(dmp_path) as f:
            dmp = json.load(f)

        pf_state = pm.apply_decisions(pf_state, fdc, cop, dmp)
        pm.save(pf_state, target_date)

        steps["e2e"] = True
    except Exception as e:
        print(f"  [validate_ai2_handoff] 에러: E2E 실패: {e}")
        import traceback
        traceback.print_exc()
        _save_report(target_date, steps, sc_count, f"E2E FAIL: {e}")
        sys.exit(1)

    # ── 4. 결과 요약 ──
    print(f"\n[4/4] 결과 요약")
    print(f"{'─'*40}")

    exec_summary = fdc.get("execution_summary", {}) if fdc else {}
    print(f"  SC 카드:   {sc_count}개")
    print(f"  승인:      {exec_summary.get('approved_count', 0)}종목")
    print(f"  거부:      {exec_summary.get('vetoed_count', 0)}종목")
    print(f"  총 노출:   {exec_summary.get('total_exposure', 0):.1f}%")
    print(f"  현금:      {exec_summary.get('cash_ratio', 0):.1f}%")
    print(f"  Regime:    {cop.get('regime', '?')}")
    print(f"  포지션:    {len(pf_state['positions'])}종목")

    buy_signals = [c for c in cards if c.get("signal") in ["strong_buy", "buy"]]
    hold_signals = [c for c in cards if c.get("signal") == "hold"]
    sell_signals = [c for c in cards if c.get("signal") in ["sell", "strong_sell"]]
    print(f"\n  Signal 분포:")
    print(f"    buy/strong_buy:   {len(buy_signals)}")
    print(f"    hold:             {len(hold_signals)}")
    print(f"    sell/strong_sell: {len(sell_signals)}")

    # ── 최종 판정 ──
    print(f"\n{'='*60}")
    all_pass = all(steps.values())
    for step, passed in steps.items():
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {step}")

    summary_text = "전체 PASS" if all_pass else "일부 FAIL"
    if all_pass:
        print(f"\n  AI #2 Handoff 검증 성공! Real StrategyCard로 E2E 통과!")
    else:
        print(f"\n  일부 단계 실패 — AI #2와 확인 필요")

    _save_report(target_date, steps, sc_count, summary_text)

    if all_pass:
        sys.exit(0)
    else:
        sys.exit(1)


def _save_report(target_date: str, steps: dict, sc_count: int, summary: str):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "target_date": target_date,
        "steps": steps,
        "sc_count": sc_count,
        "summary": summary,
        "timestamp": now_kst_iso(),
    }
    report_path = REPORT_DIR / f"VR-{target_date}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n  [validate_ai2_handoff] 리포트 저장: {report_path}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "20260322"
    validate_handoff(target)

"""
Demo Scenario Runner — 최종발표 데모 시나리오 A/B

시나리오 A: 정상 시장 (Green regime)
  - VIX proxy < 70, Market breadth > 0.45
  - 종목 추천 → 대부분 승인
  - Kanana-o가 긍정적 시장 코멘터리 생성

시나리오 B: 스트레스 시장 (Yellow/Red regime)
  - VIX proxy >= 70 (yellow) 또는 >= 90 (red)
  - tail cap 적용, 비중 축소, 일부 veto
  - Kanana-o가 경고성 리스크 내러티브 생성

Usage:
    python jobs/run_demo_scenario.py A          # 시나리오 A
    python jobs/run_demo_scenario.py B          # 시나리오 B
    python jobs/run_demo_scenario.py B-red      # 시나리오 B-red (극단 스트레스)
    python jobs/run_demo_scenario.py --both     # A, B 순차 실행
"""

import sys
import json
import copy
import argparse
from datetime import datetime
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

from connectors import now_kst_iso

DEMO_DIR = _BASE_DIR / "demo"
DEMO_DIR.mkdir(parents=True, exist_ok=True)

DMP_DIR = _BASE_DIR / "artifacts" / "daily_market_packet"


# ── DMP 오버라이드 ───────────────────────────────────────────
def load_latest_dmp() -> tuple[dict, str]:
    """가장 최근 DMP 파일 로드. (파일명 기준 정렬)"""
    dmp_files = sorted(DMP_DIR.glob("DMP-*.json"))
    if not dmp_files:
        raise FileNotFoundError(
            f"[Demo] DMP 파일 없음: {DMP_DIR}\n"
            "먼저 python jobs/build_daily_market_packet.py YYYYMMDD 실행"
        )
    latest = dmp_files[-1]
    with open(latest, encoding="utf-8") as f:
        dmp = json.load(f)
    # 날짜 파싱: DMP-YYYYMMDD.json (DMP-YYYYMMDD-HHMMSS 형태 대응)
    import re
    m = re.search(r"DMP-(\d{8})", latest.stem)
    date_str = m.group(1) if m else latest.stem.split("-")[1]
    print(f"[Demo] DMP 로드: {latest.name}")
    return dmp, date_str


def override_dmp(dmp: dict, vix_proxy: float, market_breadth: float) -> dict:
    """DMP의 macro_snapshot.vix_proxy / market_breadth를 오버라이드 (deepcopy)"""
    overridden = copy.deepcopy(dmp)
    macro = overridden.setdefault("macro_snapshot", {})
    macro["vix_proxy"] = vix_proxy
    macro["market_breadth"] = market_breadth
    return overridden


# ── LLM 코멘터리 생성 ────────────────────────────────────────
def generate_commentary(regime_label: str, approved_orders: list,
                        vetoed_count: int, total_exposure: float) -> str:
    """Kanana-o LLM 코멘터리 생성. 실패 시 fallback 메시지 반환."""
    try:
        from connectors.llm_router import call_llm

        approved_names = [
            f"{o.get('name', o['ticker'])}({o['weight']:.0f}%)"
            for o in approved_orders
        ]
        approved_str = ", ".join(approved_names) if approved_names else "없음"

        if regime_label == "green":
            prompt = (
                f"오늘 KOSPI 시장은 안정적인 흐름을 보이고 있습니다. "
                f"시장 Regime: GREEN (VIX 낮음, 시장 폭 양호). "
                f"승인된 종목: {approved_str}. 총 노출: {total_exposure:.0f}%. "
                f"긍정적 시장 환경을 반영한 2-3문장 투자 코멘터리를 작성하세요. "
                f"한국어로, 전문적이고 간결하게."
            )
        else:
            prompt = (
                f"현재 KOSPI 시장은 경계/위기 국면입니다. "
                f"시장 Regime: {regime_label.upper()} (VIX 상승, 시장 폭 축소). "
                f"승인 종목: {approved_str}, 거부 종목: {vetoed_count}개. 총 노출: {total_exposure:.0f}%. "
                f"리스크 경고를 포함한 2-3문장 시장 내러티브를 작성하세요. "
                f"한국어로, 전문적이고 간결하게."
            )

        response = call_llm([{"role": "user", "content": prompt}], max_tokens=300)
        if response and response.get("content"):
            return response["content"].strip()
    except Exception as e:
        print(f"[Demo] LLM 코멘터리 생성 실패: {e}")

    # fallback
    if regime_label == "green":
        return "오늘 KOSPI 시장은 안정적인 흐름을 보이고 있습니다. VIX 지수가 낮고 시장 폭이 양호하여 리스크 선호 심리가 우세합니다."
    elif regime_label == "yellow":
        return "시장 변동성이 확대되고 있습니다. 신규 매수 비중을 축소하고 방어적 포지셔닝을 권고합니다."
    else:
        return "시장이 극단적 스트레스 구간에 진입했습니다. 모든 신규 진입을 중단하고 현금 보유를 극대화하는 것이 안전합니다."


# ── 콘솔 출력 ───────────────────────────────────────────────
REGIME_SYMBOL = {"green": "GREEN", "yellow": "YELLOW", "red": "RED"}
BOX_WIDTH = 50


def _box_line(text: str = "", width: int = BOX_WIDTH) -> str:
    pad = width - len(text)
    return f"  {text}{' ' * max(0, pad)}  "


def print_scenario_box(scenario_label: str, regime_label: str,
                       vix_proxy: float, market_breadth: float,
                       approved_orders: list, vetoed_count: int,
                       commentary: str, explanation: str,
                       uq_cap_applied: bool):
    title = f"DEMO SCENARIO {scenario_label}: {_scenario_title(scenario_label)}"
    regime_str = REGIME_SYMBOL.get(regime_label, regime_label.upper())
    exposure = sum(o["weight"] for o in approved_orders)
    cash = 100.0 - exposure

    # 승인 종목 요약 (3개까지)
    approved_summary_parts = [
        f"{o.get('name', o['ticker'])}({o['weight']:.0f}%)"
        for o in approved_orders[:3]
    ]
    if len(approved_orders) > 3:
        approved_summary_parts.append(f"외 {len(approved_orders)-3}종목")
    approved_summary = ", ".join(approved_summary_parts) if approved_summary_parts else "없음"

    # 코멘터리 줄바꿈 (40자)
    commentary_lines = _wrap_text(commentary, 40)

    print()
    print("  " + "=" * BOX_WIDTH)
    print(f"  {title}")
    print("  " + "=" * BOX_WIDTH)
    print(f"  Regime: {regime_str}")
    print(f"  VIX Proxy: {vix_proxy:.1f} | Breadth: {market_breadth:.2f}")
    print("  " + "-" * BOX_WIDTH)
    print(f"  승인: {approved_summary}")
    print(f"  거부: {vetoed_count}종목")
    print(f"  노출: {exposure:.0f}% | 현금: {cash:.0f}%")
    if uq_cap_applied:
        print("  UQ tail cap: 적용됨")
    print("  " + "-" * BOX_WIDTH)
    print("  시장 코멘터리 (Kanana-o):")
    for line in commentary_lines:
        print(f"    {line}")
    print("  " + "-" * BOX_WIDTH)
    print("  FDC Explanation:")
    for line in _wrap_text(explanation, 40):
        print(f"    {line}")
    print("  " + "=" * BOX_WIDTH)
    print()


def _scenario_title(label: str) -> str:
    titles = {
        "A": "정상 시장 (Green)",
        "B": "스트레스 시장 (Yellow)",
        "B-red": "극단 스트레스 (Red)",
    }
    return titles.get(label, label)


def _wrap_text(text: str, width: int) -> list:
    """간단한 텍스트 줄바꿈 (공백 기준)"""
    if not text:
        return ["(없음)"]
    words = text.split()
    lines = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 <= width:
            current = (current + " " + word).strip()
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines if lines else ["(없음)"]


# ── 시나리오 실행 ────────────────────────────────────────────
def run_scenario(scenario_label: str, target_date: str, dmp: dict,
                 vix_proxy: float, market_breadth: float) -> dict:
    """
    단일 시나리오 실행.
    - DMP override → Risk Engine → FDA 순서로 실행
    - 결과를 demo/scenario_{label}_result.json에 저장

    기존 아티팩트 덮어쓰기 방지:
    - Risk Engine: dry_run=True
    - FDA: demo 전용 date key(DEMO{label}{date}) 사용 → FDC-DEMO*.json 저장
    """
    print(f"\n[Demo] 시나리오 {scenario_label} 시작 (VIX={vix_proxy}, Breadth={market_breadth})")

    # 1. DMP 오버라이드
    overridden_dmp = override_dmp(dmp, vix_proxy, market_breadth)

    # 2. PortfolioState 로드
    from jobs.portfolio_manager import PortfolioManager
    pm = PortfolioManager()
    pf_state = pm.load_or_init(target_date)

    # 3. Risk Engine (dmp_override 사용, dry_run=True로 아티팩트 덮어쓰기 방지)
    from jobs.run_risk_engine import run_risk_engine
    risk_card, cop = run_risk_engine(
        target_date,
        use_mock=True,
        portfolio_state=pf_state,
        dmp_override=overridden_dmp,
        dry_run=True,
    )

    if risk_card is None or cop is None:
        print(f"[Demo] 시나리오 {scenario_label}: Risk Engine 실패")
        return {}

    # 4. StrategyCard (mock)
    from jobs.strategy_loader import generate_mock_strategy_cards
    strategy_cards = generate_mock_strategy_cards(target_date, overridden_dmp)

    # 5. Final Decision Agent (LLM 사용)
    # demo 전용 date key를 사용하여 기존 FDC 파일 덮어쓰기 방지
    from agents.final_decision_agent import FinalDecisionAgent
    backtest_summary = {
        "status": "phase1_placeholder",
        "win_rate_hint": None,
        "baseline_delta": None,
        "failure_tags": [],
        "confidence_band": None,
        "recent_sharpe": None,
        "recent_mdd": None,
        "last_updated": None,
        "note": "Phase 1: Backtest Agent 미구현.",
    }

    demo_label = scenario_label.replace("-", "")  # e.g. "Bred"

    fda = FinalDecisionAgent(use_llm=True)
    fdc = fda.run(
        target_date=target_date,  # 실제 날짜 전달 → snapshot_dt 정상 생성
        cop=cop,
        risk_card=risk_card,
        strategy_cards=strategy_cards,
        portfolio_state=pf_state,
        backtest_summary=backtest_summary,
    )

    # FDC 파일을 demo 전용 경로로 이동 (기존 아티팩트 덮어쓰기 방지)
    import shutil
    fdc_src = _BASE_DIR / "artifacts" / "final_decision_card" / f"FDC-{target_date}.json"
    fdc_demo_path = DEMO_DIR / f"FDC-DEMO{demo_label}-{target_date}.json"
    if fdc_src.exists():
        shutil.move(str(fdc_src), str(fdc_demo_path))
        print(f"[Demo] FDC 이동: {fdc_demo_path.name}")

    # 6. 결과 추출
    regime_label = cop.get("regime", "green")
    approved_orders = cop.get("orders", [])
    exec_summary = fdc.get("execution_summary", {})
    vetoed_count = exec_summary.get("vetoed_count", 0)
    total_exposure = exec_summary.get("total_exposure", 0)
    explanation = fdc.get("execution_summary", {}).get("explanation", "")

    # UQ tail cap 적용 여부 확인 (RC conflict 기반)
    uq_cap_applied = any(
        c.get("type") == "cap_applied"
        for c in fdc.get("conflicts", [])
    )

    # 7. LLM 코멘터리 생성
    commentary = generate_commentary(
        regime_label, approved_orders, vetoed_count, total_exposure
    )

    # 8. 콘솔 출력
    print_scenario_box(
        scenario_label=scenario_label,
        regime_label=regime_label,
        vix_proxy=vix_proxy,
        market_breadth=market_breadth,
        approved_orders=approved_orders,
        vetoed_count=vetoed_count,
        commentary=commentary,
        explanation=explanation,
        uq_cap_applied=uq_cap_applied,
    )

    # 9. 결과 저장 (demo/ 디렉토리 전용)
    result = {
        "scenario": scenario_label,
        "generated_at": now_kst_iso(),
        "target_date": target_date,
        "override": {
            "vix_proxy": vix_proxy,
            "market_breadth": market_breadth,
        },
        "regime": regime_label,
        "approved_orders": approved_orders,
        "vetoed_count": vetoed_count,
        "total_exposure": total_exposure,
        "cash_ratio": 100.0 - total_exposure,
        "uq_cap_applied": uq_cap_applied,
        "commentary": commentary,
        "fdc_explanation": explanation,
        "fdc": fdc,
        "cop": cop,
        "risk_card": risk_card,
    }

    safe_label = scenario_label.replace("-", "_")
    out_path = DEMO_DIR / f"scenario_{safe_label}_result.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[Demo] 결과 저장: {out_path}")

    return result


# ── 시나리오 설정 ────────────────────────────────────────────
SCENARIO_CONFIG = {
    "A": {"vix_proxy": 50.0, "market_breadth": 0.65},
    "B": {"vix_proxy": 85.0, "market_breadth": 0.35},
    "B-red": {"vix_proxy": 95.0, "market_breadth": 0.25},
}


def main():
    parser = argparse.ArgumentParser(
        description="Elephant Lab 데모 시나리오 A/B 실행기"
    )
    parser.add_argument(
        "scenario",
        nargs="?",
        choices=["A", "B", "B-red"],
        help="실행할 시나리오 (A / B / B-red)",
    )
    parser.add_argument(
        "--both",
        action="store_true",
        help="A, B 순차 실행",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="대상 날짜 YYYYMMDD (기본: 가장 최근 DMP)",
    )
    args = parser.parse_args()

    if not args.scenario and not args.both:
        parser.print_help()
        sys.exit(1)

    # DMP 로드
    dmp, auto_date = load_latest_dmp()
    target_date = args.date or auto_date
    print(f"[Demo] 대상 날짜: {target_date}")

    # 실행할 시나리오 목록
    if args.both:
        scenarios_to_run = ["A", "B"]
    else:
        scenarios_to_run = [args.scenario]

    results = {}
    for label in scenarios_to_run:
        cfg = SCENARIO_CONFIG[label]
        result = run_scenario(
            scenario_label=label,
            target_date=target_date,
            dmp=dmp,
            vix_proxy=cfg["vix_proxy"],
            market_breadth=cfg["market_breadth"],
        )
        results[label] = result

    print(f"\n[Demo] 완료. 결과 파일 위치: {DEMO_DIR}/")
    for label in scenarios_to_run:
        safe_label = label.replace("-", "_")
        print(f"  scenario_{safe_label}_result.json")


if __name__ == "__main__":
    main()

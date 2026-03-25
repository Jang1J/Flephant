"""
중간발표 시연용 Sample Cards 3세트 생성
- Green regime (정상 시장)
- Yellow regime (경계 시장)
- Red regime (위기 시장)

각 세트: DMP요약 → StrategyCard → RiskCard → COP → FDC

Usage:
    python demo/generate_demo_cards.py
"""

import json
from pathlib import Path
from datetime import datetime

DEMO_DIR = Path("demo/sample_cards")


def make_green_scenario():
    """시나리오 1: Green regime — 정상 시장, 3종목 승인"""
    return {
        "scenario": "green_normal",
        "title": "🟢 정상 시장 — 3종목 매수 승인",
        "description": "VIX 낮고 시장 폭 넓음. 반도체/자동차/금융 섹터 분산 투자.",
        "regime": {
            "label": "green",
            "vix_proxy": 15.2,
            "market_breadth": 0.62,
            "regime_reason": "VIX proxy=15.2 → green; Market breadth=0.62 → green",
        },
        "strategy_cards": [
            {"ticker": "005930", "name": "삼성전자", "signal": "strong_buy", "confidence": 0.87, "pre_risk_score": 0.72, "news_signal": 0.65, "rationale": "HBM 수주 확대 + 반도체 업황 개선 기대"},
            {"ticker": "005380", "name": "현대차", "signal": "buy", "confidence": 0.71, "pre_risk_score": 0.45, "news_signal": 0.30, "rationale": "전기차 글로벌 판매 호조 + 환율 효과"},
            {"ticker": "105560", "name": "KB금융", "signal": "buy", "confidence": 0.65, "pre_risk_score": 0.38, "news_signal": 0.20, "rationale": "금리 안정화 기대 + 배당 매력"},
        ],
        "risk_card": {
            "tier1_pass": True,
            "position_risks": [
                {"ticker": "005930", "risk_flag": "pass", "approved_weight": 20.0, "reason": "정상 통과"},
                {"ticker": "005380", "risk_flag": "pass", "approved_weight": 20.0, "reason": "정상 통과"},
                {"ticker": "105560", "risk_flag": "pass", "approved_weight": 20.0, "reason": "정상 통과"},
            ],
        },
        "candidate_order_plan": {
            "orders": [
                {"ticker": "005930", "name": "삼성전자", "action": "buy", "weight": 20.0, "confidence": 0.87},
                {"ticker": "005380", "name": "현대차", "action": "buy", "weight": 20.0, "confidence": 0.71},
                {"ticker": "105560", "name": "KB금융", "action": "buy", "weight": 20.0, "confidence": 0.65},
            ],
            "portfolio_summary": {"total_exposure": 60.0, "cash_ratio": 40.0, "position_count": 3},
        },
        "final_decision": {
            "decisions": [
                {"ticker": "005930", "name": "삼성전자", "decision": "approve", "weight": 20.0, "veto_reason": None},
                {"ticker": "005380", "name": "현대차", "decision": "approve", "weight": 20.0, "veto_reason": None},
                {"ticker": "105560", "name": "KB금융", "decision": "approve", "weight": 20.0, "veto_reason": None},
            ],
            "explanation": "시장 환경이 안정적이고(green regime), 3종목 모두 confidence > 0.6으로 충분합니다. 섹터 분산(반도체/자동차/금융)이 확보되어 있어 집중 리스크가 낮습니다. 총 노출 60%, 현금 40%로 보수적 포지셔닝 유지합니다.",
        },
        "audit_log": [
            {"rule": "regime_gate", "action": "pass", "reason": "Green regime — 정상 투자 가능"},
            {"rule": "uq_tail_cap", "action": "skip", "reason": "Phase 1 비활성화"},
        ],
    }


def make_yellow_scenario():
    """시나리오 2: Yellow regime — 경계 시장, 비중 축소"""
    return {
        "scenario": "yellow_caution",
        "title": "🟡 경계 시장 — 비중 50% 축소 적용",
        "description": "VIX 상승 중. 신규 매수 비중을 50%로 축소.",
        "regime": {
            "label": "yellow",
            "vix_proxy": 24.5,
            "market_breadth": 0.38,
            "regime_reason": "VIX proxy=24.5 → yellow; Market breadth=0.38 → yellow",
        },
        "strategy_cards": [
            {"ticker": "000660", "name": "SK하이닉스", "signal": "strong_buy", "confidence": 0.91, "pre_risk_score": 0.80, "news_signal": 0.70, "rationale": "HBM3E 양산 기대감 + 엔비디아 수주"},
            {"ticker": "035420", "name": "NAVER", "signal": "buy", "confidence": 0.68, "pre_risk_score": 0.35, "news_signal": 0.25, "rationale": "AI 검색 도입 + 광고 매출 회복"},
            {"ticker": "009150", "name": "삼성전기", "signal": "buy", "confidence": 0.55, "pre_risk_score": 0.20, "news_signal": 0.10, "rationale": "MLCC 수요 회복 기대"},
        ],
        "risk_card": {
            "tier1_pass": True,
            "position_risks": [
                {"ticker": "000660", "risk_flag": "pass", "approved_weight": 10.0, "reason": "yellow regime: 비중 50% 축소 (20→10%)"},
                {"ticker": "035420", "risk_flag": "pass", "approved_weight": 10.0, "reason": "yellow regime: 비중 50% 축소 (20→10%)"},
                {"ticker": "009150", "risk_flag": "reject", "approved_weight": 0, "reason": "confidence 0.55 → yellow regime에서 min_confidence 미달"},
            ],
        },
        "candidate_order_plan": {
            "orders": [
                {"ticker": "000660", "name": "SK하이닉스", "action": "buy", "weight": 10.0, "confidence": 0.91},
                {"ticker": "035420", "name": "NAVER", "action": "buy", "weight": 10.0, "confidence": 0.68},
            ],
            "portfolio_summary": {"total_exposure": 20.0, "cash_ratio": 80.0, "position_count": 2},
        },
        "final_decision": {
            "decisions": [
                {"ticker": "000660", "name": "SK하이닉스", "decision": "approve", "weight": 10.0, "veto_reason": None},
                {"ticker": "035420", "name": "NAVER", "decision": "approve", "weight": 10.0, "veto_reason": None},
            ],
            "explanation": "시장이 경계 국면(yellow regime)이므로 신규 매수 비중을 50% 축소했습니다. SK하이닉스는 HBM 수주 모멘텀이 강해 축소된 비중으로도 진입 가치가 있습니다. 삼성전기는 confidence가 낮아 yellow regime에서는 진입하지 않습니다. 총 노출 20%, 현금 80%로 방어적 포지셔닝입니다.",
        },
        "audit_log": [
            {"rule": "regime_gate", "action": "yellow", "reason": "Yellow regime — 신규 매수 비중 ×0.5"},
            {"rule": "min_confidence", "ticker": "009150", "action": "reject", "reason": "confidence 0.55 < 0.6 (yellow 강화 기준)"},
            {"rule": "uq_tail_cap", "action": "skip", "reason": "Phase 1 비활성화"},
        ],
    }


def make_red_scenario():
    """시나리오 3: Red regime — 위기 시장, 신규 진입 전면 금지"""
    return {
        "scenario": "red_crisis",
        "title": "🔴 위기 시장 — 신규 진입 전면 금지",
        "description": "VIX 급등, 시장 폭 급락. 모든 신규 매수 차단.",
        "regime": {
            "label": "red",
            "vix_proxy": 35.8,
            "market_breadth": 0.22,
            "regime_reason": "VIX proxy=35.8 → red; Market breadth=0.22 → red",
        },
        "strategy_cards": [
            {"ticker": "005930", "name": "삼성전자", "signal": "strong_buy", "confidence": 0.92, "pre_risk_score": 0.85, "news_signal": 0.75, "rationale": "저가 매수 기회"},
            {"ticker": "000660", "name": "SK하이닉스", "signal": "buy", "confidence": 0.78, "pre_risk_score": 0.60, "news_signal": 0.50, "rationale": "HBM 모멘텀 유지"},
        ],
        "risk_card": {
            "tier1_pass": False,
            "position_risks": [
                {"ticker": "005930", "risk_flag": "reject", "approved_weight": 0, "reason": "Regime RED — 신규 진입 금지"},
                {"ticker": "000660", "risk_flag": "reject", "approved_weight": 0, "reason": "Regime RED — 신규 진입 금지"},
            ],
        },
        "candidate_order_plan": {
            "orders": [],
            "portfolio_summary": {"total_exposure": 0.0, "cash_ratio": 100.0, "position_count": 0},
        },
        "final_decision": {
            "decisions": [],
            "explanation": "시장이 위기 국면(red regime)입니다. VIX proxy가 35.8로 30 이상이며, 시장 폭(market breadth)이 0.22로 극도로 좁습니다. Strategy Agent가 삼성전자(confidence 0.92)를 strong_buy로 추천했지만, Risk Agent의 Tier 1 Regime Gate가 모든 신규 진입을 차단합니다. 전략의 신호 품질과 무관하게, 시장 전체 스트레스가 과도하여 '지금은 들어가지 않는 것이 옳다'고 판단합니다. 전량 현금 보유합니다.",
        },
        "audit_log": [
            {"rule": "regime_gate", "action": "block_all", "reason": "Red regime — 신규 진입 전면 금지"},
            {"rule": "regime_override", "ticker": "005930", "action": "reject", "reason": "confidence 0.92에도 regime red로 차단"},
            {"rule": "regime_override", "ticker": "000660", "action": "reject", "reason": "confidence 0.78에도 regime red로 차단"},
        ],
    }


def generate_all():
    """3개 시나리오 JSON 생성"""
    DEMO_DIR.mkdir(parents=True, exist_ok=True)

    scenarios = [
        make_green_scenario(),
        make_yellow_scenario(),
        make_red_scenario(),
    ]

    for sc in scenarios:
        path = DEMO_DIR / f"{sc['scenario']}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sc, f, ensure_ascii=False, indent=2)
        print(f"✅ {sc['title']} → {path}")

    # 요약 markdown
    summary = f"""# 중간발표 시연 시나리오

> 생성일: {datetime.now().strftime("%Y-%m-%d %H:%M")}

## 시나리오 구성

| # | 시나리오 | Regime | 결과 | 파일 |
|---|---------|--------|------|------|
| 1 | 정상 시장 | 🟢 Green | 3종목 승인, 60% 노출 | green_normal.json |
| 2 | 경계 시장 | 🟡 Yellow | 2종목 축소 승인, 20% 노출 | yellow_caution.json |
| 3 | 위기 시장 | 🔴 Red | 전면 차단, 100% 현금 | red_crisis.json |

## 핵심 메시지

1. **Regime Gate가 시장 전체 스트레스를 먼저 판단**한다
   - Strategy의 신호 품질이 아무리 좋아도, 시장이 위험하면 진입하지 않음
   - 이것이 "전략의 자신감"과 "시장의 안전성"을 분리하는 2-tier Risk 구조

2. **Final Decision은 approve/veto만 한다**
   - 비중을 재계산하지 않음 (can_change_weight = false)
   - Risk가 만든 CandidateOrderPlan을 그대로 승인하거나 거부

3. **한국어 explanation이 투자 근거를 설명**한다
   - 왜 이 종목을 샀는지, 왜 이 종목을 안 샀는지
   - 사람이 읽을 수 있는 형태로 의사결정 과정을 투명하게 공개
"""

    with open(DEMO_DIR / "README.md", "w", encoding="utf-8") as f:
        f.write(summary)

    print(f"\n✅ 시연 시나리오 3세트 + README 생성 완료: {DEMO_DIR}/")


if __name__ == "__main__":
    generate_all()

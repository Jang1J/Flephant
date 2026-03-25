# 중간발표 시연 시나리오

> 생성일: 2026-03-22 19:04

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

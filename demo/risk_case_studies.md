# 발표용 리스크 사례 정리

## 사례 1: Regime Gate 발동 — Red에서 고신뢰 종목도 차단

**상황**: VIX proxy = 35.8, Market breadth = 0.22
**Strategy 추천**: 삼성전자 strong_buy (confidence 0.92)

**Risk Agent 판단**:
```
Tier 1: Regime = RED → 신규 진입 전면 금지
→ 삼성전자 confidence 0.92에도 불구하고 차단
```

**왜 이렇게 설계했나?**
- 전략의 "종목 수준 자신감"과 시장의 "체계적 스트레스"는 다른 차원
- 2008년 금융위기, 2020년 코로나 초기에 개별 종목 신호는 좋았지만 시장 전체가 무너진 사례
- "지금은 들어가지 않는 것이 옳다"는 판단을 자동화

**RiskAuditLog 기록**:
```json
{"rule": "regime_gate", "action": "block_all", "reason": "Red regime — 신규 진입 전면 금지"}
{"rule": "regime_override", "ticker": "005930", "action": "reject", "reason": "confidence 0.92에도 regime red로 차단"}
```

---

## 사례 2: Sector Cap 발동 — 반도체 과집중 방지

**상황**: Green regime, 반도체 3종목 모두 strong_buy

**Strategy 추천**:
- 삼성전자 (confidence 0.87) → 반도체
- SK하이닉스 (confidence 0.91) → 반도체
- 삼성전기 (confidence 0.72) → 반도체 부품

**Risk Agent 판단**:
```
삼성전자: 20% 승인 (반도체 누적 20%)
SK하이닉스: 20% 승인 (반도체 누적 40% = max_sector_weight)
삼성전기: 0% 거부 (반도체 40% 한도 초과)
```

**왜 이렇게 설계했나?**
- 단일 섹터 40% 상한 → 섹터 쇼크 시 포트폴리오 피해 제한
- AI가 특정 업종에 과도하게 쏠리는 것을 구조적으로 방지
- cross-sectional ranking의 의미를 살리기 위한 분산 강제

---

## 사례 3: Stop-Loss 발동 — 급락 종목 자동 제거

**상황**: 특정 종목의 5일 수익률 = -6.2%

**Risk Agent 판단**:
```
stop-loss threshold: -5%
5일 수익률 -6.2% <= -5% → 강제 매도 (reject)
```

**RiskAuditLog 기록**:
```json
{"rule": "stop_loss", "ticker": "XXX", "action": "reject", "reason": "5일 수익률 -6.20% <= stop-loss -5%"}
```

**왜 이렇게 설계했나?**
- 손실이 확대되기 전에 자동으로 포지션 정리
- 감정적 판단 배제 — "더 오를 거야"라는 희망적 사고 차단
- FinPos 논문의 position-level risk management 구현

---

## 사례 4: Yellow Regime — 비중 50% 축소

**상황**: VIX proxy = 24.5 (yellow 구간)

**Risk Agent 판단**:
```
Yellow → new_entry_weight_multiplier = 0.5
원래 20% 비중 → 10%로 축소
```

**효과**:
- 시장 불확실성 높을 때 자동으로 보수적 운용
- 완전 차단(red)과 자유 투자(green) 사이의 중간 단계
- 총 노출이 자연스럽게 줄어들어 현금 비중 증가

---

## 사례 5: UQ Tail Cap — 불확실한 종목 비중 제한 (Phase 2)

**상황**: UQ 모델이 특정 종목의 uncertainty_score = 0.42 (P85 = 0.36 초과)

**Risk Agent 판단** (Phase 2 활성화 시):
```
uncertainty P85 초과 → 비중 상한 5%로 제한
원래 20% → 5%
```

**왜 이렇게 설계했나?**
- 전략이 "매수"라고 해도 과거 실행 결과가 불안정했던 패턴이면 비중을 줄임
- "이 종목은 맞출 때도 있지만, 틀릴 때 크게 틀린다" → 비중 자체를 낮춤
- Execution Uncertainty Proxy 논문의 핵심 아이디어

# 교수님 미팅용 1페이지 요약 — Flephant vNext

> 기준일: 2026-03-29 | 작성: AI #1 (장재원)

---

## 20초 버전

KOSPI 대형주 대상 멀티에이전트 트레이딩 알고리즘 위에, 전날 밤 해외 충격이 한국 장 개장에 어떻게 번역되는지를 보여주는 K-OPEN Pulse 서비스와 사용자 성향에 따라 서로 다른 전략을 연결해주는 Preference Resolver를 추가합니다. 알고리즘 엔진은 그대로 두고, 서비스 레이어에서 사용자가 직접 해석해야 했던 개장 전 정보를 구조화하고 성향별로 분기합니다. 리스크 정책은 성향에 무관하게 동일하게 적용됩니다.

---

## Before / After 구조 비교

| 항목 | Before (v11) | After (v12) |
|------|-------------|-------------|
| 서비스 명칭 | KOSPI 기반 모의 자동매매 서비스 | K-OPEN Pulse 기반 개장 해석형 서비스 |
| 개장 전 정보 제공 | 없음 (사용자가 직접 해석) | K-OPEN Pulse — 해외 충격 + 수급 번역 브리핑 |
| 전략 분기 | 단일 전략 (모든 사용자 동일) | 성향별 분기 (momentum / rebound) |
| 사용자 설문 | 없음 | 설문 5축 → style_score → profile 라우팅 |
| 설문과 모델의 관계 | 해당 없음 | 설문은 라우터. 모델 feature 아님 |
| 데이터 계층 | DMP + HMP | DMP + HMP + K-SHIFT 4개 Patch |
| 리스크 정책 | 고정 | 고정 (성향에 무관하게 동일 적용) |

---

## K-OPEN Pulse 4 Patch

| Patch 이름 | 코드명 | Cadence | 우선순위 | 내용 요약 |
|-----------|-------|---------|---------|----------|
| InvestorFlowPatch | IFP | daily (D+0 18:00) | P0 | 외국인/기관/개인 순매수 + 동조 이탈 지수(divergence_score) |
| OvernightSpilloverPatch | OSP | daily (D+0 07:00) | P0 | 나스닥/S&P500 등락 + 환율 변화 + 선물 → 개장 충격 등급(spillover_grade: LOW/MODERATE/HIGH/SEVERE) |
| RetailThemeGraph | RTG | hourly (장중) | P1 | 종토방 급증 종목 + 뉴스 테마 클러스터 + 테마 간 전염 그래프 |
| OpenTrapRiskPatch | OTP | hourly (장중 개장 후) | P1 | 갭상승 종목 + VI 발동 이력 + 가짜 테마 추격 위험도(hype_trap_score) |

모든 patch: standalone artifact 저장 + DMP/HMP summary 필드 이중 구조.
내부 엔진 공식 코드명: K-SHIFT (Korean Shock-Hype-Investor-Flow Translator)

---

## Preference Resolver 요약

```
사용자 설문 (1회)
  ├── 추세추종 선호      (0~10)
  ├── 반등포착 선호      (0~10)
  ├── 잦은 매매 허용도   (0~10)
  ├── 손실 회피          (0~10)
  └── 설명 선호          (0~10)
         ↓
    style_score 계산
         ↓
  >= 5.5 → momentum StrategyCard serve
  <  4.5 → rebound StrategyCard serve
  중간   → system_recommended (당일 시장 regime 참조)
         ↓
  UserPreferenceProfile artifact 생성
  (설문값은 LightGBM/CNN feature로 삽입 안 함)
```

두 전략의 StrategyCard는 매일 미리 생성해둔다. Resolver는 사용자별로 어떤 SC를 serve할지 결정할 뿐이다. 전략 생성 비용은 사용자 수에 무관하게 1회만 발생한다.

---

## 이번 학기 P0 / P1 / P2 구분

| 구분 | 항목 | 주차 |
|------|------|------|
| **P0** | K-SHIFT IFP + OSP 생산 + DMP kopen_summary 연동 | W5 |
| **P0** | OSP spillover_grade → RiskEngine Tier 1 regime gate 연결 | W6 |
| **P0** | Real SC integration + 5일 replay | W5 |
| **P0** | Backtest 1회 + FINAL 1회 + baseline 6종 | W6 |
| **P1** | RTG + OTP 장중 hourly patch | W6~W7 |
| **P1** | Preference Resolver + UserPreferenceProfile + serve 분기 | W7 |
| **P1** | KOPEN feature → LightGBM 추가 + SC v2 | W8 |
| **P1** | with/without KOPEN ablation | W10 |
| **P2** | RetailThemeGraph 전염 그래프 시각화 | W10 이후 |
| **P2** | Strategy Health Monitor (신호 안정성 모니터링) | W10 이후 |

---

## 하지 않을 것

| 금지 항목 | 이유 |
|---------|------|
| Risk personalization | 사용자 성향에 따라 stop-loss / position cap 변경 안 함. 리스크 정책 일관성 유지 |
| 설문을 model feature에 삽입 | 학습/검증 일관성 파괴. Preference Resolver 설계 원칙 위반 |
| HFT pivot | 1분 이하 초고빈도 거래는 이번 학기 범위 밖 |
| 실계좌 자동매매 | 모의투자 gateway까지만 |
| RL allocator | 강화학습 기반 비중 최적화는 범위 밖 |
| full LoRA fine-tuning | Kanana-o는 설명/해석 용도로만 사용 |
| Similar Case Retriever full 구현 | 범위 밖 |

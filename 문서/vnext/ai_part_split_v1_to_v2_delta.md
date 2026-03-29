# AI 파트 분배안 v1 → v2 수정표 (Delta)

> 기준일: 2026-03-29
> 작성: AI #1 (장재원)
> 원칙: v1 원본은 보존. 본 문서는 delta만 기록한다.
> v1 원본 경로: 문서/3주차/AI_파트_분배_v1.md

---

## 수정 철학

v2는 v1의 역할 분담 구조(교차 의존형 협업 구조)를 그대로 유지한다.
달라지는 것은 K-OPEN Pulse / K-SHIFT / Preference Resolver의 등장으로 인해 AI #1과 AI #2 각각에 추가되는 구체적 역할과 artifact다.

---

## 섹션별 Delta

---

### 0. 왜 이렇게 나누는가 (역할 나눔 근거)

**현재 (v1) 핵심:**
5-agent 파이프라인(Data → Strategy → Risk → Backtest → Final Decision)에서 AI #1이 입구(Data)와 출구(Risk → FDA)를 담당하고 AI #2가 중간 엔진(Strategy → Backtest)을 담당한다.

**수정 방향:**
K-OPEN Pulse와 Preference Resolver를 AI #1의 서비스 레이어 책임으로 명시. 이 구조가 기존 역할 나눔의 자연스러운 확장임을 설명.

**수정 후 추가 단락:**
v2에서는 K-SHIFT 엔진(4개 patch)을 Data Agent의 확장으로 AI #1이 담당하고, Preference Resolver(사용자 성향 → 전략 profile 라우팅)도 서비스 레이어와 가장 가까운 AI #1이 담당한다. AI #2는 전략 producer 역할을 확장하여 KOPEN feature를 전략 신호에 흡수하고 with/without KOPEN ablation을 수행한다.

---

### 1. 역할 분담

**AI #1 추가 역할 (v2 신규):**

| 추가 역할 | 세부 내용 |
|----------|----------|
| K-OPEN Pulse Producer | K-SHIFT 엔진 구현: IFP, OSP, RTG, OTP 4개 patch 생산 |
| Preference Resolver Spec | preference_resolver.yaml 설계, UserPreferenceProfile artifact 정의 |
| OpenTrap / Flow / Overnight Risk | OpenTrapRiskPatch를 RiskEngine에 연결. 갭상승/VI 발동 종목 자동 veto 로직 구현 |
| serve_strategy_for_user.py | 사용자 profile에 따라 적합한 StrategyCard를 serve하는 스크립트 구현 |

**AI #2 추가 역할 (v2 신규):**

| 추가 역할 | 세부 내용 |
|----------|----------|
| KOPEN Feature Consumer | IFP/OSP를 LightGBM feature로 흡수: `foreign_net_buy_rank`, `overnight_spillover_grade` 등 |
| With/Without KOPEN Ablation | KOPEN feature 포함 SC vs 미포함 SC 성과 비교 (W10 ablation에 추가) |
| Strategy Health Monitor | momentum / rebound profile별 SC의 일별 신호 안정성 모니터링 |

---

### 2. Producer / Consumer 관계

**핵심 인터페이스 변경 없음.** (DMP → SC → RiskCard → FDC 흐름 유지)

**신규 artifact 추가 (v2):**

| Artifact | Producer | Consumer | 설명 |
|----------|----------|----------|------|
| InvestorFlowPatch | AI #1 | AI #2 (KOPEN feature), RiskEngine | 외국인/기관/개인 순매수 + 동조 이탈 |
| OvernightSpilloverPatch | AI #1 | AI #2 (KOPEN feature), FDA (개장 내러티브) | 미국장/환율/선물 충격 번역 |
| RetailThemeGraph | AI #1 | OpenTrapRiskPatch producer | 종토방/뉴스 테마 전염 그래프 |
| OpenTrapRiskPatch | AI #1 | RiskEngine (veto), FDA (경보) | 갭상승/VI/가짜테마 추격 위험 |
| UserPreferenceProfile | BE/서비스 레이어 입력 → AI #1 resolve | Service Layer, serve_strategy_for_user | 사용자 설문 → profile 라우팅 결과 |

---

### 5. W5~W11 상세 계획

**W5~W7 추가 항목 (AI #1):**
- K-SHIFT 엔진 4개 patch 구현 및 DMP kopen_summary 연동 (W5, P0 patch 우선)
- preference_resolver.yaml 작성 + UserPreferenceProfile artifact 정의 (W5)
- OpenTrapRiskPatch → RiskEngine 연동 + 갭상승/VI veto 로직 (W6)
- serve_strategy_for_user.py 구현 (W7)

**W8 추가 항목 (AI #2):**
- KOPEN feature bank: `foreign_net_buy_rank`, `overnight_spillover_grade`, `divergence_score`, `hype_trap_score` 를 LightGBM feature에 추가
- KOPEN feature 포함 SC v2와 미포함 SC v1 비교 실험 준비

**W10 ablation 추가 항목:**

| # | 실험 | 비교 대상 | 근거 |
|---|------|----------|------|
| 5 | with-KOPEN vs no-KOPEN | KOPEN feature 포함 vs 제외 | K-SHIFT 데이터 계층 기여도 |

(기존 must-have 4종 + KOPEN ablation 1종 = 총 5종)

---

### 6. 핵심 Checkpoint

**변경 없음.** 기존 날짜 구조 유지.

**추가 milestone (v2):**

| 날짜 | 체크포인트 추가 |
|------|-------------|
| W5 종료 (04/24) | K-SHIFT P0 patch 2종 (IFP, OSP) 생산 + DMP kopen_summary 연동 |
| W6 종료 (05/01) | OpenTrapRiskPatch → RiskEngine 연동 PASS |
| W8 종료 (05/15) | KOPEN feature 포함 SC v2 산출 |
| W10 종료 (05/29) | with/without KOPEN ablation 결과 확보 |

---

### 8. 하지 않을 것

**현재 (v1) 목록:**
RL allocator, 실계좌 실제 자동매매, full graph relation model, full LoRA fine-tuning, HFT, agentic factor mining loop, full anonymized ticker ablation, Similar Case Retriever full 구현, xLSTM/PatchTST core backbone 교체.

**v2 추가 금지 항목:**

| 금지 항목 | 이유 |
|---------|------|
| Risk personalization | 사용자 성향에 따라 stop-loss / position cap을 변경하는 것은 리스크 정책의 일관성을 파괴한다 |
| HFT pivot | 장중 1분 단위 이하의 초고빈도 거래는 이번 학기 범위 밖 |
| 설문을 model feature에 삽입 | Preference Resolver 설계 원칙 위반 |

---

### 9. AI 완료 조건

**기존 8개 완료 조건 유지.**

**v2 추가 완료 조건 (3개):**

| # | 완료 조건 | 해당 주차 |
|---|----------|----------|
| 9 | K-SHIFT P0 patch (IFP, OSP) + DMP 연동 PASS | W5 |
| 10 | Preference Resolver + UserPreferenceProfile + serve 분기 동작 | W7 |
| 11 | with/without KOPEN ablation 결과 확보 | W10 |

v2 기준 총 완료 조건은 11개다.

---

## 변경 이력

| 버전 | 날짜 | 내용 |
|------|------|------|
| delta v1 | 2026-03-29 | v1 → v2 수정 방향 초안 작성 |

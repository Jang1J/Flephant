# 제안서 v11 → v12 수정표 (Delta)

> 기준일: 2026-03-29
> 작성: AI #1 (장재원)
> 원칙: v11 원본은 보존. 본 문서는 delta만 기록한다.
> v11 원본 경로: 문서/3주차/KOSPI_프로젝트_제안서_v11_최종.md

---

## 수정 철학

v12는 v11의 구조를 유지하면서 세 가지 방향을 추가한다.

1. **개장 해석 서비스 레이어 명시**: K-OPEN Pulse를 독립 서비스 개념으로 전면에 내세운다.
2. **성향별 모의투자 분기 명시**: Preference Resolver를 통한 사용자별 전략 분기를 서비스 방향에 포함한다.
3. **K-SHIFT 내부 엔진 정의**: 4개 patch의 데이터 계층을 시스템 구조에 공식 등재한다.

---

## 섹션별 Delta

---

### 제목

**현재 (v11):**
KOSPI 기반 멀티에이전트 트레이딩 알고리즘 및 모의 자동매매 서비스 프로토타입

**수정 방향:**
K-OPEN Pulse를 전면 서비스 명칭으로 노출. 성향별 모의투자를 서비스 범주에 포함.

**수정 후 핵심 문장:**
K-OPEN Pulse 기반 한국장 개장 해석형 멀티에이전트 트레이딩 및 성향별 모의투자 서비스

---

### 1. 프로젝트 개요

**현재 (v11) 핵심:**
KOSPI 대형주 대상 멀티에이전트 트레이딩 알고리즘 개발. 서비스 레이어는 추천 결과와 모의 자동매매 흐름을 사용자에게 보여주는 역할.

**수정 방향:**
K-OPEN Pulse를 서비스의 핵심 USP(Unique Selling Point)로 명시. 성향별 전략 분기를 서비스 범위에 포함.

**수정 후 핵심 문장:**
본 프로젝트는 KOSPI 대형주를 대상으로 멀티에이전트 트레이딩 알고리즘을 개발하며, 그 결과를 K-OPEN Pulse라는 한국장 개장 해석 서비스와 성향별 모의투자 프로토타입으로 연결한다. K-OPEN Pulse는 전날 밤 발생한 해외 충격(미국장, 환율, 선물)과 당일 투자자 수급 흐름을 번역하여 사용자가 장 시작 전 판단 근거를 확보하도록 설계된 서비스 레이어다.

---

### 2. 문제의식

**현재 (v11) 핵심:**
단일 모델의 한계 5가지 (정량+정성 통합 어려움, 설명 어려움, 리스크 독립 설계 부재, 성과 검증 구조 부재, "지금 무엇을 해야 하나" 전달 어려움).

**수정 방향:**
두 가지 문제의식 추가: (1) 해외 충격이 한국 장 개장에 미치는 영향을 사용자가 직접 해석해야 하는 불편함. (2) 모든 사용자에게 동일한 전략을 제공하면 성향 불일치로 서비스 이탈이 발생한다.

**수정 후 추가 단락:**
또한 기존 트레이딩 추천 서비스는 두 가지 문제를 가진다. 첫째, 전날 밤 미국장 급락이나 환율 급변이 발생해도 그것이 내일 한국 장에 어떻게 번역되는지를 사용자 스스로 판단해야 한다. 둘째, 단일 전략을 모든 사용자에게 동일하게 제공하면 추세추종 선호 사용자와 반등포착 선호 사용자 모두를 만족시키기 어렵다. 본 프로젝트는 이 두 문제를 K-OPEN Pulse와 Preference Resolver로 해소한다.

---

### 3. 설계 원칙

**현재 (v11) 핵심:**
6가지 원칙 (예측과 설명 분리, 방향 판단과 비중 조정 분리, raw text 최소화, raw 시계열 LLM 직접 입력 금지, 운영과 연구 분리, 검증 우선).

**수정 방향:**
7번째 원칙 추가: 성향은 전략 라우팅에만 사용하며 모델 feature로 삽입하지 않는다.

**수정 후 추가 행 (표에 삽입):**

| 원칙 | 적용 방식 | 근거 |
|------|----------|------|
| **성향 분리** | 사용자 성향은 전략 profile 선택에만 사용. LightGBM/CNN 모델 feature로 삽입 금지 | Preference Resolver 설계서 v1 |

---

### 5. 대상 시장 및 데이터 범위

**현재 (v11) 핵심:**
KRX / OpenDART / Naver 뉴스 / ECOS 4개 소스. 18:00 KST snapshot. daily + hourly cadence.

**수정 방향:**
K-SHIFT 4개 patch를 데이터 소스 계층에 공식 추가. 종토방 데이터 소스를 명시.

**수정 후 추가 행 (데이터 소스 표에 삽입):**

| 소스 | 제공 데이터 | 비고 |
|------|-----------|------|
| 미국 지수 API | 나스닥/S&P500/반도체 ETF 전일 OHLCV | OvernightSpilloverPatch 용도. Provider는 구현 단계에서 결정 |
| 종목토론방 (네이버 증권) | 게시글 수, 조회수 급증 종목 | RetailThemeGraph 용도 |

**수정 후 추가 단락 (K-SHIFT 소개):**
위 원천 데이터는 K-SHIFT(Korean Shock-Hype-Investor-Flow Translator) 엔진을 통해 4개 patch artifact로 가공된다. 각 patch는 standalone artifact로 저장되는 동시에 DMP/HMP의 `kopen_summary` 필드에 요약값이 포함된다. patch 정의 상세는 `문서/vnext/kopen_patch_contract_v1.md`를 참조한다.

---

### 6. 전체 시스템 구조

**현재 (v11) 핵심:**
Data Agent → Strategy Agent → Risk Agent → Backtest Agent → Final Decision Agent → Service Layer의 6계층 구조.

**수정 방향:**
K-SHIFT 엔진을 Data Agent 내부의 별도 모듈로 명시. Service Layer에 K-OPEN Pulse와 Preference Resolver를 명시.

**수정 후 Data Agent 항목 변경:**

**1) Data Agent (변경 후)**
- 기존 역할 전체 유지
- **K-SHIFT 엔진 (신규)**: InvestorFlowPatch, OvernightSpilloverPatch, RetailThemeGraph, OpenTrapRiskPatch 생산
- 출력: `DailyMarketPacket` (kopen_summary 포함), `TickerTextPack`, `DataQualityReport`, K-SHIFT 4개 Patch artifacts

**수정 후 Service Layer 항목 변경:**

**6) Service Layer (변경 후)**
- **K-OPEN Pulse**: 개장 전 한국장 해석 브리핑 (OvernightSpilloverPatch + InvestorFlowPatch 기반)
- **Preference Resolver**: 사용자 설문 5축 → selected_profile → 전략 분기 (momentum / rebound)
- **성향별 모의투자**: selected_profile에 따라 다른 StrategyCard를 serve

---

### 7. 핵심 파이프라인

**현재 (v11) 핵심:**
t일 18:00 KST snapshot → Data Agent → Strategy Agent → Risk Agent → Backtest Agent → Final Decision Agent → Service Layer.

**수정 방향:**
Data Agent 출력에 K-SHIFT 4개 patch 추가. Service Layer 분기 구조(K-OPEN Pulse / Preference Resolver) 명시.

**수정 후 파이프라인 하단 추가 블록:**

```
K-SHIFT Engine (Data Agent 내부)
  ├── InvestorFlowPatch (daily, P0)
  ├── OvernightSpilloverPatch (daily, P0)
  ├── RetailThemeGraph (hourly, P1)
  └── OpenTrapRiskPatch (hourly, P1)
         ↓
  DMP.kopen_summary 필드 및 standalone artifacts
         ↓
Service Layer
  ├── K-OPEN Pulse — 개장 해석 브리핑
  └── Preference Resolver
        ├── style_score >= 5.5 → momentum StrategyCard serve
        ├── style_score <  4.5 → rebound StrategyCard serve
        └── 중간 구간 → system_recommended (시장 regime 참조)
```

---

### 12. 평가 프로토콜

**현재 (v11) 핵심:**
백테스트 방식, 성과 지표, Baseline 6종, Leakage 통제 및 Ablation.

**수정 방향:**
K-OPEN Pulse 기여도를 측정하는 ablation을 baseline 비교에 추가. with/without KOPEN 비교 명시.

**수정 후 Baseline 비교 표에 추가 행:**

| Baseline | 목적 |
|----------|------|
| **No-KOPEN (K-SHIFT 비활성화)** | InvestorFlowPatch + OvernightSpilloverPatch 제거 시 성과 변화 측정 |

---

### 18. 서비스 방향

**현재 (v11) 예상 내용:**
추천 종목, 추천 이유, 모의 자동매매 흐름 제공.

**수정 방향:**
K-OPEN Pulse를 첫 번째 서비스 화면으로 명시. 성향 설문 및 profile별 분기를 서비스 흐름에 포함.

**수정 후 핵심 문장:**
서비스는 두 가지 핵심 화면으로 구성된다. 첫째, K-OPEN Pulse 화면: 사용자가 앱을 열면 오늘 장 개장에 영향을 미치는 해외 충격과 투자자 수급 흐름이 번역된 형태로 제공된다. 둘째, 성향별 추천 화면: 최초 1회 설문을 통해 확인된 투자 성향(momentum / rebound)에 따라 서로 다른 StrategyCard 기반 추천이 제공된다. 리스크 정책은 성향에 무관하게 동일하게 적용된다.

---

### 19. 기대 효과

**현재 (v11) 핵심:**
멀티에이전트 협업 구조 검증, LLM 활용 설명 가능성 강화, 성과 검증 구조.

**수정 방향:**
K-OPEN Pulse와 Preference Resolver를 통해 추가되는 기대 효과를 명시.

**수정 후 추가 단락:**
K-OPEN Pulse는 사용자가 해외 충격을 직접 해석하는 수고를 없애고, 개장 전 판단 근거를 구조화된 형태로 제공함으로써 투자 의사결정의 접근성을 높인다. Preference Resolver는 단일 전략 추천의 한계를 넘어, 동일한 알고리즘 엔진 위에서 사용자 성향에 따른 차별화된 서비스를 제공할 수 있음을 보인다.

---

### 20. 결론

**현재 (v11) 핵심:**
5-agent 협업 구조로 트레이딩 알고리즘을 개발하고 서비스화한다는 방향.

**수정 방향:**
K-OPEN Pulse, K-SHIFT, Preference Resolver를 결론에 명시하여 서비스 완결성을 강조.

**수정 후 핵심 문장:**
본 프로젝트는 KOSPI 대형주 대상 멀티에이전트 트레이딩 알고리즘 엔진 위에, 한국장 개장 해석 서비스(K-OPEN Pulse)와 성향별 모의투자 분기(Preference Resolver)를 결합하여 알고리즘과 서비스가 일체화된 프로토타입을 완성한다. K-SHIFT 엔진의 4개 patch가 시장 해석의 원천 데이터를 공급하고, Preference Resolver가 사용자별 전략 분기를 담당함으로써, 단일 모델 추천이 아닌 구조화된 협업형 트레이딩 서비스의 가능성을 실증한다.

---

---

### 4. 전략 모델 상세 (AI #2) — delta v2 추가

**현재 (v11) 핵심:**
Strategy Agent가 StrategyCard를 생성한다는 개요만 존재.

**수정 방향:**
AI #2 전략의 구체적 모델 아키텍처와 KOSPI 특화 요소를 명시.

**수정 후 추가 섹션:**

#### 4-1. LightGBM Momentum Ranker

KOSPI 대형주 26종목 유니버스 내 상대 매력도 ranking.
- 28개 피처 (manual 10 + MLF-lite 2 + UMI-lite 3 + cross-sectional pct 8 + OHLCV micro 5)
- sector-neutral excess return 레이블 (WICS 16섹터 기반 de-biasing)
- Walk-forward expanding window (train 200일, val 20일, purge/embargo 5일)
- Binary(0.6) + Ordinal(0.4) dual-supervision
- 현재 성능: Binary AUC 0.742, Ordinal AUC 0.736, P@5 0.511

#### 4-2. KR-Rebound-CNN + Committee

oversold/reversal 반등 포착 보조 전략.
- 3-channel 64x64 chart image + 39-dim context (23 base + 16 WICS sectors)
- BCEWithLogitsLoss, AdamW(lr=3e-4), Dropout 0.3/0.4, BatchNorm
- Gate-OFF train / Gate-ON infer (학습은 전체 유니버스, 추론은 과매도 종목만)
- Committee: Tree Core(0.70) + CNN(0.30) confirmatory mode
- 현재 성능: CNN AUC 0.766 (13-fold avg)

#### 4-3. O-I Decoupling (Overnight-Intraday 분리)

한국 시장 특화 novelty. 한국장은 전날 미국장의 영향으로 overnight과 intraday 수익률이 다르게 움직인다.
- overnight_direction 보조 모델: 9-fold avg AUC 0.527 (max 0.616, min 0.438)
- intraday_direction 보조 모델: 9-fold avg AUC 0.513 (max 0.554, min 0.459)
- 현재 피처셋으로는 O-I 분리 신호가 약함. K-SHIFT OSP 데이터 추가 시 개선 기대
- 학술 claim: "한국장에서 밤과 낮이 다르게 움직인다"

#### 4-4. Conformal Prediction (불확실성 정량화)

모델 예측의 신뢰 구간을 분포 가정 없이 산출.
- Split Conformal Prediction (alpha=0.05, 95% coverage)
- Walk-forward 마지막 fold validation으로 calibrate
- StrategyCard에 conformal_interval 주입

---

### 7-1. 핵심 파이프라인 — FDA Multi-Agent Debate 추가

**현재 (v11) 핵심:**
Final Decision Agent가 COP를 받아 approve/veto 결정.

**수정 방향:**
FDA에 Multi-Agent Debate 구조를 명시. 단, explanation/audit layer로만 제한.

**수정 후 추가 블록:**

FDA는 3 페르소나 기반 Multi-Agent Debate를 수행한다.
- **Foreigner Agent**: DMP macro (usd_krw, vix_proxy, treasury_3y)에 민감
- **Institution Agent**: SC rationale (DART 공시 기반) + sector 집중도에 민감
- **Retail Agent**: SC rationale (뉴스 헤드라인) + quant_score에 민감
- **Moderator**: 3자 의견 종합 + consensus_score 산출

핵심 제약:
- Debate는 explanation/audit layer로만 동작. 판정(approve/veto)을 변경하지 않는다.
- can_change_weight = false 유지.
- debate_log는 FDC artifact에 기록되어 audit trail로 활용.

Emergency Flag 메커니즘:
- Debate가 극단적 의견 충돌(consensus_score < 0.33)을 감지하면 `emergency_flag`를 FDC에 기록한다.
- Debate는 직접 판정을 변경하지 않는다. 미리 정의된 deterministic safety policy가 flag를 읽어 `new_open_blocked` 또는 `human_review_required` 상태를 발동한다.
- 즉, Debate는 원인 제공(compliance/audit), 행동은 deterministic rule이 수행한다.

---

### 5-1. 데이터 결측치/fallback 정책 — delta v2 추가

**수정 방향:**
K-SHIFT 데이터 소스의 결측/장애 시 처리 원칙을 설계 원칙에 추가.

**수정 후 추가 원칙:**

| 원칙 | 적용 방식 | 근거 |
|------|----------|------|
| **결측 시 의사결정 carry-forward 금지** | 의사결정용(Strategy/Risk) 입력에는 이전 값을 이월하지 않고 masked_missing 처리. 표시용(display)에만 시간 감쇠(decay) 허용 | PIT-Safety + 백테스트-라이브 일관성 보장 |
| **immutable snapshot** | 수집 시점 기준 원시값 불변 저장. 이후 정정(revision)이 들어와도 원본을 덮어쓰지 않음 | 재현 가능한 백테스트를 위해 수집 시점 데이터 보존 |

---

### 3-1. 설계 원칙 추가 — delta v2

**수정 방향:**
LLM 출력 신뢰도 통제 원칙과 데이터 승격 게이트를 설계 원칙에 추가.

**수정 후 추가 행 (설계 원칙 표에 삽입):**

| 원칙 | 적용 방식 | 근거 |
|------|----------|------|
| **LLM 출력 신뢰도 게이트** | LLM이 생성한 요약/압축 산출물에 confidence score를 부여하고, threshold 미달 시 Strategy/Risk에 전달하지 않고 결측 처리 | LLM hallucination 방지 + 의사결정 안전성 |
| **데이터 승격 게이트 (Promotion Gate)** | 새 데이터 소스가 의사결정 파이프라인에 연결되려면 5개 조건(coverage >= 95%, missing <= 5%, PIT 검증, baseline 대비 개선, failure case 설명 가능)을 통과해야 함 | 검증 없는 피처 추가로 인한 과적합 방지 |

---

### 7-2. K-SHIFT 시간축 명확화

**현재 (delta v1):**
K-SHIFT 4개 patch를 단순 나열.

**수정 방향:**
DMP 전용 / HMP 전용을 명확히 분리.

**수정 후:**

| Patch | Cadence | 시간축 |
|-------|---------|--------|
| InvestorFlowPatch (IFP) | daily | DMP 전용 |
| OvernightSpilloverPatch (OSP) | daily | DMP 전용 |
| RetailThemeGraph (RTG) | hourly | HMP 전용 (요약만 DMP) |
| OpenTrapRiskPatch (OTP) | hourly | **HMP 전용** (DMP에 넣지 않음) |

OTP는 개장 직후(09:00~09:30) 발생하는 신호이므로 t일 18:00 DMP에 포함하지 않는다.

---

### 7-3. K-SHIFT 한국 특화 우선순위

**수정 방향:**
K-SHIFT 4개 patch의 구현 우선순위를 한국 고유 신호 순서로 명시.

**수정 후:**

| 순위 | Patch | 한국 특화성 | 근거 |
|------|-------|-----------|------|
| 1순위 | IFP | 외국인/기관/개인 투자주체 구조 | 한국 고유 |
| 2순위 | RTG | 종토방/테마 전염 | 한국 고유 |
| 3순위 | OTP | VI/갭상승/추격 위험 | 한국 고유 |
| 4순위 | OSP | 미국장 번역 | 외부 충격 (한국 고유 아님) |

한국 내부 구조를 먼저 구조화하고, 미국장 데이터는 검증과 보조 번역 계층으로 병렬 확보한다.

---

### 12-1. 평가 프로토콜 — 성능 수치 갱신

**수정 후 현재 성능 표:**

| 메트릭 | 값 | 비고 |
|--------|-----|------|
| LightGBM Binary AUC | 0.742 | 9-fold walk-forward |
| LightGBM Ordinal AUC | 0.736 | 3-class ranking |
| LightGBM P@5 | 0.511 | 상위 5종목 정밀도 |
| CNN AUC | 0.766 | 13-fold avg (Gate OFF train) |
| O-I Overnight AUC | 0.527 (9-fold avg, max 0.616) | 야간 수익 방향. 현재 피처셋 한계 |
| O-I Intraday AUC | 0.513 (9-fold avg, max 0.554) | 주간 수익 방향. K-SHIFT 추가 시 개선 기대 |
| Conformal q_hat | 0.718 | 95% coverage |
| Backtest Sharpe | 1.69 | 거래세+슬리피지 포함 |
| Backtest MDD | -7.90% | |
| Backtest 누적수익 | +10.42% | 2024-08 ~ 2026-03 |

---

## 변경 이력

| 버전 | 날짜 | 내용 |
|------|------|------|
| delta v1 | 2026-03-29 | v11 → v12 수정 방향 초안 작성 |
| delta v2 | 2026-03-31 | AI #2 전략 상세 + Debate(+Emergency Flag) + O-I + Conformal + 시간축 + 우선순위 + 성능 수치 + 결측치 정책 + Provider Abstraction |

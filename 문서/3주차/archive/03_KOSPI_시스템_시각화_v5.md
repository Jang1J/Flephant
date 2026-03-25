# KOSPI 전용 시스템 시각화 v5

> **주의 1:** 이 문서는 “모델 스펙 freeze” 문서가 아니라 **구조 / 흐름 / 계약 / 레이어**를 팀이 같은 그림으로 보기 위한 mental model 문서다.  
> **주의 2:** 모델명(LightGBM, PPO, KoBERT, GPT API 등)은 예시 후보이며, 고정된 것은 구조와 contract다.

---

## 1. 전체 구조: mainline + reserve

```text
┌────────────────────────────────────────────────────────────────────┐
│                 Runtime Trading Mainline (이번 학기 코어)          │
│                                                                    │
│  Data Agent                                                        │
│    -> DailyMarketPacket                                            │
│  Strategy Agent                                                    │
│    -> Signal Layer                                                 │
│    -> Reasoning Layer                                              │
│    -> StrategyCard                                                 │
│  Risk Agent                                                        │
│    -> RiskCard + OrderPlan                                         │
│  Backtest Agent                                                    │
│    -> BacktestReport + FailureCaseCard                             │
│  Orchestrator                                                      │
│    -> failure 감지 시 Strategy 1회 재실행                           │
└────────────────────────────────────────────────────────────────────┘

                               ▲
                               │ 실패 원인 / 개선 포인트 축적
                               ▼

┌────────────────────────────────────────────────────────────────────┐
│             Research / Reserve Layer (후속 연구 + 개인 트랙)        │
│                                                                    │
│  Factor / Alpha expansion      -> AlphaAgent                       │
│  Outer research loop           -> R&D-Agent-Quant                  │
│  RL allocator reserve          -> MetaTrader / IRL / Dirichlet RL  │
│  Classical appendix baselines  -> FAILAB HMM / Low-pass LSTM       │
└────────────────────────────────────────────────────────────────────┘
```

---

## 2. 레이어와 논문 매핑

```text
Layer 1. System / Orchestrator / SOP
  -> MetaGPT

Layer 2. Strategy Internal Division
  -> TradExpert

Layer 3. Strategy Reasoning / Fusion / Evaluation Discipline
  -> AAPM

Layer 4. Quant Core / Feature Extraction / Stress Factors
  -> AlphaGAT
  -> AlphaAgent
  -> UMI
  -> MLF

Layer 5. KOSPI-local Risk / Allocation Gate
  -> FAILAB Instability Index

Layer 6a. RL Allocator (1차 확장, 실제 구현 후보)
  -> AlphaGAT Stage-II inspired PPO allocator
  -> HSTP-lite
  -> Smart Tangency Portfolio

Layer 6b. RL Research Reserve
  -> MetaTrader
  -> Heuristic-guided IRL
  -> Attention-Enhanced Dirichlet RL
  -> Can DRL Beat 1/N?
```

---

## 3. Runtime Trading Mainline 상세

```text
[고정 KOSPI 대형주 유니버스 20~30개]
                │
                ▼
      [1. Data Agent]
  - OHLCV / 거래량 / 기술팩터
  - security_master
  - 공시 제목 / 공시 유형 / 뉴스 헤드라인
  - MLF-lite multi-period bank
                │
                ▼
         DailyMarketPacket
                │
                ▼
   [2-1. Signal Layer / AI #1]
  Manual factors
    + MLF-lite
    + UMI-lite
    -> LightGBM LambdaMART ranker
    -> Top 5~8 shortlist
                │
                ▼
   [2-2. Reasoning Layer / AI #2]
      Market Analyst
           +
 News/Disclosure Analyst Lite
           +
    General Synthesizer
                │
                ▼
          StrategyCard
                │
                ▼
          [3. Risk Agent]
  - position cap
  - stop-loss
  - turnover cap
  - cash ratio
  - Instability gate
  - UMI-lite stress 보조 신호
                │
                ▼
      RiskCard + OrderPlan
                │
                ▼
        [4. Backtest Agent]
  - T+1 open execution
  - fee / slippage
  - walk-forward
  - failure tagging
                │
                ▼
 BacktestReport + FailureCaseCard
                │
                ▼
        [Orchestrator]
  - dependency check
  - artifact routing
  - failure 시 Strategy 1회 재실행
```

### 핵심 원칙
- **전 종목 LLM 처리 금지**
- **Quant Prefilter가 반드시 앞단**
- LLM은 shortlist 이후의 해석/종합 계층에 제한적으로 사용
- RL은 full-universe selector가 아니라 **Top-K allocator**로만 사용

---

## 4. Strategy 내부 3단 구조

```text
Stage 1. Signal Layer (팀 mainline)
  Manual factors + MLF-lite + UMI-lite
  -> LightGBM ranker
  -> shortlist

Stage 2. Reasoning Layer (팀 mainline)
  shortlist
  -> Market Analyst
  -> News/Disclosure Analyst Lite
  -> General Synthesizer
  -> StrategyCard

Stage 3. RL Allocator Layer (1차 확장)
  shortlist + signal features + risk state
  -> PPO allocator
  -> weights / cash ratio / rebalance horizon
```

### 담당 논문
- Stage 1: **MLF / UMI / AlphaGAT / AlphaAgent(확장 철학)**
- Stage 2: **TradExpert / AAPM**
- Stage 3: **AlphaGAT Stage-II / HSTP / Smart Tangency**

---

## 5. KOSPI Data Clock / Leakage Timeline

```text
T일 정규시장
09:00 ─────────────────────────────── 15:30
      [OHLCV / 거래량 / 기술팩터 확정]

T일 장종료후 시간외 / 공시 정리 구간
15:40 ─────────────────────────────── 18:00
      [공시 제목 / 공시 유형 / 후속 기사 반영]

T일 18:00 KST
      └── 공식 스냅샷 시점
          timestamp <= 18:00 인 공시/헤드라인만 사용 가능

T일 18:00 이후
      └── T+1 정보셋으로 이월

T일 밤
  Data Agent -> Strategy Agent -> Risk Agent

T+1 open
  집행

T+1 이후
  Backtest / Failure analysis / retry 판단
```

---

## 6. Artifact 흐름

### 공식 카드 6종 (registry 필수)
```text
DailyMarketPacket
   -> StrategyCard
      -> RiskCard
         -> OrderPlan
            -> BacktestReport
               -> FailureCaseCard
```

### 내부 중간 카드 3종 (debug/experiment 전용)
```text
QuantEvidenceCard
MarketAnalysis
TextSignalCard
```

### 정책
- 공식 카드 = 무조건 저장
- 내부 카드 = 필요 시 저장
- internal card는 trace와 디버깅을 위한 선택적 관찰 레이어

---

## 7. W4 중간발표 데모 범위 (C')

```text
반드시 실제 구현
  - KOSPI 고정 유니버스
  - security_master
  - Data Agent
  - DailyMarketPacket 생성 및 registry 저장
  - Quant Prefilter -> shortlist
  - StrategyCard v0
  - artifact trace viewer

얇은 baseline 또는 mock 허용
  - RiskCard v0
  - BacktestReport v0
  - FailureCaseCard v0
```

### 의미
중간발표의 목표는 “최종 성능”이 아니라,  
**artifact-driven 4-agent workflow가 실제로 굴러가기 시작했다**는 것을 보여주는 것이다.

---

## 8. W9 RL 현실성 규정

```text
W9 목표
  RL allocator 구현 완료  -> 아님
  RL allocator 설계 확정 -> 예
  PPO prototype          -> 예
  초기 실험              -> 예
```

### fallback
- RL이 밀려도 mainline 발표와 보고서는 성립한다.
- RL은 최종발표에서 **프로토타입 + 초기 실험 + 향후 연구**로 제시 가능하다.

---

## 9. 팀 역할 레인 (ownership)

```text
AI #1
  - Data Agent
  - MLF-lite / UMI-lite / ranker
  - Backtest 계산 엔진
  - RL allocator prototype

AI #2
  - Market Analyst
  - News/Disclosure Analyst Lite
  - General Synthesizer
  - Risk rule semantics
  - Failure 해석

BE #1
  - Orchestrator
  - Artifact Registry
  - Scheduler
  - experiment mode

BE #2
  - Data source adapter
  - REST API
  - Dashboard / card viewer / charts
```

---

## 10. 한 장 요약

> **v5 KOSPI 시스템은 “MLF-lite + UMI-lite + LightGBM ranker”로 shortlist를 만들고, “Market + News/Disclosure + Synthesizer”로 StrategyCard를 만들며, Risk/Backtest/feedback을 거친 뒤, RL은 shortlist 이후 PPO allocator 프로토타입으로만 붙이는 구조다.**

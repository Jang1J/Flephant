# KOSPI 프로젝트 최종계획서 v6
## 이번 학기 실제 구현 범위만 남긴 실행 버전

> 목적: KOSPI v5 계획에서 **이번 학기 안에 실제로 구현할 부분만** 남기고, 논문 매핑 / 파이프라인 / ownership / 일정까지 한 번에 정리한 실행 문서.

---

## 0. v6에서 무엇을 바꿨는가

v6는 기존 v5에서 아래를 분명히 정리한다.

### 남긴 것
- 4-agent 구조: `Data -> Strategy -> Risk -> Backtest`
- Orchestrator + Artifact Registry + feedback loop
- KOSPI 대형주 고정 유니버스
- `MLF-lite + UMI-lite + LightGBM ranker` 기반 signal layer
- `Market + News/Disclosure + General Synthesizer` reasoning layer
- `Instability Index` 기반 KOSPI-local risk gate
- `shortlist 이후 RL allocator prototype` (선택 구현)

### 뺀 것 / 뒤로 미룬 것
- Layer 6b RL Research Reserve
- Outer Research Loop 전체 구현
- AlphaAgent full factor invention loop
- R&D-Agent-Quant full auto-quant lab
- FAILAB HMM / Low-pass LSTM mainline 사용
- full memory/notes/retrieval
- Fundamental Analyst
- 전종목 LLM ranking

즉, **이번 학기 구현 범위는 mainline runtime loop + 얇은 RL prototype까지만** 본다.

---

## 1. 이번 학기 실제 구현에 직접 반영하는 논문 수

### 1-1. 메인라인 필수 논문 (7편)
1. **MetaGPT** — SOP / artifact / dependency / feedback
2. **TradExpert** — Strategy 내부 분업, shortlist 원칙
3. **AAPM** — relevance check / refined text / fusion 철학
4. **AlphaGAT** — factor backbone 철학, shortlist 이후 allocator 기준점
5. **UMI (KDD 2025)** — irrationality / stress factor
6. **MLF (KDD 2025)** — multi-period feature bank
7. **FAILAB Instability Index (2020)** — KOSPI-local risk gate

### 1-2. RL allocator 후보 논문 (택1, 1편)
- **HSTP-lite**
- **Smart Tangency Portfolio**

즉, **이번 학기 구현 범위에서 직접 반영되는 논문은 총 8편**이다.

> 계산 방식:
> - 메인라인 필수 7편
> - RL allocator 후보 2편 중 실제 구현은 1편만 택1

### 1-3. 이번 학기엔 구현하지 않지만 이름만 남기는 논문
- AlphaAgent -> 향후 Factor/Alpha Analyst 확장
- R&D-Agent-Quant -> 향후 outer research loop
- FAILAB HMM Momentum -> appendix regime baseline
- FAILAB Low-pass LSTM -> appendix denoising baseline

---

## 2. 프로젝트 한 줄 정의 (v6)

**KOSPI 대형주 고정 유니버스를 대상으로, Data Agent가 가격/공시/뉴스 메타를 정렬해 `DailyMarketPacket`을 만들고, Strategy Agent가 정량 신호와 텍스트 reasoning을 결합해 `StrategyCard`를 생성하면, Risk Agent가 KOSPI-local instability gate와 기본 리스크 규칙을 적용해 `OrderPlan`으로 변환하고, Backtest Agent가 T+1 open 기준으로 검증한 뒤 `FailureCaseCard`를 통해 Strategy를 1회 교정하는 SOP 기반 협업형 트레이딩 시스템.**

---

## 3. 불변 전제 (이번 학기 기준)

- 시장: **KOSPI 대형주 고정 유니버스 20~30개**
- 유니버스 방식: **static ex-ante**
- 빈도: **일봉**
- 정보 스냅샷: **T일 18:00 KST**
- 18:00 이후 공시/뉴스: **T+1로 이월**
- 집행 시점: **T+1 open**
- 거래비용: **편도 10bp slippage + 수수료**
- 검증 방식: **walk-forward 최소 3-fold**
- 공식 artifact: **6종**
- 내부 중간 카드: **3종 (debug/experiment only)**
- RL allocator: **must-have 아님, prototype이면 충분**

---

## 4. 이번 학기 구현 범위의 최종 레이어 구조

```text
Layer 1. System / Orchestrator / SOP          <- MetaGPT
Layer 2. Strategy Internal Division           <- TradExpert
Layer 3. Strategy Reasoning / Fusion          <- AAPM
Layer 4. Quant Core / Feature / Stress        <- AlphaGAT, UMI, MLF
Layer 5. KOSPI-local Risk Gate                <- FAILAB Instability Index
Layer 6a. RL Allocator Prototype (택1)        <- HSTP-lite 또는 Smart Tangency
```

### v6 해석 포인트
- **Layer 1~5는 이번 학기 core mainline**
- **Layer 6a는 선택 구현형 prototype**
- **Layer 6b / Outer Loop / Appendix baseline은 구현 범위 밖**

---

## 5. 전체 워크플로우 / 파이프라인 (이번 학기 구현 범위만)

```text
[KOSPI 대형주 20~30개 고정 유니버스]
            │
            ▼
   ┌─── Data Agent ───┐          AI #1 + BE #2
   │ OHLCV / 거래량     │
   │ 공시·뉴스 메타     │
   │ security_master   │
   │ MLF-lite bank     │
   └────────┬──────────┘
            ▼
     DailyMarketPacket
            │
            ▼
   ┌── Signal Layer ──┐          AI #1
   │ Manual factors    │
   │ + MLF-lite        │
   │ + UMI-lite        │
   │ -> LightGBM ranker│
   │ -> Top 5~8        │
   └────────┬──────────┘
            ▼
   ┌─ Reasoning Layer ─┐         AI #2
   │ Market Analyst     │
   │ News/Disclosure    │
   │ General Synthesizer│
   └────────┬───────────┘
            ▼
       StrategyCard
            │
            ▼
   ┌─── Risk Agent ───┐          AI #2 (+ AI #1 signal)
   │ position cap      │
   │ stop-loss         │
   │ turnover cap      │
   │ cash ratio        │
   │ Instability gate  │
   │ UMI stress 보조   │
   └────────┬──────────┘
            ▼
    RiskCard + OrderPlan
            │
            ▼
   ┌── Backtest Agent ─┐         AI #1 (+ AI #2 해석)
   │ T+1 open execution │
   │ fee / slippage     │
   │ walk-forward       │
   │ failure tagging    │
   └────────┬───────────┘
            ▼
 BacktestReport + FailureCaseCard
            │
            ▼
   ┌── Orchestrator ───┐         BE #1
   │ dependency check   │
   │ artifact routing   │
   │ failure -> retry 1 │
   └────────────────────┘

   (선택) Layer 6a. RL Allocator Prototype      AI #1
   shortlist + signal + risk state
   -> PPO allocator
   -> weights / cash ratio / rebalance horizon
```

---

## 6. 각 단계에 어떤 논문이 실제로 반영되는가

| 단계 | 실제 구현 내용 | 반영 논문 |
|---|---|---|
| **Data Agent** | OHLCV/공시/뉴스 메타 정렬, MLF-lite 입력 준비 | **MetaGPT, AAPM, MLF** |
| **Signal Layer** | Manual factors + MLF-lite + UMI-lite -> LightGBM ranker | **TradExpert, AlphaGAT, UMI, MLF** |
| **Market Analyst** | 가격/기술팩터 기반 상위 후보 해석 | **TradExpert** |
| **News/Disclosure Analyst Lite** | 공시/헤드라인 relevance check + 요약 | **AAPM, TradExpert** |
| **General Synthesizer** | 정량/정성 근거 결합 -> StrategyCard | **TradExpert, AAPM** |
| **Risk Agent** | 기본 risk rule + instability gate + stress 보조 | **FAILAB Instability Index, UMI, MetaGPT(구조)** |
| **Backtest Agent** | walk-forward + slippage + failure tagging | **MetaGPT, AAPM(평가 규율), TradExpert(분업적 실험 태도)** |
| **Orchestrator / Registry** | SOP, dependency, artifact, retry | **MetaGPT** |
| **RL Allocator Prototype** | shortlist 이후 weight/cash allocation | **AlphaGAT Stage-II 철학 + HSTP-lite 또는 Smart Tangency** |

---

## 7. 이번 학기 Quant Model 최종 정의

### 7-1. Signal layer mainline

```text
Manual Factor Bank
+ MLF-lite Multi-period Feature Bank
+ UMI-lite Irrationality / Stress Factors
-> LightGBM LambdaMART Ranker
-> Top 5~8 shortlist
```

### 7-2. 세부 구성

#### A. Manual Factor Bank
- 수익률 계열: 1D / 5D / 20D return
- 모멘텀 / 추세: RSI, MACD, price-to-MA gap
- 변동성: ATR, realized vol 5/20/60
- 거래량/유동성: volume z-score, turnover proxy
- 위치/범위: Bollinger %B, range position

#### B. MLF-lite
- **multi-period input**: 기본 `5 / 20 / 60일`
- **IRF-lite**: 기간 간 중복 feature 완화
- **LWI-lite**: 기간별 가중 평균 통합
- `MAP-lite`는 1차 확장, `Patch Squeeze`는 보류

#### C. UMI-lite
- **stock irrationality proxy**
- **market synchronism / stress proxy**
- 역할: ranker feature + Market/Risk 보조 신호

#### D. Ranker
- **LightGBM LambdaMART**
- 출력: 종목별 cross-sectional ranking score
- 사용 목적: 전 종목 LLM 처리 금지, shortlist 생성

### 7-3. 이번 학기에는 구현하지 않는 quant 관련 항목
- AlphaAgent full factor invention loop
- AlphaGAT full Stage-I reproduction
- Diffolio
- full graph relation model
- full uncertainty gate

---

## 8. 공식 카드 6종과 내부 중간 카드 3종

### 8-1. 공식 카드 (registry 필수 저장)
1. `DailyMarketPacket`
2. `StrategyCard`
3. `RiskCard`
4. `OrderPlan`
5. `BacktestReport`
6. `FailureCaseCard`

### 8-2. 내부 중간 카드 (debug / experiment only)
1. `QuantEvidenceCard`
2. `MarketAnalysis`
3. `TextSignalCard`

### 8-3. 운영 원칙
- 공식 카드 6종은 **항상 저장**
- 내부 카드 3종은 **debug mode / experiment mode에서만 저장 가능**
- 발표용 trace viewer는 공식 카드 위주로 구성

---

## 9. 팀원 ownership freeze (최종)

| 팀원 | 역할 | 비중 |
|---|---|---:|
| **AI #1** | Data Agent + Signal Layer + Backtest 계산 엔진 + RL prototype | **30%** |
| **AI #2** | Market Analyst + News/Disclosure + Synthesizer + Risk 규칙 + Failure 해석 | **27%** |
| **BE #1** | Orchestrator + Artifact Registry + Scheduler + Experiment mode | **23%** |
| **BE #2** | API + Dashboard + Data source adapter + Card viewer | **20%** |

### 9-1. 파이프라인 기준 세부 담당

| 모듈 | 주 담당 | 보조 |
|---|---|---|
| security_master / 종목코드 매핑 | **BE #2** | AI #1 |
| 가격/공시/뉴스 adapter | **BE #2** | BE #1 |
| `DailyMarketPacket` 구조 / 품질 플래그 | **AI #1** | BE #1 |
| MLF-lite / UMI-lite / ranker | **AI #1** | AI #2 |
| Market Analyst | **AI #2** | AI #1 |
| News/Disclosure Analyst Lite | **AI #2** | AI #1 |
| General Synthesizer / `StrategyCard` | **AI #2** | AI #1 |
| Risk rule / `RiskCard` / `OrderPlan` | **AI #2** | AI #1(Instability/UMI signal) |
| Backtest simulator / metrics | **AI #1** | AI #2 |
| `FailureCaseCard` 해석 | **AI #2** | AI #1 |
| Orchestrator / dependency / retry | **BE #1** | BE #2 |
| Registry / versioning / scheduler | **BE #1** | BE #2 |
| Dashboard / trace viewer / charts | **BE #2** | AI #2(semantic review) |
| RL allocator prototype | **AI #1** | BE #1 (runner), AI #2 (state review) |

---

## 10. 이번 학기 반드시 완성해야 하는 것

### 10-1. Must-have
- 고정 KOSPI 대형주 유니버스
- security_master
- Data Agent
- `DailyMarketPacket`
- `MLF-lite + UMI-lite + LightGBM ranker`
- shortlist 생성
- Market Analyst
- News/Disclosure Analyst Lite
- General Synthesizer
- `StrategyCard`
- Risk Agent (rule + instability gate)
- `RiskCard + OrderPlan`
- Backtest Agent
- `BacktestReport + FailureCaseCard`
- Orchestrator
- Artifact Registry
- trace viewer
- 1회 feedback retry

### 10-2. Nice-to-have
- RL allocator prototype
- S3 이상 실험 자동화
- 내부 카드 debug 저장

---

## 11. W4 중간발표 데모 범위 (고정)

### W4 Demo = **C'**

```text
실제 front-half + 얇은 end-to-end artifact loop
```

### 반드시 보여줄 것
- 고정 KOSPI 유니버스
- security_master
- Data Agent 실행
- `DailyMarketPacket` 생성 / 저장
- Quant Prefilter -> shortlist
- `StrategyCard v0`
- registry 저장
- trace viewer

### 얇게 보여도 되는 것
- `RiskCard v0`
- `BacktestReport v0`
- `FailureCaseCard v0`

즉, 중간발표 목표는 **성능 시연**이 아니라,
**“4-agent 시스템이 artifact 기반으로 실제 돌기 시작했다”** 를 보여주는 것이다.

---

## 12. 최종발표 전까지의 일정 (11주 기준)

| 주차 | 목표 | 필수 산출물 |
|---|---|---|
| **W1** | KOSPI universe 확정 / security_master / artifact 초안 | universe list, code map, card field v0 |
| **W2** | Data source adapter / DailyMarketPacket / registry skeleton | packet sample, DB skeleton |
| **W3** | MLF-lite / UMI-lite / LightGBM ranker baseline | shortlist result v0 |
| **W4** | 중간발표: C' demo | packet -> shortlist -> StrategyCard -> trace |
| **W5** | Market Analyst / News-Disclosure Lite v1 | MarketAnalysis v1, TextSignal v1 |
| **W6** | General Synthesizer / StrategyCard 안정화 | StrategyCard v1 |
| **W7** | Risk Agent v1 (rule + instability gate) | RiskCard / OrderPlan v1 |
| **W8** | Backtest Agent v1 + feedback retry | BacktestReport / FailureCaseCard v1 |
| **W9** | RL allocator **설계 확정 + 프로토타입 + 초기 실험** | PPO allocator v0 or design note |
| **W10** | S/R ablation, 비교표, dashboard 정리 | result tables, charts |
| **W11** | 최종 안정화 + 발표 자료 | final demo, slides, report |

### 일정 원칙
- RL allocator는 **must-have 아님**
- W9에서 늦어지면 **초기 실험 + 설계 문서**까지만 해도 된다
- mainline이 흔들리면 RL보다 core loop를 우선한다

---

## 13. 이번 학기 기준 비포함(out of scope)

- Layer 6b RL reserve 구현
- Outer Research Loop 구현
- AlphaAgent full 구현
- R&D-Agent-Quant full 구현
- FAILAB HMM / Low-pass LSTM mainline 채택
- Fundamental Analyst
- full memory / notes / retrieval
- full graph / relation model
- full LoRA fine-tuning
- 전종목 pairwise ranking
- high-frequency trading

---

## 14. 최종 한 줄 요약

> **이번 학기 우리는 KOSPI 대형주 고정 유니버스에서, `MLF-lite + UMI-lite + LightGBM ranker`로 shortlist를 만들고, `Market + News/Disclosure + Synthesizer`로 투자 thesis를 만든 뒤, `Instability Index + 기본 risk rule`로 주문을 조정하고, Backtest/Failure feedback으로 전략을 1회 교정하는 4-agent 협업형 트레이딩 시스템을 구현한다. RL은 shortlist 이후 allocator prototype까지만 시도한다.**

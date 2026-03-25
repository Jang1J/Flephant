# KOSPI 프로젝트 최종계획서 v3
## B안: FAILAB 1편 유지 + 최신 2025 quant 업그레이드

---

## 1. 최종 한 줄 정의

**KOSPI 대형주 고정 유니버스를 대상으로, Data → Strategy → Risk → Backtest 4개 에이전트가 구조화된 카드(artifact)를 주고받으며 협업하고, 실패 시 Orchestrator가 1회 재실행 feedback을 거는 SOP 기반 일봉 트레이딩 시스템을 만든다.**

이번 v3는 기존 KOSPI v2에서 **FAILAB 3편 메인라인**을 버리고,
**FAILAB 1편(Instability Index) + 최신 2025 논문 2편(UMI, MLF)** 으로 KOSPI 현지화 보강 레이어를 재구성한 버전이다.

---

## 2. v2 대비 무엇이 바뀌었는가

### 바뀌지 않은 것
- 4-agent 구조: Data / Strategy / Risk / Backtest
- Orchestrator + Artifact Registry + Feedback Loop
- 6개 코어 설계 논문 사용
- Strategy 내부 핵심 구조:
  `Quant Prefilter → Market Analyst → News/Disclosure Analyst Lite → General Synthesizer`
- KOSPI 대형주 고정 유니버스, 일봉, `T일 18:00 KST → T+1 open`
- ownership freeze: AI #1 / AI #2 / BE #1 / BE #2

### 바뀐 것
기존 KOSPI v2의 KOSPI-local 보강 레이어는 아래 3편이었다.
- FAILAB HMM Momentum (2020)
- FAILAB Instability Index + GA (2020)
- FAILAB Low-pass Filtered LSTM (2020)

이번 v3에서는 다음처럼 재배치한다.

#### 메인라인에 남김
- **FAILAB Instability Index + GA (2020)**
  - Risk Agent의 exposure / cash ratio / de-risk gate

#### 메인라인에서 제외하고 baseline/appendix로 후퇴
- **FAILAB HMM Momentum (2020)**
  - classical regime baseline
- **FAILAB Low-pass Filtered LSTM (2020)**
  - classical denoising baseline

#### 새로 메인라인에 추가
- **UMI (KDD 2025)**
  - regime/stress factor / irrationality factor
- **MLF (KDD 2025)**
  - multi-period feature extraction / feature bank

즉, KOSPI local layer는 이제
**“FAILAB anchor 1편 + 최신 2025 quant 2편”** 구조다.

---

## 3. 우리가 실제로 쓰는 논문의 개수

## 3.1 메인라인 논문: 총 9편

### Core 6 (변경 없음)
1. **MetaGPT**
2. **TradExpert**
3. **AAPM**
4. **AlphaGAT**
5. **AlphaAgent**
6. **R&D-Agent-Quant**

### KOSPI local quant/risk layer 3
7. **FAILAB Instability Index + GA (2020)**
8. **UMI (KDD 2025)**
9. **MLF (KDD 2025)**

## 3.2 비교/부록용 baseline 논문 (메인라인 아님)
- FAILAB **Momentum HMM (2020)**
- FAILAB **Low-pass Filtered LSTM (2020)**

즉,
- **메인라인 설계 논문 수 = 9편**
- **보조 baseline/appendix 논문 수 = 2편**

---

## 4. 논문 레이어 구조

```text
Layer 1. System / Orchestrator / SOP
  -> MetaGPT

Layer 2. Strategy Internal Division
  -> TradExpert

Layer 3. Strategy Reasoning / Fusion / Evaluation Discipline
  -> AAPM

Layer 4. Quant Core / Factor Engine
  -> AlphaGAT
  -> AlphaAgent
  -> MLF
  -> UMI

Layer 5. KOSPI-local Risk / Allocation Gate
  -> FAILAB Instability Index + GA

Layer 6. Offline Research Outer Loop
  -> R&D-Agent-Quant
```

핵심은,
- **6편 코어 논문은 그대로 유지**하고
- FAILAB 3편 전체를 메인라인에 두는 대신
- **Instability Index만 남기고 UMI/MLF로 quant layer를 업그레이드**했다는 점이다.

---

## 5. KOSPI 프로젝트의 불변 전제

- 시장: **KOSPI (유가증권시장)**
- 빈도: **일봉**
- 유니버스: **기준일 고정 KOSPI 대형주 20~30개**
- 정보 스냅샷: **T일 18:00 KST**
- 사용 가능 공시/뉴스: **timestamp ≤ T일 18:00 KST**
- 18:00 이후 정보: **T+1 정보셋으로 이월**
- 집행 시점: **T+1 open**
- 거래비용: **편도 10bp slippage + 수수료**
- 검증 방식: **walk-forward 최소 3-fold**
- 결과 해석: strict asset-pricing claim보다 **architecture ablation + engineering evaluation** 중심

---

## 6. 최종 파이프라인 (논문 반영 위치 포함)

```text
[KOSPI 대형주 고정 유니버스 20~30개]
                │
                ▼
      [1. DATA AGENT]
  - OHLCV / 거래량 / 기술팩터 / 공시 메타 / 뉴스 메타
  - security_master / corp_code ↔ ticker 매핑
  - MLF-inspired multi-period feature bank 생성
  - (appendix 비교용) Low-pass denoising baseline 가능

  반영 논문:
  - MetaGPT: DailyMarketPacket 스키마 / handoff contract
  - AAPM: refined input / relevance check의 출발점
  - MLF: multi-period feature extraction
                │
                ▼
        DailyMarketPacket
                │
                ▼
     [2-1. QUANT PREFILTER]
  - 전체 유니버스 점수화
  - next-day cross-sectional ranking
  - Top 5~8 shortlist 생성
  - 입력 = manual factors + MLF features + UMI factors
  - MVP backbone = LightGBM LambdaMART ranker
  - 1차 확장 = AlphaGAT Stage-I inspired factor head

  반영 논문:
  - TradExpert: shortlist 먼저, 전 종목 LLM 금지
  - AlphaGAT: raw → factor 변환 철학
  - MLF: multi-period 입력 통합
  - UMI: irrationality / market stress feature
                │
                ▼
     [2-2. MARKET ANALYST]
  - shortlist 종목의 가격/기술팩터 근거 해석
  - why-top-ranked explanation 생성
  - UMI stock-level irrationality / market-level stress 반영
  - 내부 중간 산출물: QuantEvidenceCard, MarketAnalysis

  반영 논문:
  - TradExpert: Market Analyst 분업
  - AAPM: 정성+정량 결합 태도
  - UMI: regime/stress factor
                │
                ▼
 [2-3. NEWS / DISCLOSURE ANALYST LITE]
  - 공시 제목 / 공시 유형 / 뉴스 헤드라인 / 핵심 문장
  - relevance check
  - refined text 생성
  - 내부 중간 산출물: TextSignalCard

  반영 논문:
  - AAPM: refined news / relevance check
  - TradExpert: News Analyst 우선순위
                │
                ▼
   [2-4. GENERAL SYNTHESIZER]
  - QuantEvidence + MarketAnalysis + TextSignalCard 종합
  - contradiction / confidence / thesis / action 생성

  반영 논문:
  - TradExpert: General Expert
  - AAPM: report + factor fusion 철학
  - MetaGPT: StrategyCard 스키마
                │
                ▼
          StrategyCard
                │
                ▼
          [3. RISK AGENT]
  - position cap
  - stop-loss
  - turnover cap
  - cash ratio
  - InstabilityIndexGate
  - (보조) UMI market stress cap

  반영 논문:
  - FAILAB Instability Index: exposure / cash gating
  - UMI: market stress 보조 신호
  - MetaGPT: RiskCard / OrderPlan contract
                │
                ▼
      RiskCard + OrderPlan
                │
                ▼
        [4. BACKTEST AGENT]
  - T+1 open execution
  - fee / slippage
  - walk-forward
  - signal/risk ablation
  - failure tagging / broken assumption labeling

  반영 논문:
  - MetaGPT: executable feedback
  - AAPM: leakage-aware evaluation discipline
  - TradExpert: ablation / trading metric 참고
                │
                ▼
 BacktestReport + FailureCaseCard
                │
                ▼
        [ORCHESTRATOR]
  - dependency check
  - artifact routing
  - 실패 시 Strategy 1회 재실행
  - versioning / experiment mode 제어

  반영 논문:
  - MetaGPT
```

---

## 7. Quant model에는 정확히 무엇이 들어가는가

이번 v3에서 **quant model stack**은 아래처럼 정의한다.

## 7.1 MVP main quant model

### (A) Manual technical factor bank
기본 수동 팩터 그룹:
- 수익률: 1D / 5D / 20D return
- 추세: SMA gap, EMA gap, MACD, MACD signal
- 모멘텀: RSI, Stochastic K/D, ROC
- 변동성: ATR, realized vol(5/20/60), intraday range proxy
- 거래량: volume z-score, turnover proxy
- 위치: Bollinger %B, close-to-range
- 시장 컨텍스트: KOSPI index return, market breadth, dispersion

### (B) **MLF-inspired multi-period feature bank**
MLF의 핵심 아이디어를 그대로 축소 이식한다.

적용 방식:
- 입력 윈도우: **5 / 20 / 60 trading days**
- period별 patch/summary feature 생성
- **IRF** 아이디어로 window 간 중복 정보 감소
- **LWI** 아이디어로 period별 feature importance를 학습형 가중으로 통합
- **MAP** 철학을 반영해 서로 다른 길이 입력을 균형 있게 처리
- **Patch Squeeze**는 full 재현 대신 연산량 절감 원칙으로만 차용

출력:
- `MLFFeatureVector`

### (C) **UMI-inspired irrationality / stress factors**
UMI에서 다음 두 축을 차용한다.

1. **stock-level irrationality feature**
   - 실제 가격 vs 추정 rational price 괴리
   - 개별 종목의 비정상성 / 과열 / 과매도 플래그

2. **market-level irrationality / synchronism feature**
   - 종목 동조성 이상 징후
   - 시장 전체의 irrational stress level

출력:
- `UMIStockFactor`
- `UMIMarketStress`

### (D) **Quant Prefilter 본체: LightGBM LambdaMART ranker**
MVP main ranker는 안정성을 위해 **LightGBM LambdaMART**로 둔다.

입력:
- manual factor bank
- MLF-inspired feature bank
- UMI-inspired irrationality / stress factors

출력:
- 종목별 ranking score
- shortlist Top 5~8
- feature importance / local contribution 근거

타깃:
- **T+1 open-to-open cross-sectional ranking**

### (E) **Backtest 계산 엔진**
- T+1 open execution
- fee / slippage 반영
- walk-forward
- portfolio return / Sharpe / MDD / turnover / hit rate 계산
- FailureCase tagging

즉, MVP의 quant stack은

```text
Manual Factors
 + MLF-inspired Multi-period Features
 + UMI-inspired Irrationality Factors
 -> LightGBM LambdaMART Ranker
 -> QuantEvidenceCard
```

으로 정리된다.

---

## 7.2 1차 확장 quant model

### (A) AlphaGAT Stage-I inspired factor head
- raw OHLCV / volume 시계열을 learned factor space로 변환
- CATimeMixer full reproduction이 아니라
  **raw → robust factor representation** 철학을 차용
- 출력 learned factors를 ranker 입력에 추가

### (B) AlphaAgent-inspired Factor/Alpha Analyst
- novelty / complexity control을 고려한 alpha idea generation
- full AST engine 재현은 하지 않음
- Factor / Alpha Analyst 설계 원리로만 차용

### (C) AAPM memory / notes / retrieval
- News/Disclosure Analyst와 General Synthesizer 사이 강화
- report-level refinement
- macro / micro notes

---

## 7.3 2차 확장 / 개인 연구 트랙

### RL / allocator 방향
이번 v3의 mainline은 안정성 우선이라 RL을 core에서 뺀다.
하지만 **개인 quant 연구 트랙**으로는 아래가 가장 자연스럽다.

- **AlphaGAT Stage-II**: PPO + GAT allocator 참고
- **Diffolio**: risk-aware generative portfolio optimization 참고
- **R&D-Agent-Quant**: factor-model outer research loop 참고

즉,
- **이번 학기 mainline** = ML + factor + reasoning + risk gating
- **네 개인 연구 트랙** = allocator / RL / auto-quant expansion

---

## 8. 6 코어 논문 + 3 KOSPI local 논문이 파이프라인에 어떻게 반영되는가

| 단계 | 반영 논문 | 구체 반영 내용 |
|---|---|---|
| Orchestrator / Registry / Cards | MetaGPT | SOP, artifact, dependency, feedback |
| Data Agent | MetaGPT / AAPM / MLF | DailyMarketPacket, refined input 출발점, multi-period feature bank |
| Quant Prefilter | TradExpert / AlphaGAT / MLF / UMI | shortlist 원칙, learned factor 철학, multi-period features, irrationality factors |
| Market Analyst | TradExpert / UMI | market reasoning, irrationality/stress explanation |
| News/Disclosure Analyst Lite | AAPM / TradExpert | refined text, relevance check, text reasoning |
| General Synthesizer | TradExpert / AAPM | final thesis, contradiction, confidence, report+factor fusion |
| Risk Agent | FAILAB Instability / UMI / MetaGPT | exposure gate, cash ratio, market stress cap, RiskCard contract |
| Backtest Agent | MetaGPT / AAPM / TradExpert | walk-forward, leakage-aware evaluation, FailureCase feedback |
| Factor/Alpha 확장 | AlphaGAT / AlphaAgent | learned factor / alpha extension |
| Outer Research Loop | R&D-Agent-Quant | factor-model co-optimization |

---

## 9. Ownership freeze (최종안)

## 9.1 사람 기준

| 팀원 | 역할 | 비중 |
|---|---|---:|
| **AI #1** | Data Agent + Quant Prefilter + Backtest 계산 엔진 + KOSPI local quant layer(MLF, UMI factor, Instability signal 계산) | **30%** |
| **AI #2** | Market Analyst + News/Disclosure Analyst Lite + General Synthesizer + Risk 규칙 + Failure 해석 | **27%** |
| **BE #1** | Orchestrator + Artifact Registry + Scheduler + Experiment mode | **23%** |
| **BE #2** | API + Full-stack + Dashboard + Data source adapter | **20%** |

## 9.2 파이프라인 기준 세부 ownership

| 파이프라인 / 모듈 | 주 담당 | 보조 |
|---|---|---|
| security_master / corp_code 매핑 | **BE #2** | AI #1 |
| 가격/공시/뉴스 수집 adapter | **BE #2** | BE #1 |
| DailyMarketPacket schema / quality flags | **AI #1** | BE #1 |
| manual factor bank | **AI #1** | AI #2 |
| MLF-inspired feature bank | **AI #1** | AI #2 |
| UMI-inspired stock/market factor | **AI #1** | AI #2 |
| Quant Prefilter (LightGBM ranker) | **AI #1** | AI #2 |
| QuantEvidenceCard | **AI #1** | AI #2 |
| Market Analyst | **AI #2** | AI #1 |
| News/Disclosure Analyst Lite | **AI #2** | AI #1 |
| General Synthesizer / StrategyCard | **AI #2** | AI #1 |
| InstabilityIndexGate signal 계산 | **AI #1** | AI #2 |
| Risk 규칙 적용 / RiskCard / OrderPlan 의미 설계 | **AI #2** | BE #1 |
| Backtest simulator / metrics | **AI #1** | BE #1 |
| FailureCaseCard 해석 | **AI #2** | AI #1 |
| Orchestrator / dependency / retry | **BE #1** | BE #2 |
| Artifact Registry / versioning | **BE #1** | BE #2 |
| Experiment mode (S/R/Q ablation) | **BE #1** | AI #1 |
| Dashboard / charts / card viewer | **BE #2** | AI #2(semantic review) |

---

## 10. 이번 버전이 이전 FAILAB 3편 버전보다 왜 더 좋은가

### 이전 버전
- FAILAB HMM
- FAILAB Instability Index
- FAILAB Low-pass LSTM

### 현재 v3
- FAILAB Instability Index (유지)
- UMI (추가)
- MLF (추가)
- HMM / Low-pass LSTM은 appendix baseline으로 후퇴

### 장점
1. **FAILAB 연결성은 유지**된다.
2. **기술적으로는 2025 최신 quant 논문으로 업그레이드**된다.
3. KOSPI 일봉 / 대형주 시스템에 맞는 **regime/stress + feature extraction**이 더 강해진다.
4. 면접에서는
   **“연구실 아이디어를 anchor로 두고 최신 FAI 방법론으로 확장·현지화했다”**
   는 스토리를 만들 수 있다.

---

## 11. 팀 회의용 최종 요약

### 아주 짧은 버전
> 우리는 KOSPI 대형주 고정 유니버스용 협업형 트레이딩 시스템을 만든다. 
> 6편 코어 논문은 유지하고, KOSPI local quant/risk layer는 FAILAB Instability Index + UMI + MLF로 재구성한다. 
> Quant model의 mainline은 `MLF + UMI + LightGBM ranker`이고, AlphaGAT/AlphaAgent는 1차 확장, RL allocator는 개인 연구 확장 트랙이다.

### 회의용 5문장
1. **프로젝트의 본질은 예측 모델 하나가 아니라 KOSPI용 4-agent 협업형 트레이딩 시스템이다.**
2. **6편 코어 논문은 그대로 유지하고, FAILAB 보강 레이어만 바꾼다.**
3. **FAILAB 3편 전체를 메인라인에 두지 않고, Instability Index만 남기고 UMI와 MLF를 추가한다.**
4. **Quant mainline은 MLF multi-period features + UMI irrationality factors + LightGBM ranker다.**
5. **Ownership은 Quant(AI #1), Reasoning(AI #2), Platform(BE #1), Full-stack(BE #2)로 고정한다.**

---

## 12. 다음 회의에서 확정할 것

1. Quant Prefilter의 정확한 target definition
2. MLF-inspired 구현을 full reproduction이 아니라 어느 수준까지 차용할지
3. UMI-inspired factor를 몇 개의 feature로 축소할지
4. InstabilityIndexGate를 cash ratio에 어떻게 연결할지
5. 내부 중간 카드(`QuantEvidenceCard`, `MarketAnalysis`, `TextSignalCard`)의 registry 저장 여부
6. appendix baseline(HMM, Low-pass LSTM) 실험을 실제로 넣을지 여부


# KOSPI 최종계획서 v5 (B안 + RL 강화 + 현실성 보정)

## 1. 핵심 변경 요약

- 코어 6편은 유지한다.
- KOSPI 현지화 보강 레이어는 **FAILAB Instability Index 1편 + UMI + MLF**로 유지한다.
- FAILAB HMM, FAILAB Low-pass LSTM은 mainline에서 내리고 **appendix baseline**으로 유지한다.
- Quant model은 **signal layer + reasoning layer + RL allocator layer**의 3단 구조를 유지한다.
- RL은 전 종목 end-to-end가 아니라 **shortlist 이후 allocation / risk-aware policy**에만 사용한다.
- **Layer 6을 6a / 6b로 분리**한다.
  - **Layer 6a** = 이번 학기 실제 구현 후보 RL allocator
  - **Layer 6b** = 개인 연구 트랙 / 문헌 reserve
- **W4 중간발표는 C' 범위**로 고정한다.
  - 실제 front-half + 가벼운 end-to-end artifact loop
- **W9 RL은 must-have가 아니라 prototype / 초기 실험**으로 낮춘다.
  - RL이 늦어져도 프로젝트 mainline은 흔들리지 않도록 설계한다.

---

## 2. 현재 mainline에 실제로 쓰는 논문 수

### 2.1 아키텍처 mainline (9편)
1. MetaGPT
2. TradExpert
3. AAPM
4. AlphaGAT
5. AlphaAgent
6. R&D-Agent-Quant
7. FAILAB Instability Index + GA (2020)
8. UMI (KDD 2025)
9. MLF (KDD 2025)

### 2.2 RL allocator / reserve reference (추가)
- **Layer 6a: 실제 구현 후보**
  - AlphaGAT Stage-II inspired PPO allocator *(AlphaGAT은 mainline 9편에 이미 포함)*
  - HSTP 2026 (hierarchical signal-to-policy PPO)
  - Smart Tangency Portfolio 2025 (practical PPO/A2C baseline)

- **Layer 6b: RL research reserve / 개인 연구 트랙**
  - MetaTrader 2025 (bilevel offline RL)
  - Heuristic-guided IRL + Graph Policy Learning 2025
  - Attention-enhanced Dirichlet Policy RL 2026
  - Can DRL Beat 1/N? (2025, evaluation discipline)

즉,
- **아키텍처 mainline = 9편**
- **전체 참고문헌 풀 = 15편**
- 단, 이번 학기 코어에서 실제로 구현을 강하게 노리는 RL은 **Layer 6a**만 본다.

---

## 3. 기존 FAILAB 3편에서 무엇이 바뀌었는가

### 이전 (v2)
- Instability Index + GA
- Momentum HMM
- Low-pass Filtered LSTM

### 현재 (v5)
- 유지: Instability Index + GA
- 교체: Momentum HMM -> UMI
- 교체: Low-pass Filtered LSTM -> MLF

### Appendix baseline으로 남기는 것
- FAILAB HMM = classical regime baseline
- FAILAB Low-pass LSTM = classical denoising baseline

### 의미
- FAILAB 연결고리는 **Risk / Allocation 축**에서 유지한다.
- 최신성은 **regime/stress factor(UMI)** 와 **feature extraction(MLF)** 로 보강한다.
- 즉, FAILAB을 버린 것이 아니라 **anchor 1편 + 최신 2편으로 업그레이드**한 구조다.

---

## 4. 4-agent 구조는 그대로 유지

```text
Data Agent -> Strategy Agent -> Risk Agent -> Backtest Agent
                       ^                                  |
                       |----------------------------------|
                           FailureCase feedback loop
```

구조적으로는 그대로이며,
업그레이드는 전부 **Strategy 내부 quant core / Risk gating / allocator** 쪽에서 발생한다.

---

## 5. 최신 quant / RL stack 정의

## 5.1 Mainline signal layer

```text
Manual factor bank
+ MLF-inspired multi-period feature bank
+ UMI-inspired irrationality factors
-> LightGBM LambdaMART ranker
-> Top 5~8 shortlist
```

### 포함 기법
- 수동 기술/시장 팩터
- MLF-lite multi-period input bank
- UMI-lite irrationality factor
- LightGBM ranker

### 핵심 의미
- 이번 학기 팀 mainline의 정량 backbone은 **랭커 기반 shortlist 생성**이다.
- RL이 여기서 종목 전체를 고르는 역할을 하지 않는다.

## 5.2 Reasoning layer

```text
Top 5~8 shortlist
 -> Market Analyst
 -> News/Disclosure Analyst Lite
 -> General Synthesizer
 -> StrategyCard
```

### 포함 기법
- TradExpert식 Market / News / Synthesizer 분업
- AAPM식 refined text / relevance check / report-fusion 원칙

## 5.3 RL allocator layer (1차 확장)

```text
Top-K shortlist + signal features + risk state
    -> PPO-based allocator
    -> weights / cash ratio / rebalance horizon
```

### 채택 원칙
- RL은 full-universe selector가 아니라 **shortlist allocator**
- risk-aware reward 사용
- transaction cost 반영
- drawdown / CVaR / turnover 제약 포함
- RL이 늦어지더라도 mainline 발표와 실험 구조는 성립해야 한다.

---

## 6. UMI를 우리 프로젝트에서 어떻게 구현할 것인가

## 6.1 UMI 원문 핵심
- stock-level irrationality = 실제 가격과 추정 rational price의 괴리
- market-level irrationality = anomalous synchronism

## 6.2 프로젝트용 구현 정의 (UMI-lite)

### stock-level rational price proxy
각 종목 i에 대해,
- 동종 섹터 peer basket
- KOSPI 대형주 universe 내 상관 높은 종목들
- 시장 지수(KOSPI200 또는 대표지수)
를 조합해 soft rational price proxy를 만든다.

```text
p_tilde(i,t) = Σ_j att(i,j) * p(j,t)
```

여기서 `att(i,j)`는 rolling correlation 또는 lightweight attention으로 계산한다.

### stock-level irrationality factor
```text
u_stock(i,t) = (p_tilde(i,t) - p(i,t)) / vol20(i,t)
```

### market-level irrationality factor
```text
u_market(t) = anomalous synchronism score
```
예:
- rolling average correlation surge
- cross-sectional dispersion compression
- market-wide co-movement spike

### 최종 사용 위치
- Quant Prefilter feature
- Market Analyst 보조 state
- Risk Agent stress gate 보조 signal

---

## 7. MLF를 우리 프로젝트에서 어디까지 차용할 것인가

## 7.1 MLF 원문 핵심
- IRF: inter-period redundancy filtering
- LWI: learnable weighted-average integration
- MAP: multi-period adaptive patching
- Patch Squeeze: 효율화

## 7.2 프로젝트용 차용 수준

### MVP / mainline에서 반드시 넣는 것
**MLF-lite = multi-period bank + IRF-lite + LWI-lite**

#### multi-period bank
- short: 5일
- medium: 20일
- long: 60일

#### IRF-lite
- period별 feature redundancy pruning
- highly correlated features 제거
- importance/SHAP 기준 중복 제거

#### LWI-lite
- 기간별 score를 learned scalar gate로 통합

```text
score = w5 * score_5 + w20 * score_20 + w60 * score_60
```

### 1차 확장에 넣는 것
- MAP-lite (neural branch에서 동일 patch count 유지)

### 이번 학기 보류
- full Patch Squeeze
- full end-to-end MLF reproduction

즉, 우리는 **MLF의 철학을 차용하되, full reproduction은 하지 않는다.**

---

## 8. RL 최신 논문 추가 세트 (2025/2026)

## 8.1 Layer 6a: 실제 구현 후보 RL allocator

### (A) AlphaGAT Stage-II inspired PPO allocator
- Stage I factor space 위에 Stage II policy를 얹는 철학
- factor importance / allocation 분리 설계
- 이번 시스템 구조와 가장 잘 맞는 RL 철학 기준점

### (B) HSTP 2026
- Stage 1: gradient-boosted tree + SHAP signal
- Stage 2: PPO under mean-CVaR reward
- **signal extraction과 policy learning을 분리**
- 현재 `ranker -> shortlist -> allocator` 구조와 가장 잘 맞음

### (C) Smart Tangency Portfolio 2025
- PPO / A2C actor-critic practical baseline
- mean-variance / semivariance / CVaR 통합
- 구현 난도가 상대적으로 낮아 **실무형 baseline**으로 좋음

## 8.2 Layer 6b: RL research reserve / 개인 연구 트랙

### (D) MetaTrader (2025)
- bilevel RL
- offline data에 과적합된 정책이 비현실적 매매를 암기하는 문제를 지적
- in-domain + out-of-domain performance 동시 최적화

### (E) Heuristic-guided IRL + Graph Policy Learning (IJCAI 2025)
- expert strategy generation
- inverse RL 기반 reward recovery
- graph policy learning
- diversification / correlation 구조 반영에 유리

### (F) Attention-enhanced Dirichlet Policy RL (2026)
- Dirichlet policy로 weight feasibility 보장
- cross-sectional attention으로 asset 관계 반영
- realistic turnover / drawdown을 고려하는 allocator 구조

### (G) Can DRL Beat 1/N? (2025)
- SAC 기반 대규모 평가
- DRL이 market timing은 보이지만 turnover 때문에 net benefit이 약할 수 있음을 보여줌
- RL 평가 discipline용 필수 참고문헌

---

## 9. 최종 권장 RL 전략

## 9.1 팀 mainline
```text
MLF-lite + UMI-lite + LightGBM ranker
 -> shortlist
 -> Market / News / Synthesizer
 -> Risk Agent
 -> Backtest
```

## 9.2 1차 확장 RL (이번 학기 실제 구현 후보)
```text
shortlist + signal features + risk state
 -> PPO allocator (HSTP-lite / Smart Tangency baseline / AlphaGAT Stage-II inspired)
```

### 원칙
- 이번 학기 안에 **실제로 손대는 RL은 이 레이어만** 본다.
- RL은 `must-have`가 아니라 **prototype / 초기 실험**이다.
- RL이 지연될 경우, 최종 발표에서는 **설계 + 초기 실험 + 향후 확장**으로 제시 가능해야 한다.

## 9.3 개인 연구 심화 트랙
- MetaTrader bilevel RL
- IRL + graph policy learning
- Dirichlet policy RL
- uncertainty gate / safe deployment

즉,
- **팀 mainline은 안정적 ML + reasoning**
- **네 개인 quant/RL 트랙은 allocator / offline RL / graph-RL**

---

## 10. 공식 카드 6종과 내부 중간 카드 정리

## 10.1 공식 카드 6종
1. DailyMarketPacket
2. StrategyCard
3. RiskCard
4. OrderPlan
5. BacktestReport
6. FailureCaseCard

## 10.2 내부 중간 카드
- QuantEvidenceCard
- MarketAnalysis
- TextSignalCard

## 10.3 원칙
- 공식 카드 6종 = 반드시 registry 저장
- 내부 중간 카드 = debug mode / experiment mode에서만 저장 가능
- 다음 회의에서 registry 저장 여부를 freeze

---

## 11. 최종 파이프라인과 논문 반영 위치

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
  -> HSTP 2026
  -> Smart Tangency Portfolio 2025

Layer 6b. RL Research Reserve (개인 연구 트랙)
  -> MetaTrader 2025
  -> Heuristic-guided IRL 2025
  -> Attention-enhanced Dirichlet Policy 2026
  -> Can DRL Beat 1/N? 2025
```

---

## 12. AI / BE ownership freeze

| 팀원 | 역할 | 비중 |
|---|---|---:|
| AI #1 | Data Agent + Quant Prefilter + Backtest 계산 엔진 + UMI/MLF/Instability signal layer + RL allocator prototype track | 30% |
| AI #2 | Market + News/Disclosure + Synthesizer + Risk 규칙 + Failure 해석 | 27% |
| BE #1 | Orchestrator + Registry + Scheduler + Experiment mode | 23% |
| BE #2 | API + Full-stack + Dashboard + Data source adapter | 20% |

### ownership 원칙
- AI #1 = **정량 코어 + allocator 프로토타입**
- AI #2 = **reasoning 코어 + risk/failure semantics**
- BE #1 = **platform / orchestration 코어**
- BE #2 = **API / full-stack / dashboard 코어**

---

## 13. 11주 현실 일정 (중간발표 4주, 최종발표 11주)

## W1
- v5 문서 freeze
- ownership freeze
- 공식 카드 6종 스키마 freeze
- 내부 중간 카드 저장 정책 freeze

## W2
- KOSPI security_master
- Data source adapter
- DailyMarketPacket 생성

## W3
- Manual factor bank
- MLF-lite multi-period bank
- LightGBM ranker baseline

## W4 (중간발표) — **C' demo 범위 고정**
### 반드시 보여줄 것
- 실제 KOSPI 고정 유니버스
- 실제 Data Agent
- 실제 DailyMarketPacket 생성 및 registry 저장
- 실제 Quant Prefilter 실행 -> Top 5~8 shortlist
- 실제 StrategyCard v0 생성
- artifact trace viewer

### thin baseline / mock 허용
- RiskCard v0
- BacktestReport v0
- FailureCaseCard v0

### 중간발표 목표
- “예측 성능”이 아니라 **4-agent 구조와 artifact-driven workflow가 실제로 굴러가기 시작했다**는 것을 보여준다.

## W5
- UMI-lite factor 구현
- Market Analyst 연결
- QuantEvidenceCard debug 저장

## W6
- News/Disclosure Analyst Lite
- General Synthesizer
- StrategyCard end-to-end 안정화

## W7
- Risk Agent + Instability gate
- RiskCard / OrderPlan 연결

## W8
- Backtest Agent
- BacktestReport / FailureCaseCard
- feedback loop 1회 재실행

## W9
- RL allocator **설계 확정 + 프로토타입 + 초기 실험**
- 후보: HSTP-lite PPO 또는 Smart Tangency baseline
- fallback: RL은 최종 발표에서 **초기 실험 + 향후 확장**으로 제시 가능

## W10
- S0~S3, R0~R2 ablation
- RL on/off 비교 *(프로토타입 수준이면 appendix로)*

## W11 (최종발표 준비)
- 안정화
- 최종 표/차트/발표자료
- appendix baseline(HMM, Low-pass LSTM) 정리
- RL reserve 문헌 정리

---

## 14. 이번 버전의 한 줄 정의

> KOSPI 대형주 고정 유니버스에서, MetaGPT식 협업 구조와 TradExpert/AAPM식 전략 reasoning을 유지한 채, FAILAB Instability + UMI + MLF를 quant mainline으로 올리고, RL은 shortlist 이후 PPO allocator의 프로토타입으로만 다루는 SOP 기반 금융 멀티에이전트 시스템.

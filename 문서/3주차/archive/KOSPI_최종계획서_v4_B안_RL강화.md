# KOSPI 최종계획서 v4 (B안 + RL 강화)

## 1. 핵심 변경 요약

- 코어 6편은 유지한다.
- KOSPI 현지화 보강 레이어는 **FAILAB Instability Index 1편 + UMI + MLF**로 재구성한다.
- FAILAB HMM, FAILAB Low-pass LSTM은 mainline에서 내리고 appendix baseline으로 유지한다.
- Quant model은 **signal layer + reasoning layer + RL allocator layer**의 3단 구조로 확장한다.
- RL은 전 종목 end-to-end가 아니라 **shortlist 이후 allocation / risk-aware policy**에 투입한다.

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

### 2.2 RL research track reference (추가)
- AlphaGAT Stage II (already counted in mainline)
- HSTP 2026 (Hierarchical Signal-to-Policy Learning)
- MetaTrader 2025 (Bilevel RL)
- Heuristic-guided IRL + Graph Policy Learning 2025
- Smart Tangency Portfolio 2025
- Attention-enhanced Dirichlet Policy RL 2026
- Can DRL Beat 1/N (2025, evaluation discipline)

즉,
- **아키텍처 mainline = 9편**
- **전체 참고문헌 풀 = 9편 + RL 추가 참고 6편 = 15편**

---

## 3. 기존 FAILAB 3편에서 무엇이 바뀌었는가

### 이전 (v2)
- Instability Index + GA
- Momentum HMM
- Low-pass Filtered LSTM

### 현재 (v4)
- 유지: Instability Index + GA
- 교체: Momentum HMM → UMI
- 교체: Low-pass Filtered LSTM → MLF

### Appendix baseline으로 남기는 것
- FAILAB HMM = classical regime baseline
- FAILAB Low-pass LSTM = classical denoising baseline

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
- 시장 지수(KOSPI200 또는 코스피 대표지수)
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

## 8.1 반드시 읽을 RL 논문

### (A) AlphaGAT (IJCAI 2025)
- Stage I: CATimeMixer로 alpha factor 생성
- Stage II: PPO + GAT로 factor weight 동적 조절
- 현재 프로젝트의 RL allocator 철학과 가장 직접적으로 연결

### (B) HSTP 2026
- Stage 1: gradient-boosted tree + SHAP signal
- Stage 2: PPO under mean-CVaR reward
- **signal extraction과 policy learning을 분리**
- 지금 프로젝트 구조와 가장 잘 맞음

### (C) MetaTrader (2025)
- bilevel RL
- offline data에 과적합된 정책이 비현실적 매매를 암기하는 문제를 지적
- in-domain + out-of-domain performance를 동시에 최적화
- KOSPI처럼 데이터가 제한된 환경에서 안전장치로 참고 가치 큼

### (D) Heuristic-guided IRL + Graph Policy Learning (IJCAI 2025)
- expert strategy generation
- inverse RL 기반 reward recovery
- graph policy learning
- diversification / correlation 구조 반영에 유리

### (E) Smart Tangency Portfolio (2025)
- PPO / A2C actor-critic
- mean-variance / semivariance / CVaR 통합
- dynamic rebalancing과 risk-return tradeoff를 명시적으로 다룸
- practical baseline으로 좋음

### (F) Attention-enhanced Dirichlet Policy RL (2026)
- Dirichlet policy로 weight feasibility를 보장
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

## 9.2 1차 확장 RL
```text
shortlist + signal features + risk state
 -> PPO allocator (HSTP / AlphaGAT Stage-II inspired)
```

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

Layer 6. RL Allocator / Research Reserve
  -> HSTP 2026
  -> MetaTrader 2025
  -> Heuristic-guided IRL 2025
  -> Smart Tangency Portfolio 2025
  -> Attention-enhanced Dirichlet Policy 2026
```

---

## 12. AI / BE ownership freeze

| 팀원 | 역할 | 비중 |
|---|---|---:|
| AI #1 | Data Agent + Quant Prefilter + Backtest 계산 엔진 + UMI/MLF/Instability signal layer + RL allocator research track | 30% |
| AI #2 | Market + News/Disclosure + Synthesizer + Risk 규칙 + Failure 해석 | 27% |
| BE #1 | Orchestrator + Registry + Scheduler + Experiment mode | 23% |
| BE #2 | API + Full-stack + Dashboard + Data source adapter | 20% |

---

## 13. 11주 현실 일정 (중간발표 4주, 최종발표 11주)

### W1
- v4 문서 freeze
- ownership freeze
- 공식 카드 6종 스키마 freeze
- 내부 중간 카드 저장 정책 freeze

### W2
- KOSPI security_master
- Data source adapter
- DailyMarketPacket 생성

### W3
- Manual factor bank
- MLF-lite multi-period bank
- LightGBM ranker baseline

### W4 (중간발표)
- Quant Prefilter demo
- Strategy skeleton demo
- artifact trace demo

### W5
- UMI-lite factor 구현
- Market Analyst 연결
- QuantEvidenceCard debug 저장

### W6
- News/Disclosure Analyst Lite
- General Synthesizer
- StrategyCard end-to-end

### W7
- Risk Agent + Instability gate
- RiskCard / OrderPlan 연결

### W8
- Backtest Agent
- BacktestReport / FailureCaseCard
- feedback loop 1회 재실행

### W9
- RL allocator baseline (PPO/HSTP-lite)
- 또는 Smart Tangency baseline

### W10
- S0~S3, R0~R2 ablation
- RL on/off 비교

### W11 (최종발표 준비)
- 안정화
- 최종 표/차트/발표자료
- appendix baseline(HMM, Low-pass LSTM) 정리

---

## 14. 이번 버전의 한 줄 정의

> KOSPI 대형주 고정 유니버스에서, MetaGPT식 협업 구조와 TradExpert/AAPM식 전략 reasoning을 유지한 채, FAILAB Instability + UMI + MLF를 quant mainline으로 올리고, AlphaGAT/HSTP 계열 PPO allocator를 1차 확장 RL layer로 연결하는 SOP 기반 금융 멀티에이전트 시스템.

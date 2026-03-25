# KOSPI 프로젝트 매핑서 v5

> 목적: KOSPI 프로젝트에 반영되는 **mainline 9편**과 **RL reserve 6편**을 레이어별로 정리하고, 무엇을 가져오고 무엇을 가져오지 않는지 팀 차원에서 명확히 하기 위한 기준 문서

---

## 0. 먼저 확정하는 핵심

이 프로젝트는 **논문 하나를 재현하는 프로젝트가 아니다.**  
여러 논문의 아이디어를 서로 다른 레이어에 배치해 하나의 협업형 트레이딩 시스템으로 재조합하는 프로젝트다.

### 레이어 구조 (v5)

| Layer | 역할 | 논문 |
|---|---|---|
| **Layer 1** | System / Orchestrator / SOP | **MetaGPT** |
| **Layer 2** | Strategy Internal Division | **TradExpert** |
| **Layer 3** | Strategy Reasoning / Fusion / Evaluation Discipline | **AAPM** |
| **Layer 4** | Quant Core / Feature Extraction / Stress Factors | **AlphaGAT, AlphaAgent, UMI, MLF** |
| **Layer 5** | KOSPI-local Risk / Allocation Gate | **FAILAB Instability Index** |
| **Layer 6a** | RL Allocator (이번 학기 실제 구현 후보) | **AlphaGAT Stage-II inspired PPO, HSTP-lite, Smart Tangency** |
| **Layer 6b** | RL Research Reserve (개인 연구 트랙) | **MetaTrader, IRL+Graph, Attention-Enhanced RL, Can DRL Beat 1/N?** |
| **Outer Loop** | Offline quant research / factor-model co-design | **R&D-Agent-Quant** |

### mainline과 reserve
- **mainline = 9편**
- **RL reserve 포함 전체 참고문헌 풀 = 15편**
- 이번 학기 발표에서 강하게 가져가는 것은 **Layer 1~5 + Layer 6a 일부**다.

---

## 1. 코어 6편: 그대로 유지되는 구조

## 1.1 MetaGPT
### 질문
여러 agent가 복잡한 작업을 어떻게 안정적으로 협업하는가?

### KOSPI 프로젝트에서의 위치
- Orchestrator
- Artifact Registry
- dependency-based activation
- feedback loop

### 가져오는 것
- SOP
- structured artifact
- shared message pool의 철학
- publish-subscribe
- executable feedback

### 가져오지 않는 것
- 소프트웨어 회사 역할 체계를 그대로 복제
- 코드 생성 benchmark 성능 숫자
- MetaGPT framework 전체 재현

### 한 줄 번역
**MetaGPT = 시스템 협업 문법**

---

## 1.2 TradExpert
### 질문
뉴스/시장/알파/펀더멘털을 전문가 분업으로 어떻게 종합해 트레이딩 결정을 내리는가?

### KOSPI 프로젝트에서의 위치
- Strategy Agent 내부 구조
- Market/News 우선순위
- shortlist 원칙

### 가져오는 것
- `Quant Prefilter -> Market -> News/Disclosure -> Synthesizer`
- Market/News가 핵심이라는 ablation 교훈
- 전 종목 LLM 처리 금지
- ML + LLM 하이브리드 구조

### 가져오지 않는 것
- 4개 Expert LLM 전부 LoRA fine-tuning
- O(N^2) full pairwise ranking 구현
- OHLCV reprogramming full reproduction

### 한 줄 번역
**TradExpert = Strategy 내부 분업 구조**

---

## 1.3 AAPM
### 질문
뉴스에서 얻은 정성 신호를 정량 팩터와 어떻게 결합할 것인가?

### KOSPI 프로젝트에서의 위치
- News/Disclosure relevance check
- refined text
- Strategy extension / fusion
- evaluation discipline

### 가져오는 것
- refined news
- relevance check
- report + factor 결합 철학
- memory / notes / retrieval 확장 경로
- leakage awareness

### 가져오지 않는 것
- full knowledge base 구축
- full report embedding architecture reproduction
- knowledge cutoff 정책의 기계적 복제

### 한 줄 번역
**AAPM = reasoning/fusion + 평가 규율**

---

## 1.4 AlphaGAT
### 질문
raw market data를 더 robust한 alpha/factor로 만들고, 그 중요도를 적응적으로 조절할 수 있는가?

### KOSPI 프로젝트에서의 위치
- Quant Core
- learned factor / allocator 철학

### 가져오는 것
- Stage I factor space 아이디어
- Stage II에서 “신호 생성과 allocation을 분리한다”는 철학
- shortlist 이후 allocator 설계의 기준점

### 가져오지 않는 것
- full CATimeMixer reproduction을 mainline 필수로 두는 것
- full PPO+GAT allocator를 이번 학기 core로 고정하는 것

### 한 줄 번역
**AlphaGAT = quant backbone + allocator 철학**

---

## 1.5 AlphaAgent
### 질문
시간이 지나도 빨리 죽지 않는 alpha를 어떻게 찾을 것인가?

### KOSPI 프로젝트에서의 위치
- Factor / Alpha Analyst 확장
- outer loop에서의 factor invention

### 가져오는 것
- novelty / originality를 고려한 alpha 발굴 관점
- complexity control
- alpha decay에 대한 문제의식

### 가져오지 않는 것
- full AST novelty engine
- factor invention loop 전체 재현

### 한 줄 번역
**AlphaAgent = factor/alpha 확장 철학**

---

## 1.6 R&D-Agent-Quant
### 질문
factor와 model을 자동으로 공동 최적화하는 quant R&D loop를 만들 수 있는가?

### KOSPI 프로젝트에서의 위치
- offline research outer loop
- 이번 학기 runtime core 바깥

### 가져오는 것
- research → dev → feedback 루프 관점
- factor/model co-optimization의 outer loop 구조

### 가져오지 않는 것
- 이번 학기 full auto-quant lab 구현

### 한 줄 번역
**R&D-Agent-Quant = outer research loop**

---

## 2. KOSPI local mainline 3편

## 2.1 FAILAB Instability Index + GA (2020)
### 질문
시장 불안정 상태를 측정해 자산배분/위험관리를 자동화할 수 있는가?

### KOSPI 프로젝트에서의 위치
- Risk Agent
- exposure cap / cash ratio / de-risk gate

### 가져오는 것
- instability score로 risk gate를 거는 발상
- 한국시장 anchor
- asset allocation / risk-control 연결고리

### 가져오지 않는 것
- GA threshold optimization의 full reproduction
- robo-advisor 전체 문제 설정

### 한 줄 번역
**Instability Index = KOSPI local risk/allocation anchor**

---

## 2.2 UMI (2025)
### 질문
시장 irrationality를 stock-level과 market-level에서 factor로 만들 수 있는가?

### KOSPI 프로젝트에서의 위치
- Quant Core
- Market Analyst 보조 signal
- Risk Agent stress gate 보조 signal

### 가져오는 것
- stock-level irrationality gap
- market-level anomalous synchronism / stress
- regime label 대신 stress factor로 쓰는 관점

### 가져오지 않는 것
- 원문의 full architecture 재현
- 원문 rational price estimator의 완전 복제

### 우리 프로젝트의 번역
- `UMI-lite rational price proxy`
- `u_stock`, `u_market` feature

### 한 줄 번역
**UMI = regime/stress factor의 메인라인 대체재**

---

## 2.3 MLF (2025)
### 질문
여러 기간의 금융 시계열 입력을 정확하고 효율적으로 통합할 수 있는가?

### KOSPI 프로젝트에서의 위치
- Data Agent feature bank
- Quant Prefilter input backbone

### 가져오는 것
- multi-period input bank
- IRF-lite redundancy pruning
- LWI-lite weighted integration

### 1차 확장으로 남기는 것
- MAP-lite

### 보류하는 것
- Patch Squeeze full reproduction
- full end-to-end MLF 재현

### 한 줄 번역
**MLF = feature extraction / multi-period bank mainline**

---

## 3. FAILAB에서 무엇이 빠졌고, 왜 빠졌는가

## 3.1 FAILAB HMM
### 현재 위치
- appendix baseline
- classical regime baseline

### 왜 mainline에서 내렸나
- regime detection의 baseline으로는 유용하지만,
- mainline에서 요구되는 최신성 / stress factor 확장성 / KOSPI large-cap multi-feature 구조와 비교하면 UMI가 더 유연하다.

## 3.2 FAILAB Low-pass LSTM
### 현재 위치
- appendix baseline
- classical denoising baseline

### 왜 mainline에서 내렸나
- `denoise -> signal` 철학은 좋지만,
- multi-period feature extraction과 redundancy handling 관점에서는 MLF가 더 직접적이다.

### 핵심 원칙
FAILAB을 버린 것이 아니라,
- **Instability Index는 anchor로 유지하고**
- HMM / Low-pass LSTM은 **classical comparison baseline**으로 내린 것이다.

---

## 4. RL allocator / reserve 매핑

## 4.1 Layer 6a — 실제 구현 후보

### (A) AlphaGAT Stage-II inspired PPO allocator
- factor importance / allocation 분리 설계
- shortlist 이후 allocation으로 번역하기 좋음

### (B) HSTP-lite
- signal extraction과 policy learning을 분리
- PPO under risk-aware objective
- 현재 시스템의 `ranker -> shortlist -> allocator` 구조와 가장 잘 맞음

### (C) Smart Tangency Portfolio
- PPO / A2C practical baseline
- mean-variance / CVaR와 연결하기 쉬움
- 구현 난도가 상대적으로 낮음

## 4.2 Layer 6b — 개인 연구 reserve

### MetaTrader
- partial-offline RL
- bilevel optimization
- OOD performance를 함께 최적화

### Heuristic-guided IRL + Graph Policy Learning
- expert strategy + inverse RL + graph policy
- correlation / diversification structure 반영에 유리

### Attention-Enhanced Dirichlet Policy RL
- Dirichlet policy로 feasibility 보장
- cross-sectional attention으로 asset 관계 반영

### Can DRL Beat 1/N?
- SAC 대규모 평가
- RL 평가 규율 / negative evidence
- “RL이 항상 이긴다”는 낙관을 제어하는 문헌

### 핵심 규칙
- Layer 6a만 이번 학기 실제 구현 후보
- Layer 6b는 **개인 연구 트랙 / 문헌 reserve**

---

## 5. 한 장 요약: 최종 매핑

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
  -> Attention-Enhanced RL
  -> Can DRL Beat 1/N?

Outer Loop
  -> R&D-Agent-Quant
```

---

## 6. 팀이 절대 오해하면 안 되는 것

1. **MetaGPT는 전략 모델이 아니라 협업 구조 논문이다.**
2. **TradExpert는 mainline reasoning 구조를 정의하지만 full reproduction 대상은 아니다.**
3. **AAPM은 Data Agent 논문이 아니라 Strategy 확장/fusion 논문이다.**
4. **UMI는 HMM의 direct replacement라기보다 stress/regime factor다.**
5. **MLF는 full reproduction이 아니라 MLF-lite로 차용한다.**
6. **RL은 이번 학기 코어가 아니라 shortlist 이후 allocator prototype이다.**
7. **R&D-Agent-Quant는 runtime 코어가 아니라 outer research loop다.**

---

## 7. 팀원용 한 줄 결론

> **KOSPI v5 프로젝트는 코어 6편으로 협업 구조와 reasoning을 고정하고, Instability + UMI + MLF로 KOSPI local quant/risk layer를 보강하며, RL은 shortlist 이후 PPO allocator 프로토타입으로만 다루는 구조다.**

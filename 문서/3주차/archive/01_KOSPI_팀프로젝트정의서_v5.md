# KOSPI 전용 팀 프로젝트 정의서 v5

> 이 문서는 **KOSPI_최종계획서_v5_B안_RL강화.md**와 완전히 동기화된 팀 내부 기준 문서다.  
> 기준 버전의 핵심은 **코어 6편 유지 + KOSPI local mainline 3편(Instability, UMI, MLF) + RL allocator는 1차 확장/프로토타입**이다.

---

## 1. 이 프로젝트를 한 문장으로 설명하면

**KOSPI 대형주 고정 유니버스를 대상으로, Data → Strategy → Risk → Backtest 4개 에이전트가 구조화된 카드(artifact)를 주고받으며 협업하고, 실패 시 Orchestrator가 1회 재실행 feedback을 거는 SOP 기반 일봉 트레이딩 시스템을 만든다.**

---

## 2. 우리가 실제로 만드는 것은 무엇인가

이 프로젝트는 **주가 예측 모델 하나**를 만드는 프로젝트가 아니다.  
또 **MetaGPT를 그대로 복제**하는 프로젝트도 아니다.  
또 **TradExpert처럼 여러 LLM을 전부 LoRA fine-tuning**하는 프로젝트도 아니다.

우리가 만드는 것은:

- 한국 주식 트레이딩 업무 전체를
- 4개의 역할 에이전트로 분해하고
- 각 에이전트가 자기 책임에 맞는 입력을 받아
- 구조화된 산출물을 만든 뒤
- 다음 에이전트로 핸드오프하며
- 마지막에는 백테스트 결과를 다시 전략 수정으로 연결하는

**end-to-end 협업형 트레이딩 시스템**이다.

즉, **목표는 KOSPI 트레이딩 시스템**이고, **에이전트 구조는 그 시스템을 설계하는 방식**이다.

---

## 3. 이번 버전(v5)의 핵심 변화

### 유지되는 것
- 4-agent 구조
- JSON artifact handoff
- Orchestrator + Artifact Registry
- `Data → Strategy → Risk → Backtest → feedback` 루프
- `Quant Prefilter → Market Analyst → News/Disclosure Analyst Lite → General Synthesizer`
- S0~S4 / R0~R3 실험 철학

### 바뀐 것
- FAILAB 3편을 전부 메인라인에 두지 않는다.
- **FAILAB Instability Index + GA**만 메인라인에 남긴다.
- **UMI (2025)** 를 regime/stress factor의 메인라인으로 올린다.
- **MLF (2025)** 를 feature extraction / multi-period feature bank의 메인라인으로 올린다.
- RL은 전 종목 selector가 아니라 **shortlist 이후 allocator**로 내린다.
- RL 문헌은 **Layer 6a(실구현 후보)** 와 **Layer 6b(개인 연구 reserve)** 로 분리한다.

### 현재 논문 구조
- **아키텍처 mainline = 9편**
  - 코어 6편: MetaGPT, TradExpert, AAPM, AlphaGAT, AlphaAgent, R&D-Agent-Quant
  - KOSPI local mainline 3편: FAILAB Instability Index, UMI, MLF
- **RL reserve = 6편**
  - HSTP, Smart Tangency, MetaTrader, Heuristic-guided IRL, Attention-Enhanced RL, Can DRL Beat 1/N?

---

## 4. KOSPI 버전의 불변 전제

## 4.1 시장 / 유니버스
- **시장:** KOSPI (유가증권시장)
- **빈도:** 일봉
- **유니버스:** 기준일 고정 **KOSPI 대형주 20~30개**
- **원칙:** static ex-ante universe. 사후적으로 종목을 바꾸지 않는다.
- **권장안:** `2020-01-02 기준 KOSPI large-cap 25종목`

## 4.2 정보 시계(clock) / 누수 방지
- **정규시장 데이터 cutoff:** T일 15:30 KST
- **공시/뉴스 반영 cutoff:** **T일 18:00 KST**
- **18:00 이후 텍스트 정보:** T+1 정보셋으로 이월
- **집행 시점:** **T+1 open**
- **거래비용:** 편도 10bp slippage + 수수료
- **검증 방식:** walk-forward 최소 3-fold

## 4.3 해석상 주의
- 상용 LLM의 한국어 금융 지식과 사전학습 누수를 완전히 제거하지는 못할 수 있다.
- 결과 해석은 strict asset-pricing claim보다 **architecture ablation + engineering evaluation** 중심으로 한다.
- `pykrx`류는 실무상 유용하지만 source of record는 아니다.
- KOSPI 버전은 **미국 논문 재현 프로젝트**가 아니라 **한국시장 현지화 프로젝트**다.

---

## 5. 4개 에이전트의 역할 정의

## 5.1 Data Agent
### 역할
트레이딩 판단에 필요한 원재료를 수집·정제·정렬한다.

### semester mainline 범위
- OHLCV 수집
- 수동 기술/시장 팩터 생성
- **MLF-lite multi-period feature bank** 생성
- 공시 메타데이터 / 공시 제목 수집
- 뉴스 헤드라인 메타데이터 수집
- 결측/시간 정렬
- 종목코드 매핑(`security_master`) 관리
- 데이터 캐시

### 출력
- `DailyMarketPacket`

### 주의
- 재무제표/실적/Fundamental full parse는 **MVP 필수 아님**
- MVP에서는 **가격 + 팩터 + 공시/뉴스 메타데이터**가 우선

---

## 5.2 Strategy Agent
### 역할
정량 신호와 공시/뉴스 신호를 종합해서 **무엇을 살지/줄일지/건너뛸지** 판단한다.

### v5 기준 3단 구조

#### Stage 1. Signal Layer
`Manual factor bank + MLF-lite + UMI-lite -> LightGBM LambdaMART ranker -> Top 5~8 shortlist`

#### Stage 2. Reasoning Layer
`Market Analyst -> News/Disclosure Analyst Lite -> General Synthesizer -> StrategyCard`

#### Stage 3. RL Allocator Layer (1차 확장)
`Top-K shortlist + signal features + risk state -> PPO allocator -> weights / cash ratio / rebalance horizon`

### 현재 확정된 내부 흐름
`Quant Prefilter → Market Analyst → News/Disclosure Analyst Lite → General Synthesizer`

### 의미
1. **Quant Prefilter**
   - 고정 KOSPI 유니버스 전체를 먼저 점수화
   - 후보 종목을 5~8개로 축소
   - 전 종목 LLM 처리 금지 원칙을 보장

2. **Market Analyst**
   - 가격/기술팩터/UMI-lite stress signal을 해석
   - 왜 이 종목이 상위 후보인지 정리

3. **News/Disclosure Analyst Lite**
   - 공시 제목 / 공시 유형 / 뉴스 헤드라인 중심
   - AAPM식 relevance check / refined text 철학 반영

4. **General Synthesizer**
   - 정량 + 정성 근거를 합쳐 최종 thesis 생성
   - contradiction / confidence / recommendation 정리

### 출력
- `StrategyCard`

### 핵심 원칙
- Strategy Agent는 **단일 모델**이 아니다.
- 여러 하위 모듈의 출력을 받아 구조화된 최종 판단을 만드는 **상위 의사결정 계층**이다.
- RL은 여기서 full selector가 아니라 **shortlist 이후 allocator**로만 다룬다.

---

## 5.3 Risk Agent
### 역할
전략 판단을 그대로 주문으로 내보내지 않고, 위험 제약을 적용해 **실행 가능한 주문 계획**으로 바꾼다.

### baseline 범위
- position cap
- stop-loss
- turnover cap
- cash ratio

### v5 KOSPI-local mainline
- **Instability Index gate**
  - 노출 축소 / 현금 비중 조절
- **UMI-lite market stress signal**
  - stress regime에서 cap 강화

### 출력
- `RiskCard`
- `OrderPlan`

### 핵심 포인트
Risk Agent는 옵션이 아니라 **독립 에이전트**다.  
리스크는 전략 뒤에 붙는 부가기능이 아니라 별도 의사결정 단계다.

---

## 5.4 Backtest Agent
### 역할
전략이 실제 과거 구간에서 어떻게 동작했는지 검증하고, 실패 원인을 다시 시스템에 되돌린다.

### 범위
- T+1 open execution
- fee / slippage 반영
- walk-forward evaluation
- 기본 성능지표 계산
- 실패 구간 tagging

### 출력
- `BacktestReport`
- `FailureCaseCard`

### 핵심 포인트
Backtest Agent는 “수익률만 출력하는 계산기”가 아니다.  
실패 구간과 실패 원인을 구조화해서 다시 Strategy 쪽으로 보내는 **feedback 엔진**이다.

---

## 6. 카드(artifact) 정책

## 6.1 공식 카드 6종
1. `DailyMarketPacket`
2. `StrategyCard`
3. `RiskCard`
4. `OrderPlan`
5. `BacktestReport`
6. `FailureCaseCard`

### 원칙
- 공식 카드 6종은 **반드시 registry에 저장**한다.
- 버전은 덮어쓰지 않고 `run_id / version / retry_count`로 관리한다.

## 6.2 내부 중간 카드 3종
- `QuantEvidenceCard`
- `MarketAnalysis`
- `TextSignalCard`

### 원칙
- 내부 중간 카드는 **debug / experiment mode에서만 저장 가능**
- 공식 카드와의 관계는 별도 회의에서 freeze한다.

---

## 7. 어떤 논문이 어디에 들어가는가

### 아키텍처 코어 6편
- **MetaGPT** = 상위 협업 구조 / artifact / feedback
- **TradExpert** = Strategy 내부 분업
- **AAPM** = reasoning/fusion + evaluation discipline
- **AlphaGAT** = factor/allocator 철학
- **AlphaAgent** = factor/alpha 확장
- **R&D-Agent-Quant** = outer research loop

### KOSPI local mainline 3편
- **FAILAB Instability Index** = Risk / Allocation anchor
- **UMI** = regime / stress / irrationality factor
- **MLF** = feature extraction / multi-period bank

### appendix baseline
- FAILAB HMM = classical regime baseline
- FAILAB Low-pass LSTM = classical denoising baseline

### RL allocator / reserve
- **Layer 6a (실제 구현 후보)**
  - AlphaGAT Stage-II inspired PPO allocator
  - HSTP-lite PPO allocator
  - Smart Tangency PPO/A2C baseline
- **Layer 6b (연구 reserve)**
  - MetaTrader
  - Heuristic-guided IRL + Graph Policy Learning
  - Attention-Enhanced Dirichlet Policy RL
  - Can DRL Beat 1/N?

---

## 8. MVP / 1차 확장 / reserve

## 8.1 반드시 완성해야 하는 것 (이번 학기 mainline)
- 4-agent 분리
- JSON 카드 스키마
- SQLite artifact registry
- KOSPI 일봉 / T+1 open / walk-forward
- security_master
- Manual factor bank
- **MLF-lite multi-period feature bank**
- **UMI-lite irrationality factor**
- LightGBM ranker
- Market Analyst
- News/Disclosure Analyst Lite
- General Synthesizer
- baseline Risk rule + Instability gate
- Backtest + FailureCaseCard
- 1회 feedback loop

## 8.2 1차 확장
- RL allocator prototype (Layer 6a)
- AAPM식 memory / notes / retrieval
- Factor / Alpha Analyst
- 더 강한 Synthesizer

## 8.3 reserve / appendix
- full AlphaGAT Stage-II
- Diffolio
- full AlphaAgent invention loop
- R&D-Agent-Quant outer loop automation
- FAILAB HMM / Low-pass LSTM classical baseline
- RL reserve 문헌 전부 실구현

---

## 9. 중간발표와 최종발표에서의 목표

## 중간발표 (W4)
목표는 **성능**이 아니라 **구조가 실제로 굴러간다**는 것을 보여주는 것이다.

### C' 데모 범위
- 실제 KOSPI 고정 유니버스
- 실제 Data Agent
- 실제 `DailyMarketPacket` 생성 및 registry 저장
- 실제 Quant Prefilter 실행 -> Top 5~8 shortlist
- 실제 `StrategyCard v0` 생성
- artifact trace viewer
- Risk / Backtest는 thin baseline 또는 mock 허용

## 최종발표 (W11)
- signal + reasoning mainline 완성
- Risk / Backtest / feedback loop 안정화
- S0~S3 / R0~R2 ablation
- RL allocator는 **초기 실험 / prototype** 까지면 충분

---

## 10. Ownership freeze (Option A')

| 팀원 | 역할 | 비중 |
|---|---|---:|
| **AI #1** | Data Agent + Quant Prefilter + Backtest 계산 엔진 + UMI/MLF/Instability signal layer + RL allocator prototype track | **30%** |
| **AI #2** | Market + News/Disclosure + Synthesizer + Risk 규칙 + Failure 해석 | **27%** |
| **BE #1** | Orchestrator + Artifact Registry + Scheduler + Experiment mode | **23%** |
| **BE #2** | API + Full-stack + Dashboard + Data source adapter | **20%** |

### 역할 원칙
- AI #1 = **정량 코어 + allocator prototype**
- AI #2 = **reasoning 코어 + risk/failure semantics**
- BE #1 = **platform / orchestration 코어**
- BE #2 = **API / full-stack / dashboard 코어**

---

## 11. 한 줄 최종 정의

> **KOSPI 대형주 고정 유니버스에서, MetaGPT식 협업 구조와 TradExpert/AAPM식 전략 reasoning을 유지한 채, FAILAB Instability + UMI + MLF를 quant mainline으로 올리고, RL은 shortlist 이후 PPO allocator의 프로토타입으로만 다루는 SOP 기반 금융 멀티에이전트 시스템.**

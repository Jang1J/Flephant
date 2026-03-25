# KOSPI 프로젝트 최종 정리 및 파트 분배안 v1

## 1. 프로젝트 한 줄 정의

**KOSPI 대형주 고정 유니버스를 대상으로, Data → Strategy → Risk → Backtest 4개 에이전트가 구조화된 카드(artifact)를 주고받으며 협업하고, 실패 시 Orchestrator가 1회 재실행 feedback을 거는 SOP 기반 일봉 트레이딩 시스템을 만든다.**

---

## 2. 우리가 사용하는 논문 개수

총 **6편**이다.

### Layer 1. System / Orchestrator / SOP
1. **MetaGPT**

### Layer 2. Strategy Internal Division
2. **TradExpert**

### Layer 3. Strategy Reasoning / Fusion / Evaluation Discipline
3. **AAPM**

### Layer 4. Quant Core / Factor Engine
4. **AlphaGAT**
5. **AlphaAgent**

### Layer 5. Offline Research Outer Loop
6. **R&D-Agent-Quant**

---

## 3. 6편 논문의 역할 구분

| 논문 | 실제로 답하는 질문 | 우리 프로젝트에서 들어가는 위치 | 이번 학기 반영 강도 |
|---|---|---|---|
| MetaGPT | 여러 agent가 어떻게 안정적으로 협업하는가 | 상위 시스템 구조 / 오케스트레이터 / 메시지 프로토콜 | **MVP 필수** |
| TradExpert | 뉴스/시장/알파/펀더멘털을 전문가 분업으로 어떻게 종합하는가 | Strategy Agent 내부 분업 구조 | **MVP 필수** |
| AAPM | 정성 신호와 정량 팩터를 어떻게 결합하는가 | Strategy 확장 reasoning/fusion + 평가 규율 | **MVP-lite + 1차 확장** |
| AlphaGAT | raw 시장데이터를 robust factor로 만들고 가중치를 적응적으로 조절할 수 있는가 | Quant Core / Factor Engine | **MVP 참고 + 1차 확장** |
| AlphaAgent | 빨리 죽지 않는 alpha를 어떻게 발명할 것인가 | Factor / Alpha Analyst 확장 | **1차 확장** |
| R&D-Agent-Quant | factor와 model을 자동으로 공동 최적화하는 outer loop를 만들 수 있는가 | Offline Research Outer Loop | **후속 연구** |

---

## 4. KOSPI 버전 프로젝트의 불변 전제

- 시장: **KOSPI (유가증권시장)**
- 빈도: **일봉**
- 유니버스: **기준일 고정 KOSPI 대형주 20~30개**
- 정보 스냅샷: **T일 18:00 KST**
- 사용 가능 텍스트: **timestamp ≤ T일 18:00 KST**
- 18:00 이후 공시/뉴스: **T+1 정보셋으로 이월**
- 집행 시점: **T+1 open**
- 거래비용: **편도 10bp slippage + 수수료**
- 검증 방식: **walk-forward 최소 3-fold**

---

## 5. 최종 파이프라인 (KOSPI Runtime Loop)

```text
[고정 KOSPI 대형주 유니버스 20~30개]
                │
                ▼
      [1. Data Agent]
  - OHLCV / 거래량 / 기술팩터
  - 공시 메타데이터
  - 뉴스 헤드라인 메타데이터
  - security_master
                │
                ▼
        DailyMarketPacket
                │
                ▼
   [2-1. Quant Prefilter]
  - 전체 유니버스 점수화
  - Top 5~8 shortlist
  - (AlphaGAT Stage-I 철학 참고 가능)
                │
                ▼
     [2-2. Market Analyst]
  - 가격/기술팩터 근거 해석
  - KOSPI 종목별 시장 신호 설명
  - (TradExpert Market Analyst 구조 참고)
                │
                ▼
[2-3. News/Disclosure Analyst Lite]
  - 공시 제목 / 공시 유형 / 뉴스 헤드라인
  - relevance check / refined text
  - (AAPM + TradExpert 결합)
                │
                ▼
   [2-4. General Synthesizer]
  - 정량 + 텍스트 근거 종합
  - contradiction / confidence / thesis 생성
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
  - (향후 instability gate / regime gate)
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
  - 실패 시 Strategy 1회 재실행
```

---

## 6. 논문이 파이프라인 어디에 반영되는가

## 6.1 상위 시스템 레이어

### MetaGPT
반영 위치:
- Orchestrator
- Artifact Registry
- JSON 카드 스키마
- dependency-based activation
- feedback retry 1회

핵심 역할:
- 자유 대화 금지
- structured artifact 기반 handoff
- 실행 결과로 전략 교정

---

## 6.2 Strategy 내부 분업 레이어

### TradExpert
반영 위치:
- Quant Prefilter 이후 shortlist 전략
- Market Analyst
- News/Disclosure Analyst Lite
- General Synthesizer

핵심 역할:
- 전문가 분업 구조
- Market/News 우선
- 전 종목 LLM 처리 금지

---

## 6.3 Strategy 확장 / fusion 레이어

### AAPM
반영 위치:
- News/Disclosure Analyst Lite의 relevance check
- refined text
- 정량 + 정성 분리 후 결합 원칙
- 1차 확장의 memory / notes / retrieval
- 평가 규율(leakage awareness)

핵심 역할:
- report + factor fusion
- knowledge-cutoff를 의식한 평가 태도

---

## 6.4 Quant Core / Factor Engine 레이어

### AlphaGAT
반영 위치:
- Quant Prefilter의 learned variant 후보
- Factor Engine의 raw → factor 변환 철학
- 향후 allocator 고도화 참고

### AlphaAgent
반영 위치:
- 1차 확장 Factor / Alpha Analyst
- novelty / complexity control을 고려한 alpha 확장

핵심 역할:
- 지금 당장 full reproduction이 아니라
- **Quant Core의 설계 철학 보강**

---

## 6.5 Outer Research Loop 레이어

### R&D-Agent-Quant
반영 위치:
- 이번 학기 runtime loop 밖
- 후속 연구용 자동 quant R&D loop

핵심 역할:
- factor/model 공동 탐색
- offline research automation

---

## 7. 논문 → 파이프라인 매핑 표

| 파이프라인 단계 | 핵심 역할 | 반영 논문 |
|---|---|---|
| Orchestrator / Registry / Cards | SOP, handoff, dependency, feedback | **MetaGPT** |
| Data Agent | 데이터 소스 분리, refined input의 출발점 | **AAPM, TradExpert, MetaGPT** |
| Quant Prefilter | shortlist 생성, 전 종목 LLM 금지 | **TradExpert, AlphaGAT** |
| Market Analyst | 가격/기술팩터 해석 | **TradExpert** |
| News/Disclosure Analyst Lite | 공시/헤드라인 relevance check + 요약 | **AAPM, TradExpert** |
| General Synthesizer | 정량+정성 종합 판단 | **TradExpert, AAPM** |
| Risk Agent | 독립 리스크 제약 | **MetaGPT(구조), 향후 FAILAB/확장 논문 연계 가능** |
| Backtest Agent | 실행 검증 + failure feedback | **MetaGPT, AAPM, TradExpert** |
| Factor/Alpha 확장 | alpha mining / factor engine | **AlphaGAT, AlphaAgent** |
| Outer Research Loop | factor-model joint optimization | **R&D-Agent-Quant** |

---

## 8. 4인 팀 최종 ownership freeze (권장안)

### 최종 추천: Option A'

| 팀원 | 역할 | 비중 |
|---|---|---:|
| **AI #1** | Data Agent + Quant Prefilter + Backtest 계산 엔진 | **30%** |
| **AI #2** | Market + News/Disclosure + Synthesizer + Risk 규칙 + Failure 해석 | **27%** |
| **BE #1** | Orchestrator + Registry + Scheduler + 실험 모드 | **23%** |
| **BE #2** | API + Full-stack + Dashboard | **20%** |

### 팀 단위 파트 비중
- **AI: 57%**
- **BE: 31%**
- **FE: 12%**

---

## 9. 파이프라인 기준 owner 매핑

| 파이프라인 / 모듈 | 주 담당 | 보조 |
|---|---|---|
| security_master / 종목코드 매핑 | **BE #2** | AI #1 |
| 가격/공시/뉴스 수집 adapter | **BE #2** | BE #1 |
| DailyMarketPacket 스키마 / 품질 플래그 | **AI #1** | BE #1 |
| 기술팩터 생성 | **AI #1** | AI #2 |
| Quant Prefilter | **AI #1** | AI #2 |
| Quant evidence / feature importance | **AI #1** | AI #2 |
| Market Analyst | **AI #2** | AI #1 |
| News/Disclosure Analyst Lite | **AI #2** | AI #1 |
| General Synthesizer / StrategyCard | **AI #2** | AI #1 |
| RiskCard / OrderPlan 규칙 | **AI #2** | BE #1 |
| Backtest simulator / metrics | **AI #1** | BE #1 |
| FailureCaseCard 해석 | **AI #2** | AI #1 |
| Orchestrator / dependency / retry | **BE #1** | BE #2 |
| Artifact Registry / versioning | **BE #1** | BE #2 |
| Experiment mode (S/R ablation) | **BE #1** | AI #1 |
| Dashboard / card viewer / charts | **BE #2** | AI #2(semantic review) |

---

## 10. 팀원들에게 설명할 때 핵심 5문장

1. **우리는 모델 하나를 만드는 게 아니라, KOSPI용 협업형 트레이딩 시스템을 만든다.**
2. **6편 논문은 경쟁 관계가 아니라 서로 다른 레이어를 채우는 부품이다.**
3. **이번 학기 코어는 4-agent runtime loop이고, outer research loop는 후속 연구다.**
4. **KOSPI 버전의 핵심 차이는 구조가 아니라 시장 전제(유니버스, data clock, 공시/뉴스 처리)다.**
5. **Ownership은 정량 코어(AI #1), reasoning 코어(AI #2), 플랫폼 코어(BE #1), 서비스/UI 코어(BE #2)로 나누는 것이 가장 안정적이다.**

---

## 11. 다음 회의에서 바로 확정할 것

1. 고정 KOSPI 대형주 유니버스 리스트
2. `security_master` 스키마
3. Quant Prefilter 1차 후보 2개
4. News/Disclosure Analyst Lite 입력 범위
5. artifact 6종의 필수 필드 최종 freeze
6. owner별 Git 브랜치 / 폴더 ownership


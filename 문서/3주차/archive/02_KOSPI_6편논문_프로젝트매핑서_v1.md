
# KOSPI 전용 6편 논문 프로젝트 매핑서 v1

> 팀 내부 공유용 문서  
> 목적: 기존 6편 논문(MetaGPT, AAPM, TradExpert, AlphaGAT, AlphaAgent, R&D-Agent-Quant)을 **KOSPI 버전 프로젝트**에 어느 층위에, 어떤 강도로, 어떤 순서로 적용할지 정리하기 위한 기준 문서

---

## 0. 먼저 확정하는 핵심

이 6편은 모두 AI/agent를 말하지만, **같은 종류의 논문이 아니다.**  
그래서 KOSPI 프로젝트에 넣는 위치도 달라야 한다.

| 논문 | 실제로 답하는 질문 | KOSPI 프로젝트에서 들어가는 위치 |
|---|---|---|
| **MetaGPT** | 여러 agent가 복잡한 작업을 어떻게 안정적으로 협업하는가? | **상위 시스템 구조 / 오케스트레이터 / 메시지 프로토콜** |
| **TradExpert** | 뉴스/시장/알파/펀더멘털을 전문가 분업으로 어떻게 종합해 트레이딩 결정을 내리는가? | **Strategy Agent 내부 전문가 분업 구조** |
| **AAPM** | 뉴스/보고서에서 얻은 정성 신호를 정량 팩터와 어떻게 결합하는가? | **Strategy Agent의 확장 reasoning/fusion 층 + 평가 규율** |
| **AlphaGAT** | raw 시장데이터를 더 robust한 alpha/factor로 만들고, 그 가중치를 적응적으로 조절할 수 있는가? | **Quant Core / Factor Engine** |
| **AlphaAgent** | 시간이 지나도 빨리 죽지 않는 알파를 어떻게 발명할 것인가? | **Factor / Alpha Analyst 확장** |
| **R&D-Agent-Quant** | factor와 model을 자동으로 공동 최적화하는 quant R&D loop를 만들 수 있는가? | **Offline Research Outer Loop** |

### 한 줄 결론

KOSPI 프로젝트는 아래처럼 읽는 것이 가장 정확하다.

- **MetaGPT = 시스템 협업 문법**
- **TradExpert = 전략 계층의 전문가 분업**
- **AAPM = 정성-정량 결합과 평가 규율**
- **AlphaGAT = Quant Core**
- **AlphaAgent = Alpha/Factor 확장**
- **R&D-Agent-Quant = 오프라인 연구 루프**

즉, 6편을 똑같이 “전략 모델”로 읽으면 안 된다.

---

## 1. KOSPI 프로젝트의 기준 전제

이 문서는 아래 전제를 이미 확정된 것으로 둔다.

| 항목 | 기준 |
|---|---|
| 프로젝트 목표 | KOSPI용 **협업형 금융 트레이딩 시스템** 개발 |
| 시장 | KOSPI (유가증권시장) |
| 빈도 | 일봉 |
| 유니버스 | **기준일 고정 KOSPI 대형주 20~30개** |
| 정보 스냅샷 | **T일 18:00 KST** |
| 실행 시점 | **T+1 open** |
| 거래비용 | 편도 10bp slippage + 수수료 |
| 검증 방식 | walk-forward 최소 3-fold |
| 상위 구조 | Data / Strategy / Risk / Backtest + Orchestrator |
| 중간 산출물 | JSON 카드 + artifact registry |
| feedback | Backtest 결과를 전략 수정으로 되돌리는 loop |

### 현재 KOSPI MVP 구조

```text
Data Agent
  -> DailyMarketPacket
Strategy Agent
  -> Quant Prefilter
  -> Market Analyst
  -> News/Disclosure Analyst Lite
  -> General Synthesizer
  -> StrategyCard
Risk Agent
  -> RiskCard + OrderPlan
Backtest Agent
  -> BacktestReport + FailureCaseCard
Orchestrator
  -> dependency 체크 + 1회 재실행 feedback
```

---

## 2. MetaGPT: KOSPI 프로젝트에 어떻게 들어가는가

## 2.1 MetaGPT에서 가져와야 할 것

MetaGPT에서 가져와야 하는 핵심은 **모델 성능 숫자**가 아니다.  
핵심은 다음 다섯 가지다.

1. **SOP 기반 역할 분화**
2. **Structured Artifact**
3. **Shared Message Pool / Artifact Registry**
4. **Dependency-based Activation**
5. **Executable Feedback**

## 2.2 KOSPI 프로젝트로 번역하면

소프트웨어 개발 회사의 PRD, 설계문서, 테스트 문서를 금융 도메인 카드로 치환하면 된다.

| MetaGPT 원문 | KOSPI 프로젝트 번역 |
|---|---|
| PRD / 설계문서 / 태스크 | `DailyMarketPacket`, `StrategyCard`, `RiskCard`, `OrderPlan`, `BacktestReport`, `FailureCaseCard` |
| Shared message pool | **artifact registry (SQLite/JSON 기반)** |
| 구독/활성화 | `StrategyCard` 없으면 Risk 실행 금지, `OrderPlan` 없으면 Backtest 실행 금지 |
| Executable feedback | 실패 카드 발행 → 제약 강화 → Strategy 1회 재실행 |

## 2.3 MetaGPT에서 가져오지 않는 것
- 소프트웨어 회사를 그대로 재현하는 역할 체계
- 코드 생성 benchmark 성능 숫자
- 프레임워크 전체 재현

### 요약
MetaGPT는 KOSPI 프로젝트에서 **개별 모델**이 아니라,
**Orchestrator / Artifact Registry / Message Protocol** 층을 설계하는 근거다.

---

## 3. TradExpert: KOSPI 프로젝트에 어떻게 들어가는가

## 3.1 TradExpert에서 가져와야 할 것

TradExpert의 핵심은 **전략 계층의 전문가 분업**이다.

전략 판단을 하나의 모델이 통째로 하는 대신,

- News Analyst
- Market Analyst
- Alpha Expert
- Fundamental Analyst
- General Expert

로 나눈 뒤, 마지막에 General Expert가 종합한다.

이 구조는 KOSPI Strategy Agent 내부 구조와 가장 직접적으로 맞닿아 있다.

## 3.2 TradExpert가 KOSPI 프로젝트에 주는 가장 중요한 교훈

### 교훈 1. Market과 News가 가장 중요하다
원문 ablation의 메시지는 분명하다.

- Market Analyst 제거 → 큰 타격
- News Analyst 제거 → 큰 타격
- Alpha 제거 → 중간
- Fundamental 제거 → 상대적으로 작음

따라서 KOSPI에서도 우선순위는 아래처럼 간다.

1. **Quant Prefilter**
2. **Market Analyst**
3. **News/Disclosure Analyst Lite**
4. **General Synthesizer**
5. Factor / Alpha Analyst
6. Fundamental Analyst

### 교훈 2. 전 종목에 LLM을 돌리면 안 된다
TradExpert는 ranking mode가 무겁고 comparator가 비이행적이라 많은 비교를 요구한다.  
따라서 KOSPI 버전도 반드시 **shortlist 구조**를 가져가야 한다.

```text
고정 KOSPI 유니버스 전체
  -> Quant Prefilter
  -> Top 5~8 shortlist
  -> 그 다음에만 LLM 분석
```

### 교훈 3. 순수 LLM 시스템이 아니라 하이브리드 구조다
TradExpert 원문에서도 Alpha Expert는 LightGBM 기반 선별 모델을 쓴다.  
즉, TradExpert도 **ML + LLM 하이브리드**다.

## 3.3 KOSPI 프로젝트로 번역하면

### MVP에서 가져올 것
- **Quant Prefilter**
- **Market Analyst**
- **News/Disclosure Analyst Lite**
- **General Synthesizer**

### 1차 확장
- **Factor / Alpha Analyst**

### 2차 확장
- **Fundamental Analyst**

## 3.4 KOSPI 버전에서 현지화해야 하는 부분

TradExpert의 `News Analyst`는 KOSPI MVP에서 그대로 가져오기보다,
다음처럼 현지화하는 편이 더 정확하다.

- **공시 제목**
- **공시 유형**
- **관련 헤드라인**
- **핵심 문장(있다면 1~2개)**

즉 KOSPI MVP에서는 News Analyst라기보다
**News/Disclosure Analyst Lite**라고 보는 것이 더 맞다.

## 3.5 TradExpert에서 가져오지 않는 것
- 4개 Expert LLM 전부 LoRA fine-tuning
- OHLCV reprogramming full reproduction
- pairwise comparator O(N²) ranking의 full-scale 구현
- 영어 금융 기사 전처리 전체 재현

---

## 4. AAPM: KOSPI 프로젝트에 어떻게 들어가는가

## 4.1 AAPM에서 가져와야 할 것

AAPM의 핵심은 “뉴스를 읽는 agent” 그 자체보다,
**정성 보고서와 정량 팩터를 어떻게 결합할 것인가**에 있다.

가져와야 할 핵심은 아래 다섯 가지다.

1. **Refined News**
2. **Relevance Check**
3. **Memory / RAG**
4. **Macro/Micro Notes**
5. **Report Embedding + Manual Factor Fusion**

## 4.2 AAPM의 위치: Data Agent보다 Strategy 확장에 더 가깝다

AAPM을 Data Agent에만 넣으면 해석이 약해진다.  
물론 아래 요소는 Data Agent에 일부 들어갈 수 있다.

- refined news
- relevance check

하지만 AAPM의 진짜 강점은 아래에 있다.

- iterative refinement
- memory / notes
- report embedding
- factor fusion
- leakage-aware evaluation mindset

즉, AAPM은 KOSPI에서도 **Strategy Agent의 1차 확장 reasoning/fusion 층**으로 읽는 것이 더 정확하다.

## 4.3 KOSPI 프로젝트로 번역하면

### MVP-lite에서 가져올 것
- 공시/헤드라인 relevance check
- refined text
- 정성(텍스트)과 정량(수치)을 따로 처리한 뒤 결합한다는 원칙

### 1차 확장에서 가져올 것
- memory / notes
- retrieval
- report embedding + factor fusion
- General Synthesizer 강화

### 2차 이후 고려할 것
- 더 깊은 iterative refinement
- 더 넓은 knowledge base
- asset-specific embedding 대응 구조

## 4.4 KOSPI 현지화 포인트

AAPM 원문은 WSJ와 미국 자산가격 문제를 다루므로,
KOSPI 버전에서는 다음처럼 번역해야 한다.

- WSJ 뉴스 → **공시/국내 뉴스 헤드라인**
- macro note → **국내 거시 / 정책 / 환율 / 반도체 / 수출 문맥**
- relevance check → **투자 관련 공시/헤드라인 우선**
- report+factor fusion → 그대로 유지

## 4.5 AAPM에서 반드시 가져와야 하는 태도: leakage awareness

- 가능한 경우 knowledge-cutoff 이후 평가를 우선
- 어려운 경우 LLM pretraining leakage limitation 명시
- 결과 해석은 strict asset-pricing claim보다 **architecture ablation + engineering evaluation** 중심

---

## 5. AlphaGAT: KOSPI 프로젝트에 어떻게 들어가는가

## 5.1 AlphaGAT에서 가져와야 할 것

AlphaGAT의 핵심은 **2-stage quant architecture**다.

- **Stage I:** raw market data → robust alpha/factor
- **Stage II:** RL + GAT로 factor weights 적응적 조절

## 5.2 KOSPI 프로젝트에 넣는 위치

### MVP 또는 MVP+α에서 가져올 것
- **Stage I 철학**
  - raw OHLCV를 직접 판단하지 않고
  - 더 robust한 factor/representation으로 먼저 바꾸기
- **multi-period feature 설계**
- **factor diversity를 의식한 구조**

### 1차~2차 확장에서 가져올 것
- Stage II 철학
- allocator / weight 조절 policy
- factor importance의 적응적 조절

## 5.3 KOSPI 프로젝트에서의 의미

KOSPI 버전에서 AlphaGAT은 가장 직접적으로 **Quant Prefilter / Factor Engine**을 강화한다.  
특히 한국어 텍스트 신호가 아직 불안정하더라도, **정량 코어를 더 강하게 깔아주는 논문**으로 유용하다.

### 요약
AlphaGAT은 KOSPI 프로젝트에서 **Layer 4: Quant Core**를 담당한다.

---

## 6. AlphaAgent: KOSPI 프로젝트에 어떻게 들어가는가

## 6.1 AlphaAgent에서 가져와야 할 것

AlphaAgent는 alpha decay를 막기 위해 LLM 기반 alpha generation에 다음 규율을 넣는다.

- originality
- hypothesis-factor alignment
- complexity control

## 6.2 KOSPI 프로젝트에 넣는 위치

이 논문은 상위 4-agent runtime의 코어가 아니라,
**Factor / Alpha Analyst** 확장의 설계 원리로 들어간다.

### 1차 확장에서 가져올 것
- factor가 너무 기존 신호와 비슷하지 않게 보려는 태도
- 과도하게 복잡한 식을 억제하는 규칙
- “성능 좋은 알파”보다 “빨리 죽지 않는 알파”를 찾는 문제의식

### 2차 이후
- factor invention loop
- 더 정교한 novelty engine

### 요약
AlphaAgent는 KOSPI 프로젝트에서 **Layer 4의 확장형 factor research component**다.

---

## 7. R&D-Agent-Quant: KOSPI 프로젝트에 어떻게 들어가는가

## 7.1 R&D-Agent-Quant에서 가져와야 할 것

이 논문은 runtime trading system보다,
**오프라인 quant 연구 자동화**를 다룬다.

핵심은:

- Research stage
- Development stage
- Feedback stage
- factor-model co-optimization
- scheduler 기반 탐색 방향 결정

## 7.2 KOSPI 프로젝트에 넣는 위치

이번 학기 KOSPI 코어 runtime에는 넣지 않는다.  
대신 **후속 연구용 outer loop**로 둔다.

### 현재 학기
- full reproduction 안 함
- 참고 수준으로만 사용

### 후속 연구
- 자동 factor proposal
- model branch 탐색
- experiment scheduler
- research notebook 축적 구조

### 요약
R&D-Agent-Quant는 KOSPI 프로젝트에서 **Layer 5: Offline Research Outer Loop**다.

---

## 8. 6편의 최종 매핑: KOSPI 버전 한 장 요약

## 8.1 층위별 매핑

| 프로젝트 층위 | 주로 대응하는 논문 | 핵심 적용 내용 |
|---|---|---|
| **Layer 1. System / Orchestrator / SOP** | **MetaGPT** | SOP, 카드 스키마, artifact registry, dependency, feedback loop |
| **Layer 2. Strategy Internal Division** | **TradExpert** | Quant Prefilter → Market → News/Disclosure → Synthesizer, 전문가 우선순위 |
| **Layer 3. Strategy Reasoning / Fusion / Evaluation Discipline** | **AAPM** | refined text, relevance check, memory/notes, report+factor fusion, leakage awareness |
| **Layer 4. Quant Core / Factor Engine** | **AlphaGAT, AlphaAgent** | factor generation, factor diversity, alpha decay awareness, learned factor block |
| **Layer 5. Offline Research Outer Loop** | **R&D-Agent-Quant** | factor-model co-optimization, research automation, scheduler |

## 8.2 우리 프로젝트의 최종 해석

```text
상위 협업 구조는 MetaGPT식으로 만들고,
전략 계층의 분업은 TradExpert식으로 설계하며,
정성-정량 결합과 평가 규율은 AAPM식으로 가져가고,
정량 코어는 AlphaGAT/AlphaAgent로 보강하며,
장기적으로는 R&D-Agent-Quant식 outer loop로 확장한다.
```

---

## 9. KOSPI 4개 에이전트에의 최종 적용

## 9.1 Data Agent
### 역할
가격/거래량/기술팩터/공시/헤드라인을 수집·정제해 `DailyMarketPacket`을 생성

### MVP 범위
- OHLCV
- 기술팩터
- 공시 메타데이터
- 뉴스 헤드라인 메타데이터
- 종목코드 매핑

### 논문 적용
| 논문 | 적용 내용 |
|---|---|
| **MetaGPT** | `DailyMarketPacket` 스키마 고정 |
| **AAPM** | relevance check, refined text의 가벼운 버전 |
| **TradExpert** | News / Market / Alpha / Fundamental 데이터 소스 구분 관점 참고 |

### 주의
- 재무제표/실적/Fundamental full parse는 MVP 필수 아님
- KOSPI MVP에서는 **공시와 헤드라인을 먼저 안정화**하는 것이 더 중요

---

## 9.2 Strategy Agent
### 역할
DataPacket을 받아 후보 종목을 줄이고, 분석 보고서를 종합해 `StrategyCard`를 생성

### 최종 구조

```text
Quant Prefilter
  -> Market Analyst
  -> News/Disclosure Analyst Lite
  -> General Synthesizer
  -> StrategyCard
```

### MVP 필수
- Quant Prefilter
- Market Analyst
- News/Disclosure Analyst Lite
- General Synthesizer

### 1차 확장
- Factor / Alpha Analyst
- AAPM식 memory / notes / retrieval

### 2차 확장
- Fundamental Analyst

### 논문 적용
| 논문 | 적용 내용 |
|---|---|
| **TradExpert** | 전문가 분업 구조, Market/News 우선, General Synthesizer |
| **AAPM** | 정량+정성 분리 후 결합, report/factor fusion, memory/notes 확장 |
| **AlphaGAT** | Quant Prefilter / learned factor block 후보 |
| **AlphaAgent** | Factor/Alpha Analyst 확장 원리 |
| **MetaGPT** | `StrategyCard` 스키마 표준화 |

### 주의
- Strategy Agent 설명에서 **Quant Prefilter를 가장 먼저 명시**해야 한다.
- KOSPI에서는 News signal보다 **Disclosure signal이 MVP에서 더 강할 수 있다.**

---

## 9.3 Risk Agent
### 역할
`StrategyCard`를 받아 위험을 검토하고 수정된 주문 계획을 생성

### MVP 범위
- position cap
- stop-loss
- turnover cap
- cash ratio

### 1차 확장
- volatility control
- liquidity-aware execution
- sector concentration control

### 2차 확장
- 수급 gating
- market stress / irrationality gating

### 논문 적용
| 논문 | 적용 내용 |
|---|---|
| **MetaGPT** | dependency 기반 실행, `RiskCard` / `OrderPlan` 스키마 고정 |
| **AlphaGAT** | 장기적으로 allocator / weight 조절 철학 참고 |
| **AAPM** | 직접 적용은 약하지만 평가 규율과 leakage awareness 공유 |

---

## 9.4 Backtest Agent
### 역할
`OrderPlan`을 과거 데이터에 적용해 성능을 계산하고, 실패 원인을 `FailureCaseCard`로 구조화

### MVP 범위
- next-open execution
- fee / slippage
- walk-forward evaluation
- `BacktestReport`
- `FailureCaseCard`

### 논문 적용
| 논문 | 적용 내용 |
|---|---|
| **MetaGPT** | executable feedback의 금융판 번역 |
| **TradExpert** | shortlist-based trading simulation 관점 |
| **AAPM** | 평가 태도와 누수 통제 원칙 |

### 주의
- KOSPI 버전에서는 거래정지/시초가 부재/갭 리스크 처리 규칙을 명확히 해야 한다.
- 실패 분석은 단순 MDD가 아니라 **공시/뉴스/시장 레이어별 failure tagging**으로 남기는 것이 좋다.

---

## 10. KOSPI 버전에서 반드시 피해야 하는 오해

### 오해 1
“MetaGPT도 agent 논문이고 TradExpert도 agent 논문이니까 둘 다 전략 모델이다.”  
→ 아니다. MetaGPT는 **상위 협업 구조 논문**이다.

### 오해 2
“AAPM은 뉴스 agent를 쓰니까 Data Agent용 논문이다.”  
→ 아니다. AAPM의 본체는 **Strategy 확장 reasoning/fusion + 평가 규율**이다.

### 오해 3
“KOSPI니까 미국 논문은 직접 못 쓴다.”  
→ 아니다. 실험 시장은 다르지만, **설계 원리**는 그대로 쓸 수 있다.

### 오해 4
“KOSPI로 바꾸면 아키텍처도 다시 짜야 한다.”  
→ 아니다. **상위 4-agent 구조는 유지**하고, market assumption layer만 바꾼다.

### 오해 5
“KOSPI 버전에서는 공시가 있으니 뉴스는 버려도 된다.”  
→ 아니다. MVP에서 공시가 더 강할 수는 있어도, **헤드라인 신호까지 합친 News/Disclosure layer**가 더 적절하다.

---

## 11. 구현 우선순위 — KOSPI 버전

### 이번 학기 코어
1. MetaGPT식 Orchestrator / Artifact Registry
2. KOSPI Data Agent
3. Quant Prefilter
4. Market Analyst
5. News/Disclosure Analyst Lite
6. General Synthesizer
7. Risk / Backtest / Feedback

### 1차 확장
8. AAPM식 memory / notes / retrieval
9. AlphaGAT Stage I-inspired factor block
10. Factor / Alpha Analyst

### 2차 확장
11. Fundamental Analyst
12. allocator / weight control 고도화
13. R&D-Agent-Quant식 outer loop

---

## 12. 한 줄 결론

> **KOSPI 프로젝트는 MetaGPT·TradExpert·AAPM의 상위 구조를 유지한 채, AlphaGAT/AlphaAgent로 정량 코어를 보강하고, R&D-Agent-Quant를 장기 확장으로 두는 한국 시장 현지화 버전이다.**

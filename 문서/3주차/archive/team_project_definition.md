# 팀 내부 공유용 프로젝트 정의서
## 프로젝트명(가칭)
**SOP 기반 금융 멀티에이전트 트레이딩 시스템**

---

## 1. 이 프로젝트를 한 문장으로 설명하면

**미국 DOW30 일봉 데이터를 대상으로, 데이터 수집 → 전략 판단 → 리스크 통제 → 백테스트/피드백을 4개의 역할 에이전트가 분담하고, 구조화된 카드(artifact)를 주고받으며 협업하는 트레이딩 시스템을 만든다.**

---

## 2. 우리가 지금 만들고 있는 것은 무엇인가

이 프로젝트는 **주가 예측 모델 하나**를 만드는 프로젝트가 아니다.

또 **MetaGPT를 그대로 복제**하는 프로젝트도 아니다.

또 **TradExpert처럼 여러 LLM을 전부 LoRA fine-tuning**하는 프로젝트도 아니다.

우리가 만드는 것은:

- 금융 트레이딩 업무 전체를
- 4개의 역할 에이전트로 분해하고
- 각 에이전트가 자기 책임에 맞는 입력을 받아
- 구조화된 산출물을 만든 뒤
- 다음 에이전트로 핸드오프하며
- 마지막에는 백테스트 결과를 다시 전략 수정으로 연결하는

**end-to-end 협업형 트레이딩 시스템**이다.

즉, **목표는 트레이딩 시스템**이고, **에이전트 구조는 그 시스템을 설계하는 방식**이다.

---

## 3. 왜 4개 에이전트 구조로 가는가

우리는 실제 투자회사의 역할 분업을 소프트웨어로 옮긴다.

- Data Agent: 시장 데이터를 모으고 정리
- Strategy Agent: 신호를 만들고 투자 아이디어를 생성
- Risk Agent: 위험을 통제하고 주문 가능 상태로 수정
- Backtest Agent: 전략을 과거 데이터에서 검증하고 실패 원인을 피드백

핵심은 “여러 모델을 붙이는 것”이 아니라,
**역할과 책임을 분리하고, 그 사이 handoff contract를 명시하는 것**이다.

---

## 4. 현재 기준으로 확정된 프로젝트 전제

### 4-1. 시장/유니버스
- **미국 주식**
- **일봉**
- **2020-01-01 기준 고정 DOW30**
- 즉, 현재 구성 종목을 사후적으로 바꾸지 않고, 고정 유니버스에서 실험한다.

### 4-2. 정보 시계(clock) / 누수 방지
- 정보 스냅샷 시점: **T일 16:00 ET**
- 사용 가능 뉴스: **timestamp ≤ T일 16:00 ET**
- 16:00 이후 뉴스: **T+1 정보셋으로 이월**
- 가격 특성: **T일까지의 OHLCV 및 기술팩터**
- 집행 시점: **T+1 open**
- 거래비용: **편도 10bp slippage + 수수료**
- 검증 방식: **walk-forward 최소 3-fold**

### 4-3. 해석상 주의
- 상용 LLM의 사전학습 지식으로 인한 잠재적 정보누수를 완전히 제거하지는 못할 수 있다.
- 따라서 결과 해석은 “절대적 예측력”보다 **아키텍처 설계와 ablation 개선** 중심으로 한다.

---

## 5. 4개 에이전트의 역할 정의

## 5-1. Data Agent
### 역할
트레이딩 판단에 필요한 원재료를 수집·정제·정렬하는 에이전트

### MVP 범위
- OHLCV 수집
- 기술팩터 계산
- 뉴스 메타데이터 수집/정제
- 결측/시간 정렬
- 데이터 캐시

### 출력
- `DailyMarketPacket`

### 주의
- 재무제표/실적/펀더멘털은 **MVP 필수 아님**
- 그 영역은 1차 확장 이후 고려

---

## 5-2. Strategy Agent
### 역할
정량 신호와 뉴스 신호를 종합해서 **무엇을 살지/줄일지/건너뛸지** 판단하는 에이전트

### 현재 확정된 내부 흐름
`Quant Prefilter → Market Analyst → News Analyst Lite → General Synthesizer`

### 의미
1. **Quant Prefilter**
   - DOW30 전체를 먼저 점수화
   - 후보 종목을 5~8개 수준으로 좁힘

2. **Market Analyst**
   - 가격/기술팩터 기반 근거를 해석
   - “왜 이 종목이 상위 후보인지”를 정리

3. **News Analyst Lite**
   - 최근 뉴스 헤드라인 중심으로 방향성과 근거 요약
   - MVP에서는 가볍게 구현

4. **General Synthesizer**
   - 위 결과를 합쳐 최종 투자 thesis 생성

### 출력
- `StrategyCard`

### 중요한 원칙
- Strategy Agent는 **단일 모델**이 아니다.
- 여러 하위 모듈의 출력을 받아 구조화된 최종 판단을 만드는 **상위 의사결정 계층**이다.

---

## 5-3. Risk Agent
### 역할
전략 판단을 그대로 주문으로 내보내지 않고,
위험 제약을 적용해 **실행 가능한 주문 계획**으로 바꾸는 에이전트

### MVP 범위
- position cap
- stop-loss
- turnover cap

### 확장 후보
- volatility targeting
- regime gating
- irrationality / market stress gating

### 출력
- `RiskCard`
- `OrderPlan`

### 핵심 포인트
Risk Agent는 옵션이 아니라 **독립 에이전트**다.
즉, 리스크는 전략 뒤에 붙는 부가기능이 아니라 별도 의사결정 단계다.

---

## 5-4. Backtest Agent
### 역할
전략이 실제 과거 구간에서 어떻게 동작했는지 검증하고,
실패 원인을 다시 시스템에 되돌려주는 에이전트

### MVP 범위
- next-open execution
- fee/slippage 반영
- walk-forward evaluation
- 기본 성능지표 계산

### 출력
- `BacktestReport`
- `FailureCaseCard`

### 핵심 포인트
Backtest Agent는 “수익률만 출력하는 계산기”가 아니다.
실패 구간과 실패 원인을 구조화해서 다시 Strategy 쪽으로 보내는 **feedback 엔진**이다.

---

## 6. 에이전트 간 통신: 이 프로젝트의 핵심 설계물

에이전트들은 자유롭게 채팅하지 않는다.

우리는 **정해진 스키마를 가진 카드(artifact)** 를 주고받는다.

### 기본 흐름
`Data Agent -> DailyMarketPacket -> Strategy Agent -> StrategyCard -> Risk Agent -> RiskCard / OrderPlan -> Backtest Agent -> BacktestReport / FailureCaseCard -> Strategy Agent`

### 핵심 이유
- 역할 간 책임 경계가 명확해진다
- 중간 결과를 저장/재현/디버깅할 수 있다
- 발표/보고서에서 의사결정 과정을 설명하기 쉽다
- hallucination cascade를 줄인다

---

## 7. 우리가 반드시 설계해야 하는 카드(artifact)

### 7-1. DailyMarketPacket
포함 예시:
- 날짜
- 유니버스 종목 목록
- 종목별 가격/거래량
- 기술팩터
- 뉴스 메타데이터
- 데이터 품질 플래그

### 7-2. StrategyCard
포함 예시:
- 후보 종목
- 액션(BUY / HOLD / SKIP 등)
- thesis
- evidence
- contradiction
- confidence
- recommended_weight

### 7-3. RiskCard
포함 예시:
- 원래 전략 제안
- 수정된 전략
- 비중 제한 사유
- stop-loss
- turnover 제한
- 현금 비중

### 7-4. BacktestReport
포함 예시:
- Annual Return
- Sharpe
- MDD
- Hit Rate
- Turnover
- 구간별 성능 요약

### 7-5. FailureCaseCard
포함 예시:
- 실패 날짜/구간
- 어떤 가정이 깨졌는지
- 어떤 제약을 강화해야 하는지
- 다음 실행에서 반영할 수정 제안

---

## 8. 이 프로젝트가 참고논문 3편을 어떻게 반영하는가

## 8-1. MetaGPT에서 가져오는 것
우리가 가져오는 것은 “소프트웨어 개발”이 아니라 그 안의 **협업 문법**이다.

- SOP
- structured outputs
- shared message pool
- publish-subscribe
- executable feedback

즉, 우리는 MetaGPT를 금융판으로 번역한다.

---

## 8-2. TradExpert에서 가져오는 것
우리가 가져오는 것은 “모든 LLM을 그대로 구현”이 아니라,
**전문가 분업 우선순위**다.

현재 해석:
- **Market Analyst, News Analyst = 핵심**
- Alpha / Factor = 보조
- Fundamental = 후순위, 장기 확장

즉, MVP에서 가장 먼저 살려야 하는 것은
**시장 신호 + 뉴스 신호**다.

---

## 8-3. AAPM에서 가져오는 것
우리가 가져오는 것은 “자산가격이론 전체”가 아니라,
**정성 + 정량 결합 방식**이다.

- 뉴스를 그냥 감성점수로 끝내지 않음
- 분석 보고서를 state로 사용
- manual factors와 결합
- memory / notes / refine는 1차 확장으로 배치

즉, Strategy Agent는 eventually
“뉴스 reasoning + factor evidence”를 같이 쓰는 구조로 진화한다.

---

## 9. MVP와 확장 범위

## MVP (반드시 완성)
- 4 에이전트 분리
- JSON 카드 스키마
- SQLite artifact registry
- DOW30 일봉 / T+1 open / walk-forward
- Quant prefilter
- Market Analyst
- News Analyst Lite
- 단순 Risk 규칙
- Backtest + FailureCaseCard
- 1회 feedback loop

## 1차 확장
- AAPM식 memory / notes
- retrieval
- Factor / Alpha Analyst
- 더 강한 synthesizer

## 2차 확장
- Fundamental Analyst
- UMI-style risk gating
- relation graph
- 고급 risk model

## 명확한 비포함(out of scope)
- 전종목 full LLM ranking
- LoRA 대규모 fine-tuning
- Diffusion portfolio optimization
- 고빈도 트레이딩
- 한국 시장 멀티모달 데이터셋 구축

---

## 10. 실험은 어떻게 볼 것인가

우리는 단순히 “최종 성능 하나”만 보는 프로젝트가 아니다.

### 신호 스택 ablation
- S0: Quant only
- S1: + Market Analyst
- S2: + News Analyst Lite
- S3: + General Synthesizer
- S4: + Memory/Notes

### 리스크/실행 스택 ablation
- R0: Risk 없음
- R1: + position cap + stop-loss
- R2: + turnover / volatility control
- R3: + full risk agent

즉, 이 프로젝트는 **제품 + 연구 실험 인프라**를 같이 만든다.

---

## 11. 우리 팀이 직접 만들어야 하는 것 vs 가져다 써도 되는 것

## 직접 설계해야 하는 것
- 4개 에이전트 역할 정의
- 카드 스키마
- handoff 규칙
- Orchestrator
- artifact registry
- feedback loop
- 누수 정책
- 평가 프로토콜

## 재사용 가능한 것
- ML 모델 라이브러리
- LLM backend
- 데이터 라이브러리
- DB / queue
- 일부 백테스트 유틸리티
- 기존 Libri 파이프라인 일부

즉, **우리가 직접 만드는 것은 협업 구조와 시스템 계약(agent contract)** 이다.

---

## 12. Libri와 이번 프로젝트의 차이

### Libri
- 삼성전자 중심
- 단일 파이프라인
- RL 모델이 혼자 판단
- 자연어는 결과 설명용
- 리스크 분리 없음
- 백테스트는 있으나 feedback loop 없음

### 이번 프로젝트
- DOW30 고정 유니버스
- 4개 역할 에이전트 분리
- 구조화 카드로 handoff
- Strategy 내부에 전문가적 분석 계층 존재
- Risk/Backtest 독립
- 실패사례를 다시 전략 수정으로 연결

한 문장으로:
**Libri는 모델 중심 시스템이고, 이번 프로젝트는 역할 분담 중심 시스템이다.**

---

## 13. 팀원들이 꼭 같은 그림을 봐야 하는 핵심 문장

### 문장 1
**우리는 “모델 하나”를 만드는 게 아니라 “협업하는 시스템”을 만든다.**

### 문장 2
**4개 에이전트는 모두 거대 LLM일 필요가 없다. 역할 기반 software agent여도 된다.**

### 문장 3
**핵심 기여는 최고 성능보다, SOP + structured artifact + feedback loop를 금융 도메인에 맞게 설계한 데 있다.**

### 문장 4
**MVP에서는 범위를 좁히고, Memory/Notes/Fundamental은 뒤로 미룬다.**

### 문장 5
**발표 때 제일 중요한 것은 “왜 이 판단이 나왔는지”를 카드와 실험표로 설명할 수 있는가다.**

---

## 14. 지금 당장 팀 회의에서 결정해야 하는 것

1. Quant Prefilter를 무엇으로 할지
2. Data Agent 뉴스 소스를 무엇으로 고정할지
3. 카드 스키마 초안을 어떻게 자를지
4. 백테스트 엔진 입출력 계약을 어떻게 둘지
5. 팀원별 ownership을 어디까지 나눌지

---

## 15. 최종 한 줄 정의

**이 프로젝트는 미국 DOW30 일봉 시장에서, 데이터 수집·전략 판단·리스크 통제·백테스트를 4개의 역할 에이전트로 분해하고, 이들이 구조화된 카드와 피드백 루프로 협업하도록 설계한 금융 멀티에이전트 트레이딩 시스템이다.**
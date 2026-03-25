
# KOSPI 전용 팀 프로젝트 정의서 v1
## 프로젝트명(가칭)
**SOP 기반 KOSPI 멀티에이전트 트레이딩 시스템**

> 이 문서는 기존 **미국 DOW30 버전 설계 패키지**를 KOSPI용으로 현지화한 버전이다.  
> 핵심 구조는 유지하고, 시장 전제·데이터 소스·시간 규칙·텍스트 처리 전략만 한국 시장에 맞게 바꾼다.

---

## 1. 이 프로젝트를 한 문장으로 설명하면

**KOSPI 대형주 고정 유니버스를 대상으로, 데이터 수집 → 전략 판단 → 리스크 통제 → 백테스트/피드백을 4개의 역할 에이전트가 분담하고, 구조화된 카드(artifact)를 주고받으며 협업하는 일봉 트레이딩 시스템을 만든다.**

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

## 3. 왜 KOSPI 버전을 따로 정의하는가

기존 미국 버전은 아래 전제를 깔고 있었다.

- 미국 주식
- DOW30 고정 유니버스
- T일 16:00 ET 스냅샷
- 영어 뉴스/미국 논문 재현 친화성

KOSPI로 전환하면 **시장이 바뀌는 것**보다, 사실은 아래가 바뀐다.

- 유니버스 정의 방식
- 정보 시계(clock)
- 가격/공시/뉴스 데이터 소스
- 한국어 텍스트 처리 범위
- 공시/장마감 후 정보 반영 규칙

따라서 KOSPI 버전은 **완전히 새로운 프로젝트**가 아니라,  
**기존 4-agent 구조를 한국 시장에 맞게 현지화(localization)한 버전**으로 이해해야 한다.

---

## 4. KOSPI 버전에서 확정할 불변 전제

## 4.1 시장 / 유니버스

### 시장
- **KOSPI (유가증권시장)**
- **일봉**

### 유니버스 정의 원칙
- **KOSPI 전체 종목은 다루지 않는다.**
- **고정 ex-ante 유니버스**를 사용한다.
- 즉, 사후적으로 종목을 바꾸지 않고, 기준일에 고정된 바스켓으로 실험한다.

### 권장 유니버스
- **기준일 고정 KOSPI 대형주 20~30개**
- 추천안: `2020-01-02 기준 KOSPI large-cap 25종목`
- 최종 종목 리스트는 구현 착수 전에 `universe_static.yaml`로 고정

### 이유
- 한 학기 범위에서 KOSPI 전체를 다루면 범위가 폭발한다.
- TradExpert 교훈상 **전 종목 LLM 처리 금지**가 중요하다.
- KOSPI large-cap universe는 유동성과 데이터 안정성이 상대적으로 높다.

---

## 4.2 정보 시계(clock) / 누수 방지 규칙

### KOSPI 버전 기본 규칙
- **정규시장 시세 사용 범위:** T일 정규시장 마감까지
- **공시/뉴스 반영 cutoff:** **T일 18:00 KST**
- **18:00 이후 공시/뉴스:** **T+1 정보셋으로 이월**
- **집행 시점:** **T+1 open**
- **거래비용:** 편도 10bp slippage + 수수료
- **검증 방식:** walk-forward 최소 3-fold

### 왜 18:00 KST인가
한국 시장은 **장 마감 후 공시와 후속 기사**가 다음 날 시초가에 큰 영향을 줄 수 있다.  
따라서 미국 버전의 `16:00 ET`를 한국식으로 단순 치환하는 게 아니라,
**장종료 후 공시/뉴스가 정리되는 시간까지 포함한 cutoff**를 쓰는 것이 더 자연스럽다.

### Data Clock 원칙
- T일 정규시장 데이터는 **09:00~15:30** 세션 기준
- 장종료후 시간외 정보 및 공시 반영은 **18:00 KST**까지 허용
- 18:00 이후의 모든 텍스트 정보는 **다음 날**로 넘긴다
- 백테스트와 실험은 이 규칙을 절대 어기지 않는다

---

## 4.3 해석상 주의

- 한국어 금융 텍스트에 대한 상용 LLM의 사전학습 지식과 언어 성능은 완전 통제할 수 없다.
- OpenDART/공시정보 활용마당의 정보는 제출인 책임 자료이며 제공 시차가 존재할 수 있다.
- `pykrx`는 실무적으로 매우 유용하지만 **스크래핑 기반 도구**이므로, 공식 데이터와 차이가 날 수 있다.
- 따라서 결과 해석은 “절대적 예측력”보다 **아키텍처 설계와 ablation 개선**, 그리고 **한국 시장 현지화의 타당성** 중심으로 한다.

---

## 5. KOSPI 버전의 4개 에이전트

## 5.1 Data Agent
### 역할
트레이딩 판단에 필요한 원재료를 수집·정제·정렬하는 에이전트

### MVP 범위
- KOSPI 종목 OHLCV 수집
- 기술팩터 계산
- 시가총액/거래대금/유동성 관련 기본 지표 정리
- 공시 제목/공시 메타데이터 수집
- 뉴스 헤드라인 메타데이터 수집
- 결측/시간 정렬
- 종목코드 매핑(master) 관리
- 데이터 캐시

### 출력
- `DailyMarketPacket`

### KOSPI 버전에서 중요한 추가 포인트
- `security_master`를 반드시 둔다  
  (`krx_ticker ↔ corp_code ↔ company_name` 매핑)
- 한국 시장에서는 **가격 코드와 공시 코드가 분리**되므로, 이 매핑이 없으면 Data Agent가 무너진다.

### 주의
- 재무제표/실적/Fundamental full parse는 **MVP 필수 아님**
- MVP에서는 **공시 제목 + 뉴스 헤드라인 + 가격/팩터**가 우선

---

## 5.2 Strategy Agent
### 역할
정량 신호와 공시/뉴스 신호를 종합해서 **무엇을 살지/줄일지/건너뛸지** 판단하는 에이전트

### 현재 확정된 내부 흐름
`Quant Prefilter → Market Analyst → News/Disclosure Analyst Lite → General Synthesizer`

### 의미
1. **Quant Prefilter**
   - 고정 KOSPI 유니버스 전체를 먼저 점수화
   - 후보 종목을 5~8개 수준으로 좁힘

2. **Market Analyst**
   - 가격/기술팩터 기반 근거를 해석
   - “왜 이 종목이 상위 후보인지”를 정리

3. **News/Disclosure Analyst Lite**
   - 최근 공시 제목, 공시 유형, 뉴스 헤드라인 중심으로 방향성과 근거 요약
   - MVP에서는 **기사 전문**보다 **제목/핵심 문장** 중심

4. **General Synthesizer**
   - 위 결과를 합쳐 최종 투자 thesis 생성

### 출력
- `StrategyCard`

### 중요한 원칙
- Strategy Agent는 **단일 모델**이 아니다.
- 여러 하위 모듈의 출력을 받아 구조화된 최종 판단을 만드는 **상위 의사결정 계층**이다.
- KOSPI 버전에서는 News Analyst보다 **Disclosure signal**의 중요도가 더 커질 수 있다.

---

## 5.3 Risk Agent
### 역할
전략 판단을 그대로 주문으로 내보내지 않고,
위험 제약을 적용해 **실행 가능한 주문 계획**으로 바꾸는 에이전트

### MVP 범위
- position cap
- stop-loss
- turnover cap
- cash ratio control
- 섹터 집중 제한(선택)

### KOSPI 확장 후보
- 외국인/기관 수급 gating
- 시장 스트레스 구간에서 현금 비중 확대
- 공시 이벤트 직후 과도한 갭 리스크 제어
- 거래정지/매매정지 종목 처리

### 출력
- `RiskCard`
- `OrderPlan`

### 핵심 포인트
Risk Agent는 옵션이 아니라 **독립 에이전트**다.  
즉, 리스크는 전략 뒤에 붙는 부가기능이 아니라 별도 의사결정 단계다.

---

## 5.4 Backtest Agent
### 역할
전략이 실제 과거 구간에서 어떻게 동작했는지 검증하고,
실패 원인을 다시 시스템에 되돌려주는 에이전트

### MVP 범위
- next-open execution
- fee/slippage 반영
- walk-forward evaluation
- 기본 성능지표 계산
- 실패 사례 기록

### KOSPI 버전에서 중요하게 다룰 것
- T+1 시초가 집행 가능성 확인
- 거래정지/매매정지/시초가 부재 시 처리 규칙
- 공시 직후 갭 발생 구간에 대한 failure tagging
- 종목별 유동성 부족 구간의 skip 처리

### 출력
- `BacktestReport`
- `FailureCaseCard`

### 핵심 포인트
Backtest Agent는 “수익률만 출력하는 계산기”가 아니다.  
실패 구간과 실패 원인을 구조화해서 다시 Strategy 쪽으로 보내는 **feedback 엔진**이다.

---

## 6. 에이전트 간 통신: KOSPI 버전에서도 핵심은 같다

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

## 7. 우리가 반드시 설계해야 하는 카드(artifact) — KOSPI 버전

### 7-1. DailyMarketPacket
포함 예시:
- 날짜
- 유니버스 종목 목록
- 종목별 가격/거래량
- 기술팩터
- 공시 메타데이터
- 뉴스 메타데이터
- 종목코드 매핑 정보
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
- source_refs (공시/뉴스 참조)

### 7-3. RiskCard
포함 예시:
- 원래 전략 제안
- 수정된 전략
- 비중 제한 사유
- stop-loss
- turnover 제한
- 현금 비중

### 7-4. OrderPlan
포함 예시:
- 종목별 목표 비중
- 실행 시점
- 실행 불가시 fallback 규칙
- skip 종목 목록

### 7-5. BacktestReport
포함 예시:
- Annual Return
- Sharpe
- MDD
- Hit Rate
- Turnover
- 구간별 성능 요약

### 7-6. FailureCaseCard
포함 예시:
- 실패 날짜/구간
- 실패 종목
- 어떤 가정이 깨졌는지
- 공시/뉴스/팩터 중 어느 층에서 문제가 났는지
- 다음 실행에서 강화할 제약

---

## 8. KOSPI 버전에서 참고논문 6편을 어떻게 읽는가

### MetaGPT
- KOSPI에서도 그대로 강함
- 시장과 무관하게 **SOP / structured artifact / feedback**를 준다

### TradExpert
- Strategy 계층 분업 설계에 그대로 적용
- 다만 한국 시장 MVP에서는 `News Analyst`를 **News/Disclosure Analyst Lite**로 현지화

### AAPM
- `refined news`, `relevance check`, `report+factor fusion`, `leakage awareness`를 그대로 가져옴
- 다만 WSJ 직접 재현이 아니라 **공시/헤드라인 중심 한국 시장 현지화**로 읽음

### AlphaGAT
- Quant Prefilter / Factor Engine 확장에 매우 적합
- 특히 Stage I 철학은 KOSPI MVP와 잘 맞음

### AlphaAgent
- Factor / Alpha Analyst의 1차 확장 참고용
- alpha decay를 고려한 factor 설계 규율 제공

### R&D-Agent-Quant
- 이번 학기 코어가 아니라 **후속 outer research loop** 용

---

## 9. MVP와 확장 범위 — KOSPI 버전

## MVP (반드시 완성)
- 4 에이전트 분리
- JSON 카드 스키마
- SQLite artifact registry
- KOSPI 대형주 고정 유니버스 / T일 18:00 KST / T+1 open / walk-forward
- Quant Prefilter
- Market Analyst
- News/Disclosure Analyst Lite
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
- 수급/시장국면 gating
- relation graph
- 고급 risk model

## 명확한 비포함(out of scope)
- KOSPI 전체 종목 커버
- 전종목 full LLM ranking
- LoRA 대규모 fine-tuning
- 고빈도 트레이딩
- 한국 시장 full article 멀티모달 대규모 데이터셋 구축

---

## 10. 실험은 어떻게 볼 것인가

우리는 단순히 “최종 성능 하나”만 보는 프로젝트가 아니다.

### 신호 스택 ablation
- S0: Quant only
- S1: + Market Analyst
- S2: + News/Disclosure Analyst Lite
- S3: + General Synthesizer
- S4: + Memory/Notes

### 리스크/실행 스택 ablation
- R0: Risk 없음
- R1: + position cap + stop-loss
- R2: + turnover / volatility / liquidity control
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
- KST 기준 누수 정책
- 평가 프로토콜
- 종목코드 매핑 레이어

## 재사용 가능한 것
- ML 모델 라이브러리
- LLM backend
- 데이터 라이브러리
- DB / queue
- 일부 백테스트 유틸리티
- 기존 Libri 파이프라인 일부

즉, **우리가 직접 만드는 것은 협업 구조와 시스템 계약(agent contract)** 이다.

---

## 12. KOSPI 버전에서 팀이 반드시 공유해야 하는 5문장

1. **우리는 KOSPI 예측 모델 하나를 만드는 게 아니라, 협업하는 트레이딩 시스템을 만든다.**
2. **KOSPI 전체를 다루지 않고, 고정 large-cap universe를 쓴다.**
3. **한국 시장에서는 News보다 Disclosure signal을 MVP에서 더 우선시할 수 있다.**
4. **핵심 기여는 최고 성능보다 SOP + artifact + feedback loop를 한국 시장에 맞게 이식한 데 있다.**
5. **발표 때 가장 중요한 것은 왜 이 판단이 나왔는지를 카드와 실험표로 설명할 수 있는가다.**

---

## 13. 다음 회의에서 바로 결정해야 할 것

1. 고정 유니버스를 **20종목 / 25종목 / 30종목** 중 어디로 할지
2. `snapshot_time = 18:00 KST`를 최종 확정할지
3. MVP 텍스트 입력을  
   - 공시 제목만 쓸지  
   - 공시 + 헤드라인을 쓸지  
   - 공시 + 헤드라인 + 핵심문장을 쓸지
4. Quant Prefilter baseline 2개 후보를 무엇으로 할지
5. `security_master` 매핑을 어느 agent/모듈에서 책임질지

---

## 14. 한 줄 결론

> **KOSPI 버전은 미국 버전의 대체재가 아니라, 4-agent 구조를 한국 시장에 맞게 현지화한 실행 버전이다.**

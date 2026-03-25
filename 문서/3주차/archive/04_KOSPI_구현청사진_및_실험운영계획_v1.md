
# KOSPI 전용 구현 청사진 및 실험 운영계획 v1

> 목적: KOSPI 버전 프로젝트를 실제로 어떻게 구현할지, **계약(contract) → 클래스 → 실행순서 → 실험운영** 순으로 내리는 실행 문서

---

## 1. 구현 철학

1. **Contract 먼저, 모델은 나중**
2. **MVP는 end-to-end loop가 우선**
3. **모든 모듈은 artifact를 남긴다**
4. **실험 가능한 구조를 유지한다**
5. **확장은 runtime core와 분리한다**
6. **한국 시장 현지화는 시장 전제만 바꾸고 상위 구조는 유지한다**

---

## 2. Repo 구조 제안

```text
project/
├─ docs/
│  ├─ us_dow30/
│  └─ kospi/
│     ├─ 01_KOSPI_팀프로젝트정의서_v1.md
│     ├─ 02_KOSPI_6편논문_프로젝트매핑서_v1.md
│     ├─ 03_KOSPI_시스템_시각화_v1.md
│     └─ 04_KOSPI_구현청사진_및_실험운영계획_v1.md
│
├─ backend/
│  ├─ api/
│  ├─ orchestration/
│  │  ├─ orchestrator.py
│  │  ├─ contracts.py
│  │  ├─ clock_policy.py
│  │  └─ experiment_mode.py
│  ├─ registry/
│  │  └─ artifact_registry.py
│  ├─ agents/
│  │  ├─ base_agent.py
│  │  ├─ data_agent.py
│  │  ├─ strategy_agent.py
│  │  ├─ risk_agent.py
│  │  └─ backtest_agent.py
│  ├─ strategy/
│  │  ├─ quant_prefilter/
│  │  ├─ market_analyst/
│  │  ├─ news_disclosure_analyst/
│  │  ├─ synthesizer/
│  │  └─ feature_engine/
│  ├─ data/
│  │  ├─ security_master.py
│  │  ├─ price_loader.py
│  │  ├─ disclosure_loader.py
│  │  ├─ news_loader.py
│  │  └─ factor_builder.py
│  ├─ backtest/
│  │  ├─ simulator.py
│  │  ├─ metrics.py
│  │  └─ failure_tagger.py
│  └─ config/
│     ├─ universe_static.yaml
│     ├─ source_config.yaml
│     └─ risk_policy.yaml
│
├─ frontend/
│  ├─ dashboard/
│  ├─ cards/
│  ├─ experiments/
│  └─ portfolio/
│
└─ notebooks/
   ├─ data_validation/
   ├─ prefilter_benchmarks/
   └─ ablation_analysis/
```

---

## 3. 핵심 클래스 청사진

## 3.1 BaseAgent

```python
class BaseAgent:
    name: str
    input_artifacts: list[str]
    output_artifacts: list[str]

    def can_run(self, registry, run_context) -> bool:
        ...

    def run(self, registry, run_context) -> dict:
        ...
```

### 역할
- 모든 agent의 공통 인터페이스
- prerequisite 확인
- 실행 결과를 artifact dict로 반환

---

## 3.2 ArtifactRegistry

```python
class ArtifactRegistry:
    def save(self, artifact_type: str, payload: dict, version: str, run_id: str): ...
    def latest(self, artifact_type: str, run_id: str | None = None): ...
    def history(self, artifact_type: str, key: str | None = None): ...
    def exists(self, artifact_type: str, run_id: str | None = None) -> bool: ...
    def compare_versions(self, artifact_type: str, run_id: str): ...
```

### 저장 대상
- DailyMarketPacket
- StrategyCard
- RiskCard
- OrderPlan
- BacktestReport
- FailureCaseCard

### 저장 원칙
- **재실행 결과는 덮어쓰지 않고 version을 올린다**
- run_id / experiment_mode / retry_count를 함께 저장한다

---

## 3.3 Orchestrator

```python
class Orchestrator:
    def run_daily(self, trade_date, experiment_mode): ...
    def run_feedback_retry(self, failure_card): ...
    def should_retry(self, failure_card, retry_policy): ...
```

### 책임
- 실행 순서 제어
- dependency 체크
- experiment mode(S0~S4, R0~R3) 반영
- feedback retry 1회
- artifact routing

### 기본 순서
```text
Data -> Strategy -> Risk -> Backtest
```

### feedback retry
```text
Backtest 실패
  -> FailureCaseCard 발행
  -> retry_policy.allow_once == True
  -> Strategy 재실행 1회
  -> 재실행 결과도 별도 버전 저장
```

---

## 4. KOSPI 전용 공통 정책 모듈

## 4.1 DataClockPolicy

```python
class DataClockPolicy:
    snapshot_time = "18:00:00"
    market_close = "15:30:00"
    next_open = "09:00:00"

    def is_available_for_trade_date(self, ts) -> bool: ...
    def assign_trade_date(self, ts): ...
```

### 규칙
- `timestamp <= T일 18:00 KST` → T일 정보셋
- `timestamp > 18:00 KST` → T+1 정보셋
- 가격 feature는 정규시장 마감 기준
- 집행은 T+1 open

---

## 4.2 SecurityMaster

```python
class SecurityMaster:
    def load_static_universe(self): ...
    def map_ticker_to_corp_code(self, ticker): ...
    def map_corp_code_to_ticker(self, corp_code): ...
```

### KOSPI에서 반드시 필요한 이유
- 가격 데이터는 보통 **KRX 6자리 ticker**
- 공시 데이터는 **DART corp_code**
- 이름 기준 매칭은 오염 위험이 크다

### 저장 필드 예시
- `ticker`
- `corp_code`
- `company_name_kr`
- `market`
- `sector`
- `is_active`
- `anchor_date`

---

## 5. Data Agent 구현 청사진

## 5.1 입력 소스 계층

```text
Price Loader
  - pykrx / KRX 기반 가격 로딩
Disclosure Loader
  - OpenDART / KIND 메타데이터
News Loader
  - 국내 뉴스 헤드라인 소스
Security Master
  - ticker-corp_code 매핑
```

## 5.2 Data Agent가 만드는 것

```python
DailyMarketPacket = {
    "date": "...",
    "universe": [...],
    "prices": {...},
    "factors": {...},
    "disclosure_meta": {...},
    "news_meta": {...},
    "security_master": {...},
    "quality_flags": {...}
}
```

## 5.3 MVP 범위
- 가격/거래량
- 기술팩터
- 공시 제목/유형/시각
- 뉴스 헤드라인/시각/종목연결
- 결측치 및 시간 정렬

## 5.4 구현 포인트
- DataPacket 생성 전에 **timestamp normalization**을 먼저 한다
- 종목별 공시/뉴스가 없을 수 있으므로 빈 리스트 허용
- source reliability flag를 둔다
- `quality_flags`에 누락/지연/매핑 불확실성 기록

---

## 6. Strategy Agent 구현 청사진

## 6.1 전체 구조

```text
DailyMarketPacket
   -> Quant Prefilter
   -> Market Analyst
   -> News/Disclosure Analyst Lite
   -> General Synthesizer
   -> StrategyCard
```

---

## 6.2 Quant Prefilter

### 역할
고정 KOSPI 유니버스 전체를 빠르게 점수화해 shortlist 5~8개로 줄이는 단계

### 후보군
- LightGBM Ranker
- XGBoost Ranker
- factor-score ensemble
- AlphaGAT Stage I-inspired learned factor block

### MVP 권장
- **강한 전통 baseline 1개**
- **가벼운 learned variant 1개**

### 초기 추천
1. `LightGBM ranker`
2. `factor-score ensemble`

### 입력 feature 후보
- 수익률 5/20/60일
- RSI, MACD, Bollinger %B, ATR
- 거래대금 z-score
- 변동성 5/20/60일
- 시장 대비 상대강도
- 유동성 flag
- (선택) 외국인/기관 수급 feature

### 출력
```python
QuantEvidenceCard = {
    "ticker": "...",
    "rank_score": ...,
    "rank_percentile": ...,
    "top_positive_factors": [...],
    "top_negative_factors": [...],
    "model_uncertainty": ...,
    "data_quality_flag": ...
}
```

---

## 6.3 Market Analyst

### 역할
가격/기술팩터 기반 근거를 사람이 읽을 수 있는 투자 근거로 정리

### 입력
- QuantEvidenceCard
- 최근 5/20/60일 price/factor snapshot

### 출력 예시
```python
MarketAnalysis = {
    "ticker": "...",
    "thesis": "...",
    "supporting_evidence": [...],
    "counter_evidence": [...],
    "short_horizon_view": "...",
    "market_confidence": ...
}
```

### 원칙
- raw number dump 금지
- 요약된 factor evidence + top-k local importance만 사용
- StrategyCard를 위한 중간 해석층으로 위치시킨다

---

## 6.4 News/Disclosure Analyst Lite

### 역할
공시 제목/유형/뉴스 헤드라인을 바탕으로 방향성과 근거를 가볍게 요약

### MVP 입력
- 종목별 최신 공시 1~3건
- 종목별 관련 뉴스 헤드라인 1~3건
- 시각(timestamp)
- source type

### 출력 예시
```python
TextSignalCard = {
    "ticker": "...",
    "direction": "positive | negative | neutral | skip",
    "evidence": [...],
    "confidence": ...,
    "source_refs": [...],
    "relevance_flag": ...
}
```

### 주의
- 기사 전문 다중 라운드 분석은 MVP 금지
- 먼저 **relevance check**를 둔다
- 공시 없는 종목, 뉴스 없는 종목도 허용
- “무리해서 판단”보다 `skip`을 허용한다

---

## 6.5 General Synthesizer

### 역할
Quant / Market / Text signal을 종합해 최종 투자 thesis를 생성

### 입력
- shortlist
- MarketAnalysis
- TextSignalCard
- optional risk hints

### 출력
```python
StrategyCard = {
    "date": "...",
    "selected_tickers": [...],
    "action": "...",
    "thesis": "...",
    "evidence": [...],
    "contradictions": [...],
    "confidence": ...,
    "recommended_weight": {...},
    "source_refs": [...]
}
```

### 원칙
- StrategyCard는 **최종 보고서**다
- 내부 reasoning 전체를 저장하지 말고,
  `thesis / evidence / contradiction / confidence / refs` 구조만 저장한다

---

## 7. Risk Agent 구현 청사진

## 7.1 MVP 룰셋
- position cap
- stop-loss
- turnover cap
- cash ratio floor
- 실행 불가 종목 skip

## 7.2 KOSPI 추가 룰 후보
- 유동성 부족 skip
- 거래정지 / 시초가 부재 skip
- 공시 직후 과도한 gap risk 억제
- sector concentration limit

## 7.3 출력
```python
RiskCard = {
    "date": "...",
    "original_strategy": {...},
    "adjusted_strategy": {...},
    "adjustment_reasons": [...],
    "stop_loss": {...},
    "cash_ratio": ...
}

OrderPlan = {
    "date": "...",
    "target_weights": {...},
    "execution_time": "T+1 open",
    "skip_rules": [...],
    "fallback_rules": [...]
}
```

---

## 8. Backtest Agent 구현 청사진

## 8.1 시뮬레이션 원칙
- T일 18:00 정보셋 기반
- T+1 open 집행
- fee + slippage 반영
- walk-forward
- unavailable open / halt 처리 규칙 명시

## 8.2 예외 처리
- T+1 open 가격 없음 → skip or carry cash
- 거래정지 → execution deferred 아님, 우선은 skip
- 상·하한가 근접 갭 → failure tag 기록

## 8.3 Failure Tagging
```python
FailureCaseCard = {
    "date": "...",
    "failed_tickers": [...],
    "failure_window": "...",
    "broken_assumptions": [...],
    "suspected_layer": "quant | market | text | risk | execution",
    "suggested_constraints": [...]
}
```

### 목적
- 단순 “졌음”이 아니라
- **어디서 잘못됐는지 구조화해서 다음 실행에 반영**

---

## 9. 실험 운영계획

## 9.1 신호 스택
- S0 Quant only
- S1 + Market Analyst
- S2 + News/Disclosure Analyst Lite
- S3 + General Synthesizer
- S4 + Memory/Notes

## 9.2 리스크 스택
- R0 Risk 없음
- R1 + position cap + stop-loss
- R2 + turnover / volatility / liquidity control
- R3 + full risk agent

## 9.3 최소 성공 기준
- S0~S3 완료
- R0~R2 완료
- 1회 feedback retry 동작
- artifact registry에 전 과정 저장
- KOSPI 현지화 타당성 설명 가능

---

## 10. 12주 로드맵

### 1~2주
- KOSPI 불변 규칙 확정
- static universe 고정
- security_master 설계
- 카드 스키마 freeze
- registry 구현

### 3~4주
- Data Agent
- OHLCV / factor / disclosure metadata 파이프라인
- DataPacket 생성

### 5~6주
- Quant Prefilter baseline 2개 구현
- shortlist 생성
- Market Analyst 초안

### 7주
- News/Disclosure Analyst Lite
- StrategyCard 생성

### 8주
- Risk Agent MVP
- OrderPlan 생성

### 9주
- Backtest Agent MVP
- FailureCaseCard 생성

### 10주
- feedback retry 1회 연결
- orchestration 안정화

### 11주
- S0~S3 / R0~R2 실험
- ablation 표 작성

### 12주
- 안정화
- 팀 발표 자료/데모
- 차기 확장 포인트 문서화

---

## 11. KOSPI 버전 핵심 리스크 8가지

1. **범위 폭발**
   - KOSPI 전체 종목으로 가는 순간 무너짐  
   - 대응: large-cap static universe 고정

2. **종목코드 매핑 오류**
   - ticker ↔ corp_code mismatch
   - 대응: security_master를 별도 모듈로 둠

3. **공시/뉴스 시간 정렬 오류**
   - leakage 가장 흔한 원인
   - 대응: DataClockPolicy 우선 구현

4. **스크래핑 의존성**
   - pykrx / 뉴스 소스 불안정 가능
   - 대응: adapter abstraction + 캐시

5. **한국어 텍스트 모델 품질**
   - 전문 기사 reasoning이 불안정할 수 있음
   - 대응: 제목/핵심 문장 중심 Lite 설계

6. **모델 선택 지연**
   - 구조보다 모델 선택에 시간 낭비
   - 대응: contract first, model later

7. **FE/BE 계약 불일치**
   - 카드 필드 변경이 잦으면 병목
   - 대응: JSON schema early freeze

8. **실험 미완성**
   - 모델 구현은 됐는데 ablation이 비면 발표가 약함
   - 대응: S/R ladder를 초기에 고정

---

## 12. 다음 회의에서 바로 확정해야 할 것

1. `universe_size = 20 / 25 / 30`
2. `snapshot_time = 18:00 KST` 최종 승인 여부
3. 텍스트 source 우선순위  
   - 공시 only  
   - 공시 + 헤드라인  
   - 공시 + 헤드라인 + 핵심문장
4. Quant Prefilter baseline 2개
5. security_master ownership
6. retry_policy 상세 규칙
7. Backtest 예외처리 규칙

---

## 13. 한 줄 결론

> **KOSPI 구현의 핵심은 모델 선택보다, 종목코드 매핑·시간 규칙·artifact contract를 먼저 고정한 뒤, Quant Prefilter와 News/Disclosure Lite를 얹어 4-agent loop를 end-to-end로 돌리는 것이다.**

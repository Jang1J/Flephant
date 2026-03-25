
# KOSPI 전용 시스템 시각화 v1

> **주의 1:** 시각화에 적힌 모델명(LightGBM, XGBoost, KoBERT, GPT API 등)은 예시 후보이며, 최종 모델 선택은 별도 회의에서 확정한다.  
> **주의 2:** 이 문서는 “모델 스펙” 문서가 아니라, **구조 / 흐름 / 계약 / 실험 구조**를 팀이 같은 그림으로 보기 위한 mental model 문서다.

---

## 1. 전체 구조: 2-loop 시스템

```text
┌──────────────────────────────────────────────────────────────────┐
│                    Runtime Trading Loop (이번 학기 코어)          │
│                                                                  │
│  Data Agent                                                      │
│    -> DailyMarketPacket                                          │
│  Strategy Agent                                                  │
│    -> StrategyCard                                               │
│  Risk Agent                                                      │
│    -> RiskCard + OrderPlan                                       │
│  Backtest Agent                                                  │
│    -> BacktestReport + FailureCaseCard                           │
│  Orchestrator                                                    │
│    -> failure 감지 시 Strategy 1회 재실행                         │
└──────────────────────────────────────────────────────────────────┘

                              ▲
                              │ 실패 원인 / 개선 포인트 축적
                              │
                              ▼

┌──────────────────────────────────────────────────────────────────┐
│                 Offline Research Outer Loop (후속 연구)           │
│                                                                  │
│  Factor / Alpha 연구                                             │
│  Quant model 후보 탐색                                           │
│  Data-centric feature redesign                                   │
│  R&D-Agent-Quant식 자동 연구 루프                                │
└──────────────────────────────────────────────────────────────────┘
```

### 해석
- **이번 학기 코어**는 위쪽 Runtime Loop다.
- 아래쪽 Outer Loop는 이번 학기에 반드시 만들 필요는 없지만,
  문서 구조상 **미래 확장 방향**으로 위치를 잡아 둔다.

---

## 2. 전체 시스템 아키텍처 (AI / BE / FE)

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│                                🔵 FE                                         │
│  대시보드 / 포트폴리오 현황 / 카드 뷰어 / 실험 비교 화면                     │
└───────────────────────────────┬──────────────────────────────────────────────┘
                                │ API 호출
┌───────────────────────────────▼──────────────────────────────────────────────┐
│                                🟢 BE                                         │
│  REST API / SQLite Artifact Registry / Scheduler / Orchestrator             │
│                                                                              │
│  Orchestrator                                                                │
│    Data -> Strategy -> Risk -> Backtest -> (feedback) -> Strategy           │
└───────────────┬──────────────────┬──────────────────┬────────────────────────┘
                │                  │                  │
                ▼                  ▼                  ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                                🔴 AI                                         │
│                                                                              │
│  1) DATA AGENT                                                               │
│     - OHLCV / 거래량 / 기본 시장지표                                          │
│     - 기술팩터 계산                                                           │
│     - 공시 메타데이터 수집                                                   │
│     - 뉴스 헤드라인 메타데이터 수집                                          │
│     - 종목코드 매핑(security master)                                         │
│     -> DailyMarketPacket                                                     │
│                                                                              │
│  2) STRATEGY AGENT                                                           │
│     KOSPI Large-cap Universe                                                 │
│        -> Quant Prefilter                                                    │
│        -> Top 5~8 shortlist                                                  │
│        -> Market Analyst                                                     │
│        -> News/Disclosure Analyst Lite                                       │
│        -> General Synthesizer                                                │
│        -> StrategyCard                                                       │
│                                                                              │
│  3) RISK AGENT                                                               │
│     - position cap                                                           │
│     - stop-loss                                                              │
│     - turnover / liquidity control                                           │
│     - cash ratio                                                             │
│     -> RiskCard + OrderPlan                                                  │
│                                                                              │
│  4) BACKTEST AGENT                                                           │
│     - T+1 open execution                                                     │
│     - fee / slippage                                                         │
│     - walk-forward                                                           │
│     - failure tagging                                                        │
│     -> BacktestReport + FailureCaseCard                                      │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. KOSPI Runtime 상세 구조

```text
[고정 KOSPI 대형주 유니버스 20~30개]
                │
                ▼
      [Data Agent / DailyMarketPacket]
                │
                ▼
      [Quant Prefilter / 점수화]
                │
        shortlist 5~8개
                │
                ▼
        [Market Analyst]
     - 가격/기술팩터 근거 해석
     - local factor importance
                │
                ▼
 [News/Disclosure Analyst Lite]
  - 공시 제목
  - 공시 유형
  - 뉴스 헤드라인
  - 핵심 문장(선택)
                │
                ▼
      [General Synthesizer]
     - 종합 thesis 생성
     - contradiction 정리
     - confidence 부여
                │
                ▼
           StrategyCard
                │
                ▼
            Risk Agent
                │
                ▼
        RiskCard + OrderPlan
                │
                ▼
          Backtest Agent
                │
                ▼
 BacktestReport + FailureCaseCard
                │
        실패시 제약 강화
                ▼
     Orchestrator 재실행 1회
```

### 중요한 원칙
- **전 종목 LLM 처리 금지**
- **Quant Prefilter가 반드시 앞단**
- LLM은 shortlist 이후의 해석/종합 계층에 제한적으로 사용

---

## 4. KOSPI Data Clock / Leakage Timeline

```text
T일 정규시장
09:00 ─────────────────────────────── 15:30
      [OHLCV / 거래량 / 기술팩터 확정]

T일 장종료후 시간외
15:40 ─────────────────────────────── 18:00
      [장종료후 시간외 / 공시 / 후속 기사 반영 구간]

T일 18:00 KST
      └── 공식 스냅샷 시점
          timestamp <= 18:00 인 공시/헤드라인만 사용 가능

T일 18:00 ~ 야간
      Data Agent 실행
      Strategy Agent 실행
      Risk Agent 실행
      Backtest/validation 실행
      FailureCaseCard 생성 가능

T+1일 09:00
      └── next-open execution 가정
          18:00 이후 정보는 절대 사용 금지
```

### 핵심 규칙
- **Snapshot = T일 18:00 KST**
- `timestamp <= 18:00` 만 T일 정보셋에 포함
- **18:00 이후 공시/뉴스는 T+1로 이월**
- 백테스트와 실험은 이 규칙을 절대 어기지 않는다

---

## 5. KOSPI Artifact Flow (6종 카드)

```text
1. DailyMarketPacket
{
  date,
  universe,
  prices,
  factors,
  disclosure_meta,
  news_meta,
  security_master,
  quality_flags
}

2. StrategyCard
{
  date,
  selected_tickers,
  action,
  thesis,
  evidence,
  contradictions,
  confidence,
  recommended_weight,
  source_refs
}

3. RiskCard
{
  date,
  original_strategy,
  adjusted_strategy,
  adjustment_reasons,
  stop_loss,
  turnover_limit,
  cash_ratio
}

4. OrderPlan
{
  date,
  target_weights,
  execution_time,
  skip_rules,
  unavailable_rules
}

5. BacktestReport
{
  annual_return,
  sharpe,
  mdd,
  hit_rate,
  turnover,
  regime_summary
}

6. FailureCaseCard
{
  failure_window,
  failed_tickers,
  broken_assumptions,
  suspected_layer,
  suggested_constraints
}
```

---

## 6. Feedback Loop 시각화

```text
StrategyCard
   │
   ▼
Risk Agent
   │
   ▼
OrderPlan
   │
   ▼
Backtest Agent
   │
   ├── 성능 양호
   │      -> BacktestReport 저장
   │
   └── 성능 저하 / 위험 breach / 실패 패턴 발견
          -> FailureCaseCard 발행
          -> Orchestrator가 제약 강화
          -> Strategy Agent 1회 재실행
```

### 예시
- 특정 공시 이후 갭다운 구간 반복
- 특정 종목군에서 유동성 부족
- News/Disclosure layer가 과도한 낙관 신호 생성
- stop-loss 부재로 drawdown 확대

---

## 7. Signal / Risk Ablation 구조

## 7.1 Signal Stack

```text
S0  Quant only
S1  + Market Analyst
S2  + News/Disclosure Analyst Lite
S3  + General Synthesizer
S4  + Memory/Notes
```

### 의미
- S0~S4는 **신호 생성 품질**의 변화를 보기 위한 계단식 실험
- “왜 종목 선택이 좋아졌는가?”를 설명하는 축

## 7.2 Risk / Execution Stack

```text
R0  Risk 없음
R1  + position cap + stop-loss
R2  + turnover / volatility / liquidity control
R3  + full risk agent
```

### 의미
- R0~R3는 **수익률 분포와 실행 안정성**의 변화를 보기 위한 축
- “왜 MDD가 줄었는가?”를 설명하는 축

---

## 8. KOSPI 버전에서 팀원 역할 레인

```text
AI 팀
  - Data Agent 로직
  - Quant Prefilter
  - Market / News/Disclosure Analyst
  - Risk / Backtest 로직
  - 실험 및 ablation

BE 팀
  - Orchestrator
  - API
  - Artifact Registry
  - Scheduler
  - Source Adapter

FE 팀
  - Dashboard
  - Card Viewer
  - Backtest 결과 비교 화면
  - FailureCase 시각화
```

---

## 9. KOSPI 버전의 핵심 구현 포인트 5개

1. **security_master 매핑 레이어**  
   - `ticker ↔ corp_code ↔ company_name`
2. **18:00 KST snapshot rule**
3. **News보다 Disclosure를 포함한 텍스트 layer**
4. **Quant Prefilter 선행**
5. **FailureCaseCard 기반 feedback**

---

## 10. 한 줄 요약

> **KOSPI 버전은 4-agent 구조를 유지한 채, 정보 시계를 KST 기준으로 재정의하고, Strategy 내부의 News layer를 Disclosure-aware 형태로 현지화한 한국 시장용 협업 시스템이다.**

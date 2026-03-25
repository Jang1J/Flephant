# BE 파트 역할 안내

## 우리 프로젝트에서 BE가 하는 일

우리 프로젝트는 일반적인 웹앱(회원가입, 게시판, 결제 등)이 아닙니다.
**4개의 AI 에이전트가 매일 정해진 순서대로 실행되고, 각 단계의 결과(카드)가 저장되고, 실패하면 재시도하는 파이프라인 시스템**입니다.

BE는 이 파이프라인이 실제로 돌아가게 만드는 역할을 합니다.

---

## 웹 백엔드와 비교하면

웹 백엔드에 익숙한 분들을 위해 대응시켜보면 이렇습니다.

| 웹 백엔드에서 아는 개념 | 우리 프로젝트에서의 대응 |
|---|---|
| Controller | REST API (FastAPI) |
| Service | Orchestrator (에이전트 실행 순서 제어) |
| Repository | Artifact Registry (카드 저장/조회/버전관리) |
| Worker / Cron | Scheduler (매일 장마감 후 자동 실행) |
| DTO / Schema | 카드 JSON 스키마 (6종) |

즉, 구조 자체는 익숙한 백엔드와 같습니다.
다만 도메인이 "유저/주문/게시판"이 아니라 **DailyMarketPacket / StrategyCard / RiskCard / BacktestReport** 같은 트레이딩 카드인 것만 다릅니다.

---

## BE가 만드는 핵심 5가지

### 1. Orchestrator (워크플로우 엔진)

매일 이 순서가 정확히 돌아가게 만드는 엔진입니다.

```
Data Agent 실행 → DailyMarketPacket 생성 확인
  → Strategy Agent 실행 → StrategyCard 생성 확인
    → Risk Agent 실행 → RiskCard 생성 확인
      → Backtest Agent 실행 → BacktestReport 확인
        → 실패 시 → FailureCaseCard 발행 → Strategy 1회 재실행
```

- 이전 단계 카드가 없으면 다음 단계 실행 차단 (dependency check)
- 실패 시 1회 재시도 (retry policy)
- 실험 모드별 에이전트 on/off 제어 (S0~S4, R0~R3)

### 2. Artifact Registry (카드 저장소)

SQLite 기반으로 6종 카드를 저장하고 관리합니다.

```
저장하는 정보:
- run_id, agent_name, artifact_type
- artifact_version (retry 시 v1, v2 분리)
- json_payload (카드 내용)
- created_at
- experiment_mode (S1, R2 등)
```

카드 이력 조회, 버전 비교, 디버깅, 발표용 trace가 여기서 나옵니다.

### 3. REST API 서버 (FastAPI)

FE 대시보드와 수동 실행을 위한 API입니다.

```
POST /runs/daily              → 일일 파이프라인 실행
POST /runs/experiment         → 실험 모드 실행
GET  /runs/{run_id}           → 실행 결과 조회
GET  /artifacts/latest?type=  → 최신 카드 조회
GET  /backtests/{run_id}      → 백테스트 결과
GET  /dashboard/summary       → 대시보드 요약
```

### 4. Scheduler (자동 실행)

일봉 시스템이므로 매일 자동으로 파이프라인이 돌아야 합니다.

- 장마감 후(18:00 KST) 자동 실행
- 실험 배치 실행
- 실패 시 재시도 + 로그 기록

### 5. FE용 데이터 공급

FE 대시보드에서 보여줄 데이터를 제공합니다.

- 최신 포트폴리오 현황
- StrategyCard / RiskCard 내용
- 백테스트 수익률 차트 데이터
- 실험 결과 비교 데이터
- 실패 이력

---

## 반대로, BE가 안 하는 것

| AI가 주도 | BE가 주도 |
|---|---|
| Quant Prefilter 모델 학습 | REST API 서버 |
| Market/News/Synthesizer LLM 로직 | Orchestrator (실행 순서 제어) |
| Risk rule의 의미와 threshold 설정 | Artifact Registry (DB 설계) |
| Backtest 지표 공식 | Scheduler (자동 실행) |
| 카드 안에 뭘 넣을지 결정 | 카드가 어떻게 저장/라우팅/조회되는지 구현 |

**한 줄 요약: AI가 판단을 만들고, BE가 그 판단이 시스템 안에서 흐르게 만든다.**

---

## BE 2명 역할 분배

### BE #1 — Platform / Orchestrator 담당

| 모듈 | 예상 기간 |
|---|---|
| Orchestrator 코어 | 2~3주 |
| Artifact Registry + DB 스키마 | 1~2주 |
| Scheduler | 0.5주 |
| 실험 모드 컨트롤러 | 1~2주 |
| 통합 테스트 / retry 정책 | 1주 |

### BE #2 — API / Full-stack 담당

| 모듈 | 예상 기간 |
|---|---|
| REST API 서버 (FastAPI) | 1~2주 |
| 데이터 소스 연결 (pykrx/OpenDART) | 1~2주 |
| 카드 뷰어 / 대시보드 (FE) | 2~3주 |
| 실험 결과 비교 화면 | 1주 |

---

## 정리

우리 BE는 유저 관리 같은 웹앱 백엔드가 아니라,
**AI 에이전트 4개를 매일 자동으로 순서대로 돌리고, 카드를 저장하고, 실패하면 재시도하는 파이프라인 엔진**을 만드는 겁니다.

비유하면 **Airflow 같은 워크플로우 엔진을 우리 프로젝트에 맞게 직접 만드는 것**이라고 보면 됩니다.

# Elephant Lab: KOSPI 1분봉 멀티에이전트 Decision OS

KOSPI 30종목 대상 1분봉 멀티에이전트 Decision OS (종합설계 프로젝트, A+).

장중 매 1분 퀀트 시그널 + 이벤트 시 LLM 에이전트 개입 + 장마감 후 자동 진화의 **2모드 × 5레이어** 구조.

## 시스템 개요

```
Mode A 장중 (09:00~15:30):
  Hot Path : 1분봉 → LightGBM → PPO → PM → FDA approve/veto   (<100ms, LLM 미호출)
  Cold Path: 뉴스/공시/수급 → News/Risk/Debate Agent → FDA    (이벤트 시, 10~30초)

Mode B 장마감 (18:00~22:59):
  Alpha Factor Engine → Co-STEER → Backtest Agent → 22:00 배포 게이트
```

- **6 시스템 에이전트**: News, Risk(Fast/Slow), Quant, Debate, FDA, Backtest(Mode B)
- **18 API Contracts**: C1~C18 (`new/specs/api_contracts.md` = SSOT, v3.8 현행)
- **Blackboard 통신**: Shared Message Pool + Pub/Sub (MetaGPT 기반)
- **Dual-Source**: 뉴스↔커뮤니티 divergence = uncertainty 신호

## 프로젝트 결과

**종합설계 최종 평가 A+ · 학기 프로젝트 제출 범위 완료**

| 영역 | 상태 | 비고 |
|---|---|---|
| 기본 인프라 | 완료 | `new/src/` 19개 모듈 영역, connector/config/test harness |
| Hot Path | 완료 | LightGBM + PPO + Portfolio Manager + FDA |
| Cold Path | 완료 | News/Risk Fast·Slow/Debate + Blackboard + Event Gateway |
| Mode B | 완료 | Alpha Factor Engine + Co-STEER + Backtest Agent |
| 통합 검증 | 완료 | Dual-Source, C12/C14 gate, KIS virtual paper evidence |
| 동적 유니버스 | 완료 | KOSPI200 watch → admission/exit lifecycle |

데이터 수집, 피처화, Mode B 검증, C12 deploy gate, C14 service replay, KIS virtual broker evidence까지 paper-safe 범위에서 검증했다. production registry는 비활성으로 유지했고 실거래 전환은 프로젝트 범위에서 제외했다.

### Prelive Validation

아래 수치는 최종 paper-safe 검증 기록의 요약이다. 원천 시세, 모델 binary, 계좌 정보, broker evidence 원본은 공개본에서 제외했다.

| 항목 | 결과 |
|---|---|
| **C12 BacktestAgent** | verdict=`pass`, deployable=`true`, leakage=`pass`, registry_mutated=`false` |
| **C14 service-policy replay** | gate=`PASS`, no_naked_short, order_caps_respected, days=12 |
| **Dual-Source 80일 archive** | coverage=`1.0` (Naver+DART, FinBERT 5피처) |
| **Exogenous 80일 archive** | coverage=`1.0` (interest_rate, usd_krw, US 지수, 수급) |
| **Feature quality** | 90,230/90,230 non-neutral rows (dual-source + exogenous) |
| **KIS paper binding** | balance reconciliation PASS, probe order PASS, order-history match PASS |
| **KIS paper auto cycle** | external KIS virtual, paper_auto_cycle PASS, filled sell evidence matched |
| **service_readiness** | `PASS` (deploy_quality=PASS, broker_evidence=PASS, registry_mutated=false) |

성능 (`backtest_BUNDLE-20260512-0AEEE37A_20260514_202334.json`):
- IC=0.058, RankIC=0.077 (1분봉 cross-sectional ranking SOTA 범위 내)
- ARR=0.197, SR/IR=8.31 (12일 service-policy sample, deflation 검증 필요)
- MDD=-0.0008, cost_burn_pct_total=38.40% (cost-aware retraining 후보 식별됨)

### 공개본 범위

이 저장소는 소스 코드와 테스트 중심의 포트폴리오 공개본이다. API 키, 계좌 정보, 원천 시세, 학습된 모델, runtime registry, broker evidence는 포함하지 않는다. 따라서 코드 검증은 fresh clone에서 재현할 수 있지만, 과거 모델 배포 결과를 그대로 재실행하려면 별도 데이터와 모델 산출물이 필요하다.

## 빠른 시작

### 환경 부트스트랩

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -c constraints.txt

PYTHON=$PWD/.venv/bin/python ./init.sh  # 구조 확인 + gRPC stub 생성
PYTHON=$PWD/.venv/bin/python ./smoke.sh # mock/unit 기반 12개 smoke
```

### 단위 테스트

```bash
PYTHONPATH=$PWD/new .venv/bin/python -m pytest new/tests/unit -q
```

검증 기준은 Python 3.12.13이며 정확한 top-level 패키지 버전은 `constraints.txt`에 고정했다. C-extension 충돌이 발생하면 각 `new/tests/unit/test_*.py` 파일을 별도 pytest process로 실행한다.

### 발표 데모 (Hot / Cold / Mode B)

```bash
./demo.sh                              # Hot + Cold + Mode B 통합 데모
PYTHONPATH=new .venv/bin/python new/src/jobs/run_final_demo.py --demo hot
PYTHONPATH=new .venv/bin/python new/src/jobs/run_final_demo.py --demo cold
PYTHONPATH=new .venv/bin/python new/src/jobs/run_final_demo.py --demo mode_b
```

### Mode B 18:00~22:59 KST window

```bash
ELEPHANT_MODE=mode_b PYTHONPATH=new .venv/bin/python new/scripts/c12_recheck_runner.py --bundle-id BUNDLE-20260512-0AEEE37A --run
PYTHONPATH=new .venv/bin/python new/scripts/service_policy_replay.py --bundle-id BUNDLE-20260512-0AEEE37A
PYTHONPATH=new .venv/bin/python new/scripts/service_readiness_status.py --bundle-id BUNDLE-20260512-0AEEE37A
```

## 핵심 차별점

| 항목 | 설명 |
|---|---|
| 속도 | Hot Path <100ms, LLM 미호출. p50=0.7ms, p95=0.8ms 실측 (20종목 baseline.pkl) |
| 설명력 | FDA가 승인/거부 이유를 `reason_code` + CoT로 남김. Top-3 coverage threshold 0.80 |
| 적응력 | Mode B가 매일 밤 모델/팩터/필터 재점검. AST 중복 탐지 + 3중 정규화 |
| 국장 특화 | KIS 1분봉, DART, 네이버 뉴스, 커뮤니티, KRX 수급, ECOS 거시 |
| Cause attribution | L3 평가: FDA reason_code vs 사후 label 일치율 (threshold 0.60) |

## 현재 범위

연구형 / 모의운용형 MVP. **mock → replay → paper-trading** 단계로 검증.

- 실거래 (KIS real account)는 프로젝트 범위 밖이다. 현재는 paper (virtual) broker만 다룬다.
- production registry active version 승격 금지 유지.
- broker action UI / live order action 차단 (`safe_to_enable_live_actions=false`).

## 프로젝트 구조

```
Elephant_Lab/
├── new/
│   ├── docs/               # architecture.md, architecture_visual.md, paper_*.md (6편), evaluation_metrics.md
│   ├── specs/              # api_contracts.md (SSOT, C1~C18), failure_case_card.schema.yaml
│   ├── config/             # risk_config.yaml, dual_source.yaml, sector_config.yaml, dynamic_universe_config.yaml
│   ├── src/
│   │   ├── agents/         #   FDA + Hot (Quant/Risk Fast) + Cold (News/Risk Fast & Slow/Debate)
│   │   ├── connectors/     #   KIS, KRX, DART, Naver, Community, ECOS, US Market (7개)
│   │   ├── orchestration/  #   LLM Router (Kanana-o + GPT-4o fallback + circuit breaker)
│   │   ├── models/         #   LightGBM (ranking), PPO Allocator, Committee
│   │   ├── mode_b/         #   Alpha Factor Engine, Co-STEER, Scheduler, Backtest Engine
│   │   ├── data/           #   Backfill, dataset builder, dual_source/exogenous feature stores, news/text filter
│   │   ├── blackboard/     #   Shared Message Pool + Pub/Sub
│   │   ├── execution/      #   Execution Gateway (paper/virtual/real 3 모드)
│   │   ├── portfolio/      #   Portfolio Manager
│   │   ├── dynamic_universe/  # C15/C16 admission/exit lifecycle (KOSPI200 watch)
│   │   ├── eval/           #   L3 reason_code 분포 + Cause Attribution
│   │   └── ops/            #   AuditLogger (C18), profiler, persistent cache
│   ├── tests/              # pytest contract/unit/integration (2,221 tests collected)
│   ├── src/jobs/           # E2E / replay / backfill / Mode B / final_demo
│   └── scripts/            # phase2_feature_backfill, c12_recheck, service_*, kis_paper_*
├── artifacts/              # gitignored 런타임 산출물 (모델/팩터/오디트/reports)
├── init.sh / smoke.sh / demo.sh / eval.sh
├── requirements.txt
├── CLAUDE.md               # v3 프로젝트 가이드
└── .env.example            # 키 이름만 (실제 .env 는 gitignored)
```

## 핵심 제약 (불변 5원칙, 절대 위반 금지)

1. **PIT-Safety**: 미래 데이터 사용 금지. snapshot 18:00 KST.
2. **FDA can_change_weight = false**: approve/veto만. 비중은 PPO, order_deltas는 PM.
3. **Backtest Agent Mode B 전용**: 장중 경로 절대 미개입 (`forbidden_permissions` 6+4).
4. **LLM 예산**: 장중 Cold Path Kanana-o 100회/일. Mode B GPT-4o 전용. Hot Path 동기 LLM 금지.
5. **하드코딩 금지**: 모든 임계값/종목코드는 `new/config/risk_config.yaml`에서 로드.

추가: 종목코드 `str(ticker).zfill(6)`. `.env` 파일은 수정/읽기 금지.

## LLM 구성

| 경로 | 모델 | 한도 |
|---|---|---|
| Hot Path (1분 주기) | 미호출 | <100ms 동기 |
| Cold Path (이벤트 시) | Kanana-o | 100회/일 |
| Mode B (장마감) | GPT-4o | 별도 예산 |
| Fallback | GPT-4o (429/timeout 시) | circuit breaker 3회 → 5분 |

## Runtime artifacts (gitignored)

런타임 산출물은 `artifacts/` 아래에서 생성되며 공개 저장소에는 포함하지 않는다. 주요 경로:

| 경로 | 내용 |
|---|---|
| `artifacts/data/{YYYYMMDD}/{ticker}.parquet` | 1분봉 backfill |
| `artifacts/dual_source/{YYYYMMDD}.json` | 08:00 KST dual-source 5피처 |
| `artifacts/exogenous/{YYYYMMDD}.json` | 거시 지표 + 수급 |
| `artifacts/lgbm/registry.json` + `artifacts/lgbm_paper/registry.json` | ModelRegistry |
| `artifacts/bundles/BUNDLE-{date}-{hash}/` | Mode B 후보 번들 |
| `artifacts/reports/backtest/` + `reports/c12_recheck/` | C12 검증 결과 |
| `artifacts/reports/service_readiness/` + `reports/service_policy_replay/` | C14 게이트 결과 |
| `artifacts/reports/paper_trading/` | KIS paper broker evidence |
| `artifacts/agent_memory/{agent}/{YYYYMMDD}.jsonl` | 에이전트 메모리 일별 append |
| `artifacts/audit/agent_performance/{YYYYMMDD}.jsonl` | C18 AgentPerformance 18 필드 |

## 공개 정책

개인 종합설계 포트폴리오 공개본. 실거래 키, 계좌 정보, 원천 데이터, 런타임 산출물은 포함하지 않는다. `vendor/reference/`의 제3자 자료는 각 원출처의 권리를 따른다.

# Elephant Lab v3 학기말 최종 Plan

> 본 문서는 **학기말 6주 (W1~W6) 실행 가이드**다.
> 기준: GPT Pro 메타 검증 + Claude 자기 감사 정정 반영 (2026-05-06).
> SSOT 아님. 보조 문서. 계약/정책/구조 SSOT는 각각 `api_contracts.md` / `risk_config.yaml` / `architecture.md` 유지.

---

## 0. 메타데이터

| 항목 | 값 |
|---|---|
| 버전 | **v1.3 (2026-05-09 SSOT 충돌 해소: analysis/ → eval/)** |
| 작성 | Claude Code (사용자 confirm 후 SHIP) |
| 기반 | GPT Pro 1차+2차 권고 (1순위) + Claude 자기 감사 + 코드 실측 fact 정정 |
| 적용 범위 | 학기말 발표 (D-5~7주 추정) |
| 학기 진척 | 55/57 (96.5%), 미완 2건 모두 KIS 키 의존 |
| pytest | 1124 passed, 12 fail (numpy 1.x→2.x ABI 충돌) |
| 미push commit | 5건 (`33d9902 → 0ae7adf`) |
| Mode B fail | 4단계 cascade root cause (GPT 2차 정정) |

---

## 1. 자기 감사 정정 사항

내가(Claude) 직전 plan에서 환각 또는 임의 변경한 항목을 GPT 안 또는 fact로 정정한다.

| 정정 | Before (Claude 환각) | After (GPT/fact) |
|---|---|---|
| 산출물 경로 | `new/src/eval/...` | **`new/src/eval/...`** (evaluation_metrics.md SSOT 우선, v1.3 정정) |
| 슬라이드 수 | 17~18장 | **14장** (GPT 명시) |
| `architecture_visual.md` 버전 | "3중 drift" | **drift 아님**. 변경 이력 v3.1.0까지 정상 |
| `requirements.txt` 의존성 | "GPT 주장 틀림" | **3개 다 있음**, 단 numpy 1.x→2.x ABI 충돌 (sklearn + tsfresh + sb3 동일 원인) |

### v1.1 추가 정정 (GPT 2차 검증 + 코드 실측)

| 정정 | v1.0 (Claude 1차) | v1.1 (GPT 2차 + 코드 실측) |
|---|---|---|
| Mode B fail root cause | "ELEPHANT_MODE 환경 변수" (1단계) | **4단계 cascade**: ELEPHANT_MODE → BacktestAgent wrong SSOT → artifacts/data 부재 → sb3 ABI 충돌 |
| BacktestAgent universe | `bundle['universe']` 기본값 보장 | **`backtest.py:L142` SSOT 수정**: `risk_config.yaml`(universe 키 없음) → `universe_config.yaml` 참조 |
| Wrong universe 동작 | 검증 미완 | **silent fallback** (`active_tickers` default `["005930", "000660"]` 2종목으로 backtest 실행). KeyError 아님, 더 위험 |
| requirements vs venv | "있음 = 문제 없음" | **있음 ≠ 격리 OK**. venv lock 별도 필요 (pip freeze 또는 conda env export) |

**원칙**: 외부 규칙 약화 금지 (memory `feedback_external_rule_no_weakening.md`). GPT 1순위 (memory `feedback_gpt_pro_priority.md`).

---

## 2. 발표 전략 (Phase 0)

```
기말발표 = 연구형/모의운용형 Decision OS의 작동 구조와 검증 가능성
critical path:
  돌아가는 mock/replay 데모
  + reason_code 기반 설명 가능성
  + L1/L2/L3 평가 수치
  + Mode B가 회귀 위험을 막는 장면
```

**핵심 메시지**: "기능이 많다"가 아니라 **"시스템이 왜 판단했고, 그 판단이 맞았는지 수치로 보여준다"**.

---

## 3. 우선순위 P0~P4

```
P0  실행 재현성 + E2E Mode B 복구
P1  L3 reason_code / Cause Attribution 수치 생성
P2  C10 audit → KB feedback loop
P3  SSOT drift / config drift 정리
P4  발표자료 + 리허설
```

### 3.1 P0 한 줄 정의 (GPT 2차 §7 결론)

```
P0 = smoke/env + ELEPHANT_MODE + universe_config + synthetic data + demo runner
```

**플랜 종합 메시지** (GPT 2차 마지막 문장):
> 이 방향으로 가면 Claude 플랜의 강점인 L3 발표 임팩트는 살리고,
> 실제 코드 실행에서 터질 P0 리스크도 막을 수 있다.

---

## 4. P0 실행 재현성 (W1) — 6단계 (GPT 2차)

### 4.1 작업 (P0-1 ~ P0-6)

| ID | 작업 | 담당 | 비고 |
|---|---|---|---|
| **P0-1** | `smoke.sh` Python 경로 하드코딩 제거 | 사용자 | L8 `/opt/anaconda3/envs/elephant/bin/python` → `${PYTHON:-python3}` |
| **P0-2** | `ELEPHANT_MODE=mode_b` 처리 | 사용자 | pytest fixture 또는 `e2e_runner` 내부 context |
| **P0-3** | **BacktestAgent universe SSOT 수정** | 사용자 | `backtest.py:L142` `config_load("risk_config.yaml", "universe")` → `config_load("universe_config.yaml", ...)` 변경. 현재 silent fallback으로 default 2종목만 사용 |
| **P0-4** | synthetic 1분봉 parquet 생성 | 예경님 | `artifacts/data/{ticker}/bars_1m_YYYYMMDD.parquet`. LightGBM retrain tickers=0 해소 |
| **P0-5** | dependency import 검증 (독립 gate) | 공통 | venv에서 `import pyarrow / tsfresh / stable_baselines3 / sklearn` 실제 검증. numpy ABI 충돌 시 venv lock |
| **P0-6** | `demo.sh` 또는 `run_final_demo.py` 고정 | 사용자 | **W1에 반드시 작성**. 발표 당일 여러 명령 조합 시 실패 확률 큼. Hot/Cold/Mode B 단일 명령 |

### 4.2 Mode B fail 4단계 cascade (GPT 2차 + 코드 실측)

```
1차: ELEPHANT_MODE=mode_b 미설정
     → 5개 가드 발동 (NightlyLGBMRetrainer / NightlyPPORetrainer / CoSteer / BacktestAgent.run)
     → 해소: P0-2

2차: BacktestAgent backtest.py:L142 wrong SSOT
     universe_cfg = config_load("risk_config.yaml", "universe")  # 키 없음 → {} fallback
     universe = active_tickers default ["005930", "000660"]
     → 실제 SSOT: new/config/universe_config.yaml (sectors / backtest_universe_mode)
     → 현재 동작: silent fallback (KeyError 아님). 그래도 wrong universe (2종목만)
     → 해소: P0-3

3차: artifacts/data/ 디렉토리 부재
     → LightGBM retrain "tickers=0" 발생
     → 해소: P0-4 (synthetic parquet)

4차: stable_baselines3 numpy ABI 충돌
     numpy 1.x로 컴파일된 모듈 + 현재 numpy 2.4.4
     → import 자체 실패 (matplotlib._path 단계에서)
     → 해소: P0-5 (venv lock)
```

각 단계는 **순서 의존**. 1차 해소 후 2차 노출, 2차 해소 후 3차 노출 cascade.

### 4.3 Mode B verdict 분리 (GPT 권고)

```
backtest_verdict: pass | warn | fail            # BacktestAgent 원판정
deploy_status: deployed | operator_review | blocked | skipped_no_candidates  # Deployer 결과
```

이유: 두 역할 섞이면 "Backtest pass였는데 deploy 실패" 추적 불가.
적용처: `api_contracts.md` C12 + `e2e_scenario_runner.py` mode_b_result + `test_e2e_scenario.py:312` assertion.

---

## 5. P1 L3 Cause Attribution (W2)

### 5.1 산출물

```
new/src/eval/reason_code_stats.py        ← evaluation_metrics.md SSOT (v1.3 정정)
new/src/eval/cause_attribution.py        ← evaluation_metrics.md SSOT (v1.3 정정)
artifacts/metrics/reason_code_stats_YYYYMMDD.json
artifacts/metrics/cause_attribution_YYYYMMDD.json
```

### 5.2 지표

| 지표 | 계산 |
|---|---|
| reason_code 분포 | `Counter(reason_code)` |
| veto precision | veto 후 `label_t5_ret < 0` 비율 |
| RISK_FAST_TRIGGER accuracy | Risk Fast veto 후 5분 수익률 하락 비율 |
| NEWS_DIVERGENCE accuracy | divergence veto 후 5분 하락 또는 변동성 확대 비율 |
| NORMAL_APPROVE hit-rate | approve 후 `label_t5_ret > 0` 비율 |
| Cause Attribution Accuracy | reason_code가 사후 결과와 일치한 비율 |

### 5.3 FDA reason_code 7종 (`fda.py:14-15` 확인)

```
NORMAL_APPROVE / TIMEOUT / RISK_FAST_TRIGGER / DEBATE_CONFLICT
NEWS_DIVERGENCE / QUANT_ANOMALY / MISSING_PORTFOLIO_PATCH
```

---

## 6. P2 KB Feedback Loop (W3)

### 6.1 흐름

```
장중:    ExecutionGateway → audit_log append   (Hot Path 보호)
장마감:  ModeBPerformanceAggregator → audit_log 읽기
        → KB decision_history / backtest_history batch write
        → agent memory summary 갱신
```

**원칙**: Hot Path에서 KB write 금지 (latency 보호). 장마감 batch만.

### 6.2 함수

| 함수 | 설명 |
|---|---|
| `write_execution_kb_entry()` | execution_report + final_decision + outcome → KB 저장 |
| `update_agent_memory_summary()` | News / Risk / FDA / Quant / Debate 5종 day summary append |
| `reconciliation` | mock/paper에서는 optional. KIS position 있을 때만 실행 |
| unit test | KB entry 필수 필드 + PIT-Safety + storage_type 검증 |

---

## 7. P3 SSOT / drift cleanup

| 작업 | 우선순위 | 비고 |
|---|---|---|
| `feature_list.json` Sprint 5 progress drift fix | P1 | 5분, JSON 3줄 수정 |
| `backtest.py` forbidden_permissions 명칭 통일 (C12 기준) | P1 | 20분, code 명칭 → 계약서 명칭 |
| `smoke.sh` 경로 하드코딩 제거 | P0 | P0과 통합 |
| Dynamic overlay state | P2 | Sprint 5 demo 넣을 때만 |
| `dynamic_universe_config.yaml` 0.60/0.40 score | P2 | 코드 주장하려면 추가 |

### 7.1 Dynamic overlay 처리 (GPT 안)

전역 `PipelineState` 늘리지 말고 `DynamicUniverseManager` 내부 sub-state 5개로 처리:

```
DISABLED → WATCHING → CANDIDATE_ADMITTED → HOLDING_ACTIVE → EXITED
```

이유: Dynamic Event Universe는 core pipeline state 아닌 **Sprint 5 overlay 확장안** (v4 보고서 기준).

---

## 8. P4 발표 데모 + 슬라이드 (W4~W5)

### 8.1 데모 3개

#### Demo A — Hot Path (~2분 30초)

```
입력: 5종목 synthetic 1분봉
흐름: Quant → PPO/PM → FDA non-LLM → mock execution
산출: final_decision.json, latency p95, audit_log
```

**메시지**: "Hot Path는 LLM 없이 <100ms로 돈다. FDA는 비LLM 7체크리스트만 수행한다."

#### Demo B — Cold Path 이벤트 (~2분 30초)

```
입력: NEWS_DIVERGENCE 또는 comm_volume_spike inject
흐름: EventGateway → News/Risk Slow → FDA → veto
산출: reason_code = NEWS_DIVERGENCE or RISK_FAST_TRIGGER
```

**메시지**: "가격/거래량만 보지 않고, 뉴스-커뮤니티 divergence나 커뮤니티 spike를 정성 리스크로 해석한다."

#### Demo C — Mode B blocked (~1분, 사전 캡처)

```
입력: candidate_bundle
흐름: ModeBScheduler (7-stage cron) → BacktestAgent → ModeBDeployer (atomic swap or block)
산출: backtest_verdict=fail → deploy_status=blocked → baseline 유지

설명 분리 (architecture_visual.md 정합):
  - ModeBScheduler: 7-stage cron (stage_0 DQR ~ stage_7 deploy)
  - BacktestAgent: 회귀 검증 + verdict (pass/warn/fail)
  - ModeBDeployer: atomic swap (verdict=pass 시) 또는 block (verdict=fail 시)
```

**메시지**: "내일의 시스템은 오늘과 다르지만, Backtest가 회귀 위험을 감지하면 배포하지 않는다."

### 8.2 슬라이드 14장 (GPT 명시)

```
1. 문제 정의
2. 왜 퀀트 단독으로 부족한가
3. 5 Layer + 5 Agent 구조
4. Hot Path / Cold Path
5. Quant Stream
6. Event / Risk Stream
7. FDA reason_code
8. Debate Agent
9. Mode B
10. Backtest Agent gate
11. Evaluation Matrix L1 / L2 / L3
12. Demo A / B / C
13. 구현 현황
14. 한계와 향후 계획
```

---

## 9. 역할 분담 (GPT 안)

### 9.1 예경님: Quant Stream

```
1. Quant Agent: LightGBM inference / score+confidence / quant_signal / anomaly_detected
2. 피처 실험: 1분봉 OHLCV / multi-scale / dual-source 5피처 / foreign_age / ablation
3. Debate Agent: conflict detection / Top-10 pairwise / win-count rerank / debate_resolution / pairwise_ranking
4. 발표 산출: feature importance / ablation 표 / LightGBM baseline vs Committee 비교
```

### 9.2 사용자(나): Event / Risk / Decision / Integration

```
1. News Agent: NewsFilter / TextPack / news_signal / dart_alert / 뉴스·공시·커뮤니티 이벤트 해석
2. Risk Agent: Risk Fast/Slow / comm_volume_spike / foreign_alert / news_comm_divergence / risk_warning / veto_recommendation
3. FDA: approve/veto / reason_code 7종 / veto_reason / risk_overrides / Hot 7체크리스트 / Cold CoT
4. LLM Router / Event orchestration: Kanana-o/GPT-4o routing / budget / fallback / EventGateway / MessagePool
5. Integration / Mode B / 발표: E2E demo / Mode B blocked demo / reason_code_stats / cause_attribution / KB feedback / 최종 발표 흐름
```

---

## 10. 6주 일정 (W1~W6)

### W1 (5/6~5/12) — 실행 재현성 / 데모 복구 (GPT 2차 분담)

**사용자**:
| 작업 | 매핑 |
|---|---|
| `smoke.sh` Python 경로 수정 | P0-1 |
| `ELEPHANT_MODE=mode_b` context 처리 | P0-2 |
| **BacktestAgent universe_config 로딩 수정** | **P0-3 (신규)** |
| `demo.sh` 또는 `run_final_demo.py` 생성 | P0-6 |
| Cold Path event inject 1개 고정 | Demo B prep |

**예경님**:
| 작업 | 매핑 |
|---|---|
| synthetic 1분봉 parquet 생성 | P0-4 |
| feature 실험용 panel 생성 | Quant prep |
| Hot Path demo input 준비 | Demo A prep |

**공통**:
| 작업 | 매핑 |
|---|---|
| dependency import 검증 (pyarrow / tsfresh / sb3 / sklearn / numpy) | P0-5 |
| Hot Path demo 1개 고정 | Demo A |

### W2 (5/13~5/19) — L3 Cause Attribution

**사용자**:
- `reason_code_stats.py`
- `cause_attribution.py`
- FDA reason_code별 사후 수익률 표
- mock/replay audit_log 생성

**예경님**:
- feature ablation 1차 결과
- **LightGBM baseline 수치** (신규)

### W3 (5/20~5/26) — KB feedback / SSOT cleanup

**사용자**:
- execution audit → KB batch write
- memory summary append
- Backtest forbidden_permissions 명칭 통일
- `feature_list.json` progress drift 수정

**예경님**:
- Debate ranking output 정리
- **pairwise_ranking 예시 생성** (신규)

### W4 (5/27~6/2) — 발표자료 1차

```
- 14장 슬라이드 작성
- Hot/Cold/Mode B demo 캡처
- L1/L2/L3 수치 슬라이드
- Sprint 5 Dynamic Universe는 overlay 확장안으로만 언급 (core 아님)
  → DynamicUniverseManager sub-state 5개 (DISABLED/WATCHING/CANDIDATE_ADMITTED/HOLDING_ACTIVE/EXITED)
```

### W5 (6/3~6/9) — 리허설 / 수치 freeze

산출물 freeze:

| 산출물 | 상태 |
|---|---|
| Hot Path latency p95 | 고정 |
| reason_code distribution | 고정 |
| Cause Attribution Accuracy | 고정 |
| feature ablation table | 고정 |
| Mode B blocked example | 고정 |
| Q&A 문서 | 고정 |

### W6 (예비) — fallback 준비

```
- demo 영상 백업
- JSON artifact 캡처
- live 실행 대신 screenshot fallback
- 발표 script 정리
- Q&A 예상 질문 정리
```

---

## 11. Defer 7건 (학기말 범위 외)

| 항목 | 이유 |
|---|---|
| KIS 실거래 / 자동매매 | non-critical. 현재 범위 = mock/replay/paper. OMS Phase 2 이후 |
| 커뮤니티 특화 LLM | 연구 확장 (Phase 2A small LLM / 2B LoRA) |
| STGNN / Temporal CP / LLM-DRL | 대학원 트랙 (FAILAB) |
| AAPM N-round 고도화 | 발표 직접 기여도 낮음 |
| Dynamic Universe 완전 실운영 | overlay demo로 충분 |
| `risk_config.yaml` 자동 수정 | operator/manual approval 대상 (`architecture.md`) |
| 배포 게이트 강화 (자동 수정 구조) | default config + validation 기준으로 격하 |

---

## 12. 발표 프레이밍 (PT-1 ~ PT-6)

```
PT-1 "기존 퀀트는 수익률만 봅니다. 저희 시스템은 FDA가 왜 approve/veto 했는지
     reason_code로 남기고, 그 reason_code가 사후 가격 흐름과 맞았는지를
     Cause Attribution으로 측정합니다."

PT-2 "기말발표에서 점수를 만드는 건 '기능이 많다'가 아니라 '시스템이 왜 판단했고,
     그 판단이 맞았는지 수치로 보여준다' 입니다."

PT-3 "Hot Path는 LLM 없이 <100ms 목표로 돈다. FDA는 Hot에서도 존재하지만
     비LLM 7체크리스트만 수행한다."

PT-4 "가격/거래량만 보지 않고, 뉴스-커뮤니티 divergence나 커뮤니티 spike를
     정성 리스크로 해석한다."

PT-5 "내일의 시스템은 오늘과 다르지만, Backtest가 회귀 위험을 감지하면
     배포하지 않는다."

PT-6 "판단 권한은 FDA, 비중 결정은 PPO Allocator, 실행은 Portfolio Manager.
     에이전트가 서로의 권한을 침범할 수 없다."
```

### 12.1 GPT 2차 5문장 (PT-7 ~ PT-11)

GPT 2차 결론에서 명시한 발표 핵심 5문장. 슬라이드 매핑 함께.

```
PT-7  "Hot Path가 빠르게 돈다."
      → 슬라이드 4 (Hot/Cold) 첫 문장

PT-8  "Cold Path가 이벤트를 설명한다."
      → 슬라이드 6 (Event/Risk) 제목

PT-9  "FDA가 reason_code로 판단 근거를 남긴다."
      → 슬라이드 7 (FDA reason_code) 핵심 1줄

PT-10 "L3 Cause Attribution으로 그 판단이 맞았는지 수치화한다."
      → 슬라이드 11 (L1/L2/L3) 킬러 문장

PT-11 "Mode B는 개선 후보를 만들지만, Backtest fail이면 배포를 막는다."
      → 슬라이드 10 (Backtest gate) + Demo C 메시지
```

---

## 13. 즉시 SHIP 가능 (5~30분 단위, 사용자 결정 무관) — 5건 (GPT 2차 확정)

W1 작업 중 즉시 가능:

| # | 작업 | 파일 | 소요 | 우선순위 |
|---|---|---|---|---|
| 1 | `feature_list.json` Sprint 5 progress drift fix | `feature_list.json` | 5분 | P3 |
| 2 | `backtest.py` forbidden_permissions 명칭 통일 (C12 기준) | `new/src/agents/mode_b/backtest.py` | 20분 | P3 |
| 3 | `smoke.sh` L8 Python 경로 fix | `smoke.sh` | 10분 | P0 |
| 4 | `ELEPHANT_MODE=mode_b` pytest fixture 추가 | e2e test fixture | 15분 | P0 |
| 5 | **`backtest.py:L142` universe SSOT 수정** (risk_config → universe_config) | `new/src/agents/mode_b/backtest.py` | 15분 | **P0 (신규)** |

**5번 신규 추가 근거**: `risk_config.yaml` 최상위 `universe` 키 미존재 코드 실측 완료. silent fallback으로 default 2종목만 backtest 실행 (`["005930", "000660"]`). 실제 SSOT는 `universe_config.yaml`. KeyError 아닌 silent wrong → 더 위험한 케이스.

---

## 14. 사용자 결정 변수

| Q | 옵션 |
|---|---|
| Q1 | **즉시 SHIP 5건 (GPT 2차 확정) 지금 일괄 진행**? (~65분) |
| Q2 | W1 트랙 시작 (P0)? `new/src/eval/` 디렉토리 + cause_attribution.py + reason_code_stats.py 진입? |
| Q3 | 미push 5건 push 시점 (지금 / W1 끝 / W2 끝)? |
| Q4 | 기말 발표 일자 / 시간 (10/15/20분)? — W4 진입 시점 결정 |
| Q5 | KIS 키 신청 시점? (defer = OMS Phase 2지만, 발급되면 Sprint 100% 보너스) |

---

## 15. 변경 이력

| 버전 | 날짜 | 변경 |
|---|---|---|
| v1.0 | 2026-05-06 | 초안. GPT Pro 메타 검증 + Claude 자기 감사 정정 통합 |
| **v1.1** | **2026-05-06** | **GPT Pro 2차 검증 반영. Mode B fail 4단계 cascade 명시 (P0-3 BacktestAgent universe SSOT 추가). 즉시 SHIP 4건 → 5건. PT-7~PT-11 추가. P0 5단계 → 6단계 확장. silent fallback 사실 정정 (KeyError 아님)** |
| **v1.2** | **2026-05-06** | **GPT 2차 누락 8건 보강. (1) §3.1 P0 한 줄 정의 + 종합 메시지 추가 (2) §4.1 P0-6 demo runner W1 필수 강조 (3) §8.1 Demo C에 ModeBScheduler 7-stage + Deployer atomic swap 분리 (4) §10 W1 사용자/예경님/공통 분담 표 + BacktestAgent universe + demo runner + dependency import + feature panel + Hot Path input 추가 (5) §10 W2 LightGBM baseline 수치 추가 (6) §10 W3 pairwise_ranking 예시 추가 (7) §10 W4 overlay 확장안 표현 정정 (8) §14 Q1 4건 → 5건 정정 (drift fix)** |
| **v1.3** | **2026-05-09** | **SSOT 충돌 해소. analysis/ → eval/ (Claude + GPT Pro + 사용자 3자 합의). evaluation_metrics.md L155/L160 SSOT 우선. v1.2의 임의 변경 (eval → analysis) 롤백. 수정: §1 정정 표 / §5.1 산출물 경로 / §14 Q2 / 변경 이력 추가. eval/__init__.py + reason_code_stats.py + cause_attribution.py 신설 (W2 P1 SHIP, 15 unit test PASS, pytest 1164 → 1179)** |

---

## 16. 참조 문서 (SSOT 우선순위)

```
1. new/specs/api_contracts.md      ← C1~C18 계약 SSOT
2. new/config/risk_config.yaml     ← 리스크/임계값 SSOT
3. new/docs/architecture.md        ← 구조 SSOT (v3.8)
4. new/docs/architecture_visual.md ← 시각 SSOT (v3.1.0)
5. new/docs/evaluation_metrics.md  ← 평가 SSOT (3-Layer)
6. new/docs/final_plan.md          ← 본 문서 (학기말 가이드, 보조)
```

---

**STATUS**: 본 문서는 W1 진입 전 사용자 confirm 필요. 사용자 결정 후 트랙별 SHIP 시작.

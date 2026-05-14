# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

KOSPI 30종목(active 20 + pending 10) 대상 1분봉 멀티에이전트 Decision OS (종합설계 프로젝트).
장중 매 1분 퀀트 시그널 생성 + 이벤트 시 LLM 에이전트 개입 + 장마감 후 자동 진화.

## 파이프라인 v3 (2모드 × 5레이어)

```
Mode A 장중 (09:00~15:30):
  Hot Path (매 1분, <100ms, LLM 미호출):
    1분봉 → LightGBM → PPO Allocator → PM → FDA approve/veto
  Cold Path (이벤트 시, 10~30초):
    뉴스/공시/수급 → News/Risk/Debate Agent → FDA

Mode B 장마감 (18:00~22:00):
  Alpha Factor Engine → Co-STEER → Backtest Agent → 22:00 배포 게이트
```

## 핵심 제약: 불변 원칙 5개 (절대 위반 금지)

1. **PIT-Safety**: 미래 데이터 사용 금지. snapshot 기준 18:00 KST.
2. **FDA can_change_weight = false**: FDA는 approve/veto만. 비중은 PPO Allocator, order_deltas는 Portfolio Manager.
3. **Backtest Agent Mode B 전용**: 장중 경로 절대 미개입. C12 BacktestAgent forbidden_permissions 6개 + C14 Mode B Scheduler forbidden_permissions 4개.
4. **Kanana-o 100회/일 예산**: 장중 LLM 한도. Mode B는 GPT-4o 전용.
5. **하드코딩 금지**: 모든 수치/임계값은 `new/config/risk_config.yaml`에서 로드.

### 추가 제약
- **FDA reason_code 필수**: FDA는 모든 판단에서 `reason_code` 출력 필수 (`api_contracts.md` C9 `reason_code_required: true`). cause 중심 설계, Sprint 2 enum 확정.
- **종목코드**: `str(ticker).zfill(6)` (6자리 zero-padded).
- **GPT Pro 우선**: GPT Pro 피드백 1순위. Claude 판단으로 제외/축소 금지.
- **계약서 SSOT**: `new/specs/api_contracts.md`가 필드 정의의 단일 진실 소스.
- **Hot Path <100ms**: 동기 LLM 호출 금지.

## 코드 컨벤션

- JSON: `json.dump(data, f, ensure_ascii=False, indent=2)`
- 경로: `pathlib.Path`, `mkdir(parents=True, exist_ok=True)`
- 에러: bare `except:` 금지. `except Exception as e:` 사용.
- 로그: 한국어, `[모듈명]` 접두사

## 주요 파일

| 파일 | 용도 |
|------|------|
| `new/docs/architecture.md` | v3 상세 아키텍처 (**가장 중요**) |
| `new/docs/architecture_visual.md` | v3 ASCII 시각화 (architecture.md 보조, Layer 별 박스 다이어그램) |
| `new/specs/api_contracts.md` | C1~C18 API 계약서 (**SSOT**, v3.8 현행: C15/C16 정식화 + ADM/EXT/WS UUID8 등록 + weight_decision_authority + activation_gate + polling_authority, v3.5: PP/BUNDLE/BT/RPT/FCC/RGC UUID8 정정, v3.4: MSG/APM UUID8, v3.3: C9 uncertainty_score extension, v3.2: C18 AgentPerformance 신설) |
| `new/config/risk_config.yaml` | 리스크 정책 + 임계값 |
| `new/docs/connector_design.md` | 커넥터 방법론 10개 |
| `new/docs/evaluation_metrics.md` | 3-레이어 평가 매트릭 (Layer 1 모델 / Layer 2 Agent / Layer 3 System Cause Attribution) |
| `new/docs/cross_paper_synergies.md` | 6개 논문 교차 시너지 5가지 (AST+KB통합, pairwise, 메시지통일, Cross-Asset, 동적LLM라우팅) |
| `new/docs/paper_1_aapm.md` ~ `paper_6_alphaagent.md` | 6편 논문 분석 요약 (AAPM/AlphaGAT/MetaGPT/RD-Agent/TradeXpert/AlphaAgent, 설계 근거) |
| `new/src/connectors/naver_rest.py` | Naver 뉴스 커넥터 v3 S2-3 (Cold Path NewsAgent 입력, auth/rate/normalizer 3중 통합) |
| `new/src/connectors/community.py` | 커뮤니티 크롤러 v3 S2-4 (3-stage 필터 + Dual-Source 원천) |
| `new/src/connectors/ecos_rest.py` | ECOS 거시 지표 커넥터 v3 S2-5 (interest_rate / usd_krw, 08:00 배치) |
| `new/src/connectors/us_market.py` | US Market 야간 지수 커넥터 v3 S2-5 (SPX/NDX/VIX/SOXX, yfinance fallback) |
| `new/src/orchestration/llm_router.py` | LLM Router v3 S2-2 (Kanana-o + GPT-4o fallback + circuit breaker) |
| `new/src/ops/audit_logger.py` | C18 AgentPerformance 18 필드 audit log (L2 9 지표 집계 인프라, PIT-Safety backfill guard) |
| `new/config/sector_config.yaml` | KOSPI 6 섹터 + 20 ticker 매핑 (L2 sector_tracking_error 전제) |
| `new/src/mode_b/performance_aggregator.py` | `ModeBPerformanceAggregator` S2-10 실구현 (C18 L2 9지표 + 8d 벡터, PIT-Safety 18:00 KST 이후만) |
| `new/src/data/filter_loader.py` | 4 yaml loader + 캐시 (S2-6, news_filter/spam_rules/manipulation_rules/sentiment_dict) |
| `new/src/data/news_filter.py` | NewsFilter 3-level 매칭 (S2-6, ticker/sector/market) |
| `new/src/data/text_pack_builder.py` | TSFresh 30분 통계 → 자연어 변환 (S2-6, 13 템플릿 + fallback + 3-way policy + NaN 가드) |
| `new/src/agents/cold/news.py` | NewsAgent (S2-7 실구현, Cold Path Kanana-o CoT + C5 news_signal/dart_alert + micro/macro memory) |
| `new/src/agents/cold/risk_fast.py` | RiskAgentFast Cold Path (S2-8 실구현, trigger_catalog 6규칙 비LLM, <50ms, C5 risk_warning) |
| `new/src/agents/cold/risk_slow.py` | RiskAgentSlow Cold Path (S2-8 실구현, Kanana-o CoT, C5 risk_warning + channel 분기 regime_change/veto_recommendation) |
| `new/src/agents/cold/debate.py` | DebateAgent Cold Path (S2-9 실구현, C6 pairwise CoT 45회, debate_resolution/pairwise_ranking, debate_history JSONL) |
| `new/src/connectors/base.py` | BaseConnector S2-11 (7개 커넥터 공통 기반: _load_defaults + _http_get_json + urllib fallback) |
| `new/src/utils/pit_guard.py` | PIT-Safety SSOT (`is_pit_safe` + `PITViolationError`, snapshot_ts 18:00 KST) |
| `new/src/data/dual_source_scorer.py` | C3A Dual-Source 5피처 배치 (S4-1, FinBERT + 3-yaml + decay + divergence + noise multiplier) |
| `new/src/data/dual_source_runner.py` | 08:00 KST 장전 배치 실행기 (S4-1, artifacts/dual_source/YYYYMMDD.json) |
| `new/src/jobs/run_dual_source_ablation.py` | Dual-Source ablation CLI (S4-2, w/ vs w/o LightGBM 재학습 비교) |
| `new/src/runner/e2e_scenario_runner.py` | E2E 시나리오 오케스트레이터 (S4-3, 5일 Mode A + Mode B 시뮬) |
| `new/src/runner/event_injector.py` | 합성 이벤트 주입기 (S4-3, news/dart/community/macro 4종) |
| `new/src/jobs/run_e2e_scenario.py` | E2E 시나리오 CLI (S4-3, week1_basic.yaml) |
| `new/src/ops/profiler.py` | Hot Path 6단계 레이턴시 측정 (S4-4, p50/p95/p99 + SLA alert) |
| `new/src/dqr/dqr_runner.py` | DQR 일별 자동화 (S4-5, Mode B stage_0, 8 커넥터 5 메트릭) |
| `new/src/cache/persistent_cache.py` | SQLite TTL 캐시 (S4-7, Cold Path 레이턴시 최적화) |
| `new/src/agents/memory_restorer.py` | Bootstrap 에이전트 메모리 복원 (S4-8, KB 5종 storage → agent restore) |
| `new/src/dynamic_universe/snapshot_fetcher.py` | C16 WatchSnapshotFetcher (Sprint 5 S5-1, KIS REST 60초 polling + PIT-Safety + PersistentCache + JSONL append) |
| `new/src/dynamic_universe/admission_engine.py` | C15 AdmissionEngine (Sprint 5 S5-2, candidate_pool 편입 + trigger_loader 매칭 + cooldown + max=10 가드 + admission_event jsonl) |
| `new/src/dynamic_universe/holdings_manager.py` | C15 HoldingsManager (Sprint 5 S5-2, fixed_rule_only sizing + per_stock_max 0.03 + total_max 0.10 + weight_decision_authority 분리) |
| `new/src/dynamic_universe/exit_engine.py` | C15 ExitEngine (Sprint 5 S5-3, 청산 4조건 market_close/ttl_expiry/stop_loss/spike_resolved + KST 일관성) |
| `new/src/dynamic_universe/gate.py` | C15 DynamicUniverseGate (Sprint 5 S5-4, enabled 60s cache + FORBIDDEN_CALLERS + transition jsonl) |
| `new/src/dynamic_universe/manager.py` | C15 DynamicUniverseManager 오케스트레이터 (Sprint 5 S5-4, snapshot→admission→holdings→exit lifecycle 통합) |
| `new/src/utils/trigger_loader.py` | trigger_catalog 공통 로더 (Sprint 5 S5-2, risk_fast _load_trigger_rules 추출 + filter_action 지원) |
| `new/config/dynamic_universe_config.yaml` | C15 운영 파라미터 SSOT (Sprint 5 P3, ttl_sec/stop_loss_pct/cache_ttl/admission/holdings/forbidden_runtime_checks, mode_b_metadata 8필드) |
| `new/config/watch_universe_kospi200.yaml` | C16 KOSPI200 watch universe (Sprint 5 P5, 200종목 + watch_rules + mode_b_editable=false) |
| `new/scripts/generate_watch_universe.py` | KRX KOSPI200 종목 자동 생성 스크립트 (Sprint 5 P5, 658줄, --source krx/static, dry-run + diff) |
| `new/src/eval/reason_code_stats.py` | L3 reason_code 분포 + Top-3 coverage 일별 산출 (W2 P1 SHIP 2026-05-09, ~210줄, threshold 0.80) |
| `new/src/eval/cause_attribution.py` | L3 Cause Attribution Accuracy 산출 (W2 P1 SHIP 2026-05-09, ~250줄, FDA reason_code vs label_t5_ret 사후 일치율, threshold 0.60, 발표 킬러 지표) |
| `new/src/eval/synth_audit_log.py` | 발표용 synthetic audit_log generator (W2 P1, KIS 키 미설정 시 7종 reason_code × 107 entries 합성, hit_rate 분포 보존) |
| `new/src/jobs/run_final_demo.py` + `demo.sh` | 발표 단일 명령 demo runner (W1 P0-6, Hot/Cold/Mode B 3 demo wrapping, --demo all/hot/cold/mode_b) |
| `.env` | API 키 (DART, Naver, ECOS, KRX, Kanana, OpenAI) |

## LLM 구성

- **장중 Hot Path**: LLM 미호출
- **장중 Cold Path**: Kanana-o 100회/일
- **Mode B**: GPT-4o 전용
- **Fallback**: GPT-4o (429/timeout 시), Circuit breaker 3회→5분

## 하네스 시스템 (v3, 2026-05-03 audit 후 = 10 에이전트 + 20 스킬)

**에이전트 10개**: architect, reviewer, coder, runner, modeler, data-engineer, presenter, doc-writer, analyst, gpt-tracker

**스킬 20개 (4 카테고리)**:

1. **핵심 팀 스킬 (6)**: 단일 작업 단위, 2명 이상 팀 디스패치
   - `/code-review` (reviewer+architect) · `/code-fix` (coder+reviewer) · `/run-pipeline` (runner+reviewer)
   - `/validate` (reviewer+architect) · `/build-model` (modeler+data-engineer+runner) · `/team-merge` (architect+gpt-tracker)

2. **전문가 스킬 (8)**: 단일 에이전트 디스패치 또는 도구 호출
   - `/arch-sync` (architect) · `/gpt` (gpt-tracker) · `/smoke-test` (runner) · `/cleanup` (reviewer 단독 탐지, 삭제 user 승인)
   - `/agent-research` (analyst) · `/paper-trending` (analyst) · `/worklog` · `/present` (default: presenter+analyst, review 모드: presenter+reviewer 병렬)

3. **세이프티/세션 (5)**: 운영 안전성 + 세션 컨텍스트 관리
   - `/careful` · `/freeze` · `/guard` (careful+freeze 동시) · `/unfreeze` · `/checkpoint`

4. **오케스트레이터 (1)**: 복합 작업 자연어 호출
   - `/elephant-ops [자연어]`

### Hooks (.claude/settings.local.json)
- **PreToolUse**: `.env` 파일 수정 차단 (Edit/Write 매처)
- **PostToolUse**: `new/docs|specs|config/*` 수정 시 `/arch-sync` 또는 `/validate` 권장 출력
- **Notification**: macOS osascript 알림 (작업 완료)
- **SessionStart**: 스킬/에이전트 목록 + 세션 시작 시퀀스 안내

### Preamble + Rules (.claude/preamble + .claude/rules)
- `_elephant_preamble.md` 10 섹션 + §7.1/7.2/7.3 (5/11 verification + 5/12 interpretation 모드)
- 15 rule 파일:
  - 기존 9: `agents-code` · `architecture-v3` · `composition` · `confidence_calibration` · `config-protection` · `connectors` · `jobs` · `schemas` · `voice`
  - 신규 6 (5/11~5/12 Codex 패턴 흡수): `preamble-load` · `deep-fix` · `test-isolation` · `cross-check` · `performance-interpretation` · `env-config` (canonical env 단일 SSOT)
- 충돌 시 우선순위: Preamble > rule 파일 > 스킬 본문 > 에이전트 정의

### bin (.claude/bin/)
- `check-careful` · `check-freeze`: 세이프티 hook 보조 스크립트
- `elephant-learnings-log` · `elephant-learnings-search`: calibration learning CLI

### 외부 출처 스킬 정책
v3 KOSPI 1분봉 OS 도메인 외 스킬은 보유하지 않는다 (2026-05-03 audit으로 a4-print-design / docx / project-spec-writer 삭제).
docx 산출물은 사용자가 GPT 등 외부 도구로 처리한다 (memory `feedback_docx_quality.md`).

### Codex 협업 패턴 (2026-05-11 verification + 2026-05-12 interpretation 입증)

별도 Codex 하네스 (`.Codex/`) 가 Claude (`.claude/`) 와 mirror 유지. 차이 4개만:
- 경로 (`.Codex/` ↔ `.claude/`)
- 도구명 (`spawn_agent / send_input / wait_agent` ↔ `Agent / SendMessage / TaskCreate`)
- 스킬 prefix (`/codex-*`)
- agent 포맷 (`.toml` ↔ `.md`)

본문 내용 (architect 11 카테고리 + code-review workflow + agent 본문)은 동일.

**Codex 사용 시점**:
- Claude self-review 한계 인정 시 (rules/cross-check.md §1)
- Critical fix 후 외부 cross-check 필요 시 (rules/cross-check.md §2 trigger)
- 깊이 있는 multi-role 검증 필요 시 (4-role 병렬 default, preamble §7.1)

**Hand-off prompt 표준** (rules/cross-check.md §5):

```
/codex-{skill}

[배경] claude-progress.md / Codex-progress.md 해당 섹션 인용 + 현재 작업 / 제약
[발견 사항] file:line:category + 증상 / 증거 / confidence
[Fix 방향] 구체적 코드 sketch (before/after) + 영향받는 path 모두 명시
[제약] .env 안 읽음 + 진행 중 작업 (예: 80일 backfill / artifacts/data) 안 건드림
[검증] targeted pytest + dry-run + compileall + git diff --check
```

**Codex 5/11 입증 결과** (`Codex-progress.md` "AI 파트 최종 연결 검증" 참고):
- AI/Mode B 핵심 묶음: 192 passed, 1 skipped
- 데이터/계약/통합 묶음: 178 passed + 59 passed
- LGBM 단독: 9 passed
- unit 파일별 독립: 76 files PASS (subprocess per file, canonical env)
- 31 파일 변경 +1176/-206 (Cold Path publish bridge + LLMRouter mock 제한 + Mode B safety + Dual-Source train/serve skew 제거 + 등)

**fingerprint 통합** (rules/cross-check.md §6): Claude + Codex 양측 confirm 시 effective confidence +1 (cap 10), 한쪽만 발견 시 caveat "single-source, verify".

**Codex 5/12 입증** (성능 해석 도메인, rules/performance-interpretation.md 신규):
- 3-agent 병렬 (modeler+reviewer+analyst) + 외부 논문 인용 (Bailey & Lopez de Prado 2014 Deflated Sharpe `arxiv:2010.08601` / AFML §7 walk-forward / AAPM/AlphaGAT SOTA IC range)
- in-sample IR=25.35 overfit 의심 → 5-stage verdict (in-sample → 4-perspective → Deflated Sharpe → OOS BacktestAgent → 1주 paper trading)
- verification mode (5/11) 와 분리된 interpretation mode (preamble §7.3, code-review SKILL "4-role 변형: 성능 해석")
- WebSearch / WebFetch primary source 인용 의무 (analyst.md 강화)
- reviewer.md 체크리스트 신규 3 row: PIT leakage label horizon / metric 산식 오류 / train-val cross-sectional leakage

## v3 핵심 구조

- **6 시스템 에이전트**: News, Risk(Fast/Slow), Quant, Debate, FDA, Backtest(Mode B)
- **18 API Contracts**: C1~C18 (`new/specs/api_contracts.md` = SSOT, v3.8 현행: C15/C16 정식화 + ADM/EXT/WS UUID8 등록, v3.5: 6종 ID UUID8 전수 통일 + BT {tool} 제거)
- **Blackboard 통신**: Shared Message Pool + Pub/Sub (MetaGPT 기반)
- **Dual-Source**: 뉴스↔커뮤니티 divergence = uncertainty

## Sprint 로드맵

- **Sprint 0**: 프로젝트 구조 + 커넥터 + 기본 파이프라인
- **Sprint 1**: Hot Path (Quant + LightGBM + PPO + PM + FDA)
- **Sprint 2**: Cold Path (News/Risk/Debate + Blackboard + Event Gateway)
- **Sprint 3**: Mode B (Alpha Factor Engine + Co-STEER + Backtest Agent)
- **Sprint 4**: 통합 + 성능 최적화 + Dual-Source
- **Sprint 5**: 동적 유니버스 확장 (KOSPI200 watch → 이벤트 편입)

## 세션 운영

- 세션 시작 시 `claude-progress.md` + `feature_list.json` 먼저 읽기
- **세션 시작 시 `.claude/preamble/_elephant_preamble.md` 도 Read tool로 로드**. 10개 섹션(불변 5원칙 / Ethos / AskUserQuestion / Completion Status / Escalation / Plan Mode / Self-Improvement / User Sovereignty / Voice / Context Recovery)은 모든 스킬/에이전트 작업의 상위 규칙. 충돌 시 Preamble 우선.
- 코드 전에 상태 확인. 코딩부터 하지 않기.
- unrelated task 전환 시 `/clear`
- 세션 끝에 `claude-progress.md`에 done/next/blockers 남기기

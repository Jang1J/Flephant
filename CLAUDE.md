# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

KOSPI 30종목(active 20 + pending 10) 대상 1분봉 멀티에이전트 Decision OS (종합설계 프로젝트).
장중 매 1분 퀀트 시그널 + 이벤트 시 LLM 에이전트 개입 + 장마감 후 자동 진화.

현재 단계 (2026-06-25 KST): Sprint 0~5 완료. Pre-Live Gate Phase 3 + Phase 4 non-live/paper-safe 경로 진행 중. `ai-1` 최신 head = `f2f9a45` (PR #39 paper-candidate-start-gate + #42 recommendation rate-limit merge 반영). 현재 작업 브랜치 `feature/paper-service-ops-bundle`: paper-service scheduler 실행기(`new/src/ops/paper_service_scheduler.py`) + deploy payload(`new/deploy/AI_DEPLOYMENT_PAYLOAD.md`) 정리 중. 휘발성 baseline/head/열린 PR 상세는 `PROGRESS.md` 상단 snapshot이 정본(이 줄에 hash를 박지 말 것). 현재 주간 목표는 BE 연결 live reliability pilot 준비이며, 우선순위는 operating path 정렬, 1분 cadence 병목(rolling/incremental bars cache), 4-gate/prelive evidence다. production registry `active_version=null` + `live_trading_allowed=false` 유지. `.env`는 사용자 승인 하 one-shot subshell source만 가능하며 원문 출력/저장 금지. 세부 최신 상태는 `PROGRESS.md` 상단 snapshot + 최신 세션 로그를 정본으로 본다.

## 파이프라인 v3 (2모드 × 5레이어)

```
Mode A 장중 (09:00~15:30):
  Hot Path (매 1분, <100ms, LLM 미호출): 1분봉 → LightGBM → PPO → PM → FDA approve/veto
  Cold Path (이벤트 시, 10~30초): 뉴스/공시/수급 → News/Risk/Debate → FDA
Mode B 장마감 (18:00~22:00, operator-run CLI):
  Alpha Factor Engine → Co-STEER → Backtest Agent → 22:00 배포 게이트
Pre-Live Gate (Phase 3, 진행 중):
  prelive_gate → service_readiness_status → deploy_candidate --dry-run → build_sanitized_release → paper_auto_trade
  4 게이트: deploy_quality + broker_evidence + registry_mutated=false + live_trading_allowed=false
```

## 핵심 제약: 불변 원칙 5개 (절대 위반 금지)

1. **PIT-Safety**: 미래 데이터 사용 금지. snapshot 기준 18:00 KST.
2. **FDA can_change_weight = false**: FDA는 approve/veto만. 비중은 PPO Allocator, order_deltas는 Portfolio Manager.
3. **Backtest Agent Mode B 전용**: 장중 경로 절대 미개입. C12 forbidden_permissions 6 + C14 Mode B Scheduler forbidden_permissions 4.
4. **Kanana-o 100회/일 예산**: 장중 LLM 한도. Mode B는 GPT-4o 전용.
5. **하드코딩 금지**: 모든 수치/임계값은 `new/config/risk_config.yaml`에서 로드.

### 추가 제약
- **FDA reason_code 필수** (C9 `reason_code_required: true`, Sprint 2 enum 확정)
- **종목코드**: `str(ticker).zfill(6)` (6자리 zero-padded)
- **GPT Pro 우선**: 피드백 1순위. Claude 판단으로 제외/축소 금지
- **계약서 SSOT**: `new/specs/api_contracts.md` (필드 정의 단일 진실 소스)
- **Hot Path <100ms**: 동기 LLM 호출 금지
- **`.env` 보호**: Read/Write 직접 접근 금지. 실데이터 smoke가 필요하면 사용자 승인 후 서브셸에서만 `set -a; source .env; set +a`로 주입. 키 원문 출력/로그/저장 금지.

## 코드 컨벤션

- JSON: `json.dump(data, f, ensure_ascii=False, indent=2)`
- 경로: `pathlib.Path`, `mkdir(parents=True, exist_ok=True)`
- 에러: bare `except:` 금지. `except Exception as e:` 사용
- 로그: 한국어, `[모듈명]` 접두사
- 외부/LLM/operator 입력: `safe_bool / safe_int / safe_float / safe_confidence` 정규화 (문자열 `"false"`가 gate를 True로 오판 차단, Codex 5/16 03:42)

## 주요 파일

### SSOT 문서

| 파일 | 용도 |
|---|---|
| `new/docs/architecture.md` + `architecture_visual.md` | v3 상세 아키텍처 + ASCII 시각화 (**가장 중요**) |
| `new/specs/api_contracts.md` | C1~C18 (**SSOT**, v3.8: C15/C16 정식화 + ADM/EXT/WS UUID8 + weight_decision_authority + activation_gate + polling_authority) |
| `new/config/risk_config.yaml` + `sector_config.yaml` + `dynamic_universe_config.yaml` + `watch_universe_kospi200.yaml` | 리스크/섹터/동적유니버스/KOSPI200 watch SSOT |
| `new/docs/{connector_design,evaluation_metrics,cross_paper_synergies}.md` | 커넥터 방법론 10개 / 3-레이어 평가 / 6 논문 시너지 5 |
| `new/docs/paper_1_aapm.md` ~ `paper_6_alphaagent.md` | 6편 논문 분석 (AAPM/AlphaGAT/MetaGPT/RD-Agent/TradeXpert/AlphaAgent) |
| `.env` | API 키 (DART/Naver/ECOS/KRX/Kanana/OpenAI). **직접 접근 금지** |

### 코어 구현 (Layer 2~5 + Sprint 5)

| 경로 | 용도 |
|---|---|
| `new/src/connectors/{kis_rest,kis_ws,dart_rest,krx_rest,naver_rest,ecos_rest,us_market,community,base}.py` | 8 커넥터 + BaseConnector (S0~S2-5, S2-11) |
| `new/src/utils/pit_guard.py` | PIT-Safety SSOT (`is_pit_safe` + `PITViolationError`) |
| `new/src/data/{filter_loader,news_filter,text_pack_builder}.py` | News 3-level 필터 + TSFresh 30분 통계 (S2-6) |
| `new/src/data/{dual_source_scorer,dual_source_runner,exogenous_feature_store,dataset_builder}.py` | C3A Dual-Source 5피처 + 일별 exogenous loader + MultiIndex training panel (S1-0/S4-1/Phase 2) |
| `new/src/agents/cold/{news,risk_fast,risk_slow,debate}.py` + `agents/fda.py` + `agents/memory_restorer.py` | Cold 4 + FDA + Memory bootstrap (S2-7~9, S4-8) |
| `new/src/orchestration/llm_router.py` | Kanana-o + GPT-4o fallback + circuit breaker (S2-2) |
| `new/src/models/{metrics,splitter,ranking_loss,registry,lgbm_trainer}.py` | IC/ICIR/RankIC/AR/IR/MDD/SR + WalkForward + LambdaRank NDCG@5 + C17 Registry + end-to-end CLI (S1-0 B+C) |
| `new/src/ops/{audit_logger,profiler,service_readiness_status}.py` + `dqr/dqr_runner.py` + `cache/persistent_cache.py` | C18 18 필드 audit + Hot Path p50/p95/p99 + DQR 8커넥터 + SQLite TTL + broker_evidence 선택 |
| `new/src/mode_b/performance_aggregator.py` | C18 L2 9지표 + 8d 벡터 (S2-10) |
| `new/src/eval/{reason_code_stats,cause_attribution,synth_audit_log}.py` | L3 reason_code 분포 + Cause Attribution + 발표용 generator (W2 P1) |
| `new/src/runner/{e2e_scenario_runner,event_injector}.py` + `jobs/{run_e2e_scenario,run_dual_source_ablation,run_final_demo}.py` + `demo.sh` | E2E 5일 + 합성 이벤트 + ablation + 발표 demo runner (S4-3) |
| `new/src/dynamic_universe/{snapshot_fetcher,admission_engine,holdings_manager,exit_engine,gate,manager}.py` + `utils/trigger_loader.py` | C15/C16 6 컴포넌트 + trigger 로더 (Sprint 5) |
| `new/src/execution/{paper_trading,paper_auto_trading,execution_gateway,kill_switch}.py` | paper trading + C10 Execution Gateway + 실계좌 kill switch |

### Pre-Live Gate 스크립트 (Phase 1~3, 2026-05-13~)

| 그룹 | 스크립트 |
|---|---|
| Phase 2 historical materialize | `phase2_feature_backfill`, `materialize_dual_source_history`, `materialize_exogenous_history`, `build_news_dart_archive`, `build_dart_corp_code_cache`, `export_agent_memory_dual_source_raw`, `prepare_dual_source_neutral_scores`, `prepare_paper_lgbm_registry` |
| readiness + 게이트 | `live_data_readiness`, `prelive_gate`, `post_backfill_prelive`, `deploy_candidate`, `service_policy_replay`, `c12_recheck_runner`, `service_readiness_status`, `build_sanitized_release`, `model_registry_readiness`, `kis_minute_retention_probe`, `print_env_readiness` |
| paper trading (장중만 실호출) | `paper_auto_preflight`, `paper_auto_trade`, `paper_auto_service_rehearsal`, `paper_service_rehearsal`, `paper_trading_smoke`, `kis_paper_account_diagnose` |
| cost-aware 성능 진단 | `cost_aware_label_horizon_scan`, `cost_aware_retraining_plan` (recommended `label_195m_net_ret`, do_not_auto_deploy=true 유지) |
| Phase 4 research (5/18 신규) | `service_policy_threshold_sweep` (read-only 6조합 grid), `stage_lgbm_candidate_bundle` (registry candidate → bundle staging), `collect_kis_paper_evidence` (단일 process token 공유로 KIS EGW00133 회피) |
| 기타 | `generate_watch_universe` (KRX KOSPI200, --source krx/static), `kis_minute_retention_probe` (KIS 1년 retention 경계 확인) |

## LLM 구성

- 장중 Hot Path: LLM 미호출 · 장중 Cold Path: Kanana-o 100회/일 · Mode B: GPT-4o 전용 (operator-run CLI)
- Fallback: GPT-4o (429/timeout 시), Circuit breaker 3회→5분

## 실계좌 전환 직전 운영 게이트 (Phase 3)

4 게이트 PASS + 사용자 명시 승인 후에만 production registry `active_version` 승격.

| 게이트 | 명령 | PASS 조건 |
|---|---|---|
| `deploy_quality` | `deploy_candidate.py --bundle-id <ID> --dry-run` | status=PASS, service_policy_gate_pass=true, registry_mutated=false |
| `broker_evidence` | `service_readiness_status.py --bundle-id <ID>` | external_kis_api=true + bundle match + order-history matched PASS |
| `prelive_gate` | `prelive_gate.py --bundle-id <ID> --end-date YYYYMMDD --business-days 80` | 9 stage PASS, blockers=[] |
| `sanitized_release` | `build_sanitized_release.py` | forbidden_entries=[], secret_sources_in_zip=[] |

**전환 전 유지 (변경 시 사용자 명시 승인 필요)**: production registry `active_version=null` · `live_trading_allowed=false` · 실계좌 action UI 비활성 · 장중 추가 실거래 주문 금지.

## v3 핵심 구조

- **6 시스템 에이전트**: News, Risk(Fast/Slow), Quant, Debate, FDA, Backtest(Mode B)
- **18 API Contracts**: C1~C18 (`api_contracts.md` SSOT, v3.8 + 6종 ID UUID8 전수 통일)
- **Blackboard 통신**: Shared Message Pool + Pub/Sub (MetaGPT 기반)
- **Dual-Source**: 뉴스↔커뮤니티 divergence = uncertainty (Phase 2 80일 historical 확보 후 운영)
- **Pre-Live Gate 4종**: deploy_quality + broker_evidence + prelive_gate + sanitized_release

## 하네스 시스템 (v3, 2026-06-25 audit = 10 에이전트 + 21 스킬)

**에이전트 10**: architect, reviewer, coder, runner, modeler, data-engineer, presenter, doc-writer, analyst, gpt-tracker.

**스킬 21 (4 카테고리)**:
1. **핵심 팀 (6)**: `/code-review` `/code-fix` `/run-pipeline` `/validate` `/build-model` `/team-merge`
2. **전문가 (9)**: `/arch-sync` `/gpt` `/smoke-test` `/cleanup` `/agent-research` `/paper-trending` `/worklog` `/present` `/deploy-sync`
3. **세이프티/세션 (5)**: `/careful` `/freeze` `/guard` `/unfreeze` `/checkpoint`
4. **오케스트레이터 (1)**: `/elephant-ops [자연어]`

**Hooks (`.claude/settings.local.json`)**: PreToolUse `.env` 차단, PostToolUse `new/docs|specs|config/*` 시 `/arch-sync` 권장, Notification osascript, SessionStart 안내.

**Preamble + Rules**: `.claude/preamble/_elephant_preamble.md` 10 섹션 (§7.1 verification + §7.2 4-yes check + §7.3 interpretation) + 15 rule (기존 9 + 5/11~12 신규 6: `preamble-load` `deep-fix` `test-isolation` `cross-check` `performance-interpretation` `env-config`). 우선순위: Preamble > rule > 스킬 > 에이전트.

**bin**: `check-careful` `check-freeze` (hook 보조) · `elephant-learnings-{log,search}` (calibration CLI).

**외부 출처 스킬 정책**: v3 KOSPI 1분봉 OS 도메인 외 스킬 금지 (2026-05-03 audit으로 a4-print/docx/project-spec-writer 삭제). docx는 GPT 외부 도구 (`feedback_docx_quality.md`).

### Codex 협업 패턴

별도 Codex 하네스 (`.Codex/`) 가 Claude (`.claude/`) 와 mirror (차이 4개: 경로 / 도구명 / 스킬 prefix / agent 포맷). 본문 동일.

**사용 시점**: self-review 한계 인정 / Critical fix 후 cross-check / multi-role 검증 / Phase 2~3 long-running 패치 cycle.

**Hand-off**: `/codex-{skill}` + [배경/발견/Fix 방향/제약/검증] 5 블록 (rules/cross-check.md §5 표준).

**입증 누적**: 5/11 AI 파트 검증 31파일 +AI/Mode B 192 + 데이터 237 + LGBM 9 PASS + 76파일 subprocess / 5/12 성능 해석 3-agent + Deflated Sharpe + 5-stage / 5/14 Full pipeline (C12 deployable + C14 dry-run + KIS balance 5억원) / 5/15~16 Phase 2+3 (Dual-Source 80일 PASS, as-of drift 차단, safe_* 정규화, broker_evidence 선택 fix, **Full regression 1493 passed / 1 skipped**) / 5/17 Codex 36시간 (overnight 11 fix fail-closed + 249영업일/30종목 확장 + backfill 7470/7470 + cost-aware horizon scan best 195분/+121bps + **Full regression 1815 passed 기록상**) / 5/18 04:35 **Phase 4 첫 게이트 PASS** (`BUNDLE-20260518-195M0001` cost-aware 195m DS-off, service-policy +28.95bps SR 5.32 materiality 15bps 통과) + 5/18 10:15 KIS paper balance/probe PASS + paper-auto qty cap fix + AuthManager EGW00133 retry / 5/21 13:54 최신 cold-risk 주입 KIS virtual paper-auto PASS (`paper_auto_service_rehearsal_20260521_135439.json`, external_kis_virtual, order-history matched, live=false).

**Fingerprint 통합**: Claude + Codex 양측 confirm → effective confidence +1 (cap 10), 한쪽만 → "single-source, verify" caveat.

## Sprint 로드맵 + Post-Sprint Phase

**Sprint 0~5 (완료)**: S0 인프라 / S1 Hot Path (Quant+LightGBM+PPO+PM+FDA) / S2 Cold Path (News/Risk/Debate+Blackboard) / S3 Mode B (AFE+Co-STEER+Backtest) / S4 통합+Dual-Source / S5 동적 유니버스 (KOSPI200 watch).

**Post-Sprint Phase (Pre-Live Gate, 2026-05-13~)**:

| Phase | 범위 | 상태 |
|---|---|---|
| 1 | Top-K long-only replay + regression_evidence 4 + trade_signal_threshold deprecated | 완료 (5/13) |
| 2 | Dual-Source/exogenous 80영업일 historical + exogenous_feature_store + LightGBM 재학습 | 완료 (5/14~15, coverage 1.0) |
| 3 | 실계좌 전환 직전 안전 가드 + paper evidence + 검증 흐름 + fail-closed gate | paper-safe path PASS 다수. 최신 KIS virtual evidence `paper_auto_service_rehearsal_20260521_135439.json` PASS. 실계좌/live는 금지 |
| 4 | cost-aware `label_195m_net_ret` research + service-policy + expanding post-close 자동진화 | `BUNDLE-20260518-195M0001` C12/service-policy/deploy/readiness PASS. 20260520 data/features/sector readiness는 PASS지만 최신 post-close full candidate는 Mode B에서 재학습/C12/prelive 필요 |
| 5 | 실계좌 전환 (사용자 승인 + 4 게이트 + 1주 paper 보고서) | 미진입. production registry `active_version=null`, live trading false 유지 |

## 세션 운영

- 세션 시작: `PROGRESS.md` (Claude+Codex 단일 공유 handoff, 상단 현재 snapshot + 최신 세션 로그) + `feature_list.json` + `.claude/preamble/_elephant_preamble.md` 본문 Read
- 코드 전 상태 확인. 코딩부터 하지 않기. unrelated 전환 시 `/clear`
- 세션 끝에 `PROGRESS.md`에 done/next/blockers (헤더에 `[Claude]` 태그)
- 실데이터 smoke: `.env` 사용자 승인 후 서브셸에서만 source. 키 원문 출력/저장 금지
- 4-yes check (`rules/cross-check.md §4`): "Critical 0건" 표기는 격리 / subprocess / canonical env Full PASS / 외부 cross-check 4개 모두 yes일 때만 허용
- Canonical env: `/opt/anaconda3/envs/elephant/bin/python` (numpy 1.26.4 / lightgbm 4.6.0 / SB3). 최신 세션에서는 focused paper/service tests `35 passed`, 5/21 readiness/deploy no-write PASS. 더 큰 regression 기준은 `PROGRESS.md` 최신 로그를 우선한다.
- 현재 협업 기준 branch: `ai-1` (최신 head `f2f9a45`), 작업 브랜치 `feature/paper-service-ops-bundle`. 휘발성 head/PR는 `PROGRESS.md` snapshot이 정본.
- PR #6 merge 반영 완료: `7372bf3 merge: PR #6 service-policy evidence gate 검증 추가`. deployer/scheduler service-policy evidence gate는 expected date range/universe binding 기준으로 hardening.
- 공유 progress 파일(`PROGRESS.md`, `feature_list.json`)은 local handoff SSOT다 (`claude-progress.md`/`Codex-progress.md`는 2026-05-29 PROGRESS.md로 통합, 이제 redirect stub). 커밋/공유 여부는 사용자 지시에 따른다.
- BE 연결은 read-only + paper-safe 범위만 허용. live trading enable, production registry mutation, 실계좌 주문 UI는 계속 금지.

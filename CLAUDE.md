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
3. **Backtest Agent Mode B 전용**: 장중 경로 절대 미개입. forbidden_permissions 6개.
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
| `new/specs/api_contracts.md` | C1~C18 API 계약서 (**SSOT**, v3.5 현행: PP/BUNDLE/BT/RPT/FCC/RGC UUID8 정정, v3.4: MSG/APM UUID8, v3.3: C9 uncertainty_score extension, v3.2: C18 AgentPerformance 신설) |
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
| `.env` | API 키 (DART, Naver, ECOS, KRX, Kanana, OpenAI) |

## LLM 구성

- **장중 Hot Path**: LLM 미호출
- **장중 Cold Path**: Kanana-o 100회/일
- **Mode B**: GPT-4o 전용
- **Fallback**: GPT-4o (429/timeout 시), Circuit breaker 3회→5분

## 하네스 시스템 (v3, 2026-04-10)

**에이전트 10개**: architect, reviewer, coder, runner, modeler, data-engineer, presenter, doc-writer, analyst, gpt-tracker

**핵심 팀 스킬**:
- `/code-review` (reviewer+architect) · `/code-fix` (coder+reviewer) · `/run-pipeline` (runner+reviewer)
- `/validate` (reviewer+architect) · `/build-model` (modeler+data-engineer+runner) · `/team-merge` (architect+gpt-tracker)

**전문가 스킬**:
- `/arch-sync` · `/gpt` · `/smoke-test` · `/cleanup` · `/agent-research` · `/paper-trending` · `/worklog` · `/present`

**오케스트레이터**: `/elephant-ops [자연어]`

### Hooks
- **PreToolUse**: `.env` 수정 차단
- **PostToolUse**: 핵심 파일 수정 시 안내
- **SessionStart**: 스킬/에이전트 목록 안내

### 경로별 규칙 (.claude/rules/)
- `new/docs/*`, `new/specs/*`, `new/config/*` → v3 4축 동기화 (불변 원칙 5개)

## v3 핵심 구조

- **6 시스템 에이전트**: News, Risk(Fast/Slow), Quant, Debate, FDA, Backtest(Mode B)
- **18 API Contracts**: C1~C18 (`new/specs/api_contracts.md` = SSOT, v3.5 현행: 6종 ID UUID8 전수 통일 + BT {tool} 제거)
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

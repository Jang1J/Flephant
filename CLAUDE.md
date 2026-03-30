# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

KOSPI 대형주 26종목 대상 멀티에이전트 트레이딩 알고리즘 (종합설계 프로젝트).
t일 장마감 데이터로 t+1 거래일 종목별 BUY/HOLD/SELL 판단을 내리는 시스템.

## 파이프라인 (7단계)

```
DailyMarketPacket(DMP) → TickerTextPack(TTP) → StrategyCard(SC)
→ RiskEngine → RiskCard(RC) + CandidateOrderPlan(COP)
→ FinalDecisionAgent(FDA) → FinalDecisionCard(FDC)
→ PortfolioManager → PortfolioState(PFS)
```

## 실행 명령어

Python 환경: `/opt/anaconda3/envs/elephant/bin/python`

```bash
# E2E 파이프라인 1회 실행
python jobs/run_e2e_pipeline.py YYYYMMDD

# 개별 단계
python jobs/build_daily_market_packet.py YYYYMMDD
python jobs/build_ticker_text_pack.py YYYYMMDD [종목코드]
python jobs/run_risk_engine.py YYYYMMDD [--mock]

# 연속 N거래일 replay
python jobs/run_replay.py --days 5

# 과거 데이터 backfill
python jobs/backfill_packets.py --days 20

# 장중 사이클
python jobs/run_intraday_cycle.py YYYYMMDD HHMM [--all]

# UQ 모델
python jobs/uq_calibration.py --train
python jobs/uq_calibration.py --predict YYYYMMDD

# 커넥터 smoke test
python connectors/krx.py
python connectors/dart.py
python connectors/naver_news.py
python connectors/ecos.py
python connectors/llm_router.py
```

## 핵심 제약 (절대 위반 금지)

- **PIT-Safety**: 미래 데이터 사용 금지. snapshot 기준 18:00 KST. `is_within_snapshot()` 필터 필수.
- **can_change_weight = false**: FDA는 비중 수정 불가. approve/veto만 가능.
- **스키마 준수**: 아티팩트 출력은 반드시 `schemas/*.json`과 일치해야 함.
- **정책 동기화**: `config/risk_policy_v0.yaml` 값을 코드에 하드코딩하지 않는다.
- **종목코드**: 항상 6자리 zero-padded (`str(ticker).zfill(6)`).

## 리스크 정책 요약 (risk_policy_v0.yaml)

- Regime Gate: VIX proxy >= 90 → red, >= 70 → yellow. Market breadth < 0.30 → red, < 0.45 → yellow.
- Position: max 10종목, 단일 <= 20%, 섹터 <= 40%, min confidence 0.3
- Stop-loss: -5%, Turnover cap: 30%/일, 최소 현금: 10%

## 코드 컨벤션

- JSON 저장: `json.dump(data, f, ensure_ascii=False, indent=2)`
- 경로: `pathlib.Path` 사용, `mkdir(parents=True, exist_ok=True)`
- jobs/ 스크립트 import: `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))`
- 에러 처리: bare `except:` 금지. `except Exception as e:` 사용.
- 로그: 한국어, `[모듈명]` 접두사

## 주요 설정 파일

| 파일 | 용도 |
|------|------|
| `config/universe_v1.csv` | 유니버스 26종목 (ticker, name, wics_sector) |
| `config/risk_policy_v0.yaml` | 리스크 정책 전체 |
| `.env` | API 키 (DART, Naver, ECOS, KRX, Kanana, OpenAI) |
| `prompts/final_decision_contract_v0.md` | FDA 프롬프트 계약서 |

## LLM 구성

- Primary: Kanana-o (한국어 추론)
- Fallback: GPT-4o (429/timeout 시 자동 전환)
- Circuit breaker: 연속 3회 실패 → 5분 cooldown

## 하네스 시스템

### 에이전트 (8개)

| 에이전트 | 역할 | 권한 |
|---------|------|------|
| reviewer | 코드 리뷰 (PIT-safety, 정책, 스키마) | Read-only |
| fixer | 코드 수정 + 버그 픽스 | Read/Write |
| runner | 파이프라인 실행 + 테스트 | Read-only |
| qa-inspector | 통합 정합성 검증 | Read-only |
| modeler | KR-Rebound-CNN ML 모델링 | Read/Write |
| doc-writer | 문서 작성/갱신/정합성 | Read/Write |
| analyst | ML 성능 분석/해석 | Read-only |
| gpt-feedback-tracker | GPT Pro 피드백 추적 | Read-only |
| cleaner | dead code/문서/아티팩트 정리 탐지 | Read-only |

### 단일 작업 — 전문 스킬 직접 호출
| 명령 | 팀 구성 | 기능 |
|------|---------|------|
| `/code-review [파일\|all]` | reviewer + qa-inspector | 코드 리뷰 |
| `/code-fix [파일\|이슈]` | fixer + qa-inspector | 코드 수정 + 검증 |
| `/run-pipeline [YYYYMMDD\|replay N]` | runner + qa-inspector | 파이프라인 실행 + 아티팩트 검증 |
| `/validate [schema\|policy\|all]` | qa-inspector + reviewer | 정합성 검증 |
| `/smoke-test [커넥터명\|all]` | runner 단독 | 커넥터 smoke test |
| `/build-model [dataset\|train\|evaluate\|emit\|publish]` | modeler + fixer + qa-inspector | KR-Rebound-CNN 모델 구축 |
| `/agent-research [주제]` | analyst | 논문/기술 조사 |
| `/worklog [내용\|status]` | doc-writer | 작업 로그 기록 |
| `/paper-trending [분야]` | analyst | 논문 트렌드 조사 |
| `/gpt [답변 붙여넣기\|status]` | gpt-feedback-tracker | GPT Pro 피드백 처리 |
| `/cleanup [all\|code\|docs\|artifacts]` | cleaner | dead code/문서/아티팩트 정리 탐지 |

### 복합 작업 — 오케스트레이터
```
/elephant-ops [자연어로 원하는 복합 작업]
```
예시:
- `/elephant-ops 리뷰하고 문제 있으면 수정해줘`
- `/elephant-ops 돌려보고 문제 있으면 고쳐`
- `/elephant-ops 전체 점검하고 수정해`

### 에이전트 팀 모드 (AGENT_TEAMS=1)
모든 스킬이 TeamCreate로 에이전트 팀을 구성하여 실행. 팀원들이 SendMessage로 직접 통신하며 협업한다.

### 에이전트 모드 (세션 전체)
```bash
claude --agent reviewer             # 리뷰 전용
claude --agent fixer                # 코드 수정 전용
claude --agent runner               # 파이프라인 실행 전용
claude --agent qa-inspector         # 정합성 검증 전용
claude --agent modeler              # ML 모델링 전용
claude --agent doc-writer           # 문서 작성 전용
claude --agent analyst              # 성능 분석 전용
claude --agent gpt-feedback-tracker # GPT Pro 피드백 추적
```

### 자동화 Hooks
- **PreToolUse**: `.env` 파일 수정 시 자동 차단
- **PostToolUse**: 핵심 파일 수정 시 리뷰/스키마 검증 안내
- **SessionStart**: 사용 가능한 스킬/에이전트 목록 안내

### 경로별 규칙 (.claude/rules/)
- `connectors/*.py` → API timeout, 에러 처리, .env 키 관리
- `jobs/*.py` → PIT-safety, 아티팩트 ID 형식, 정책 동기화
- `schemas/*.json` → required/enum/type 코드 동기화
- `agents/*.py` → can_change_weight=false, LLM Router 사용
- `config/*` → .env 수정 금지, universe 수정 시 승인 필수

## Phase 구분

- **Phase 1 (완료)**: Mock StrategyCard, UQ synthetic data, deterministic FDA rules
- **Phase 1.5 (현재)**: 실제 StrategyCard(AI #2) 생성 완료, LightGBM AUC 0.750 + CNN AUC 0.765, 거래세/슬리피지 반영 백테스트, Multi-Agent Debate 구현 예정
- **Phase 2**: live HourlyPatch, UQ 실데이터(OOF residual), K-OPEN Pulse(교수님 미팅 후), Preference Resolver

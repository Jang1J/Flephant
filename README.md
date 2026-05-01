# Elephant Lab — KOSPI 1분봉 멀티에이전트 Decision OS

KOSPI 30종목(active 20 + pending 10) 대상 1분봉 멀티에이전트 Decision OS (종합설계 프로젝트).

## 시스템 개요

```
Mode A 장중 (09:00~15:30):
  Hot Path: 1분봉 → LightGBM → PPO → PM → FDA approve/veto (<100ms, LLM 미호출)
  Cold Path: 뉴스/공시/수급 → News/Risk/Debate Agent → FDA (이벤트 시)

Mode B 장마감 (18:00~22:00):
  Alpha Factor Engine → Co-STEER → Backtest Agent → 22:00 배포 게이트
```

- **6 시스템 에이전트**: News, Risk(Fast/Slow), Quant, Debate, FDA, Backtest(Mode B)
- **18 API Contracts**: C1~C18 (`new/specs/api_contracts.md` = SSOT, v3.5 현행)
- **Blackboard 통신**: Shared Message Pool + Pub/Sub (MetaGPT 기반)

## 핵심 차별점

| 항목 | 설명 |
|------|------|
| 속도 | Hot Path <100ms, LLM 미호출 |
| 설명력 | FDA가 승인/거부 이유를 CoT로 남김 |
| 적응력 | Mode B가 매일 밤 모델/팩터/필터 재점검 |
| 국장 특화 | KIS 1분봉, DART, 네이버 뉴스, 커뮤니티, KRX 수급 |

## 현재 범위

연구형/모의운용형 MVP. mock / replay / paper-trading 기준. 실거래는 Phase 2 이후.

## 프로젝트 구조

```
Elephant_Lab/
├── new/
│   ├── docs/               # architecture.md, architecture_visual.md, paper_*.md (6편), 평가/시너지/커넥터 문서
│   ├── specs/              # api_contracts.md (SSOT, C1~C18), failure_case_card.schema.yaml
│   ├── config/             # risk_config.yaml, universe_config.yaml, dual_source.yaml, sector_config.yaml 등
│   ├── src/
│   │   ├── agents/         #   FDA + Hot/Cold (News, Risk Fast/Slow, Debate)
│   │   ├── connectors/     #   KIS, KRX, DART, Naver, Community, ECOS, US Market
│   │   ├── orchestration/  #   LLM Router, Hot Runner
│   │   ├── models/         #   LightGBM, PPO Allocator, Committee
│   │   ├── mode_b/         #   Alpha Factor Engine, Co-STEER, Scheduler
│   │   ├── data/           #   Backfill, dataset builder, news/text 필터
│   │   ├── blackboard/     #   Shared Message Pool + Pub/Sub
│   │   ├── execution/      #   Execution Gateway
│   │   ├── portfolio/      #   Portfolio Manager
│   │   └── ops/            #   AuditLogger (C18)
│   ├── tests/              # pytest (920 passed, 1 skipped — Sprint 3 ship-ready)
│   ├── jobs/               # E2E / replay / backfill / Mode B 진입 스크립트
│   └── artifacts/          # gitignored 런타임 산출물 (모델/팩터/오디트)
├── CLAUDE.md               # v3 기준 프로젝트 가이드
├── init.sh / smoke.sh / eval.sh   # 환경 부트스트랩 + 커넥터 smoke + 평가
├── requirements.txt
└── .env.example            # 키 이름만 (실제 .env는 gitignored)
```

## 핵심 제약 (불변 원칙 5개)

1. **PIT-Safety**: 미래 데이터 사용 금지 (snapshot 18:00 KST)
2. **FDA can_change_weight = false**: approve/veto만
3. **Backtest Agent Mode B 전용**: 장중 경로 절대 미개입
4. **Kanana-o 100회/일**: 장중 LLM 한도. Mode B는 GPT-4o
5. **하드코딩 금지**: 모든 임계값은 yaml에서 로드

## 라이선스

종합설계 프로젝트 — 비공개

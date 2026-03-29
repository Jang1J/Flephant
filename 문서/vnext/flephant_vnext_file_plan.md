# Flephant vNext 파일별 수정 계획

> 기준일: 2026-03-29
> 작성: AI #1 (장재원)
> 범례: 신규(N) / 수정(M) / 점검만(R) | 우선순위: P0 / P1 / P2

---

## 1. 신규 파일

### 1.1 connectors (4개)

| 파일 | Owner | 우선순위 | 역할 |
|------|-------|---------|------|
| `connectors/overnight_market.py` | AI #1 | P0 | 미국 지수(나스닥/S&P500/다우) + USDKRW 환율 + KOSPI200 선물 조회. Yahoo Finance 또는 yfinance 기반. timeout/retry 포함 |
| `connectors/krx_investor_flow.py` | AI #1 | P0 | KRX Open API에서 외국인/기관/개인 일별 순매수 데이터 조회. 6자리 종목코드 zfill 적용 |
| `connectors/stockboard_crawler.py` | AI #1 | P1 | 네이버 증권 종목토론방 게시글 수 / 조회수 크롤링. RetailThemeGraph 원천 데이터. robots.txt 준수 |
| `connectors/theme_news_parser.py` | AI #1 | P1 | 뉴스 헤드라인에서 테마 키워드 클러스터 추출. Naver 뉴스 API 기반. 테마 전염 그래프 구조 생성 보조 |

### 1.2 jobs (2개)

| 파일 | Owner | 우선순위 | 역할 |
|------|-------|---------|------|
| `jobs/run_preference_resolver.py` | AI #1 | P1 | UserPreferenceProfile artifact 생성. 설문 5축 → style_score → selected_profile 계산 + JSON 저장 |
| `jobs/serve_strategy_for_user.py` | AI #1 | P1 | UserPreferenceProfile 읽어서 해당 profile의 StrategyCard를 서비스 레이어에 serve. momentum / rebound 분기 |

### 1.3 config (1개)

| 파일 | Owner | 우선순위 | 역할 |
|------|-------|---------|------|
| `config/preference_resolver.yaml` | AI #1 | P1 | 설문 5축 가중치 + routing threshold 정의. strategy_profiles.yaml과 분리 유지. 값 하드코딩 금지 |

### 1.4 schemas (3개)

| 파일 | Owner | 우선순위 | 역할 |
|------|-------|---------|------|
| `schemas/kopen_patch_v1.json` | AI #1 | P0 | InvestorFlowPatch + OvernightSpilloverPatch JSON schema (daily P0 patch 2종 통합 또는 개별) |
| `schemas/kopen_patch_hourly_v1.json` | AI #1 | P1 | RetailThemeGraph + OpenTrapRiskPatch JSON schema (hourly P1 patch 2종) |
| `schemas/user_preference_profile_v1.json` | AI #1 | P1 | UserPreferenceProfile artifact schema. survey_responses, style_score, selected_profile 필드 포함 |

---

## 2. 수정 파일

### 2.1 Data / K-SHIFT 관련

| 파일 | Owner | 우선순위 | 수정 범위 |
|------|-------|---------|----------|
| `jobs/build_daily_market_packet.py` | AI #1 | P0 | `kopen_summary` 필드 추가. IFP + OSP 결과를 DMP에 통합하는 로직 삽입. PIT-safety 유지 |
| `jobs/build_hourly_patch.py` | AI #1 | P1 | `kopen_delta` 필드 추가. RTG + OTP 장중 업데이트를 HMP에 반영. cadence 유지 |

K-SHIFT 4개 patch의 독립 jobs 파일 (신규):

| 파일 | Owner | 우선순위 | 수정 범위 |
|------|-------|---------|----------|
| `jobs/build_investor_flow_patch.py` | AI #1 | P0 | 신규. IFP artifact 생성. krx_investor_flow.py 소비. divergence_score 계산 포함 |
| `jobs/build_overnight_spillover_patch.py` | AI #1 | P0 | 신규. OSP artifact 생성. overnight_market.py 소비. spillover_grade 계산 포함 |
| `jobs/build_retail_theme_graph.py` | AI #1 | P1 | 신규. RTG artifact 생성. stockboard_crawler.py + theme_news_parser.py 소비 |
| `jobs/build_open_trap_risk_patch.py` | AI #1 | P1 | 신규. OTP artifact 생성. 갭상승/VI 발동/hype_trap_score 계산 포함 |

### 2.2 Risk Engine 관련

| 파일 | Owner | 우선순위 | 수정 범위 |
|------|-------|---------|----------|
| `jobs/run_risk_engine.py` | AI #1 | P0 | OvernightSpilloverPatch의 spillover_grade를 Tier 1 regime gate 입력에 추가. InvestorFlowPatch의 divergence_score를 보조 시그널로 연결. OpenTrapRiskPatch 구독 + 갭상승/VI 발동 종목 자동 veto 로직 추가 |

### 2.3 Final Decision Agent 관련

| 파일 | Owner | 우선순위 | 수정 범위 |
|------|-------|---------|----------|
| `agents/final_decision_agent.py` | AI #1 | P1 | OvernightSpilloverPatch 기반 개장 리스크 내러티브 생성 추가. OpenTrapRiskPatch의 추격 경보를 veto 근거에 포함. can_change_weight = false 유지 |

### 2.4 전략 모델 관련 (AI #2)

| 파일 | Owner | 우선순위 | 수정 범위 |
|------|-------|---------|----------|
| `models/lgbm_ranker.py` | AI #2 | P1 | KOPEN feature bank 추가: `foreign_net_buy_rank`, `overnight_spillover_grade_enc`, `divergence_score`, `hype_trap_score`. walk-forward 내 leakage 주의 |
| `jobs/build_strategy_card_momentum.py` | AI #2 | P1 | KOPEN feature 소비. IFP/OSP 기반 신호 보정 로직 반영. artifact_prefix = SC-momentum |
| `agents/synthesizer.py` | AI #2 | P1 | KOPEN feature 기반 conflict resolution 로직 보강. 투자자 수급 방향과 전략 신호 충돌 시 해소 규칙 추가 |
| `agents/news_strategy.py` | AI #2 | P1 | RetailThemeGraph의 spike_tickers를 뉴스 분석 대상에 우선 포함하는 로직 추가 |
| `jobs/build_strategy_card_rebound.py` | AI #2 | P2 | KOPEN feature 소비. 반등 신호와 OpenTrapRiskPatch hype_trap_score 간 충돌 처리 추가 |

### 2.5 설정 파일

| 파일 | Owner | 우선순위 | 수정 범위 |
|------|-------|---------|----------|
| `config/risk_policy_v0.yaml` | AI #1 | P0 | spillover_grade → regime gate 매핑 규칙 추가 (예: SEVERE → 자동 red). 기존 파라미터 값 변경 없음 |

---

## 3. 점검만 (코드 수정 없음, 정합성 확인)

| 파일 | Owner | 우선순위 | 점검 내용 |
|------|-------|---------|---------|
| `config/strategy_profiles.yaml` | AI #1 | R | generator catalog 역할만 유지 중인지 확인. 성향 매핑 로직이 혼입되지 않았는지 점검 |
| `schemas/daily_market_packet_v1.json` | AI #1 | R | kopen_summary 필드 추가 후 schema 동기화 필요 여부 확인 |
| `schemas/strategy_card_v1.json` | AI #2 | R | KOPEN feature 포함 후 StrategyCard schema 호환성 확인 |
| `prompts/final_decision_contract_v0.md` | AI #1 | R | OpenTrapRiskPatch 경보가 veto 근거로 포함될 때 계약서 내용과 충돌 없는지 확인 |

---

## 4. 신규/수정 파일 요약표

| 구분 | AI #1 | AI #2 | BE | 합계 |
|------|-------|-------|-----|------|
| 신규 connectors | 4 | 0 | 0 | 4 |
| 신규 jobs (K-SHIFT 포함) | 6 | 0 | 0 | 6 |
| 신규 config | 1 | 0 | 0 | 1 |
| 신규 schemas | 3 | 0 | 0 | 3 |
| 수정 jobs (기존) | 2 | 2 | 0 | 4 |
| 수정 agents | 1 | 2 | 0 | 3 |
| 수정 models | 0 | 1 | 0 | 1 |
| 수정 config | 1 | 0 | 0 | 1 |
| 점검만 | 2 | 2 | 0 | 4 |
| **합계** | **20** | **7** | **0** | **27** |

---

## 변경 이력

| 버전 | 날짜 | 내용 |
|------|------|------|
| v1.0 | 2026-03-29 | 최초 작성 (vnext 설계 초안) |

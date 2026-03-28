# GPT Pro 프롬프트 #1 — AI #1 W1~W11 코드 로직 리뷰 요청

아래 프롬프트를 GPT Pro에 복사해서 넣으세요.

---

## 프롬프트

나는 종합설계(캡스톤) 프로젝트에서 **AI #1 (Data Agent + Risk Agent + Final Decision Agent)** 을 담당하고 있어. KOSPI 대형주 26종목 대상 멀티에이전트 트레이딩 알고리즘을 만들고 있고, 팀은 AI 2명 + BE 2명으로 구성되어 있어.

W1~W11까지의 AI #1 태스크를 **전부 구현 완료**했어. 지금부터 내가 구현한 전체 코드의 로직을 네가 검토해줬으면 해. 특히 아래 관점에서 봐줘:

### 검토 관점

1. **파이프라인 설계가 논리적으로 타당한지** — 데이터 흐름이 맞는지, agent 간 handoff가 깨끗한지
2. **리스크 관리 로직이 견고한지** — Regime Gate, Position Constraints, UQ Tail Cap, Stop-loss, Turnover Cap
3. **PIT-Safety(미래 데이터 사용 금지)가 잘 지켜지는지**
4. **확장성** — AI #2가 real StrategyCard를 넣으면 바로 연결 가능한 구조인지
5. **발표/논문 관점에서 어필 포인트** — 어떤 부분이 기여도가 높은지

### 시스템 구조 (4+1 Agent)

```
Data Agent (AI #1) → Strategy Agent (AI #2) → Risk Agent (AI #1) → Backtest Agent (AI #2) → Final Decision Agent (AI #1)
```

### 파이프라인 (7단계)

```
DailyMarketPacket(DMP) → TickerTextPack(TTP) → StrategyCard(SC)
→ RiskEngine → RiskCard(RC) + CandidateOrderPlan(COP)
→ FinalDecisionAgent(FDA) → FinalDecisionCard(FDC)
→ PortfolioManager → PortfolioState(PFS)
```

### 내가 구현한 코드 (AI #1 전체)

**connectors/ (데이터 수집 5종)**
- `krx.py` (214줄) — KRX OHLCV + 시가총액 + 기술적 지표 (pykrx + Open API fallback)
- `dart.py` (142줄) — DART 공시 수집
- `naver_news.py` (110줄) — Naver 뉴스 API
- `ecos.py` (123줄) — 한국은행 매크로 지표 (금리, 환율)
- `llm_router.py` (227줄) — Kanana-o primary + GPT-4o fallback + circuit breaker

**agents/ (의사결정)**
- `final_decision_agent.py` (437줄) — FDA: approve/veto 판정 + LLM explanation + conflict detection + Rule 1~6

**jobs/ (파이프라인 + 운영 19개)**
- `build_daily_market_packet.py` (514줄) — DMP 생성 + Kanana-o 시장 코멘터리
- `build_ticker_text_pack.py` (557줄) — TTP 생성 + Kanana-o 뉴스 요약/감성/공시 해석
- `run_risk_engine.py` (835줄) — Risk Agent v3: 2-tier Regime Gate + Position Constraints + UQ Tail Cap + Stop-loss + Turnover Cap + dry_run(ablation)
- `run_e2e_pipeline.py` (210줄) — 7단계 E2E 파이프라인
- `portfolio_manager.py` (250줄) — PortfolioState 관리 (진입가, 보유일수, 미실현 PnL, stop-loss)
- `run_intraday_cycle.py` (210줄) — 장중 hourly delta 파이프라인
- `run_replay.py` (165줄) — N거래일 stateful replay
- `backfill_packets.py` (260줄) — 과거 데이터 backfill
- `build_hourly_patch.py` (138줄) — HourlyMarketPatch 생성
- `build_data_quality_report.py` (210줄) — DataQualityReport
- `uq_calibration.py` (355줄) — UQ 모델 학습/추론/threshold 재검증
- `strategy_loader.py` (95줄) — real SC 자동 감지 + mock fallback
- `validate_ai2_handoff.py` (207줄) — AI #2 StrategyCard 통합 테스트 harness
- `paper_trading_executor.py` (454줄) — 모의투자 주문 실행 + KIS stub + idempotency + reconciliation
- `daily_auto_trading.py` (200줄) — 일일 자동 운영 (E2E + Paper + Metrics + Report)
- `ops_metrics_collector.py` (800줄) — 운영 메트릭 7종 수집 + 일간/주간 리포트
- `run_ablation.py` (367줄) — W10 ablation 프레임워크 (UQ on/off, PFS stateful/stateless)
- `build_be_handoff_payload.py` (280줄) — BE 대시보드 payload 4종 생성
- `run_demo_scenario.py` (397줄) — 데모 시나리오 A(Green)/B(Yellow)/B-red(Red)

**schemas/ (16개 JSON 스키마)**
DMP, TTP, SC, RC, COP, FDC, PFS, HMP, UQ, DQR, PTL, BRR, OPM, backtest_summary_contract, dashboard_payload_contract, ablation_result

**총 코드량**: Python 약 7,200줄 + JSON 스키마 16개 + 설정/문서

### LLM 활용 (Kanana-o, 한국어 특화 LLM)

파이프라인 6곳에서 Kanana-o를 활용:
1. TTP 뉴스 요약 + 감성 분석 (news_sentiment -1~+1)
2. TTP 공시 해석 (주가 영향 판단)
3. DMP 시장 코멘터리 (일일 시황 분석)
4. RC 리스크 내러티브 (regime 판단 근거)
5. FDC 투자 판단 설명 (한국어 explanation)
6. FDC 갈등 해소 분석 (신호 간 충돌 분석)

### 리스크 정책 (YAML 기반, 하드코딩 없음)

- Regime Gate: VIX proxy >= 90 → red, >= 70 → yellow
- Position: max 10종목, 단일 <= 20%, 섹터 <= 40%, min confidence 0.3
- UQ Tail Cap: P85 threshold 0.7, reduction 0.5 (Phase 1 disabled)
- Stop-loss: -5%, Turnover cap: 30%/일, 최소 현금: 10%

### 핵심 제약

1. **PIT-Safety**: snapshot 기준 18:00 KST, `is_within_snapshot()` 필터
2. **can_change_weight = false**: FDA는 비중 수정 불가, approve/veto만
3. **스키마 준수**: 모든 아티팩트 출력은 JSON 스키마와 일치
4. **정책 동기화**: risk_policy_v0.yaml에서 동적 로드, 하드코딩 금지

### 검증 결과

- E2E 파이프라인: PASS (7단계 전부 + 스키마 검증)
- 커넥터 smoke test: 5/5 PASS
- 데모 A(Green): 승인 3종목, 노출 40%
- 데모 B(Yellow): 비중 축소 20%→10%, 노출 30%
- 코드 리뷰: CRITICAL 0건 잔존, WARNING 0건 잔존

---

위 내용을 바탕으로:
1. 파이프라인 설계의 강점/약점을 분석해줘
2. 리스크 관리 로직에 빈틈이 있는지 봐줘
3. 발표에서 어필할 수 있는 차별화 포인트를 뽑아줘
4. AI #2가 real SC를 넣었을 때 예상되는 이슈가 있는지 봐줘
5. 개선 제안이 있으면 알려줘

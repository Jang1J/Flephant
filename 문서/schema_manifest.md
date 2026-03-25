# Schema Manifest — Elephant Lab

> 전체 아티팩트 스키마 목록 + 설명
> 작성일: 2026-03-25

---

## 핵심 파이프라인 아티팩트 (7종)

| 스키마 파일 | 아티팩트 ID 패턴 | Producer | Consumer | 설명 |
|------------|----------------|----------|----------|------|
| `daily_market_packet.json` | `DMP-{YYYYMMDD}-{HHMMSS}` | Data Agent (AI #1) | Strategy Agent (AI #2) | 유니버스 전체 종목 daily backbone: OHLCV, 기술지표, 매크로 스냅샷, 뉴스/공시 인덱스 |
| `ticker_text_pack.json` | `TTP-{YYYYMMDD}-{TICKER}` | Data Agent (AI #1) | Strategy Agent / News Strategy (AI #2) | 종목별 3-level 텍스트 패키지: macro → sector → target_company |
| `strategy_card.json` | `SC-{YYYYMMDD}-{TICKER}` | Strategy Agent (AI #2) | Risk Agent (AI #1) | 종목별 투자 신호 + confidence. Quant(LightGBM) + News(Kanana-o) 결합 |
| `risk_card.json` | `RC-{YYYYMMDD}-{HHMMSS}` | Risk Agent (AI #1) | Final Decision Agent (AI #1) | Tier 1 regime gate + Tier 2 position-level 제약 적용 결과 |
| `candidate_order_plan.json` | `COP-{YYYYMMDD}-{HHMMSS}` | Risk Agent (AI #1) | Final Decision Agent (AI #1) | 리스크 필터링 완료된 후보 주문 계획 (FDA는 approve/veto만 가능) |
| `final_decision_card.json` | `FDC-{YYYYMMDD}-{HHMMSS}` | Final Decision Agent (AI #1) | PortfolioManager / Service Layer | 최종 투자 결정. can_change_weight=false 원칙 적용 |
| `portfolio_state.json` | `PFS-{YYYYMMDD}-{HHMMSS}` | PortfolioManager (AI #1) | Risk Agent / FDA (내부) | 포트폴리오 보유 상태: 진입가, stop-loss, 보유일수, turnover 추적 |

---

## 운영 아티팩트 (3종)

| 스키마 파일 | 아티팩트 ID 패턴 | 설명 |
|------------|----------------|------|
| `portfolio_state.json` | `PFS-{YYYYMMDD}-{HHMMSS}` | 포트폴리오 보유 상태 (진입가, 미실현손익, 보유일수, daily_turnover, cash_ratio) |
| `hourly_market_patch.json` | `HMP-{YYYYMMDD}-{HHMMSS}` | 장중 1시간 단위 delta 패킷. base_dmp_id에 기반하여 가격/뉴스 변경분만 포함 |
| `ops_metrics_daily.json` | `OPM-{YYYYMMDD}` | 일일 운영 메트릭 7종 + latency (order_success_rate, llm_fallback_rate 등) |

---

## 인터페이스/Contract 아티팩트 (3종)

| 스키마 파일 | 설명 |
|------------|------|
| `backtest_summary_contract.json` | AI #2 Backtest Agent → AI #1 FDA handoff. FDA가 투자 판단 시 참조하는 백테스트 요약 |
| `dashboard_payload_contract.json` | AI → BE 대시보드 payload 인터페이스. recommendation_history / portfolio_nav / paper_trading_log_summary / risk_trace 4개 sub-payload |
| `ablation_result.json` | Ablation 실험 결과. baseline vs variant 비교 (uq_on_off / pfs_stateful_stateless) |

---

## 보조 아티팩트 (3종)

| 스키마 파일 | 아티팩트 ID 패턴 | 설명 |
|------------|----------------|------|
| `data_quality_report.json` | `DQR-{YYYYMMDD}` | DMP + TTP 데이터 품질 점검 결과. overall_pass + dmp/ttp 개별 이슈 목록 |
| `paper_trading_log.json` | `PTL-{YYYYMMDD}` | 모의투자 주문 실행 로그. KIS 모의투자 API 연동 결과 |
| `broker_reconcile_report.json` | `BRR-{YYYYMMDD}` | PFS ↔ Paper Trading 대조 리포트. 불일치 항목 상세 기록 |

---

## UQ 모델 명세

| 파일 | 설명 |
|------|------|
| `uq_model_io_v1.json` | Uncertainty Quantification 모델 입출력 스펙. Phase 1: 비활성. Phase 2: Logistic Regression 기반 실행 불확실성 추정 |

---

## 필드 요약 — Required 필드 + 핵심 enum 값

### DailyMarketPacket (`daily_market_packet.json`)

**Required:** `snapshot_id`, `snapshot_dt`, `available_at`, `artifact_version`, `tickers`, `market_data`, `macro_snapshot`, `news_index`, `disclosure_index`, `meta`

| 필드 | 타입 | 비고 |
|------|------|------|
| `snapshot_id` | string | 패턴: `DMP-\d{8}-\d{6}` |
| `market_data[ticker].ohlcv` | object | required: open, high, low, close |
| `market_data[ticker].tech_features` | object | sma_5/20/60, rsi_14, bb_upper/lower, macd, atr_14, volume_ratio_20 |
| `macro_snapshot.vix_proxy` | number\|null | Phase 1 null 허용 |
| `macro_snapshot.market_breadth` | number\|null | Phase 1 null 허용 |
| `llm_market_analysis.market_mood` | enum | `bullish` \| `bearish` \| `neutral` \| `uncertain` |
| `meta.as_of_dt` | datetime | PIT-safe 기준 시각 |

---

### TickerTextPack (`ticker_text_pack.json`)

**Required:** `pack_id`, `snapshot_dt`, `artifact_version`, `ticker`, `sector_name`, `macro_docs`, `sector_docs`, `target_company_docs`, `meta`

| 필드 | 타입 | 비고 |
|------|------|------|
| `pack_id` | string | 패턴: `TTP-\d{8}-[A-Z0-9]+` |
| `*_docs[].source` | enum | `naver_news` \| `dart` \| `ecos` \| `manual` |
| `llm_news_analysis.news_sentiment` | number | -1 ~ 1 |
| `llm_disclosure_analysis.disclosure_impact` | enum | `positive` \| `negative` \| `neutral` |

---

### StrategyCard (`strategy_card.json`)

**Required:** `card_id`, `snapshot_dt`, `artifact_version`, `ticker`, `direction`, `signal`, `confidence`, `pre_risk_score`, `rationale`, `source_strategy`, `evidence_ids`

| 필드 | 타입 | 비고 |
|------|------|------|
| `card_id` | string | 패턴: `SC-\d{8}-[A-Z0-9]+` |
| `direction` | enum | `long` \| `short` \| `neutral` |
| `signal` | enum | `strong_buy` \| `buy` \| `hold` \| `sell` \| `strong_sell` |
| `confidence` | number | 0 ~ 1 |
| `source_strategy` | enum | `quant` \| `news` \| `synthesized` |
| `uncertainty_score` | number\|null | Phase 1: null. Phase 2+: 0~1 |

---

### RiskCard (`risk_card.json`)

**Required:** `risk_id`, `snapshot_dt`, `artifact_version`, `regime`, `tier1_pass`, `portfolio_constraints`, `position_risks`

| 필드 | 타입 | 비고 |
|------|------|------|
| `risk_id` | string | 패턴: `RC-\d{8}-\d{6}` |
| `regime.label` | enum | `green` \| `yellow` \| `red` |
| `tier1_pass` | boolean | red → false (신규 진입 금지) |
| `uq_enabled` | boolean | ablation 추적용 |
| `position_risks[].risk_flag` | enum | `pass` \| `cap` \| `reject` |
| `llm_risk_analysis.risk_level` | enum | `low` \| `moderate` \| `elevated` \| `high` |

---

### CandidateOrderPlan (`candidate_order_plan.json`)

**Required:** `plan_id`, `snapshot_dt`, `artifact_version`, `regime`, `execution_time`, `orders`, `portfolio_summary`

| 필드 | 타입 | 비고 |
|------|------|------|
| `plan_id` | string | 패턴: `COP-\d{8}-\d{6}` |
| `regime` | enum | `green` \| `yellow` \| `red` |
| `orders[].action` | enum | `buy` \| `hold` \| `sell` |
| `orders[].weight` | number | 0 ~ 100 (%) |
| `orders[].risk_flag` | enum | `pass` \| `cap` |

---

### FinalDecisionCard (`final_decision_card.json`)

**Required:** `decision_id`, `snapshot_dt`, `artifact_version`, `plan_id`, `fallback_used`, `decisions`, `conflicts`, `execution_summary`

| 필드 | 타입 | 비고 |
|------|------|------|
| `decision_id` | string | 패턴: `FDC-\d{8}-\d{6}` |
| `fallback_used` | boolean | true = GPT-4o fallback 사용 |
| `decisions[].decision` | enum | `approve` \| `veto` (비중 변경 불가) |
| `conflicts[].conflict_type` | enum | `signal_disagreement` \| `risk_override` \| `confidence_gap` \| `regime_conflict` \| `stop_loss_conflict` \| `backtest_conflict` \| `holding_conflict` \| `confidence_weight_conflict` |
| `conflicts[].llm_conflict_analysis.recommended_action` | enum | `approve` \| `veto` \| `cautious_approve` |

---

### PortfolioState (`portfolio_state.json`)

**Required:** `state_id`, `snapshot_dt`, `artifact_version`, `positions`, `cash_ratio`, `daily_turnover`, `total_exposure`

| 필드 | 타입 | 비고 |
|------|------|------|
| `state_id` | string | 패턴: `PFS-\d{8}-\d{6}` |
| `positions[].entry_price` | number | stop-loss 계산 기준 |
| `positions[].unrealized_pnl_pct` | number | % |
| `positions[].stop_loss_hit` | boolean | -5% 도달 여부 |
| `daily_turnover` | number | 30% cap (risk_policy_v0.yaml) |
| `cash_ratio` | number | 최소 10% (risk_policy_v0.yaml) |

---

### HourlyMarketPatch (`hourly_market_patch.json`)

**Required:** `patch_id`, `snapshot_dt`, `artifact_version`, `base_dmp_id`, `changed_tickers`, `price_patch`

| 필드 | 타입 | 비고 |
|------|------|------|
| `patch_id` | string | 패턴: `HMP-\d{8}-\d{6}` |
| `base_dmp_id` | string | 기반 DMP snapshot_id 참조 |
| `price_patch[ticker].chg_pct` | number | 전일 종가 대비 등락률 (%) |

---

### OpsMetricsDaily (`ops_metrics_daily.json`)

**Required:** `metrics_id`, `target_date`, `generated_at`, `metrics`

| 필드 | 타입 | 비고 |
|------|------|------|
| `metrics_id` | string | 패턴: `OPM-\d{8}` |
| `metrics.order_success_rate` | number\|null | 0~1 |
| `metrics.llm_fallback_rate` | number\|null | Kanana→GPT-4o 전환 비율 |
| `metrics.daily_turnover` | number\|null | PFS daily_turnover 직접 읽기 |
| `metrics.emergency_veto_count` | integer\|null | regime=red + veto 건수 |
| `metrics.p50_latency_sec` | number\|null | E2E 중앙값 소요시간 |
| `metrics.p95_latency_sec` | number\|null | E2E 95th percentile 소요시간 |

---

### BacktestSummaryContract (`backtest_summary_contract.json`)

**Required:** `status`

| 필드 | 타입 | 비고 |
|------|------|------|
| `status` | enum | `live` \| `phase1_placeholder` \| `stale` \| `error` |
| `win_rate_hint` | number\|null | 0~1. Phase 1: null |
| `baseline_delta` | number\|null | baseline 대비 초과수익률 (%) |
| `failure_tags` | array | 최근 실패 패턴 태그 |
| `confidence_band` | object\|null | lower / upper 수익률 구간 |
| `recent_sharpe` | number\|null | 최근 구간 Sharpe ratio |
| `recent_mdd` | number\|null | 최근 구간 MDD (%) |

---

### DashboardPayloadContract (`dashboard_payload_contract.json`)

4개 sub-payload 구조:

| Sub-payload | ID 패턴 | 설명 |
|-------------|---------|------|
| `recommendation_history` | `RH-{YYYYMMDD}-{YYYYMMDD}` | FDC 기반 종목별 추천 이력 (action, weight, decision) |
| `portfolio_nav` | `NAV-{YYYYMMDD}-{YYYYMMDD}` | PFS 기반 순자산 추이 (total_exposure, cash_ratio, daily_turnover) |
| `paper_trading_log_summary` | `PTS-{YYYYMMDD}-{YYYYMMDD}` | PTL 기반 거래 요약 (total_orders, executed, rejected, cancelled) |
| `risk_trace` | `RT-{YYYYMMDD}-{YYYYMMDD}` | RC 기반 리스크 추적 (regime, vix_proxy, market_breadth, uq_enabled) |

`risk_trace[].regime` enum: `green` \| `yellow` \| `red` \| `null`

---

### AblationResult (`ablation_result.json`)

**Required:** `ablation_id`, `experiment`, `target_date`, `generated_at`, `baseline`, `variant`, `comparison`

| 필드 | 타입 | 비고 |
|------|------|------|
| `ablation_id` | string | 패턴: `ABL-(uq\|pfs)-\d{8}` |
| `experiment` | enum | `uq_on_off` \| `pfs_stateful_stateless` |
| `baseline.label` / `variant.label` | string | ex) "UQ ON", "Stateful" |
| `comparison.exposure_delta` | number | variant - baseline |
| `comparison.approval_diff` | array | 승인 결과가 다른 종목 목록 |

---

### DataQualityReport (`data_quality_report.json`)

**Required:** `report_id`, `target_date`, `generated_at`, `overall_pass`

| 필드 | 타입 | 비고 |
|------|------|------|
| `report_id` | string | 패턴: `DQR-\d{8}` |
| `overall_pass` | boolean | DMP + TTP 모두 이슈 없으면 true |
| `dmp.stats.missing_tickers` | array | 누락 종목 목록 |
| `dmp.stats.null_mktcap_count` | integer | Phase 1 정상 (pykrx 이슈) |
| `ttp.stats.empty_packs` | array | 문서가 없는 종목 목록 |

---

### PaperTradingLog (`paper_trading_log.json`)

**Required:** `log_id`, `target_date`, `generated_at`, `dry_run`, `orders`, `summary`

| 필드 | 타입 | 비고 |
|------|------|------|
| `log_id` | string | 패턴: `PTL-\d{8}` |
| `dry_run` | boolean | true = 실제 주문 없음 |
| `orders[].action` | enum | `buy` \| `sell` \| `cancel` |
| `orders[].status` | enum | `executed` \| `rejected` \| `dry_run` \| `cancelled` \| `cancel_failed` |

---

### BrokerReconcileReport (`broker_reconcile_report.json`)

**Required:** `report_id`, `target_date`, `generated_at`, `status`, `mismatches`

| 필드 | 타입 | 비고 |
|------|------|------|
| `report_id` | string | 패턴: `BRR-\d{8}` |
| `status` | enum | `pass` \| `mismatch` \| `error` |
| `mismatches[].field` | string | 불일치 필드명 |
| `mismatches[].pfs_value` / `ptl_value` | any | 양쪽 값 비교 |

---

## 파이프라인 흐름 요약

```
DMP (DMP-*) ──→ TTP (TTP-*) ──→ SC (SC-*) ──→ RC (RC-*) + COP (COP-*)
                                                         ↓
                                               FDC (FDC-*) ──→ PFS (PFS-*)
                                                         ↓
                                              PTL (PTL-*) → BRR (BRR-*)
                                                         ↓
                                              DashboardPayload (BE Handoff)
```

보조 흐름:
- `DMP + TTP` → `DQR` (품질 점검)
- `PFS + FDC + PTL + RC` → `OPM` (운영 메트릭)
- `HMP` (장중 delta, DMP 위에 얹음)
- `ABL` (ablation 실험 결과)
- `UQ Model I/O` (uncertainty_score → SC → RC → COP → FDC)

# KOSPI Decision OS v3: Evaluation Matrix (3-Layer)

> 작성일: 2026-04-21
> 버전: v1.0 (Sprint 2 진입 전 신설)
> SSOT 기준: api_contracts.md v3.4 (C9, C12, C13, C17, C18), architecture.md §4.2 §5.6 §8.1

---

## 1. 목적 / 왜 3-Layer

기존 퀀트 평가는 Sharpe, AR, MDD 세 숫자로 끝난다. 그 세 숫자는 "얼마 벌었나"에 대한 답이다. 하지만 멀티에이전트 Decision OS에서는 그것으로 충분하지 않다. "누가 결정했나", "왜 그 결정을 내렸나", "그 결정이 맞았나"를 수치로 추적하지 못하면 에이전트의 존재 이유가 없다.

우리가 6-에이전트 구조(Quant, News, Risk Fast/Slow, Debate, FDA)를 선택한 이유는 교수님 피드백("cause 중심")과 직결된다. 에이전트별 기여도를 정량화하지 못하면 "멀티에이전트를 쓴 것"이 아니라 "멀티에이전트를 달아놓은 것"이다.

3-Layer 구조는 이 문제를 세 층위로 분리해 다룬다. L1(모델 성능, 업계 표준 7종)은 비교 기준점이다. L2(에이전트 기여도, C18 SSOT 7종)는 멀티에이전트 OS 고유 측정이다. L3(System OS 차별점, 7종)는 "왜 이 시스템이 다른가"를 숫자로 보여주는 발표 킬러다.

**총괄 킬러 문장**: "기존 퀀트는 '얼마 벌었나'만 본다. 우리는 '누가, 왜, 언제 결정했고, 그게 맞았나'를 매트릭으로 측정한다."

---

## 2. Layer 1: 모델 성능 (구현 완료, Sprint 1)

L1은 학계/업계 표준 지표 7종이다. 여기까지는 일반 퀀트 모델이 하는 것과 동일하다. 우리 시스템의 차별점은 L2/L3에 있다.

### 2.1 7종 지표 (C12/C13 SSOT)

| 메트릭 | 수식 | 측정 창 | 수용 기준 | config 키 | metrics.py 함수 |
|--------|------|---------|----------|-----------|----------------|
| IC | `mean(corrcoef(y_true, y_pred))` per ts_close | cross-sectional per ts_close, 시계열 평균 | mean(IC) >= 0.02 | `evaluation.ic_significance_threshold: 0.02` | `ic()`, `panel_ic()` |
| ICIR | `mean(IC_t) / std(IC_t, ddof=1)` | walk-forward fold IC array | 목표 ICIR >= 0.3 (실무 기준, 강제 threshold 없음) | `evaluation.min_daily_pnl_std` (분모 0 방지) | `icir()` |
| RankIC | `corrcoef(rank(y_true), rank(y_pred))` per ts_close | cross-sectional per ts_close, 시계열 평균 | mean(RankIC) >= 0.02 | (별도 키 없음) | `rank_ic()`, `panel_rank_ic()` |
| ARR | `prod(1+r_t)^(252/N) - 1` | walk-forward 전체 daily_pnl | >= 0.08 (연 8%) | `evaluation.annualization_factor: 252` | `annualized_return()` |
| IR | `mean(excess) / std(excess) * sqrt(252)` | walk-forward 전체 | >= 0.5 | `evaluation.annualization_factor`, `min_daily_pnl_std` | `information_ratio()` |
| SR | `mean(r) / std(r) * sqrt(252)` | walk-forward 전체 | >= 1.0 | `evaluation.annualization_factor` | `sharpe_ratio()` |
| MDD | `min((cum_t - peak_t) / peak_t)` | walk-forward 전체 누적 | >= -0.20 | `evaluation.mdd_sign: "negative"` | `max_drawdown()` |

> ICIR threshold `>= 0.3`은 야간 Mode B 8차원 벡터 평가 전용 목표값이다. 장중 Hot/Cold Path에서 강제 threshold로 사용하지 않는다. `evaluation.min_daily_pnl_std`는 ICIR 분모 0 방지용이며 threshold 키가 아니다. Sprint 3 이후 야간 평가에서 constraint로 확정 예정.

구현 파일: `new/src/models/metrics.py`. 347/347 pytest pass (Sprint 1 완료 시점).

### 2.2 8차원 성과 벡터 (architecture.md §8.1 SSOT)

수식:

```
x_t = [IC, ICIR, Rank(IC), Rank(ICIR), ARR, IR, -MDD, SR]
       m1   m2    m3          m4         m5   m6  m7   m8
```

각 차원 설명:
- `Rank(IC)` (m3), `Rank(ICIR)` (m4): 30일 rolling window 내 cross-sectional rank. 0~1 정규화.
- `-MDD` (m7): 음수 규약의 MDD를 부호 반전. 클수록 좋음으로 통일.
- 호출: `MetricsBundle.to_performance_vector(ic_history)` (metrics.py:L304)
- 정규화: `normalize_performance_vector(vec, history_df)` (metrics.py:L446). min-max + clip [0.01, 0.99]. history 비어있거나 min==max이면 0.5 (neutral).
- rolling window config: `evaluation.performance_vector_rolling_window: 30`

`to_performance_vector()`는 raw 8-dim 값만 반환한다. min-max 정규화는 호출자 레벨에서 `normalize_performance_vector()`를 경유해야 한다. 이 분리가 §8.1 설계 의도다.

### 2.3 PIT-Safety

- 레이블: `label_5m_ret` 기반. `drop_last_n_bars=5` 적용으로 미래 유출 차단.
- walk-forward fold 간 purge_bars=60, embargo_bars=78 (Prado 1% rule, 1분봉 기준).
- fold 경계를 넘나드는 미래 참조 금지. 이를 위반하는 train/test split 코드는 Critical.

**L1 킬러 문장**: "walk-forward 8 fold 기준 SR 1.84, IC 0.031, MDD -12.3%. 여기까지는 업계 표준 퀀트 모델이 하는 것이다."

---

## 3. Layer 2: Agent 기여도 (Sprint 2~3 구축, C18 SSOT)

L2는 이 시스템이 "멀티에이전트 Decision OS"인 이유를 수치로 보여준다. 에이전트 없이 Quant 단독으로 운용했을 때와 비교해 각 에이전트의 기여를 분리한다.

### 3.1 7종 지표 (C18 AgentPerformanceContract 필드, 9개 수식 필드)

| 메트릭 | 대상 에이전트 | 수식 | 수용 기준 (yaml 키) | PIT-Safety |
|--------|-------------|------|-------------------|-----------|
| `prediction_accuracy` | Quant | `count(signal_score > 0 AND label_t5_ret > 0) / count(signal)` | `agent_performance.prediction_accuracy_min: 0.52` | post-hoc, 18:00 이후만 |
| `realized_pnl_contribution` | All | leave-one-out ablation: PnL_full - PnL_without_agent_X | `agent_performance.realized_pnl_min_positive_count: 5` (30일 중 양수 일수) | T+1 fill 기준 |
| `slippage_execution_shortfall_bps` | ExecutionGateway | `mean((fill_price - snapshot_vwap) / snapshot_vwap * 10000)` | `agent_performance.slippage_bps_max: 15` | 실시간 가능 |
| `anomaly_detection_precision` | Quant | `count(anomaly_flag=true AND label_t5_ret < anomaly_threshold) / count(anomaly_flag=true)` | `agent_performance.anomaly_precision_min: 0.60` | post-hoc, 18:00 이후만 |
| `anomaly_detection_recall` | Quant | `count(anomaly_flag=true AND label_t5_ret < anomaly_threshold) / count(label_t5_ret < anomaly_threshold)` | `agent_performance.anomaly_recall_min: 0.40` | post-hoc, 18:00 이후만 |
| `sector_exposure_tracking_error` | PortfolioManager | `std(w_sector_actual - w_sector_target)` across sectors | `agent_performance.sector_tracking_error_max: 0.05` | 실시간 가능 |
| `veto_precision` | FDA | `count(veto AND label_t5_ret < veto_loss_threshold) / count(veto)` | `agent_performance.veto_precision_min: 0.60` | post-hoc, 18:00 이후만 |
| `veto_recall` | FDA | `count(veto AND label_t5_ret < veto_loss_threshold) / count(label_t5_ret < veto_loss_threshold)` | `agent_performance.veto_recall_min: 0.40` | post-hoc, 18:00 이후만 |
| `false_positive_event_trigger_rate` | News, Risk (Fast/Slow) | `count(llm_called=true AND reason_code='NORMAL_APPROVE') / count(llm_called=true)` | `agent_performance.false_positive_rate_max: 0.40` | FDA 판단 완료 시점 기준 |

`anomaly_threshold_pct: -0.01` (label_t5_ret < -1% 이면 anomaly ground truth), `veto_loss_threshold_pct: -0.005` (label_t5_ret < -0.5% 이면 veto 정답). 둘 다 `risk_config.yaml agent_performance` 섹션.

> 표기상 7 컨셉, 9 필드 (precision + recall 분리). architecture.md §4.2 "7종" 표기와 일치.

### 3.2 audit_log.jsonl 스키마 (C18, 18 필드)

장중 판단 데이터를 누적하는 append-only 파일. 파일 경로: `artifacts/audit_log.jsonl`.

```jsonl
{
  "ts": "2026-04-21T14:31:07+09:00",
  "decision_id": "DEC-20260421-1431-007",
  "agent": "quant",
  "event_type": "signal",
  "ticker": "005930",
  "reason_code": null,
  "signal_score": 0.72,
  "anomaly_flag": false,
  "target_weight": null,
  "actual_weight": null,
  "fill_price": null,
  "snapshot_vwap": null,
  "slippage_bps": null,
  "sector": "반도체",
  "llm_called": null,
  "llm_model": null,
  "label_t5_ret": null,
  "price_t5_snapshot": null
}
```

**PIT-Safety 핵심 제약**: `label_t5_ret` / `price_t5_snapshot` 두 필드는 장중 **반드시 null**. 18:00 이후 배치 job에서만 backfill. 장중에 이 두 필드를 채우는 코드는 PIT-Safety 위반으로 즉시 Critical 처리.

C18 `forbidden` 블록: `intraday_real_time_post_hoc_calc`, `label_backfill_before_1800_kst`, `fda_reason_code_mutation_for_metric_tuning` 세 가지 금지.

### 3.3 집계 트리거 + Owner

- Trigger: `ModeBScheduler` C14 `stage_1_data_rollup`, 18:00 KST
- 실제 집계 주체: `ModeBPerformanceAggregator` (Sprint 2 신설 예정)
- PIT-Safety 체크: `label_t5_ret is null` 레코드는 집계 제외 (`aggregation.pit_safety_check` C18)
- 출력 경로:
  - `artifacts/metrics/agent_performance_{YYYY-MM-DD}.json` (일별 rollup)
  - `knowledge_base/agent_performance_history.jsonl` (KB 누적)
- Daily rollup SLA: 10분 이내 (`C18 sla.daily_rollup_duration_min: 10`)

### 3.4 구현 로드맵 (Sprint 2~3)

| Sprint | 세부 작업 | 이슈 |
|--------|---------|------|
| Sprint 2 S2-0 | AuditLogger 18 필드 확장. `label_t5_ret` 장중 null 보장 로직 추가. | 현재 AuditLogger 없음. 신설 필요. |
| Sprint 2 (병행) | ExecutionGateway `_execute_mock` VWAP snapshot 실계산. 현재 `slippage: 0.0` 하드코딩. | `slippage_execution_shortfall_bps` 측정 전제 조건. |
| Sprint 2 (별도) | `new/config/sector_config.yaml` 신설. 종목 → 섹터 매핑. | `sector_exposure_tracking_error` 계산 전제. |
| Sprint 2 S2-2 (병행) | LLM Router 설계 시 `llm_called`, `llm_model` 필드 포함. | `false_positive_event_trigger_rate` 계산 전제. |
| Sprint 3 | Eval Agent 신설. `anomaly_detection_precision/recall`, `veto_precision/recall` 집계. | `new/src/eval/` 디렉토리 신설. |
| Mode B 18:00 | `ModeBPerformanceAggregator` 구축. `label_t5_ret` backfill + 전체 L2 집계. | C14 stage_1_data_rollup 트리거에 연결. |

**L2 킬러 문장**: "3월 외국인 이탈 이벤트에서 Risk Agent veto precision 68%, News Agent false_positive_rate 31%. Quant 단독 baseline 대비 당일 PnL -1.7% 방어. 에이전트가 숫자로 기여했다."

---

## 4. Layer 3: System OS 차별점 (Sprint 4 구축)

L3는 발표 킬러다. "멀티에이전트가 있었다"는 주장이 아니라 "시스템이 왜 어제보다 나아졌는지, FDA가 왜 거부했는지를 숫자로 추적한다"는 사실을 보여준다.

### 4.1 7종 지표

| 지표 | 정의 | 데이터 소스 | 계산 주기 | 구현 경로 | 수용 기준 (yaml 키) |
|------|------|------------|---------|---------|-------------------|
| `cause_attribution_accuracy` | FDA reason_code가 사후 원인과 일치한 비율 | C9 reason_code + 장마감 bar backfill | 일별 (Mode B 18:00) | `new/src/eval/cause_attribution.py` (Sprint 3 신설) | `system_os_metrics.cause_attribution_accuracy_min: 0.60` |
| `hot_path_latency_p95_ms` | p95 latency <= 100ms 달성률 | OpsMonitor latency ring | 실시간 rolling + 일별 | `ops/monitor.py` (Sprint 1 구현됨) | `quant_agent.latency_p95_target_ms: 100` (SSOT. 중복 신설 금지) |
| `cold_path_budget_efficiency` | Kanana-o 1회당 marginal PnL | LLM Router log + fill | 일별 | `analytics/llm_budget_analyzer.py` (Sprint 2 신설) | `system_os_metrics.cold_path_marginal_pnl_min_pct_per_call: 0.0001` |
| `self_evolution_gain_sharpe` | `SR(v_{n+1}) - SR(v_n)` | C17 registry.json 버전 비교 | Mode B 배포마다 | `ModelRegistry.compare_versions()` (registry.py:L228, 구현 완료) | `system_os_metrics.self_evolution_gain_threshold: 0.05` |
| `dual_source_divergence_lead_time_min` | divergence 탐지 시각 vs price break 시각 시차 (분) | dual_source_scorer + bar data | 이벤트별 | `analytics/dual_source_scorer.py` (Sprint 4 S4-1) | `system_os_metrics.dual_source_lead_time_target_min: 0` |
| `reason_code_distribution` | FDA reason_code 분포 + 상위 3 coverage | audit_log.jsonl 집계 | 일별 | `eval/reason_code_stats.py` (Sprint 3 신설) | `system_os_metrics.reason_code_top3_coverage_min: 0.80` |
| `regime_agent_contribution_matrix` | regime x agent 2D heatmap (4 regime x 7 agent = 28 셀) | L2 `realized_pnl_contribution` x L1 `regime_breakdown` | Mode B | `metrics.py regime_breakdown_fill()` 확장 + `analytics/regime_attribution.py` | `system_os_metrics.regime_breakdown_min_days_per_regime: 10` |

`hot_path_latency_p95_ms`의 SSOT는 `quant_agent.latency_p95_target_ms: 100`이다. `system_os_metrics` 섹션에 중복 선언하지 않는다. risk_config.yaml 주석에 명시됨.

### 4.2 의존 체인 (L3 <- L2/L1/Contracts)

```
cause_attribution_accuracy
    <- C9 reason_code (장중 FDA 판단 시 기록)
    + 장마감 bar backfill (18:00 이후)

hot_path_latency_p95_ms
    <- ops/monitor.py latency ring (Sprint 1 구현됨)

cold_path_budget_efficiency
    <- C18 audit_log (llm_called, llm_model 필드)
    + L2 realized_pnl_contribution (ablation 결과)

self_evolution_gain_sharpe
    <- C17 ModelRegistry.compare_versions() (registry.py:L228)
    <- L1 SR (MetricsBundle.sr, walk-forward 결과)

dual_source_divergence_lead_time_min
    <- Sprint 4 dual_source_scorer log
    + bar data (과거 bar 후향 탐지 허용, 미래 bar 기준 금지)

reason_code_distribution
    <- C18 audit_log reason_code 필드 집계
    <- C9 reason_code enum (SSOT)

regime_agent_contribution_matrix
    <- L2 realized_pnl_contribution (에이전트별)
    + L1 regime_breakdown (MetricsBundle.regime_breakdown_fill())
```

### 4.3 PIT-Safety 세부

- `cause_attribution_accuracy`: reason_code는 장중 기록. 사후 원인 일치 판단은 18:00 이후만. 장중 일치 여부를 계산하는 코드는 PIT-Safety 위반.
- `self_evolution_gain_sharpe`: 비교 대상 두 버전(v_n, v_{n+1})의 SR은 모두 과거 walk-forward 결과로 계산. 미래 fold 데이터가 SR 계산에 개입하면 안 됨.
- `dual_source_divergence_lead_time_min`: divergence 시각 vs price break 시각의 시차를 과거 bar로 후향 탐지하는 것은 허용. 미래 bar를 기준으로 divergence 판단 시점을 소급 결정하는 것은 금지.

**L3 킬러 문장**: "FDA가 장중 판단마다 reason_code를 출력한다. 한 달 누적 결과: 전체 판단의 58%가 NEWS_DIVERGENCE, 그 중 71%는 실제 가격 하락으로 이어졌다. '왜 거부했는가'에 숫자 답을 댈 수 있는 시스템이다."

---

## 5. 메트릭 의존성 그래프

순환 없음 (단방향 DAG). L1이 기반, L2가 에이전트 기여, L3가 시스템 차별점.

```
Layer 1 (기반, Sprint 1 구현 완료)
=====================================================================
ic(), panel_ic() ──────────────┐
rank_ic(), panel_rank_ic() ────┤
icir() ────────────────────────┼──► MetricsBundle.compute()
annualized_return() ───────────┤         │
information_ratio() ───────────┤         ├──► to_performance_vector()  [metrics.py:L304]
sharpe_ratio() ────────────────┤         │         → 8차원 벡터 x_t
max_drawdown() ────────────────┘         │
                                         └──► regime_breakdown_fill()
                                               → regime x agent 교차 (L3 input)

Layer 2 (에이전트 기여, Sprint 2~3 구축, C18 SSOT)
=====================================================================
audit_log.jsonl
  (장중 append, label_t5_ret=null)
          │
          ├──► prediction_accuracy          (post-hoc, 18:00 이후만)
          ├──► anomaly_detection_precision  (post-hoc, 18:00 이후만)
          ├──► anomaly_detection_recall     (post-hoc, 18:00 이후만)
          ├──► veto_precision               (post-hoc, 18:00 이후만)
          ├──► veto_recall                  (post-hoc, 18:00 이후만)
          └──► false_positive_event_trigger_rate  (FDA 판단 완료 시점)

KIS fill feedback
          ├──► slippage_execution_shortfall_bps   (실시간 가능)
          └──► realized_pnl_contribution          (ablation, T+1 fill)

PM state ──────────────────────────────────────────────────────────
          └──► sector_exposure_tracking_error     (실시간 가능)

realized_pnl_contribution ────────────────────────────────────────► (Layer 3 input)
8차원 벡터 x_t ──────────────────────────────────────────────────► (Layer 3 input)

Layer 3 (System OS 차별점, Sprint 4 구축)
=====================================================================
C9 reason_code + bar backfill (18:00 이후)
          └──► cause_attribution_accuracy

ops/monitor.py latency ring (Sprint 1 구현)
          └──► hot_path_latency_p95_ms

C18 audit_log (llm_called) + L2 realized_pnl_contribution
          └──► cold_path_budget_efficiency

C17 ModelRegistry.compare_versions() [registry.py:L228]
    └── consumes L1 SR (walk-forward 결과)
          └──► self_evolution_gain_sharpe

dual_source_scorer log + bar data (Sprint 4 S4-1)
          └──► dual_source_divergence_lead_time_min

C18 audit_log reason_code counts + C9 enum
          └──► reason_code_distribution

L2 realized_pnl_contribution x L1 regime_breakdown
          └──► regime_agent_contribution_matrix
```

---

## 6. 발표 킬러 문장 4개

발표 서사에 직접 사용 가능한 숫자 기반 문장. 실제 수치는 Sprint 2~4 구축 후 교체.

**총괄** (발표 도입부):
"기존 퀀트는 '얼마 벌었나'만 본다. 우리는 '누가, 왜, 언제 결정했고, 그게 맞았나'를 매트릭으로 측정한다."

**L1** (모델 baseline 슬라이드):
"walk-forward 8 fold 기준 SR 1.84, IC 0.031, MDD -12.3%. 여기까지는 업계 표준 퀀트 모델이 하는 것이다."

**L2** (에이전트 기여도 슬라이드):
"3월 외국인 이탈 이벤트에서 Risk Agent veto precision 68%, News Agent false_positive_rate 31%. Quant 단독 baseline 대비 당일 PnL -1.7% 방어. 에이전트가 숫자로 기여했다."

**L3** (차별점 슬라이드, cause 중심):
"FDA가 장중 판단마다 reason_code를 출력한다. 한 달 누적 결과: 전체 판단의 58%가 NEWS_DIVERGENCE, 그 중 71%는 실제 가격 하락으로 이어졌다. '왜 거부했는가'에 숫자 답을 댈 수 있는 시스템이다."

---

## 7. SSOT 참조 테이블

| 메트릭 | Layer | SSOT 원천 | 계약 참조 |
|--------|-------|----------|---------|
| ic, icir, rank_ic, arr, ir, sr, mdd | L1 | `new/src/models/metrics.py MetricsBundle` | C12/C13 `output.metrics` |
| 8차원 벡터 x_t | L1 | `MetricsBundle.to_performance_vector()` (metrics.py:L304) | architecture.md §8.1 |
| `prediction_accuracy` | L2 | `audit_log.jsonl` 18:00 배치 집계 | C18 `aggregation.output.metrics.prediction_accuracy` |
| `realized_pnl_contribution` | L2 | leave-one-out ablation, T+1 fill | C18 `aggregation.output.metrics.realized_pnl_contribution` |
| `slippage_execution_shortfall_bps` | L2 | ExecutionGateway fill vs VWAP | C18 `aggregation.output.metrics.slippage_execution_shortfall_bps` |
| `anomaly_detection_precision`, `anomaly_detection_recall` | L2 | `audit_log.jsonl` 18:00 배치 집계 | C18 `aggregation.output.metrics.anomaly_detection_*` |
| `sector_exposure_tracking_error` | L2 | PM state (실시간) | C18 `aggregation.output.metrics.sector_exposure_tracking_error` |
| `veto_precision`, `veto_recall` | L2 | `audit_log.jsonl` 18:00 배치 집계 | C18 `aggregation.output.metrics.veto_*` |
| `false_positive_event_trigger_rate` | L2 | `audit_log.jsonl` llm_called + reason_code | C18 `aggregation.output.metrics.false_positive_event_trigger_rate` |
| `cause_attribution_accuracy` | L3 | audit_log + C9 reason_code + bar backfill | C9 + Sprint 3 Eval Agent |
| `hot_path_latency_p95_ms` | L3 | `ops/monitor.py` latency ring | `risk_config.yaml quant_agent.latency_p95_target_ms` (SSOT) |
| `cold_path_budget_efficiency` | L3 | C18 audit_log (llm_called) + L2 realized_pnl | C9 `llm_budget` + C18 |
| `self_evolution_gain_sharpe` | L3 | `ModelRegistry.compare_versions()` (registry.py:L228) | C17 |
| `dual_source_divergence_lead_time_min` | L3 | Sprint 4 dual_source_scorer | (Sprint 4 C3A DualSourceScoreContract) |
| `reason_code_distribution` | L3 | audit_log reason_code 집계 | C9 + C18 |
| `regime_agent_contribution_matrix` | L3 | `metrics.py regime_breakdown_fill()` + L2 | C12 + C18 |

---

## 8. 구현 로드맵 (Sprint별)

| Sprint | 작업 | 산출물 | 상태 |
|--------|------|--------|------|
| Sprint 1 (완료) | L1 7종 전체 + Hot Path latency OpsMonitor + `ModelRegistry.compare_versions()` 선행 신설 + 8차원 벡터 `to_performance_vector()` + `normalize_performance_vector()` | `new/src/models/metrics.py`, `new/src/models/registry.py`, `new/ops/monitor.py` | DONE |
| Sprint 2 | AuditLogger 18 필드 확장 (`label_t5_ret` 장중 null 보장) + `sector_config.yaml` 신설 + ExecutionGateway VWAP 실계산 + LLM Router (`llm_called`, `llm_model`) + `ModeBPerformanceAggregator` 신설 | C18 구현체, L2 실시간 지표 인프라 | 미착수 |
| Sprint 3 | Eval Agent 신설 (`cause_attribution_accuracy`, `reason_code_distribution`, `anomaly_detection_precision/recall`, `veto_precision/recall` 집계) | `new/src/eval/cause_attribution.py`, `eval/reason_code_stats.py` | 미착수 |
| Sprint 4 | `self_evolution_gain_sharpe` 배포 연동 + `dual_source_divergence_lead_time_min` + `regime_agent_contribution_matrix` + 평가 Dashboard | L3 7종 전체 구현체 | 미착수 |
| Sprint 5 | regime_breakdown 확장 (regime 라벨 정교화, KOSPI200 편입 데이터 반영) | `regime_breakdown_fill()` 확장 버전 | 미착수 |

Sprint 2 착수 시 가장 먼저 할 것은 AuditLogger 18 필드 확장이다. `label_t5_ret` 장중 null 보장 코드가 없으면 L2 지표 전체가 PIT-Safety 위반 상태로 쌓인다.

---

## 9. 변경 이력

| 날짜 | 버전 | 내용 |
|------|------|------|
| 2026-04-21 | v1.0 | 신설. 3-Layer 21 메트릭 (L1 7 + L2 9 필드 / 7 컨셉 + L3 7). api_contracts.md v3.2 C18 신설과 동시 작성. Sprint 1 구현 완료 상태 기준. |

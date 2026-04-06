# KOSPI Decision OS v2.1 — API Contracts

> GPT Pro 제공 (2026-04-05). architecture v2.1 기준.
> 연구형/모의운용형 구현용. 실거래 전환 시 OMS Phase 2 + EventAdmission 필수.

> **이 파일이 스키마/필드 정의의 SSOT(Single Source of Truth)이다.** architecture.md와 architecture_visual.md는 파생 문서. 불일치 시 이 파일이 우선한다.

## 공통 규약

```yaml
time:
  format: ISO8601
  timezone: Asia/Seoul

identity:
  ticker: "6-digit KRX code"
  event_id: "EVT-{yyyymmdd}-{source}-{scope}"
  message_id: "MSG-{yyyymmdd}-{hhmm}-{seq}"
  portfolio_patch_id: "PP-{yyyymmdd}-{seq}"
  decision_id: "DEC-{yyyymmdd}-{hhmm}-{seq}"
  order_plan_id: "OP-{yyyymmdd}-{hhmm}-{seq}"

modes:
  hot_path: "매 1분, 퀀트 중심"
  cold_path: "이벤트 시, News/Risk/Debate 활성"

governance:
  source_of_truth:
    target_weights: PPO Allocator
    order_deltas: Portfolio Manager
    final_approval: FDA
  can_change_weight:
    FDA: false

message_taxonomy:
  # SSOT — 모든 publish name, report type, action type은 여기서 정의

  publish_channels:  # 에이전트가 Message Pool에 publish하는 채널명
    - news_signal
    - dart_alert
    - sentiment_update
    - theme_score
    - risk_warning
    - regime_change
    - veto_recommendation
    - quant_signal
    - quant_alert
    - anomaly_detected
    - investor_flow_alert
    - debate_resolution
    - pairwise_ranking
    - final_decision

  action_types:  # 메시지의 action_type 필드 enum
    - signal
    - alert
    - veto_recommendation
    - regime_change
    - resolution

  report_types:  # C5 AgentReportContract의 보고서 유형
    - news_signal
    - risk_warning
    - quant_signal
    - investor_flow_alert
    - theme_score
```

## C1. MinuteBarIngestContract

```yaml
name: MinuteBarIngestContract
owner: Data Layer / KIS Gateway
transport: websocket-primary + rest-backfill
request:
  universe: [ticker]
  bar_interval: "1m"
  session_date: "YYYY-MM-DD"
response:
  batch_id: string
  source: "KIS"
  bars:
    - ticker: string
      ts_close: ISO8601
      open: float
      high: float
      low: float
      close: float
      volume: float
      vwap: float
      turnover: float
      change: float
      ingest_ts: ISO8601
      completeness: "full|partial|backfilled"
constraints:
  required_features: [open, high, low, close, volume, vwap, turnover, change]
  uniqueness_key: [ticker, ts_close]
  exactly_once_semantics: false
  idempotent_upsert: true
errors:
  - BAR_MISSING
  - DUPLICATE_BAR
  - WS_DISCONNECTED
  - BACKFILL_REQUIRED
auth:
  method: "oauth2"
  token_refresh: "access token 만료 15분 전 자동 갱신"
  refresh_failure_action: "주문 중단 + Emergency Path 진입"
  token_storage: "메모리 (파일 저장 금지)"
```

## C2. EventNormalizeContract

```yaml
name: EventNormalizeContract
owner: Data Layer / Event Gateway
transport: pull + webhook + crawler
request:
  raw_event:
    source: "naver_news|dart|community|us_market|ecos|krx_investor_flow"
    raw_payload_ref: string
response:
  event:
    event_id: string
    source: string
    event_type: "news|dart|macro|us_market|community|regime|investor_flow"
    scope: "ticker:{code}|sector:{name}|market"
    title: string
    summary: string
    occurred_at: ISO8601
    ingest_ts: ISO8601
    priority: "urgent|normal|low"
    llm_required: bool
    ttl: int
    expires_at: ISO8601
    supersedes: string|null
constraints:
  dedupe_key: [source, occurred_at, scope, title]
  stale_drop: true
  pre_filter:
    source: "news_filter.yaml"
    method: "keyword_match(title, summary)"
    pass_condition: "match_any = true"
    no_match_action: "drop (LLM 미호출)"
    drop_log: "prefilter_drop_log (dead_letter_log와 별도)"
    drop_log_fields: [timestamp, source, title, drop_reason]
    used_by: "Mode B §8.5.1 키워드 진화 (놓친 뉴스 분석)"
errors:
  - MALFORMED_EVENT
  - UNSUPPORTED_SOURCE
  - DUPLICATE_EVENT
  - CRAWL_FAILED
  - CRAWL_TIMEOUT
  - SOURCE_UNAVAILABLE
```

## C3. PreprocessingContract

```yaml
name: PreprocessingContract
owner: Data Layer / Preprocessing Pipeline
input:
  bar_batch_ref: string
  event_batch_ref: string|null
output:
  quant_frame:
    asof: ISO8601
    universe: [ticker]
    features_ref: string
    feature_spec:
      normalization: "robust_zscore_mad"
      missing_policy: ["forward_fill", "cross_sectional_mean"]
      multiscale: ["1m", "5m", "30m", "60m"]
    feature_manifest:
      # SSOT — 모든 피처 컬럼을 명시적으로 열거

      kis_raw:  # KIS 1분봉 원천 8피처
        - open
        - high
        - low
        - close
        - volume
        - vwap
        - turnover
        - change

      multiscale_derived:  # multi-scale 파생
        scales: ["1m", "5m", "30m", "60m"]
        per_scale: ["mean", "std", "min", "max", "trend"]

      us_overnight:  # 미국 시장 (08:30 1회 수집)
        - us_sp500_change
        - us_nasdaq_change
        - us_vix
        - us_soxx_change
        availability: "08:30 KST, d-1 US close"
        time_alignment: "us_close(d-1) → krx_data(d)"

      investor_flow:  # KRX 투자자별
        - foreign_net_buy
        - institutional_net_buy
        - retail_net_buy

      macro:  # ECOS 거시
        - interest_rate
        - usd_krw

      alpha_factors:  # LLM 생성 (Mode B에서 갱신)
        source: "Alpha Factor Engine"
        dynamic: true
        manifest_ref: "별도 alpha_factor_manifest로 관리"
  agent_context:
    summaries:
      - scope: "ticker|sector|market"
        text: string
        ts: ISO8601
        pit_safe: true
rules:
  llm_raw_price_exposure: false
  future_leakage: false
errors:
  - FEATURE_BUILD_FAILED
  - PIT_SAFETY_VIOLATION
  - MISSING_BAR_THRESHOLD_EXCEEDED
```

## C4. SharedMessagePoolContract

```yaml
name: SharedMessagePoolContract
owner: Agent Layer / Blackboard
operations:
  - publish(message)
  - subscribe(filter)
  - ack(message_id)
  - supersede(old_message_id, new_message_id)
  - expire(now)
message_schema:
  message_id: string
  content: string
  cause_by: string
  sent_from: string
  send_to: string|list|null
  priority: "urgent|normal|low"
  confidence: float
  reasoning: string
  evidence_ids: [string]
  uncertainty: string
  prediction: string|null
  risk_level: "low|medium|high"|null
  timestamp: ISO8601
  ttl: int
  expires_at: ISO8601
  scope: string
  event_id: string|null
  supersedes: string|null
  action_type: "signal|alert|veto_recommendation|regime_change|resolution"
  portfolio_patch_id: string|null
delivery_semantics:
  mode: "at-least-once"
  subscriber_filtering: true
  dependency_activation: true
errors:
  - MESSAGE_EXPIRED
  - INVALID_SCOPE
  - SUPERSEDE_CONFLICT
```

## C5. AgentReportContract

```yaml
name: AgentReportContract
owner: Layer 4 Agents
report_types:
  news_signal:
    payload:
      stance: "buy|sell|neutral"
      impacted_tickers: [ticker]
      impacted_sectors: [string]
      narrative: string
  risk_warning:
    payload:
      stance: "veto_recommendation|risk_reduce|neutral"
      risk_level: "low|medium|high"
      macro_note_ref: string|null
      micro_note_ref: string|null
  quant_signal:
    payload:
      scores: [{ticker: string, score: float, confidence: float}]
      anomalies: [{ticker: string, anomaly_type: string, score: float}]
      top10_candidates: [ticker]
      filter:
        min_confidence: "risk_config.yaml에서 로드. 미달 종목은 top10_candidates에서 제외"
  investor_flow_alert:
    payload:
      stance: "risk_reduce|neutral"
      flow_type: "foreign|institutional|retail"
      net_amount: float
      impacted_tickers: [ticker]
      narrative: string
  theme_score:
    payload:
      scores: [{ticker: string, theme_score: float, mention_count: int, sentiment: float}]
      trending_themes: [string]
      ttl: 86400  # 1일 이내 소멸
activation:
  hot_path:
    quant_only: true
  cold_path:
    trigger: ["news_detected", "dart_alert", "vol_spike", "regime_change", "anomaly"]
sla:
  news_agent_max: 10s
  risk_agent_max: 10s
  quant_agent_mode: "no-llm"
  llm_budget:
    source: "risk_config.yaml llm_budget"
    overflow: "gpt-4o fallback"
    tracking: "daily counter per agent"
```

## C6. DebatePairwiseRankingContract

```yaml
name: DebatePairwiseRankingContract
owner: Debate Agent
input:
  mode: "pairwise_request|conflict_detected"
  candidates: [ticker]
  reports_ref: [message_id]
output:
  debate_resolution:
    conflict_id: string|null
    winner_view: "news|risk|quant|mixed"
    reasoning: string
  pairwise_ranking:
    comparison_count: int
    wins: [{ticker: string, win_count: int}]
    ranked: [ticker]
    top_k: [ticker]
activation:
  hot_path: false  # 평상시 호출 안 됨
  cold_path:
    trigger: "conflict_detected"  # 퀀트 vs 에이전트 충돌 시만
    conflict_criteria:
      - "quant_top10 vs agent_veto_recommendation 상반"
      - "quant_buy vs risk_high 동시"
      - "news_sell vs quant_top5 동시"
    fallback_if_no_conflict: "퀀트 점수 순 랭킹 유지, Debate 미호출"
rules:
  max_pairwise_for_top10: 45
  comparator_non_transitive: true
  evidence_required: true
sla:
  max_runtime: 15s
errors:
  - TOO_MANY_CANDIDATES
  - MISSING_REPORTS
  - LATENCY_BUDGET_EXCEEDED
```

## C7. PPOAllocatorContract

```yaml
name: PPOAllocatorContract
owner: Model Layer / PPO Allocator
input:
  top_k: [ticker]
  quant_scores: [{ticker: string, score: float, confidence: float}]
  current_positions: [{ticker: string, qty: float, weight: float}]
  market_state:
    cash_ratio: float
    sector_exposure: {sector: float}
    regime_state: string|null
output:
  allocation_plan:
    target_weights: {ticker: float}
    cash_weight: float
    policy_version: string
    constraints_applied:
      max_names: int
      max_single_name: float
      max_sector: float
      min_cash: float
rules:
  state: ["quant_signal", "current_position", "market_state"]
  action: "continuous_weights_softmax"
  reward: "return_minus_transaction_cost"
  inference_only_intraday: true
  retrain_after_close: true
  turnover_cap:
    daily_max: "risk_config.yaml에서 로드. 초과 시 주문 축소"
  regime_gate:
    source: "risk_config.yaml"
    effect: "red → 신규 진입 금지, yellow → 비중 50% 감축"
config_defaults:
  max_names: 10
  max_single_name: 0.20
  max_sector: 0.40
  min_cash: 0.10
errors:
  - INVALID_WEIGHT_SUM
  - CONSTRAINT_VIOLATION
  - POLICY_NOT_LOADED
```

> **주의**: config_defaults 값은 default이며 final freeze 전. 구현 시 risk_config.yaml에서 로드해야 함. 하드코딩 금지.

## C8. PortfolioDeltaPlannerContract

```yaml
name: PortfolioDeltaPlannerContract
owner: Layer 1 / Portfolio Manager
input:
  target_weights: {ticker: float}
  current_positions: [{ticker: string, qty: float, weight: float}]
  latest_prices: {ticker: float}
  portfolio_value: float
output:
  portfolio_patch:
    portfolio_patch_id: string
    based_on_ts: ISO8601
    target_weights: {ticker: float}
    order_deltas:
      - ticker: string
        side: "buy|sell"
        qty: int
        reason: "rebalance|exit|risk_reduce|cash_raise"
rules:
  source_of_truth: "Portfolio Manager generates order_deltas"
  fda_may_edit: false
  excluded_holding_policy: "weight=0 generates sell delta"
  cold_path_exit_trigger:
    - quant_anomaly
    - risk_veto
  order_validation:
    - price_gt_zero: true
    - qty_gt_zero: true
    - ticker_in_universe: true
    - order_value_lt_position_limit: true
    - daily_pnl_check: "daily_pnl <= -risk_config.daily_loss_threshold"
errors:
  - PRICE_UNAVAILABLE
  - LOT_SIZE_ERROR
  - NEGATIVE_QTY
```

## C9. FDADecisionContract

```yaml
name: FDADecisionContract
owner: FDA
input:
  active_reports: [message_id]
  portfolio_patch_ref: string
  dependency_status:
    news: "done|skipped|timeout"
    risk: "done|skipped|timeout"
    quant: "done|skipped|timeout"
    debate: "done|skipped|timeout"
output:
  final_decision:
    decision_id: string
    approved: bool
    target_weights: {ticker: float}   # read-only echo
    order_deltas: [{ticker: string, side: string, qty: int, reason: string}]  # read-only echo
    veto_reason: string|null
    risk_overrides: [{rule: string, original: string, override: string, justification: string}]
    confidence: float
    expiry: ISO8601
rules:
  can_change_weight: false
  must_include_reasoning: true
  veto_if_uncertain: true
  dependency_wait: "all active agents or timeout"
errors:
  - MISSING_PORTFOLIO_PATCH
  - EXPIRED_DECISION
  - ILLEGAL_DELTA_MODIFICATION_ATTEMPT
```

> **주의**: risk_overrides는 audit metadata 전용. FDA가 실제 리스크 규칙을 변경하는 경로로 사용 금지. `ILLEGAL_DELTA_MODIFICATION_ATTEMPT` 에러로 order_deltas 수정 시도 시 런타임 거부.

## C10. ExecutionFeedbackContract

```yaml
name: ExecutionFeedbackContract
owner: Layer 1 / Execution Gateway
input:
  final_decision_ref: string
  approved: bool
  order_deltas: [{ticker: string, side: string, qty: int, reason: string}]
  execution_mode: "mock|paper|live"  # default: mock. live_enabled=false이면 live 무시
  live_enabled: false                # 실계좌 주문 스위치. 기본값 꺼짐
output:
  execution_report:
    order_plan_id: string
    submitted_at: ISO8601
    status: "submitted|rejected|filled|partial_filled|cancelled"
    fills:
      - ticker: string
        side: string
        qty: int
        avg_fill_price: float
        fill_ts: ISO8601
    estimated_cost: float
    realized_slippage: float
  feedback_record:
    kb_message_id: string
    pnl_contribution: float
    execution_shortfall: float
    lesson_stub: string|null
rules:
  if approved=false: "no order submission"
  mode_guard:
    if execution_mode=mock: "주문 미발송, mock 체결 결과 생성"
    if execution_mode=paper: "KIS 모의투자 서버로 주문"
    if execution_mode=live: "KIS 운영 서버로 주문 (live_enabled=true 필수)"
    if live_enabled=false AND execution_mode=live: "REJECTED — live 스위치 꺼짐"
  after_execution:
    - record_slippage
    - record_cost
    - update_memory
    - write_kb_entry
  reconciliation:
    frequency: "매 실행 사이클 후"
    compare: "system_positions vs kis_actual_positions"
    on_mismatch: "알림 + system_positions = kis_actual_positions으로 리셋"
  audit_log:
    format: "JSON Lines"
    fields: [timestamp, order_id, ticker, side, qty, price, status, latency_ms, error]
    retention: "최소 1년"
  kill_switch:
    trigger: "daily_pnl <= -daily_loss_threshold (config는 양수 절대값)"
    action: "전량 시장가 청산 + EMERGENCY_HALT"
    threshold_source: "risk_config.yaml (하드코딩 금지)"
    manual_override: "CLI 명령으로 수동 발동 가능"
known_gaps_current_phase:
  - partial_fill_logic_not_implemented
  - amend_cancel_not_implemented
  - child_order_split_not_implemented
  - order_queue_not_implemented
  - session_auction_logic_not_implemented
  - websocket_failover_not_implemented
  - api_rate_limit_governor_not_implemented
```

## C11. EventAdmissionControlContract

```yaml
name: EventAdmissionControlContract
owner: Agent Layer / Event Admission
input:
  incoming_events: [event]
output:
  admitted_events: [event_id]
  dropped_events: [{event_id: string, reason: string}]
  dead_letter_log:
    format: "JSON Lines"
    fields: [timestamp, event_id, drop_reason, original_event_ref]
    retention_days: 30
    used_by: "Mode B §8.5.1 nightly keyword evolution"
rules:
  dedupe_by: [event_id, supersedes]
  priority_order: ["market", "sector", "ticker"]
  stale_drop: "expires_at < now"
  max_cold_path_jobs_per_minute: int
  conflict_merge: true
  comparator:
    sort_key: ["priority", "trigger_type", "scope", "recency"]
    priority_order: "urgent > normal > low"
    trigger_order: "vol_spike > dart_alert > news_detected > regime_change > anomaly"
    scope_order: "market > sector > ticker"
    recency: "newest first"
  backlog_overflow:
    action: "drop lowest priority → dead_letter_log"
    max_backlog_vs_jobs_per_minute: "backlog = concurrent slots, jobs_per_minute = throughput cap"
```

> GPT Pro "반드시 추가" 권고. architecture.md §7.2 Cold Path 이벤트 관리 정책(C-2)의 공식 계약서.

## 구현 순서 (GPT Pro Sprint 계획)

> MVP 목표: "수익이 아니라 1분봉 입력 → 구조화된 판단 → mock execution → 피드백 저장이 안정적으로 도는 것"
> 첫 2주: 하나의 결정이 end-to-end로 흐르는 것 하나만.
> 틀린 시작: 25개 모듈 동시 개발 + live execution까지 한 번에

### Sprint 0 — 사양 동결
C4(Message Pool), C8(PM), C9(FDA), C10(Execution), C11(EventAdmission) freeze.
TTL/expiry/supersedes는 이미 SSOT로 확정.

### Sprint 1 — 최소 수직 슬라이스 (9개)
KIS Gateway, Preprocessing, Quant Agent, Top-10 filter,
Debate **stub**, PPO inference **stub**, Portfolio Manager, FDA, Mock Execution.
→ 검증: 30종목 1분봉에서 final_decision 생성 → mock execution → KB 저장

### Sprint 2 — Cold Path (5개)
Event Admission, News Agent, Risk Agent, LLM Router, Persistent Cache.
→ 검증: 이벤트 없으면 quant-only, 있으면 LLM 개입

### Sprint 3 — PPO 실전화
stable-baselines3 PPO 학습, 거래비용 reward, 현재 포지션 state,
Top-K 밖 보유종목 weight=0 청산 경로.

### Sprint 4 — Mode B
Alpha Factor Engine, Co-STEER, Thompson Sampling, 3중 정규화, Alpha Decay Monitor.
→ 코어 거래 루프가 돈 뒤에 붙인다.

## 미루면 안 되는 것

1. C11 EventAdmission/Backpressure — 코드 시작 전에 추가
2. OMS Phase 2 — live 전환 전 **별도 epic**으로 분리 관리
3. **Contract Tests 먼저 작성** — 이 시스템은 모델보다 계약이 중요하다.
   C4/C8/C9/C10/C11에 대한 schema test, replay test, idempotency test를 구현 코드보다 먼저 작성.

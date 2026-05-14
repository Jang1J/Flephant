# GPT Pro Deep Review Prompt - Finish By 2026-05-15

아래는 Elephant Lab v3 KOSPI Decision OS 프로젝트의 현재 상태입니다.
목표는 2026-05-15까지 "실계좌 전환만 제외하고" 기획한 모든 기능을 모의투자/서비스 시연 가능한 수준으로 끝내는 것입니다.

당신은 GPT Pro 심층 리뷰어입니다. Codex/Claude의 결론을 그대로 믿지 말고, 아래 evidence를 기준으로 과장, gate 우회, 명세 불일치, 발표 리스크를 찾아주세요.

## 프로젝트 핵심 제약

- 대상: KOSPI active 20 종목, 1분봉 Decision OS.
- Mode A: 장중 Hot Path는 LLM 호출 금지, Quant -> PPO -> Portfolio Manager -> FDA approve/veto -> Execution.
- Mode A Cold Path: 뉴스/공시/수급/커뮤니티 이벤트 시 LLM agent 개입.
- Mode B: 18:00-22:59 KST, Alpha Factor -> retrain -> BacktestAgent(C12) -> C14 deploy gate.
- 실계좌 전환은 이번 마감 범위에서 제외.
- 실계좌 제외 후에도 paper/virtual KIS evidence, BE read-only status, demo, C12/C14 dry-run은 가능한 한 완료하고 싶음.
- 불변 원칙:
  - PIT-Safety
  - FDA는 weight 변경 금지
  - BacktestAgent는 Mode B 전용
  - Hot Path 동기 LLM 호출 금지
  - 임계값은 risk_config.yaml SSOT
  - `.env`는 Codex가 읽거나 수정하지 않음

## 현재 feature_list 상태

- `feature_list.json`: 총 57개 기능 중 55 done, 2 blocked.
- blocked:
  - `S1-8 KIS virtual/real 전환 실구현`
  - `S4-6 Paper Trading (mock -> paper 전환)`
- 단, 최근 live smoke에서는 KIS virtual API가 실제로 PASS했음.
- 실계좌 real 전환은 제외하므로, S1-8을 "virtual complete, real excluded"로 재분류할 수 있는지 판단 필요.

## 최신 데이터 연결 evidence

최신 live API smoke:

- Report: `artifacts/reports/data_readiness/data_readiness_20260514_014944.json`
- `status=PASS`
- `allow_mock=false`
- `failures=[]`
- `generated_at=2026-05-14T01:49:39+09:00`
- stages:
  - `kis_investor_daily`: PASS, `source_mode=virtual`, requested date valid `1/1`
  - `krx_investor_bridge`: PASS, `provider_mode=virtual`, `is_mock=false`
  - `dart`: PASS, `event_count=3`, `is_mock=false`
  - `naver`: PASS, `item_count=3`, `is_mock=false`
  - `community`: PASS, `event_count=9`, `is_mock=false`
  - `kospi_batch_snapshot`: PASS, requested/received `3/3`, `source_mode=virtual`
  - `ecos_macro`: PASS, `interest_rate=2.5`, `usd_krw=1450.8`, `is_mock=false`
  - `us_overnight`: PASS, `source=yfinance`, `is_mock=false`

Important caveat:

- 이 report는 smoke-only입니다. `stages=["smoke"]`, 3 tickers, 1 date.
- `prelive_gate.py`가 참조한 80일 real readiness report는 더 오래된 `data_readiness_20260511_101656.json`입니다.
- 그 report 기준 20 tickers, 80 business days, min rows/day 381, PASS.

질문 1:
현재 상태에서 "데이터 연결 완료"라고 말해도 되는 범위를 정의해주세요.
특히 live smoke PASS와 80일 backfill PASS를 어떻게 분리해서 발표/보고해야 하나요?

질문 2:
15일까지 마감이라면 지금 사용자 터미널에서 아래 full command를 다시 돌리는 것이 필요한가요?

```bash
COMMUNITY_SCRAPE_ENABLED=1 PYTHONPATH=/Users/jangjaewon/Desktop/Elephant_Lab/new python new/scripts/live_data_readiness.py --all --end-date 20260508 --business-days 80 --max-tickers 20 --require-train
```

필요하다면 이유, 예상 실패 지점, 대체 축약 검증을 제안해주세요.

## 80일 feature materialization evidence

- `artifacts/reports/build_news_dart_archive/build_news_dart_archive_20260514_005143.json`
  - PASS, 80 files, 41,690 events
- `artifacts/reports/dual_source_history/materialize_dual_source_history_20260514_012123.json`
  - PASS, 80/80, date coverage 1.0
- `artifacts/reports/exogenous_history/materialize_exogenous_history_20260513_202226.json`
  - PASS, 80/80, coverage 1.0
  - provider availability: US market real, ECOS real, KIS investor real
- `artifacts/reports/phase2_feature_backfill/phase2_feature_backfill_20260514_012921.json`
  - PASS, dual_source/exogenous coverage all 1.0

But Dual-Source caveat:

- `new/artifacts/dual_source/20260508.json`
  - `raw_event_count=6725`
  - `news_event_count=6725`
  - `community_event_count=0`
  - every sample score has `comm_score_t_1=0.0`, `comm_score_t_2=0.0`
  - `source_notes="finbert|news_scope=ticker|comm_scope=none"`
- Current community live path:
  - `new/config/dual_source.yaml`
  - `community.real_provider="naver_search"`
  - `naver_search_providers=["cafearticle", "blog"]`
  - This is not DC/종토방 scraping. It is Naver Search cafe/blog proxy.

질문 3:
이 상태에서 "Dual-Source"라고 부르는 것이 정직한가요?
가능한 답변을 세 가지로 나눠주세요.

1. 그대로 Dual-Source라고 해도 되는 경우
2. "News/DART primary + community live-smoke proxy"로 낮춰 말해야 하는 경우
3. 15일까지 최소 수정해야 하는 경우

질문 4:
row-level C12 feature_quality를 맞추기 위해 sector/market fallback, market backstop을 넣었습니다.
이는 threshold 우회는 아니지만 ticker differentiation을 낮춥니다.
이 설계를 유지해도 되는지, 아니면 gate metric을 date-level과 row-level로 분리하는 것이 더 정직한지 판단해주세요.

## C12, C14, deploy gate 상태

현재 2026-05-14 02:00 KST 기준, C12는 아직 최신 v2 feature로 재실행되지 않았습니다.
이유는 Mode B window가 18:00-22:59 KST이고, BacktestAgent는 Mode B 전용이기 때문입니다.

Latest service status:

- Command:
  - `PYTHONPATH=new /opt/anaconda3/envs/elephant/bin/python new/scripts/service_readiness_status.py --bundle-id BUNDLE-20260512-0AEEE37A --no-write-report`
- Result:
  - `status=PARTIAL`
  - `deploy_quality=BLOCKED`
  - `broker_evidence=BLOCKED`
  - `live_trading_allowed=false`
  - production registry active_version is null
  - paper registry active_version exists

Latest prelive gate no-write:

- `status=BLOCKED`
- PASS:
  - code SSOT
  - real data readiness
  - 80 business day data
  - LGBM real train
  - ops risk config
- BLOCKED:
  - `05_backtest_real_candidate`
  - `06_paper_balance`
  - `07_paper_reconciliation`
  - `08_paper_probe_order`

Latest stale backtest:

- `artifacts/reports/backtest/backtest_BUNDLE-20260512-0AEEE37A_20260513_202742.json`
- `verdict=pass`
- `sr=2.7049`, `ic=0.0596`, leakage pass, service-policy replay pass
- But feature_quality gate false:
  - dual_source row non-neutral `65417/97750 = 0.6692`
  - threshold is 0.8
- This is stale, before latest 2026-05-14 v2 feature materialization.

질문 5:
18:00 KST 이전에 C12를 강제로 실행할 개발용 override를 만드는 것이 맞나요?
아니면 Mode B only 원칙을 지키고, 18:00 자동 실행을 기다리는 것이 맞나요?
마감이 15일이라는 현실까지 고려해 판단해주세요.

질문 6:
production `active_version=null`은 C14 deploy 전까지 의도된 blocker입니다.
실계좌 제외 마감 기준에서는 production registry를 그대로 두고 paper registry만 active로 쓰는 것이 맞나요?
아니면 "paper-only completion"을 위해 별도 evidence label을 더 만들어야 하나요?

## Paper trading, KIS virtual broker evidence 상태

Latest internal fake rehearsal:

- `artifacts/reports/paper_auto_trading/paper_auto_service_rehearsal_20260514_013052.json`
- `status=PASS`
- `evidence_level=internal_fake_kis`
- `external_kis_api=false`

Latest external KIS paper reports:

- `artifacts/reports/paper_trading/paper_trading_balance_reconciliation_20260512_192053.json`
  - `status=FAIL`
  - failure: `balance`
  - reason: `KIS_ACCOUNT_NUMBER/KIS_ACCOUNT_PRODUCT_CODE` missing in that Codex process
- `paper_trading_submit_probe_order_20260511_140445.json`
  - `status=SKIP`
  - reason: confirm phrase missing/mismatch

But the user terminal has since proven KIS env exists:

- `print_env_readiness.py`: PASS
- KIS virtual token issued
- KIS investor daily and price snapshot smoke PASS

질문 7:
실계좌 제외, 모의투자 완성 기준에서 다음 세 가지 중 무엇을 반드시 해야 하나요?

1. `paper_trading_smoke.py --action balance`
2. `paper_trading_smoke.py --action submit-probe --confirm-phrase PAPER_ORDER_OK`
3. `paper_trading_smoke.py --action order-history`

각각의 risk, 필요성, 대체 가능한 read-only evidence를 평가해주세요.

질문 8:
모의 주문 probe를 실제로 넣는 것이 종합설계 마감 관점에서 필요한가요?
필요하다면 안전 조건을 명시해주세요.
필요하지 않다면 어떤 evidence로 paper trading completion을 대체할 수 있나요?

## Mode B Scheduler 구현 caveat

`feature_list.json`은 Sprint 3 Mode B를 done으로 표시합니다.
하지만 `new/src/mode_b/scheduler.py`에는 아직 다음 stub 성격이 남아 있습니다.

- `stage_1_performance_analysis`: returns `{"status": "stub"}`
- `stage_5_agent_self_improvement`: returns `{"status": "stub"}`
- `stage_7_deploy`: deployer 미주입 시 `stub_no_deployer`, `not_configured`

반면 현재 실제 gate/evidence 경로는 다음 CLI 기반으로 움직입니다.

- `new/scripts/c12_recheck_runner.py`
- `new/scripts/deploy_candidate.py`
- `new/scripts/service_policy_replay.py`
- `new/scripts/service_readiness_status.py`
- `new/scripts/prelive_gate.py`

질문 9:
15일까지의 MVP completion 기준에서 ModeB Scheduler 본체 stub를 반드시 없애야 하나요?
아니면 CLI orchestration evidence path를 "operator-run Mode B pipeline"으로 인정해도 되나요?
인정한다면 문서/발표에서 어떻게 설명해야 하나요?

## LLM Router caveat

`new/src/orchestration/llm_router.py`는 Kanana-o, GPT-4o, fallback, circuit breaker를 구현했습니다.
하지만 실제 provider smoke evidence는 현재 요약에 없습니다.
코드에는 `allow_mock_provider`가 있고, API key/url 누락 시 graceful failure가 있습니다.

질문 10:
실계좌 제외 마감 기준에서 LLM Router는 실제 API smoke가 필수인가요?
아니면 Hot Path 비LLM, Cold Path 구조 및 fallback/circuit breaker unit test로 충분한가요?
필수라면 어떤 최소 smoke prompt와 evidence를 생성해야 하나요?

## BE 연결 caveat

현재 BE 관련 evidence:

- `new/GPT/ai_be_openapi_reviewed.yaml`
  - AI-BE integration OpenAPI spec
  - PortfolioPatch, FinalDecision, ExecutionFeedback, SharedMessage, AgentReport endpoints
- `service_readiness_status.py`
  - read-only local artifact status endpoint semantics
  - `safe_to_show_dashboard=true`
  - `safe_to_enable_order_actions=false`
  - `safe_to_enable_live_actions=false`

질문 11:
"BE 연결까지 모의투자로 진행"이라는 목표에서 현재 상태는 충분한가요?
아니면 실제 FastAPI/mock server endpoint, sample request/response JSON, 또는 BE contract test가 더 필요하다고 보나요?
15일까지 가장 작은 완성 단위를 제안해주세요.

## 모델 성능과 overfit caveat

LGBM trainer validation proxy:

- `n_train_rows=448639`
- `sr=24.8898`, `ir=24.8898`, `ic=0.1031`
- `metric_scope=trainer_validation_proxy`
- deploy_quality=false

Backtest stale report:

- `sr=2.7049`, `ic=0.0596`, leakage pass
- feature_quality stale gate fail

질문 12:
성능 스토리텔링을 어떻게 해야 하나요?
trainer proxy 수치는 overfit risk가 있으므로 어떤 문장을 피해야 하고, C12 real backtest와 paper evidence가 나온 후 어떤 문장으로 바꾸는 것이 맞나요?

## Dynamic Universe caveat

Sprint 5 dynamic universe는 feature_list상 done입니다.
하지만 마감 기준에서 실제 Mode A/Paper auto path와 연결되어 있는지는 불확실합니다.

질문 13:
Dynamic Universe를 "구현 완료"라고 말하려면 어떤 최소 evidence가 필요하나요?
unit tests와 manager lifecycle artifact로 충분한가요, 아니면 paper/Mode A cycle에서 watch -> admission -> holdings -> exit sample report가 필요하나요?

## 마감 의사결정 요청

당신의 답변은 다음 형식으로 주세요.

1. `MUST BEFORE 2026-05-15`
   - 최대 7개만.
   - 각 항목은 "왜 필수인지", "실행 명령 또는 산출물", "성공 기준" 포함.

2. `SHOULD IF TIME`
   - 최대 7개만.
   - 발표 리스크를 낮추지만 필수는 아닌 것.

3. `DO NOT DO`
   - 마감 전 하지 말아야 할 것.
   - 예: 실계좌 전환, production registry 수동 승격, gate threshold 낮추기, 무리한 DC scraping 등.

4. `CLAIM WORDING`
   - 발표/보고에서 써도 되는 문장 5개.
   - 쓰면 안 되는 문장 5개.

5. `FINAL VERDICT`
   - "실계좌 전환 제외 2026-05-15 completion 가능성"을 0-10으로 점수화.
   - 점수 근거.
   - 가장 위험한 blocker 3개.

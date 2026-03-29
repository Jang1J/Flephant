# W1~W11 AI#1 + AI#2 전체 매핑 표

> 기준일: 2026-03-29
> AI_파트_분배_v1.md 기반 매핑

---

## Phase 1: W1~W4 (중간발표까지)

| Week | AI #1 산출물 | AI #2 산출물 | 상태 |
|------|------------|------------|------|
| **W1** | universe_v1.csv, DMP/TTP/RC/FDC schema, risk_policy_v0.yaml, .env API keys | SC schema, label 확정 | DONE |
| **W2** | DMP pipeline (build_daily_market_packet.py), TTP (build_ticker_text_pack.py), DQR, PIT tagging, Risk v1 (run_risk_engine.py), LLM router v0 | manual factor (return_5d/20d, rsi, volume_ratio), SC 샘플 | DONE |
| **W3** | backfill (backfill_packets.py, 401일), stress gate, UQ tail cap, FDA v1 (final_decision_agent.py), UQ calibration v0 | SC v1 mock, Synthesizer v1 | DONE |
| **W4** | fallback 강건화, known limitations | 성과 스냅샷, 시각화 | DONE |

### W1~W4 산출물 매핑

| Artifact | Producer | Consumer | 파일 | 상태 |
|----------|----------|----------|------|------|
| DailyMarketPacket | AI #1 | AI #2 | jobs/build_daily_market_packet.py → artifacts/daily_market_packet/ | 401일 축적 |
| TickerTextPack | AI #1 | AI #2 | jobs/build_ticker_text_pack.py → artifacts/ticker_text_pack/ | DONE |
| StrategyCard (mock) | AI #1 | AI #1 | jobs/run_e2e_pipeline.py (mock SC) | Phase 1 mock |
| RiskCard + COP | AI #1 | AI #1 | jobs/run_risk_engine.py → artifacts/risk_card/, candidate_order_plan/ | DONE |
| FinalDecisionCard | AI #1 | Service | agents/final_decision_agent.py → artifacts/final_decision_card/ | DONE |
| PortfolioState | AI #1 | AI #1 | jobs/portfolio_manager.py → artifacts/portfolio_state/ | DONE |
| UQ Model | AI #1 | AI #1 | jobs/uq_calibration.py → models/uq_model_v0.pkl | v0 synthetic |

---

## Phase 1.5: AI #1 추가 구현 (2nd Strategy)

| 항목 | 파일 | 상태 |
|------|------|------|
| KR-Rebound-CNN v1.0 | models/rebound_cnn/ (model.py, dataset.py, train.py, evaluate.py) | DONE |
| Committee v1.1 (ElasticNet+CNN) | models/rebound_cnn/committee.py | DONE |
| Shared preprocess.py | models/rebound_cnn/preprocess.py | DONE (Step 0-1) |
| SC Emitter (rebound) | jobs/build_strategy_card_rebound.py | DONE |
| Variant Publisher | jobs/publish_strategy_variant.py | DONE |
| Replay Backtest | jobs/run_backtest_replay.py | DONE |
| Strategy Profiles | config/strategy_profiles.yaml | DONE |
| 설계서 | 문서/KR_Rebound_CNN_v1_설계서.md | DONE |

---

## Phase 2: W5~W8 (AI #2 전체 구현)

| Week | AI #1 산출물 | AI #2 산출물 | 상태 |
|------|------------|------------|------|
| **W5** | validate_ai2_handoff.py 승격, real SC 연결 | real SC v1, 3-branch output (quant/news/full) | DONE |
| **W6** | FDA backtest_summary 연결, backtest_summary_contract.json | run_backtest.py, FailureCaseCard, baseline 6종, walk-forward+purge+embargo | DONE |
| **W7** | KIS mock trading (paper_trading_executor.py) | daily SC 공급 안정화 | DONE (기존) |
| **W8** | UQ tail cap 정식 연결 | MLF-lite, UMI-lite, LightGBM v2 retrain | DONE |

### W5~W8 산출물 매핑

| Artifact | Producer | Consumer | 파일 | 상태 |
|----------|----------|----------|------|------|
| StrategyCard (real, momentum) | AI #2 | AI #1 | jobs/build_strategy_card_momentum.py → artifacts/strategy_card_variants/momentum/ | DONE |
| StrategyCard (real, rebound) | AI #1 | AI #1 | jobs/build_strategy_card_rebound.py → artifacts/strategy_card_variants/rebound/ | DONE |
| SC quant_only variant | AI #2 | Ablation | → artifacts/strategy_card_variants/quant_only/ | DONE |
| SC news_only variant | AI #2 | Ablation | → artifacts/strategy_card_variants/news_only/ | DONE |
| BacktestReport | AI #2 | AI #1 | jobs/run_backtest.py → reports/backtest/ | DONE |
| FailureCaseCard | AI #2 | AI #1 | jobs/run_backtest.py → reports/backtest/FCC-*.json | DONE |
| backtest_summary | AI #2 | FDA | schemas/backtest_summary_contract.json | DONE |
| LightGBM model | AI #2 | AI #2 | models/strategy_model/lgbm_ranker.py → artifacts/strategy_model/ | DONE |
| Feature Importance | AI #2 | 분석 | artifacts/strategy_model/feature_importance_*.json | DONE |

### MLF-lite + UMI-lite 피처 매핑

| 피처 | 카테고리 | 계산 | 파일 |
|------|---------|------|------|
| return_5d | MLF-lite | 5일 수익률 | lgbm_ranker.py |
| return_20d | MLF-lite | 20일 수익률 | lgbm_ranker.py |
| return_60d | MLF-lite | 60일 수익률 (DMP return_5d/20d 조합) | lgbm_ranker.py |
| period_agreement_score | MLF-lite | sign(ret_5d)+sign(ret_20d)+sign(ret_60d) 합 | lgbm_ranker.py |
| stock_sync_score | UMI-lite | 개별 종목-유니버스 평균 수익률 상관 | lgbm_ranker.py |
| market_synchronism | UMI-lite | breadth 가중 동기화 점수 | lgbm_ranker.py |
| rational_price_gap | UMI-lite | 섹터 평균 close/sma20 대비 괴리도 | lgbm_ranker.py |

---

## Phase 3: W9~W11 (운영/검증/발표)

| Week | AI #1 | AI #2 | 상태 |
|------|-------|-------|------|
| **W9** | 5거래일 paper trading ops, ops_metrics | ablation 4종 실행, dashboard payload | 예정 |
| **W10** | 최종 E2E 안정화, FDA 정제 | 비교표 정리, 시각화, feature importance | 예정 |
| **W11** | manifest freeze, runbook 확정, 발표 리허설 | 최종 리포트, demo 시나리오 | 예정 |

---

## 하네스 시스템 매핑

### 에이전트 (8개)

| 에이전트 | 역할 | AI # | 파일 |
|---------|------|------|------|
| reviewer | 코드 리뷰 | 공통 | .claude/agents/reviewer.md |
| fixer | 코드 수정 | 공통 | .claude/agents/fixer.md |
| runner | 파이프라인 실행 | 공통 | .claude/agents/runner.md |
| qa-inspector | 정합성 검증 | 공통 | .claude/agents/qa-inspector.md |
| modeler | KR-Rebound-CNN | AI #1 | .claude/agents/modeler.md |
| doc-writer | 문서 작성 | 공통 | .claude/agents/doc-writer.md |
| analyst | 성능 분석 | 공통 | .claude/agents/analyst.md |
| gpt-feedback-tracker | GPT Pro 피드백 | 공통 | .claude/agents/gpt-feedback-tracker.md |

### 스킬 (10개)

| 스킬 | 용도 | 파일 |
|------|------|------|
| /code-review | 코드 리뷰 | .claude/skills/code-review/SKILL.md |
| /code-fix | 코드 수정 | .claude/skills/code-fix/SKILL.md |
| /run-pipeline | 파이프라인 실행 | .claude/skills/run-pipeline/SKILL.md |
| /validate | 정합성 검증 | .claude/skills/validate/SKILL.md |
| /smoke-test | 커넥터 테스트 | .claude/skills/smoke-test/SKILL.md |
| /build-model | CNN 모델 구축 | .claude/skills/build-model/SKILL.md |
| /elephant-ops | 복합 작업 | .claude/skills/elephant-ops/SKILL.md |
| /agent-research | 논문 조사 | .claude/skills/agent-research/SKILL.md |
| /worklog | 작업 로그 | .claude/skills/worklog/SKILL.md |
| /paper-trending | 논문 트렌드 | .claude/skills/paper-trending/SKILL.md |

---

## 전체 파일 트리 (신규/수정)

```
Elephant_Lab/
├── models/
│   ├── rebound_cnn/
│   │   ├── preprocess.py          [NEW] shared preprocessing
│   │   ├── dataset.py             [MOD] import from preprocess
│   │   └── config.yaml            [MOD] dimension comments
│   └── strategy_model/
│       ├── __init__.py            [NEW]
│       ├── config.yaml            [NEW] LightGBM hyperparams
│       ├── lgbm_ranker.py         [NEW] LightGBM + MLF-lite + UMI-lite
│       ├── news_strategy.py       [NEW] news signal extraction
│       └── synthesizer.py         [NEW] quant+news synthesis
├── jobs/
│   ├── build_daily_market_packet.py  [MOD] JSON regex fix
│   ├── build_strategy_card_rebound.py [MOD] preprocess import
│   ├── build_strategy_card_momentum.py [NEW] real SC builder
│   └── run_backtest.py            [NEW] full backtest + baselines
├── agents/
│   └── final_decision_agent.py    [MOD] warning_reason + JSON regex
├── schemas/
│   ├── final_decision_card.json   [MOD] warning_reason field
│   └── failure_case_card.json     [NEW]
├── .claude/
│   ├── agents/
│   │   ├── doc-writer.md          [NEW]
│   │   ├── analyst.md             [NEW]
│   │   └── gpt-feedback-tracker.md [NEW]
│   └── skills/
│       ├── agent-research/SKILL.md [NEW]
│       ├── worklog/SKILL.md       [NEW]
│       └── paper-trending/SKILL.md [NEW]
├── 문서/
│   ├── presentation_guide.md      [NEW]
│   └── w1_w11_mapping.md          [NEW]
└── CLAUDE.md                      [MOD] harness section
```

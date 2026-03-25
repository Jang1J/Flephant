# AI 파트 분배 제안서

> 본 문서는 프로젝트 제안서 v11을 기준으로 작성한 **AI 파트 전용 역할 분담 + 실행계획 통합 문서**이며, AI #1 / AI #2의 ownership, artifact handoff, W1~W11 실행 로드맵을 정의한다.

> 기준일: 2026-03-23(일)
> 중간발표: 2026-04-17(금)
> 최종발표: 2026-06-05(금)

---

## 0. 왜 이렇게 나누는가

우리 AI 파트의 핵심 구조는 **5-agent 파이프라인**이다.

```
Data → Strategy → Risk → Backtest → Final Decision
```

이 구조에서

- **AI #1**이 파이프라인의 **입구(Data)**와 **출구(Risk → Final Decision)**를 맡고
- **AI #2**가 파이프라인의 **중간 엔진(Strategy → Backtest)**을 맡으면,

각자의 역할이 겹치지 않으면서도 artifact 기반으로 정확하게 연결된다.
즉 데이터는 **AI #1 → AI #2 → AI #1** 순서로 흐르며, 이 때문에 두 사람의 관계는 단순 병렬이 아니라 **교차 의존형 협업 구조**가 된다.

중간발표 이후 W5~W11에서도 이 기본 구조는 유지된다.
달라지는 것은 역할 분담 자체가 아니라, **real integration, feature 고도화, 검증/운영/발표 패키징**이 추가된다는 점이다.
특히 W5 이후에는 PortfolioState, HourlyMarketPatch, BacktestReport, FailureCaseCard, UQModel 같은 운영/검증 artifact가 본격적으로 들어오면서, AI #1은 **online serving / governance**, AI #2는 **offline modeling / validation** 역할을 더 분명하게 갖게 된다.

---

## 1. 역할 분담 근거

### AI #1 (장재원): Data Agent + Risk Agent + Final Decision Agent + UQ calibration

**왜 이 조합인가:**

① **Data Agent와 Final Decision Agent는 파이프라인의 양 끝**이다.
한 사람이 입구와 출구를 동시에 잡으면 전체 데이터 흐름을 통제할 수 있고, 입력 이상이 최종 판단 오류로 이어질 때 E2E 디버깅이 빠르다.

② **Risk Agent는 규칙 기반 + 운영 정책 중심**이다.
시장 스트레스, volatility, cash ratio, turnover, stop-loss, uncertainty tail cap 같은 값은 Data와 직접 연결되므로, Data Agent를 만드는 사람이 Risk까지 맡는 것이 자연스럽다.

③ **Final Decision Agent는 Kanana-o를 직접 사용**한다.
prompt engineering, fallback, explanation generation, contract 관리까지 한 사람이 잡아야 일관성이 높다.

④ **UQ calibration의 운영 책임은 Risk와 붙어 있다.**
uncertainty 모델 자체의 offline 학습은 AI #2가 지원하지만, P85 threshold를 어디에 두고 tail-cap policy를 어떻게 쓸지는 AI #1이 직접 잡아야 Risk 정책과 일관된다.

**핵심 키워드: online serving + governance + pipeline endpoints + risk policy**

**Kanana-o LLM 확장 담당 항목 (AI #1):**

| # | 적용 위치 | 기능 | artifact 필드 |
|---|----------|------|--------------|
| 1 | TickerTextPack | 뉴스 LLM 분석 (요약 + 감성 점수 + 토픽) | `llm_news_analysis` |
| 2 | TickerTextPack | 공시 LLM 해석 (주가 영향 판단 + 리스크 플래그) | `llm_disclosure_analysis` |
| 3 | DailyMarketPacket | 일일 시장 코멘터리 (시황 분석 + 시장 분위기) | `llm_market_commentary` |
| 4 | RiskCard | 리스크 내러티브 (Regime 근거 한국어 설명) | `llm_risk_narrative` |
| 5 | FinalDecisionCard | 갈등 해소 분석 (신호 갈등 시 상세 분석 + 권장 액션) | `llm_conflict_analysis` |

모든 LLM 호출은 선택적(optional). 실패해도 파이프라인 정상 동작. Kanana-o primary, GPT-4o fallback 자동 전환.

### AI #2 (팀원): Strategy Agent (Quant + News + Synthesizer) + Backtest Agent

**왜 이 조합인가:**

① **Quant Strategy가 프로젝트의 ML 핵심 엔진**이다.
LightGBM ranker 학습, feature engineering(manual → MLF-lite → UMI-lite), cross-sectional ranking label 설계는 전부 여기서 일어난다.

② **News/Market Strategy는 Quant와 분리되기 어렵다.**
Quant가 shortlist를 만들고, 그 shortlist에 대해서만 News/Market Strategy가 해석을 수행하므로, shortlist를 만든 사람이 뉴스 해석과 General Synthesizer까지 함께 맡는 것이 더 자연스럽다.

③ **Backtest Agent는 Strategy의 검증 도구다.**
어떤 전략이 실패했는지, 어떤 baseline보다 나았는지, failure case를 어떻게 고칠지를 가장 잘 아는 사람은 전략을 만든 사람이다. 그래서 Backtest ownership은 AI #2에 두는 것이 맞다.

④ **UQModel도 Backtest 과정에서 학습/보정된다.**
historical uncertainty score, calibration curve, reliability diagram, no-UQ ablation은 offline validation에 가깝기 때문에 AI #2가 만드는 것이 효율적이다.

**핵심 키워드: offline modeling + validation + ML core engine + evaluation**

---

## 2. Producer / Consumer 관계

### 핵심 인터페이스 (W1~W4부터 유지)

| Artifact | Producer | Consumer | 설명 |
|----------|----------|----------|------|
| DailyMarketPacket | AI #1 | AI #2 | 유니버스 전체 daily backbone packet |
| TickerTextPack | AI #1 | AI #2 | macro / sector / target 3-level text pack |
| StrategyCard | AI #2 | AI #1 | 최종 전략 신호 |
| UQModel | AI #2 (offline) | AI #1 (online) | uncertainty score inference용 |

### 확장 인터페이스 (W5~W11 추가)

| Artifact | Producer | Consumer | 설명 |
|----------|----------|----------|------|
| BacktestReport | AI #2 | AI #1 | FDA가 신뢰도 판단에 참조 |
| FailureCaseCard | AI #2 | AI #1 | Risk/FDA와 failure trace 연결 |
| backtest_summary | AI #2 | AI #1 | FDA가 직접 읽는 요약형 handoff |
| QuantShortlist | AI #2 | AI #2 내부 | News Strategy 대상 결정용 |
| PortfolioState | AI #1 | AI #1 내부 | 상태성 운영 artifact (진입가/보유일수/turnover) |
| HourlyMarketPatch | AI #1 | AI #1 중심 | 장중 1시간 delta artifact |
| RiskCard / COP | AI #1 | AI #1 내부 | sizing / gating / execution |
| FinalDecisionCard | AI #1 | Service Layer | 최종 사용자 출력 |

---

## 3. Phase 매핑

| Phase | 기간 | 핵심 |
|-------|------|------|
| **Phase 1** | W1~W4 | manual factors + macro → LightGBM baseline, 3-level text pack, core risk rules, artifact 기반 E2E |
| **Phase 2** | W5~W8 | real SC 통합, Backtest 정식 실행, MLF-lite + 최소 UMI-lite + UQ v1, mock trading gateway |
| **Phase 3** | W9~W11 | 5거래일 paper trading ops, ablation/비교표, 서비스 handoff, 최종 freeze |

---

## 4. W1~W4 요약 (중간발표까지)

핵심 목표: **"5-agent 시스템이 artifact 기반으로 실제 돌기 시작했다"**를 보여주는 것.

| 주차 | AI #1 | AI #2 | 공동 종료 기준 |
|------|-------|-------|-------------|
| **W1** | 유니버스, API smoke test, DMP/TTP/RC/FDC schema, Risk policy, UQ I/O spec, Kanana 접근권 확인 | label 확정, SC/QuantShortlist schema, manual factors, LightGBM 환경, Backtest protocol v0 | 4개 핵심 인터페이스 schema 합의 |
| **W2** | DMP, TTP, DQR, PIT tagging, Risk v1, LLM router v0 | manual factor 계산, LightGBM v0, shortlist, News prompt v0, SC 샘플 | 파일 기반 mock E2E 1회 성공 |
| **W3** | backfill, stress gate, UQ tail cap prototype, RiskAuditLog, FDA v1, UQ calibration v0 | SC v1, Synthesizer v1, 제한 구간 backtest v0, baseline 2종 | 5거래일 replay 성공 |
| **W4** | fallback 강건화, sample cards, known limitations, 리스크 사례 | 성과 스냅샷, FailureCase, 시각화 | 리허설 1회 + 데모 대체 시나리오 준비 |

---

## 5. W5~W11 상세 계획 (중간발표 이후)

### 순서 원칙

> **검증(Backtest/UQ freeze)을 실행(paper trading)보다 먼저 고정한다.**

따라서: W6 = Backtest freeze → W7 = mock trading gateway 순서로 배치한다.

---

### W5 (04/20 ~ 04/26) — Real SC 통합 + Live LLM + Contract Freeze

**AI #1**
- validate_ai2_handoff.py를 공식 통합 테스트 harness로 승격
- SC-{date}.json, SC-{date}-*.json 자동 감지/검증
- Kanana-o live smoke test
- GPT-4o fallback smoke test
- real StrategyCard[]를 FDA runtime에 실제 연결
- known_limitations.md 업데이트
- strategy_loader 정리

**AI #2**
- real StrategyCard v1 생성
- **quant-only, news-only, full branch output 3종 저장** (W10 ablation 대비)
- StrategyCard schema/필드 freeze
- real SC 기반 replay 샘플 생성

**공동 종료 기준**
- real SC 기반 E2E 1회 성공
- real SC 기반 5거래일 replay 성공
- strategy_contract_v1.md 합의 완료

---

### W6 (04/27 ~ 05/03) — Backtest Agent v1 + DEV/FINAL Freeze + Baseline Suite

**Owner: AI #2 중심, AI #1은 consumer/연결 담당**

**AI #2**
- run_backtest.py 정식 구현
- BacktestReport, FailureCaseCard schema 작성
- walk-forward + purge + embargo (60일 기본 / 90일 robustness)
- DEV/FINAL split freeze
- **baseline 6종 실행:**
  - KOSPI200 / equal-weight / pure-quant / pure-news / simple momentum / no-UQ risk
- FailureCaseCard 1차 생성
- feedback retry 1회
- dev_final_split.yaml, baseline_table_v1.csv 작성

**AI #1**
- FDA에 backtest_summary 정식 연결
- **backtest_summary_contract.json** 작성 (FDA↔Backtest handoff schema)
  - 최소 필드: status, win_rate_hint, baseline_delta, failure_tags, confidence_band
- RiskAuditLog와 FailureCaseCard 근거 연결
- No-UQ Risk 토글 경로 정리

**공동 종료 기준**
- Backtest 1회 + FINAL 1회 frozen 실행
- FailureCaseCard 1개 이상
- FDA가 backtest_summary 실제 consume
- backtest_summary_contract.json 합의 완료
- baseline 6종 결과 확보

---

### W7 (05/04 ~ 05/10) — 증권사 모의투자 Gateway + Reconciliation

> 주의: "실계좌 연동"이 아닌 **"내부 mock/paper trading gateway"** 수준으로 정의한다.

**AI #1**
- KIS 모의투자 API 연결
- paper_trading_executor.py (주문/잔고/체결 조회)
- paper_trading_log.json, broker_reconcile_report.json
- dry_run_only flag, max_order_notional, order journal

**AI #2**
- W6 기준 Backtest / SC 안정화
- daily SC 공급 체계 확정

**공동 종료 기준**
- KIS 모의투자 환경에서 매수/매도/취소 1회씩 성공
- reconcile PASS 1회

---

### W8 (05/11 ~ 05/17) — MLF-lite + 최소 UMI-lite + UQ v1

**AI #2**
- MLF-lite feature bank: multi_period_5d / 20d / 60d, period_agreement_score
- 최소 UMI-lite: stock_sync_score, market_synchronism, rational_price_gap
- LightGBM retrain (v1 → v2)
- feature importance 비교표 작성
- **(Optional) PatchTST-lite embedding 실험:**
  - PatchTSTModel로 최근 48~72 trading bars → hidden state 추출
  - PCA 8~16차원 축소 → LGBM feature에 concat
  - LGBM vs LGBM+PatchTST embedding 비교
  - 주의: leakage 방지 (walk-forward 내 train/test split 기준 fit/transform 분리)
  - PatchTST-lite 실험은 core milestone에 영향 없이 별도 브랜치에서 수행

**AI #1**
- online UQ integration 정리
- uncertainty_score → Risk/FDA trace 강화
- P85 tail cap audit log 고도화
- UQ threshold 재검증

**공동 종료 기준**
- StrategyCard v2 산출 (MLF-lite + UMI-lite 반영)
- v1 대비 성과 비교표 1개
- feature importance 차트 1개

---

### W9 (05/18 ~ 05/24) — 5거래일 Paper Trading Ops + Daily/Hourly 운영 안정화

**AI #1**
- daily_auto_trading.py
- run_intraday_cycle.py 정식 운영
- DMP → TTP → SC → Risk(PFS) → FDA → FDC → PFS update 전체 연결
- 5거래일 mock 운영
- 운영 메트릭 수집:
  - order success / reject rate, LLM fallback rate, daily turnover, cash ratio drift
  - reconciliation mismatch rate, p50 / p95 latency, emergency veto count
- **ops_metrics_daily.json** 생성 (위 7종 메트릭 일별 기록)
- **ops_report_weekly.md** 작성 (5거래일 운영 요약)

**AI #2**
- daily SC 공급 (5거래일)
- critical bugfix만 수행
- strategy drift note 기록

**공동 종료 기준**
- 5거래일 mock 운영 완료
- ops report 1개 이상
- intraday cycle 1일 이상 정상 동작
- 운영 메트릭 7종 수집 완료

---

### W10 (05/25 ~ 05/31) — Ablation / 비교표 / BE Handoff Payload

**Must-have ablation:**

| # | 실험 | 비교 대상 | 근거 |
|---|------|----------|------|
| 1 | pure-quant vs quant+news | Quant만 vs full | LLM reasoning 기여도 |
| 2 | UQ on vs No-UQ Risk | tail cap 있음 vs 없음 | Risk 2-tier 기여도 |
| 3 | PFS(stateful) vs stateless | 진입가 기반 vs return_5d | PortfolioState 기여도 |
| 4 | semantic pairing vs keyword pairing | 3-level TTP vs keyword match | TickerTextPack 설계 정당화 |

**Optional ablation (시간 여유 시에만 수행, core milestone에 영향 없음):**

| # | 실험 | 근거 |
|---|------|------|
| 5 | shuffled news subset | LLM memorization 방어 |
| 6 | anonymized ticker subset | BlindTrade식 검증 |
| 7 | LGBM vs LGBM+PatchTST embedding | 시계열 임베딩 기여도 (W8 실험 결과 시, 별도 브랜치에서 수행) |

**AI #1**
- Risk/UQ/PFS 관련 ablation 실행 (#2, #3)
- dashboard_payload_contract.json 초안
- BE handoff payload: recommendation_history, portfolio_nav, paper_trading_log, risk_trace

**AI #2**
- 전략 성과 분석 (#1 pure-quant vs full)
- semantic pairing vs keyword pairing 실험 (#4)
- FailureCaseCard Top 3 정리
- 백테스트 차트 (cumulative return, drawdown, hit ratio)

**공동 종료 기준**
- ablation 표 4개 이상 (must-have 전부)
- 차트 5개 이상
- BE handoff payload 1회 PASS

---

### W11 (06/01 ~ 06/05) — Code Freeze / Manifest / Runbook / 최종발표

**AI #1**
- code freeze
- .env, .DS_Store, .claude 정리
- demo_runbook.md, ops_runbook.md, schema_manifest.md
- 데모 A/B 시나리오 (정상 시장 vs 스트레스 시장)

**AI #2**
- final performance summary
- 논문 매핑 표
- experiment summary

**공동**
- experiment_manifest.md + experiment_manifest.json (machine-readable)
- final_artifact_bundle.zip
- 리허설 2회
- 데모 A/B 검증
- 발표 슬라이드 최종 확정

**종료 기준**
- 리허설 2회, 데모 A/B PASS, artifact bundle 완성, 슬라이드 lock

---

## 6. 핵심 Checkpoint

| 날짜 | 체크포인트 |
|------|----------|
| **04/17(금)** | 중간발표 |
| **04/24(금)** | real SC 기반 E2E + 5일 replay 성공, SC contract freeze |
| **05/01(금)** | Backtest 1회 + FINAL 1회 + baseline 6종 완료 |
| **05/08(금)** | KIS 모의투자 주문 3종 성공 + reconcile PASS |
| **05/15(금)** | MLF-lite + UMI-lite 반영 SC v2 + 성과 비교표 |
| **05/22(금)** | 5거래일 paper trading 완료 + ops report |
| **05/29(금)** | ablation 4종 + BE payload PASS |
| **06/03(화)** | 데모 freeze, 발표 자료 lock |
| **06/05(금)** | 최종발표 |

---

## 7. UQ Calibration 협업

UQ는 cross-cutting module로 다룬다.

- **AI #2**: Backtest에서 historical uncertainty score 산출, calibration curve 실험, UQModel 생성
- **AI #1**: P85 threshold 결정, tail-cap policy, online inference, Risk Agent 통합

**한 줄 정리:** AI #2가 모델을 만들고, AI #1이 정책을 운영한다.

---

## 8. 이번 학기 AI 파트에서 하지 않을 것

- RL allocator / 강화학습
- 실계좌 실제 자동매매 (모의투자 gateway까지만)
- full graph relation model
- full LoRA fine-tuning
- high-frequency trading
- agentic factor mining loop
- full anonymized ticker ablation
- Similar Case Retriever full 구현
- xLSTM / PatchTST를 **core backbone으로 교체** (optional feature experiment로만)

---

## 9. AI 완료 조건 (8개)

W11 종료 시 아래가 **모두 충족**되면 AI core는 완료다.

| # | 완료 조건 | 해당 주차 |
|---|----------|----------|
| 1 | real SC integration이 main runner에서 PASS | W5 |
| 2 | BacktestReport + FailureCaseCard + FDA backtest_summary 연결 완료 | W6 |
| 3 | MLF-lite + 최소 UMI-lite 반영 완료 | W8 |
| 4 | PFS/HMP/FDA가 daily + hourly 운영까지 일관되게 동작 | W9 |
| 5 | KIS mock trading + reconciliation 완료 | W7 |
| 6 | baseline 6종 + ablation 4종 + dashboard payload 완료 | W10 |
| 7 | manifest / runbook / doc sync freeze 완료 | W11 |
| 8 | 최종 발표 데모 A/B 완료 | W11 |

이 8개가 닫히면, 남는 것은 **optional/범위 밖**이다.

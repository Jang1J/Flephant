# 종합설계 프로젝트 제안서 v11 (최종)
## KOSPI 기반 멀티에이전트 트레이딩 알고리즘 및 모의 자동매매 서비스 프로토타입

---

## 1. 프로젝트 개요

본 프로젝트는 KOSPI 대형주를 대상으로,
데이터 수집 → 전략 판단 → 리스크 통제 → 백테스트 → 최종 의사결정의 전 과정을
여러 역할의 에이전트가 분담하여 협업하는 멀티에이전트 트레이딩 알고리즘을 개발하는 것을 목표로 한다.

또한 개발된 알고리즘을 단순 실험으로 끝내지 않고,
사용자가 추천 결과와 모의 자동매매 흐름을 확인할 수 있는 앱/서비스 프로토타입으로 연결하고자 한다.

즉, 본 프로젝트의 중심은 **금융 트레이딩 알고리즘 엔진**이며,
앱은 이를 사용자에게 보여주고 활용하게 만드는 **서비스 레이어**로 설계한다.

---

## 2. 문제의식

기존의 단일 모델 기반 투자 시스템은 다음과 같은 한계를 가진다.

- 가격·거래량 같은 정량 정보와 뉴스·공시 같은 정성 정보를 함께 반영하기 어려움
- 모델이 왜 그런 판단을 내렸는지 설명하기 어려움
- 리스크 관리가 전략과 독립적으로 설계되지 않는 경우가 많음
- 과거 데이터에서 실제로 어느 정도 성과가 났는지 구조적으로 검증하기 어려움
- 최종적으로 사용자에게 "그래서 지금 무엇을 해야 하는가"를 명확히 전달하기 어려움

이에 본 프로젝트는 트레이딩을 하나의 예측 문제가 아니라,
**역할이 분리된 협업형 의사결정 시스템**으로 재구성한다.

### 문제 정의

본 프로젝트의 기본 예측 대상은 **t일 장 마감 시점까지 관측 가능한 정보만을 활용하여 t+1 거래일에 실행 가능한 종목별 상대 매력도(score)와 BUY/HOLD/SELL action을 산출하는 것**이다. 전략 예측은 개별 종목의 절대 방향성 분류보다 고정 유니버스 내 **상대 ranking 문제**로 정의하며, Top-K 종목을 선별한 뒤 Risk Agent가 최종 비중을 조정한다.

---

## 3. 핵심 설계 원칙

본 프로젝트는 최신 2025–2026 문헌 분석을 바탕으로, 아래 6가지 설계 원칙을 일관되게 적용한다.

| 원칙 | 적용 방식 | 근거 |
|------|----------|------|
| **예측과 설명 분리** | score/action의 1차 산출은 정량/규칙 기반, LLM은 텍스트 해석과 설명 생성 중심 | AI-Trader, StockBench: LLM 자유형 트레이딩의 한계 |
| **방향 판단과 비중 조정 분리** | Strategy는 방향과 우선순위, Risk는 size/gate/cap | FinPos: direction과 position adjustment 분리 |
| **raw text 직접 투입 최소화** | Data Agent가 TickerTextPack으로 정리한 뒤 Strategy에 전달 | FinTexTS: multi-level semantic pairing |
| **raw 시계열 LLM 직접 입력 금지** | 숫자 시계열은 template/annotation으로 요약 후 LLM에 제공 | VTA: textual annotation + backbone 결합 |
| **운영과 연구 분리** | online serving은 AI #1, offline calibration/backtest는 AI #2 | R&D-Agent-Quant: research–development feedback loop |
| **검증 우선** | walk-forward + DEV/FINAL + purge/embargo + leakage ablation | When Alpha Breaks, BlindTrade |

---

## 4. 설계 방향

초기 검토 단계에서는 전략 에이전트를 더 세분화한 구조도 고려했으나,
이번 학기에는 범위를 줄이고 실제 구현 가능성을 높이기 위해 아래 방향을 택한다.

- 전략 에이전트 4개를 모두 독립 구현하지 않음
- 전략 판단은 핵심 2개 축으로 단순화
  - **정량 전략 축** (Quant Strategy)
  - **뉴스/시장 전략 축** (News / Market Strategy)
- 여러 판단을 종합하는 전문가 에이전트 개념은 **Final Decision Agent로 흡수**
- 백테스트 에이전트는 단순 수익률 계산이 아니라 **성과 검증 + 실패 원인 분석 + 과최적화 방어 + 피드백 구조**까지 포함해 강화
- Risk Agent는 단순 position cap을 넘어 **전략 수준 regime gate + 종목 수준 uncertainty tail cap**의 2-tier 구조로 설계
- 즉, 아이디어는 풍부하게 가져가되, 이번 학기에는 **실제로 돌아가는 코어 시스템 구현에 집중**한다.

---

## 5. 대상 시장 및 데이터 범위

### 대상 시장
- KOSPI 대형주 20~30개 고정 유니버스
- 기준 시점의 KOSPI200 구성 종목 또는 시가총액 상위 종목으로 선정
- 본 프로젝트는 학기 내 구현 가능성을 위해 **연구용 고정 유니버스**를 사용한다. 이는 과거 각 시점의 실제 투자 가능 유니버스를 완전 재현하려는 목적이 아니라, 시스템 설계와 agent 협업 구조 검증을 위한 **engineering scope control** 목적의 선택이며, 최종 보고서에서는 이로 인한 survivorship/look-ahead 한계를 별도로 명시한다.

### 데이터 소스
| 소스 | 제공 데이터 | 비고 |
|------|-----------|------|
| KRX Open API | 유가증권 일별 OHLCV, 거래량, 시가총액 | 2010년 이후 |
| OpenDART | 공시 제목, 유형, 접수시각, 원문 | Open API |
| Naver 뉴스 검색 API | 뉴스 헤드라인, 언론사, 게시시각 | 일 25,000회 한도 |
| ECOS (한국은행) | 금리, 환율, 거시지표 | Open API |

### 데이터 시간 정렬 원칙
- **18:00 KST snapshot**: t일 장 마감(15:30) 이후 확정된 데이터만 사용
- 뉴스/공시는 장중·장후 cutoff를 적용하여 미래 정보 유입(leakage)을 차단
- 모든 레코드에 `published_at`, `available_at`, `as_of_dt`, `evidence_id` 강제 태깅
- 헤드라인 중복 제거(near-dup clustering) 및 종목 alias mapping 적용

### 운영 Cadence
- 연구/백테스트 canonical cadence는 t일 18:00 KST snapshot 기반의 **일별 pipeline**을 유지한다.
- 다만 서비스/모의 자동매매 운영에서는 DailyMarketPacket을 backbone으로 사용하고, 장중 1시간 단위의 **HourlyMarketPatch**를 추가 반영한다.

### 구현 수준
- 실제 실계좌 자동매매까지는 가지 않음
- 추천 + 모의 자동매매 프로토타입 수준 구현
- 백테스트를 통해 "이 전략이 과거에 어느 정도 수익/손실을 냈는가"를 보여주는 데 초점

---

## 6. 전체 시스템 구조

본 프로젝트는 교수님이 제시하신 4-agent 구조를 기본 골격으로 하고, 서비스 연결을 위해 그 결과를 종합하는 Final Decision Agent를 상위 의사결정 계층으로 추가한다.

### 1) Data Agent
- 가격, 거래량, 공시, 뉴스, 거시지표 수집
- **PIT-safe multimodal packager**: 종목별 입력 패킷 정리 (시간 정렬 및 leakage 방지 적용)
- TickerTextPack: 뉴스/공시를 macro / sector / target company 3-level로 semantic pairing
- 출력: `DailyMarketPacket`, `TickerTextPack`, `DataQualityReport`

### 2) Strategy Agent
- **Quant Strategy**: LightGBM ranker + MLF-lite + UMI-lite → Top 5~8 shortlist
- **News/Market Strategy**: Kanana-o 기반 뉴스/공시 해석 + template market summary
- **General Synthesizer**: deterministic pre-score 산출 후 LLM이 conflict resolution + 설명
- 출력: `StrategyCard`

### 3) Risk Agent
- **Tier 1 — Strategy-level regime gate**: 시장 전체 스트레스 판단 (green/yellow/red)
- **Tier 2 — Position-level uncertainty tail cap**: P85 이상 불확실 종목 비중 50% 삭감
- position cap / stop-loss / turnover cap / cash ratio
- 출력: `RiskCard`, `CandidateOrderPlan`

### 4) Backtest Agent
- 과거 데이터 기준 성과 검증 (walk-forward + purge/embargo)
- DEV/FINAL 분리: DEV에서만 threshold 조정, FINAL은 1회 평가
- 실패 원인 분석, baseline 비교, 과최적화 방어
- 출력: `BacktestReport`, `FailureCaseCard`

### 5) Final Decision Agent
- Strategy / Risk / Backtest 결과를 종합하는 **constrained aggregator**
- `CandidateOrderPlan`을 approve / veto 하고, 사용자에게 보여줄 설명을 생성
- LLM(Kanana-o)은 충돌 해소와 설명 생성에만 사용하며, **비중/수량을 새로 계산하지 않음**
- 출력: `FinalDecisionCard`

### 6) Service Layer
- 위 결과를 사용자에게 보여주는 앱/웹 프로토타입
- 추천 종목, 추천 이유, 모의 자동매매 흐름, 수익률 분석 제공

---

## 7. 핵심 파이프라인

```
[KOSPI 대형주 20~30개]
  t일 18:00 KST snapshot
         ↓
1) Data Agent
   - KRX OHLCV / DART 공시 / Naver 뉴스 / ECOS 거시지표
   - alias normalization + dedup + PIT 태깅
   - TickerTextPack (macro / sector / target 3-level pairing)
   - 출력: DailyMarketPacket + TickerTextPack
         ↓
2) Strategy Agent
   ┌─ Quant Strategy ─────────────────────┐
   │ Manual factors + MLF-lite + UMI-lite  │
   │ → LightGBM ranker → Top 5~8          │
   └───────────┬───────────────────────────┘
               ↓
   ┌─ News/Market Strategy ───────────────┐
   │ shortlist 종목만 분석                  │
   │ template market summary (raw TS 금지) │
   │ TickerTextPack → Kanana-o 해석        │
   │ → event_type, direction, strength     │
   └───────────┬───────────────────────────┘
               ↓
   ┌─ General Synthesizer ────────────────┐
   │ pre_risk_score = w1*quant + w2*news  │
   │ + LLM conflict resolution (Kanana-o) │
   │ → StrategyCard                        │
   └───────────┬───────────────────────────┘
               ↓
3) Risk Agent
   ┌─ Tier 1: Strategy-level ─────────────┐
   │ stress gate: green / yellow / red     │
   │ red → no new entry                    │
   │ yellow → new buy weight × 0.5        │
   └───────────┬───────────────────────────┘
               ↓
   ┌─ Tier 2: Position-level ─────────────┐
   │ position cap 20% / stop-loss -5%     │
   │ turnover cap 30% / cash ratio 10%    │
   │ uncertainty > P85 → weight × 0.5     │
   └───────────┬───────────────────────────┘
   → RiskCard + CandidateOrderPlan
         ↓
4) Backtest Agent
   - T+1 open 기준 실행 시뮬레이션
   - fee(0.015%) + slippage(10bp)
   - walk-forward + purge/embargo + DEV/FINAL
   - baseline 비교 + failure 분석
   → BacktestReport + FailureCaseCard
         ↓
5) Final Decision Agent (Kanana-o)
   - deterministic pre-check
   - conflict case만 LLM 호출
   - CandidateOrderPlan approve / veto
   - 비중/수량 수정 금지, 설명만 생성
   → FinalDecisionCard
         ↓
6) Service Layer
   - 추천 종목 + 이유 + 모의 자동매매
         ↓
  t+1 거래일 장 시작 시 실행
```

---

### AI #1 추가 구현: Backup Strategy Agent (KR-Rebound-Committee v1.1)

AI #1은 기존 계획(Data Agent + Risk Agent + FDA)에 더해, 독립형 backup Strategy Agent를 추가 구현했다.
- **전략**: KR-Rebound-Committee v1.1 — Oversold Gate → ElasticNet core + CNN confirmation → score fusion
- **설계 근거**: GPT Pro 작성 KR-Rebound-CNN v1.0 설계서 (문헌 기반, peer-reviewed journals 2025~2026)
- **통합 방식**: 기존 StrategyCard 스키마 100% 호환, variant→publish→canonical SC 구조로 기존 파이프라인에 무수정 연결
- **성과**: 401일 DMP, 14-fold genuine expanding walk-forward, 평균 Val AUC ~0.64, 실가격 backtest +3.51% (95거래일)
- KR-Rebound-Committee는 AI #2의 momentum 전략과 plugin형으로 교체 가능한 독립 모듈이다.

---

## 8. Strategy Agent 세부 구조

Strategy Agent는 단일 모델이 아니라, **두 가지 핵심 전략 축 + 종합 계층**으로 구성한다.

### 1) Quant Strategy (비LLM / ML 기반)

핵심 도구: **LightGBM LambdaMART Ranker**

| Feature Group | 구성 | 근거 논문 |
|--------------|------|----------|
| Manual factors | mom_5d/20d/60d, vol_20d, adv_20d, rsi_14, macd | 기본 기술지표 |
| MLF-lite | multi_period_5d/20d/60d, period_agreement_score | MLF |
| UMI-lite | rational_price_gap, stock_sync_score, market_synchronism, market_stress | UMI |
| Macro | usdkrw_change, base_rate_level, base_rate_change | ECOS |

- 학습 타깃: **5거래일 forward excess return rank** (주 목표), 1일은 보조 모니터, 20일은 robustness
- 출력: 종목별 cross-sectional ranking score → **Top 5~8 shortlist**
- Feature bank 구현 순서: Phase 1 manual + macro → Phase 2 MLF-lite → Phase 3 UMI-lite

**LSTM/GNN에 대한 판단**: 2026년 대형 벤치마크에서 경쟁력이 있었던 것은 plain LSTM이 아니라 VSN+LSTM, VSN+xLSTM, LSTM+PatchTST 같은 하이브리드 계열이었으며, generic TSFM도 금융에서 tree-based benchmark를 자주 넘지 못했다. 따라서 이번 학기 core는 LightGBM ranker로 두고, LSTM/GNN은 **optional shadow baseline**으로 배치한다.

### 2) News / Market Strategy (LLM 기반)

- **shortlist 종목만 분석** (전종목 LLM 처리 금지)
- Data Agent의 TickerTextPack(3-level)을 입력으로 사용
- 시장 숫자는 **template market summary**로 변환 후 LLM에 제공 (raw 시계열 직접 입력 금지)
- 출력: event_type, direction, strength, impact_horizon, confidence, evidence_ids

### 3) General Synthesizer

- 숫자 score를 먼저 **deterministic하게 합산** (`pre_risk_score = w1*quant + w2*news`)
- LLM(Kanana-o)은 **conflict case의 해소와 자연어 요약에만** 사용
- 출력: `StrategyCard`

### LLM 적용 방식

| 모듈 | 방식 | 이유 |
|------|------|------|
| **Quant Strategy** | 비LLM (ML/수학) | 팩터 계산, 랭킹 = 수치 연산 |
| **Market Analyst** | template 기반 + 필요 시 Kanana-o 보조 | 정량 지표 해석 중심 |
| **News/Disclosure Analyst** | **Kanana-o 메인** | 한국어 뉴스/공시 맥락 해석 |
| **General Synthesizer** | **Kanana-o 메인** | 한국어 종합 판단 및 설명 생성 |
| 전체 fallback | GPT-4o | 베타 안정성 대비 |

본 프로젝트에서는 Kanana-o의 **한국어 뉴스·공시 텍스트 reasoning과 설명 생성 기능만 사용**하며, 음성/이미지 모달은 범위에서 제외한다. Kanana-o는 현재 2026/5/27까지 대기 등록·선별 일정이 공지된 closed beta이므로, 사용 가능성과 응답 안정성 변동에 대비해 GPT-4o fallback을 함께 둔다.

### Kanana-o LLM 확장 적용 현황 (AI #1 영역)

파이프라인 전반에 걸쳐 Kanana-o를 활용한 언어 분석 기능을 선택적으로 적용한다. LLM 호출은 모두 **선택적(optional)** 로 설계되어 있어, API 키가 없거나 호출이 실패해도 파이프라인은 정상 동작한다. Kanana-o primary, GPT-4o fallback 자동 전환.

| 적용 위치 | 기능 | 설명 |
|----------|------|------|
| **TickerTextPack** | 뉴스 요약/감성 분석 | 종목별 뉴스를 Kanana-o로 분석하여 핵심 이슈 요약 + 감성 점수(-1~+1) + 주요 토픽 추출 (`llm_news_analysis`) |
| **TickerTextPack** | 공시 해석 | DART 공시를 Kanana-o로 해석하여 주가 영향(positive/negative/neutral) 판단 + 리스크 플래그 (`llm_disclosure_analysis`) |
| **DailyMarketPacket** | 일일 시장 코멘터리 | 매크로/시장 데이터를 종합한 시황 분석 + 시장 분위기(bullish/bearish/neutral) (`llm_market_analysis`) |
| **RiskCard** | 리스크 내러티브 | Regime 판단 근거를 한국어로 설명 + 리스크 수준 판단(low/moderate/elevated/high) (`llm_risk_narrative`) |
| **FinalDecisionCard** | 갈등 해소 분석 | 투자 신호 간 갈등 시 LLM 기반 상세 분석 + 권장 액션 제시 (`llm_conflict_analysis`) |

---

## 9. Risk Agent 세부 구조

Risk Agent는 LLM 없이 **규칙 기반 + uncertainty 모델**로 운영한다. 최신 문헌 기준으로 전략 수준 gate와 종목 수준 cap을 분리하는 **2-tier 구조**를 채택한다.

### Tier 1: Strategy-level Regime Gate
| 상태 | 조건 | 조치 |
|------|------|------|
| 🟢 Green | 정상 시장 | 정상 운영 |
| 🟡 Yellow | 스트레스 경계 | 신규 매수 비중 × 0.5 |
| 🔴 Red | 고스트레스 | 신규 진입 금지 |

### Tier 2: Position-level Constraints
| 규칙 | 값 | 비고 |
|------|----|------|
| Position cap | 20% | 단일 종목 최대 비중 |
| Stop-loss | -5% (기본) | 변동성 과대 시 ATR-based widened stop 가능 |
| Turnover cap | 30% | 일일 회전율 제한 |
| Min cash ratio | 10% | 최소 현금 보유 |
| **Uncertainty tail cap** | P85 초과 → 비중 × 0.5 | 가장 불확실한 예측만 discrete하게 삭감 |

**핵심 설계 근거**: cross-sectional ranking에서 strongest signals가 score tail에 몰리면서 동시에 uncertainty도 높은 경우가 있다. continuous inverse-uncertainty sizing은 이 strongest idea를 de-lever하는 역효과를 낸다. 따라서 상위 15% uncertainty tail만 discrete하게 cap하는 정책이 더 적절하다 (When Alpha Breaks).

**UQ calibration**: UQ calibration의 운영 책임은 AI #1이 가진다. AI #2는 Backtest 단계에서 uncertainty score 산출 및 재보정 실험을 지원하며, AI #1은 threshold calibration, tail-cap policy, online inference를 담당한다.

---

## 10. Final Decision Agent 역할

Final Decision Agent는 **constrained aggregator**로 설계한다. 자유형 블랙박스 의사결정기가 아니다.

### 작동 방식
1. **Deterministic pre-check**: StrategyCard + RiskCard + BacktestReport 정합성 확인
2. **Conflict case만 LLM 호출**: 전략/리스크 간 모순이 있는 경우에만 Kanana-o로 해소
3. **Explanation 생성**: 최종 판단 이유를 한국어로 생성
4. **출력**: FinalDecisionCard (approve / veto / explain)

### 계약
- `CandidateOrderPlan`의 비중·수량을 **절대 수정하지 않음**
- raw 뉴스 직접 읽기 금지 (evidence_ids만 참조)
- 모든 판단에 evidence_ids 근거 필수

### 구현 Phase
- Phase 1(W1~W4)의 Final Decision Agent는 deterministic pre-check와 LLM explanation wrapper 중심으로 구현한다.
- Phase 2부터 BacktestReport와 conflict-resolution prompt를 포함한 full constrained FDA로 확장한다.

### 예시
- ✅ Approve: "삼성전자 매수 15% — Quant 상위 3위 + HBM 공급 확대 뉴스, 리스크 통과"
- ⏸ Hold: "SK하이닉스 보유 유지 — 추가 매수 신호 없음, 현 비중 적정"
- ❌ Veto: "카카오 매수 보류 — stress gate yellow + uncertainty P88 (tail cap 적용)"

---

## 11. 논문 활용 방식

본 프로젝트는 논문을 그대로 재현하기보다, 각 논문의 핵심 아이디어를 실제 구현 가능한 구조로 통합하는 방향을 택한다. 교수님이 추천하신 6편으로 멀티에이전트 구조의 방향을 잡았고, 세부 설계 결정은 2025–2026 최신 문헌을 추가로 조사하여 근거를 만들었다.

### 교수님 추천 논문 6편

| 논문 | 활용 |
|------|------|
| MetaGPT | 에이전트 협업 구조, 역할 분리, artifact handoff, feedback 개념 |
| TradExpert | Strategy 내부 분업, 최종 종합 판단 구조 |
| AAPM | 뉴스/텍스트와 정량 정보의 결합 방식 |
| AlphaGAT | factor backbone 및 allocation 확장 아이디어 |
| AlphaAgent | alpha/factor 확장 방향 |
| R&D-Agent-Quant | 후속 offline quant 연구개발 loop, feedback 구조 |

### 본문 핵심 5편 (아키텍처 뼈대)

| 논문 | 활용 | 반영 위치 |
|------|------|----------|
| **Toward Expert Investment Teams** | fine-grained task decomposition, specialist workflow | 5-agent 역할 분리 전체 |
| **FinPos** | direction과 position adjustment 분리 | Strategy↔Risk 분리, Final Decision sizing 금지 |
| **MLF** | multi-period feature bank (IRF, LWI) | Quant Strategy의 MLF-lite |
| **FinTexTS** | macro/sector/target multi-level text pairing | TickerTextPack 3-level 구조 |
| **When Alpha Breaks** | regime gate + P85 uncertainty tail cap | Risk Agent 2-tier 구조 |

### Guardrail 2편 (LLM 제한 사용 근거)

| 논문 | 핵심 메시지 |
|------|-----------|
| **AI-Trader** | 대부분의 LLM agent가 poor returns + weak risk management |
| **StockBench** | 대부분의 LLM 모델이 simple buy-and-hold를 안정적으로 넘지 못함 |

→ LLM을 자유형 트레이더가 아니라 **constrained layer (설명/해석 전용)** 로 두는 근거

### Appendix (세부 구현 및 검증 참고)

| 논문 | 활용 |
|------|------|
| UMI | stock-level irrationality, market synchronism factor → UMI-lite |
| VTA | raw 시계열 → textual annotation 변환 근거 |
| FinSrag | domain-specific retrieval, similar case 검색 근거 |
| BlindTrade | ticker anonymization, negative control → backtest ablation |
| Memorization Problem | LLM 학습 데이터 memorization/look-ahead 경고 |
| TradingGroup | self-reflection, failure memory, dynamic stop-loss 참고 |
| Deep Learning Benchmark | risk-adjusted performance 기준 → LSTM/GNN 판단 보조 |
| TSFM for Financial TS | TSFM vs task-specific model 비교 → shadow baseline 판단 보조 |
| Hybrid Transformer GNN | full GNN 적용 범위 판단 보조 |
| FAILAB Instability Index | KOSPI 특화 stress 참고 |

---

## 12. 평가 프로토콜

### 백테스트 방식

| 항목 | 설정 |
|------|------|
| 실행 규칙 | t일 마감 정보 → t+1 open 실행 |
| walk-forward | expanding window |
| purge | overlapping label 구간 제거 |
| embargo | 60일 기본, 90일 robustness check |
| DEV/FINAL | DEV에서만 threshold/hyper 조정, FINAL은 frozen 1회 평가 |
| 거래비용 | fee 0.015% + slippage 10bp |
| holding horizon | H=5일 (주 목표), H=1일 보조, H=20일 robustness |

### 성과 지표
- 누적수익률, CAGR, MDD, Sharpe, turnover, hit ratio
- 거래비용 반영 후 성과 포함
- 가능한 경우 Deflated Sharpe Ratio 또는 이에 준하는 보정 적용

### Baseline 비교

| Baseline | 목적 |
|----------|------|
| KOSPI200 인덱스 | 시장 대비 초과수익 확인 |
| 동일 유니버스 equal-weight | 유니버스 선택 효과 분리 |
| Pure-quant (Quant Strategy만) | LLM reasoning 기여도 측정 |
| Pure-news (News Strategy만) | 정량 전략 기여도 측정 |
| Simple momentum (20일) | 단순 전략 대비 복잡성 정당화 |
| **No-UQ Risk** | uncertainty gate/tail cap 기여도 측정 |

### Leakage 통제 및 Ablation

| 실험 | 목적 | 우선순위 |
|------|------|---------|
| keyword pairing vs semantic multi-level pairing | 텍스트 매칭 방식 효과 검증 | Phase 2 |
| shuffled news / random headline control | 뉴스 신호 실존 여부 검증 | Phase 3 |
| anonymized ticker/company-name control | LLM memorization 배제 검증 | Phase 3 (선택) |

---

## 13. Artifact 스키마

### 공식 Artifact (8종)

| Artifact | Producer | 핵심 필드 |
|----------|----------|----------|
| `DailyMarketPacket` | AI #1 | date, cutoff_ts, universe_id, tickers[], market_data{ticker: {ohlcv, volume, mktcap, tech_features}}, macro_snapshot, news_index[], disclosure_index[], available_at |
| `TickerTextPack` | AI #1 | ticker, as_of_dt, macro_docs[], sector_docs[], target_company_docs[], dedup_ratio |
| `QuantShortlist` | AI #2 | ticker, quant_score, rank, top_factors[] |
| `StrategyCard` | AI #2 | ticker, pre_risk_score, quant_score, news_signal, direction, confidence, evidence_ids[], rationale |
| `RiskCard` | AI #1 | regime_status, portfolio_stress, position_risks[]{ticker, stress_level, uncertainty_score, position_cap_applied, gate_status, blocked_reasons[], holding_days, entry_price, unrealized_pnl_pct} — portfolio-level artifact이며 종목별 판단은 position_risks[]에 포함 |
| `CandidateOrderPlan` | AI #1 | ticker, action, target_weight, sizing_rule, execution_time, risk_tags[] |
| `BacktestReport` | AI #2 | period, execution_rule, cumulative_return, net_return, MDD, Sharpe, turnover, baseline_comparison, leakage_checks |
| `FinalDecisionCard` | AI #1 | approved_plan_id, final_action, veto_reason, explanation, confidence, fallback_used, evidence_ids[] |

### 운영 Artifact (모의 자동매매용)

모의 자동매매 운영을 위해 상태성 artifact인 PortfolioState와 장중 delta artifact인 HourlyMarketPatch를 추가 사용한다. PortfolioState는 진입가·보유일수·실제 turnover를 추적하고, HourlyMarketPatch는 daily backbone 위에 장중 가격/뉴스/시장스트레스 증분을 반영한다.

| Artifact | Producer | 핵심 필드 |
|----------|----------|----------|
| `PortfolioState` | AI #1 | positions[]{ticker, weight, entry_price, holding_days, unrealized_pnl_pct}, cash_ratio, total_exposure, daily_turnover, realized_pnl[] |
| `HourlyMarketPatch` | AI #1 | patch_id, base_dmp_id, changed_tickers[], price_patch{}, new_evidence_ids[], market_stress_update |

### 논리적 독립 Artifact (구현은 유연하게)

| Artifact | 위치 | 역할 |
|----------|------|------|
| `FailureCaseCard` | BacktestReport.failure_cases[] 내부 또는 독립 | failure_type, analog_cases, suggested_fix |
| `DataQualityReport` | DailyMarketPacket 부속 | doc_count, dedup_ratio, missing_flags |
| `UQModel` | AI #2 학습 → AI #1 사용 | uncertainty_score 산출 모델 |

### 스키마 운영 원칙
- **W2 수요일까지 JSON schema freeze** 🔒
- CandidateOrderPlan을 Final Decision Agent가 다시 계산하지 않음
- 모든 artifact에 timestamp + version 태깅

---

## 14. Producer/Consumer 관계

| Artifact | Producer | Consumer | 비고 |
|----------|----------|----------|------|
| DailyMarketPacket | AI #1 | AI #2 | raw market snapshot |
| TickerTextPack | AI #1 | AI #2 | semantic paired text |
| QuantShortlist / StrategyCard | AI #2 | AI #1 | 전략 신호 |
| UQModel | AI #2 (offline) | AI #1 (online) | 학습/추론 분리 |
| RiskCard / CandidateOrderPlan | AI #1 | AI #1 | sizing/gating |
| BacktestReport / FailureCaseCard | AI #2 | AI #1 | validation |
| FinalDecisionCard | AI #1 | Service Layer | 최종 출력 |

AI #1은 **online serving / governance**, AI #2는 **offline modeling / validation** 역할이 분명하게 분리된다.

---

## 15. 이번 학기 실제 구현 범위

### 필수 구현 (이번 학기 core)
- KOSPI 대형주 고정 유니버스
- Data Agent (KRX + DART + Naver + ECOS, PIT-safe 태깅, TickerTextPack 3-level)
- Strategy Agent (LightGBM ranker + MLF-lite + News/Market + Synthesizer)
- Risk Agent (2-tier: regime gate + position rules + P85 uncertainty tail cap)
- Backtest Agent (walk-forward + purge/embargo + DEV/FINAL + baseline 비교)
- Final Decision Agent (constrained aggregator, approve/veto, Kanana-o + fallback)
- Artifact schema 기반 파이프라인
- 서비스 프로토타입 (추천 + 모의 자동매매 화면)

### Phase별 구현 전략

| Phase | 기간 | 구현 내용 |
|-------|------|----------|
| **Phase 1** | W1~W4 | manual factors + macro → LightGBM baseline, 3-level text pack, core risk rules, E2E 파이프라인 |
| **Phase 2** | W5~W8 | real SC 통합, Backtest 정식 실행, MLF-lite + 최소 UMI-lite + UQ v1, mock trading gateway |
| **Phase 3** | W9~W11 | 5거래일 paper trading ops, ablation/비교표, 서비스 handoff, 최종 안정화 |

### 선택 확장 (여유 시)
- related_company_docs (4th level text pairing)
- Similar Case Retriever (factor-vector KNN)
- xLSTM/PatchTST shadow baseline
- anonymization ablation test
- 사용자 투자성향별 explanation tone

### 이번 학기 범위 밖
- 실계좌 주문 연동 / 실제 자동매매 실행
- 금융보안/본인인증 완성형 서비스
- 논문 full reproduction
- vanilla LSTM/GNN을 core 모델로 채택
- RL allocator / 강화학습
- full graph relation model
- full LoRA fine-tuning
- 전종목 pairwise ranking
- high-frequency trading
- agentic factor mining loop

---

## 16. 팀원 역할 분담

| 팀원 | 담당 | 핵심 키워드 |
|------|------|-----------|
| **AI #1** (장재원) | Data Agent, Risk Agent, Final Decision Agent, **UQ calibration** | online serving, PIT-safe, Kanana-o, uncertainty gate |
| **AI #2** | Strategy Agent (Quant + News + Synthesizer), Backtest Agent | LightGBM, MLF-lite, walk-forward, evaluation |
| **BE #1** | API 서버, 실행 파이프라인 연결, 결과 저장, 스케줄링 | orchestration, scheduling |
| **BE #2** | 앱·웹 UI, 대시보드, 결과 화면, 사용자 서비스 흐름 | frontend, visualization |

AI 팀은 멀티에이전트 트레이딩 엔진, BE 팀은 이를 연결한 서비스 레이어를 맡는다.

현재 AI #2의 공식 Strategy Agent(LightGBM + News + Synthesizer)는 구현 진행 중이다.

AI 측 handoff contract/payload는 준비 완료되었으며, BE 서비스 구현은 별도 레포/팀에서 진행한다.

---

## 17. 일정 계획

### W1~W4: 중간발표까지 (4/17)

| 주차 | 목표 | 핵심 산출물 |
|------|------|-----------|
| **W1** (3/21~3/27) | 기반 세팅 + 스키마 합의 | API 연동 확인, 유니버스 확정, schema 초안, **Kanana-o 접근권 확인 + fallback smoke test** |
| **W2** (3/28~4/3) | 데이터 파이프라인 + Quant baseline | DailyMarketPacket 자동 생성, TickerTextPack v1, LightGBM v1, **schema freeze** 🔒 |
| **W3** (4/4~4/10) | 핵심 모듈 연결 + Risk 고도화 | Risk 2-tier 구조 + 핵심 제약 규칙 완성, Final Decision v1, **E2E 첫 성공** |
| **W4** (4/11~4/17) | 안정화 + 발표 | 데모 샘플, Backtest baseline, **🎤 중간발표** |

### W5~W11: 최종발표까지 (6/5)

| 주차 | 목표 | 핵심 산출물 |
|------|------|-----------|
| **W5** (4/20~4/26) | Real SC 통합 + Live LLM + Contract Freeze | real SC E2E, branch output 3종, strategy_contract freeze |
| **W6** (4/27~5/3) | Backtest Agent v1 + DEV/FINAL Freeze + Baseline Suite | BacktestReport, FailureCaseCard, baseline 6종, backtest_summary_contract |
| **W7** (5/4~5/10) | 증권사 모의투자 Gateway + Reconciliation | KIS mock 주문 3종, reconcile report, bounded execution |
| **W8** (5/11~5/17) | MLF-lite + 최소 UMI-lite + UQ v1 | StrategyCard v2, feature importance 비교표, backtest delta |
| **W9** (5/18~5/24) | 5거래일 Paper Trading Ops + Daily/Hourly 안정화 | 5일 ops report, 운영 메트릭 7종, intraday cycle |
| **W10** (5/25~5/31) | Ablation / 비교표 / BE Handoff Payload | ablation 4종, 차트 5+, dashboard payload contract |
| **W11** (6/1~6/5) | Code Freeze / Manifest / Runbook / 최종발표 | experiment manifest, artifact bundle, **🎤 최종발표 (6/5)** |

### 일정 원칙
- Phase 1 (W1~W4)이 흔들리면 Phase 2 범위를 줄인다
- mainline E2E가 최우선, feature 확장은 그 다음
- 중간발표 목표는 성능 시연이 아니라 **"5-agent 시스템이 artifact 기반으로 실제 돌기 시작했다"** 를 보여주는 것

---

## 18. 서비스 프로토타입 방향

이번 학기 서비스 목표는 실제 금융 서비스 구현이 아니라, AI 트레이딩 엔진의 결과를 사용자가 이해하고 활용할 수 있는 **모의 자동매매/추천 앱 프로토타입**을 구현하는 것이다.

- 오늘의 시장 현황 확인
- 추천 종목 및 추천 이유 확인 (FinalDecisionCard 기반)
- 사용자 투자 성향 기반 추천
- 모의 자동매매 실행 결과 확인
- 수익률 / 손실 / 거래 내역 시각화
- 전략 변경 및 설정 관리

---

## 19. 기대 효과

- 금융 트레이딩을 협업형 AI 시스템으로 구조화
- 새로운 정보(뉴스/공시 등)를 반영한 투자 판단 구현
- 리스크와 백테스트를 독립적으로 설계하고, uncertainty-aware 운영 구조 달성
- 최종 판단 결과를 사용자에게 설명 가능한 형태로 제공
- 한국어 특화 LLM(Kanana-o)을 활용한 KOSPI 맞춤 reasoning
- 2-tier risk structure로 market regime과 개별 종목 불확실성을 동시에 관리
- baseline 비교 + ablation을 통한 멀티에이전트 구조의 추가 기여 검증
- KOSPI 기반 멀티에이전트 트레이딩 알고리즘 + 앱 프로토타입 구현

---

## 20. 실가격 Backtest 결과

실제 가격 기반 replay backtest 모듈(run_backtest_replay.py)이 구현되었으며, 95거래일 기준 +3.51% 수익(벤치마크 EW 대비 -13.56%)을 기록했다. 이는 synthetic simulation이 아닌 t+1 시가 체결 기반 실가격 결과이다.

---

## 21. 결론

본 프로젝트는 단일 주가 예측 모델이 아니라, KOSPI 대형주를 대상으로 한 멀티에이전트 트레이딩 알고리즘을 개발하고, 이를 바탕으로 사용자가 활용할 수 있는 모의 자동매매/추천 앱 프로토타입을 구현하는 것을 목표로 한다.

- **Data Agent**가 PIT-safe 데이터를 수집하고 TickerTextPack으로 텍스트를 구조화하고
- **Strategy Agent**가 LightGBM ranker로 종목을 선별하고 LLM으로 뉴스/공시를 해석하여 투자 아이디어를 만들고
- **Risk Agent**가 2-tier 구조(regime gate + uncertainty tail cap)로 위험을 관리하고 `CandidateOrderPlan`을 생성하며
- **Backtest Agent**가 walk-forward + purge/embargo + DEV/FINAL로 과거 성과를 엄격히 검증하고
- **Final Decision Agent**가 `CandidateOrderPlan`을 승인/거부하고 최종 설명을 생성하며
- **BE 팀**이 이를 사용자 서비스로 연결하는 구조이다.

KOSPI·한국어 뉴스/공시·한국어 사용자 서비스라는 특성을 반영해, Reasoning 계층과 Final Decision Agent에는 Kanana-o를 우선 적용하고, 안정성을 위해 GPT-4o fallback을 함께 둔다.

최신 2025–2026 문헌에 기반하여 "예측과 설명의 분리, 방향 판단과 비중 조정의 분리, uncertainty-aware risk management, contamination-free evaluation"이라는 설계 원칙을 일관되게 적용함으로써, 학술적 근거와 실제 구현이 정합하는 **한국형 멀티에이전트 트레이딩 시스템 프로토타입**을 구현하고자 한다.

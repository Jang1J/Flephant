# KOSPI 전용 구현 청사진 및 실험 운영계획 v5

> 목적: v5 기준 KOSPI 프로젝트를 실제로 어떻게 구현할지, **contract -> 클래스 -> 실행 순서 -> 실험 운영** 순으로 내리는 실행 문서

---

## 1. 구현 철학

1. **Contract 먼저, 모델은 나중**
2. **MVP는 end-to-end loop가 우선**
3. **모든 공식 모듈은 artifact를 남긴다**
4. **실험 가능한 구조를 유지한다**
5. **확장은 runtime core와 분리한다**
6. **KOSPI 현지화는 시장 전제만 바꾸고 상위 구조는 유지한다**
7. **RL은 mainline을 대체하지 않고 allocator prototype으로만 붙인다**

---

## 2. Repo 구조 제안

```text
project/
├─ docs/
│  └─ kospi/
│     ├─ 01_KOSPI_팀프로젝트정의서_v5.md
│     ├─ 02_KOSPI_프로젝트매핑서_v5.md
│     ├─ 03_KOSPI_시스템_시각화_v5.md
│     ├─ 04_KOSPI_구현청사진_및_실험운영계획_v5.md
│     └─ 05_KOSPI_추가논문_해체분석_v1.md
│
├─ backend/
│  ├─ api/
│  ├─ orchestration/
│  │  ├─ orchestrator.py
│  │  ├─ contracts.py
│  │  ├─ clock_policy.py
│  │  └─ experiment_mode.py
│  ├─ registry/
│  │  └─ artifact_registry.py
│  ├─ agents/
│  │  ├─ base_agent.py
│  │  ├─ data_agent.py
│  │  ├─ strategy_agent.py
│  │  ├─ risk_agent.py
│  │  └─ backtest_agent.py
│  ├─ strategy/
│  │  ├─ feature_engine/
│  │  │  ├─ manual_factor_bank.py
│  │  │  ├─ multi_period_bank.py
│  │  │  ├─ irf_lite.py
│  │  │  ├─ lwi_lite.py
│  │  │  └─ umi_lite.py
│  │  ├─ quant_prefilter/
│  │  │  ├─ lightgbm_ranker.py
│  │  │  └─ evidence_builder.py
│  │  ├─ market_analyst/
│  │  ├─ news_disclosure_analyst/
│  │  ├─ synthesizer/
│  │  └─ rl_allocator/
│  │     ├─ ppo_allocator.py
│  │     ├─ reward.py
│  │     └─ state_builder.py
│  ├─ data/
│  │  ├─ security_master.py
│  │  ├─ price_loader.py
│  │  ├─ disclosure_loader.py
│  │  ├─ news_loader.py
│  │  └─ cache_store.py
│  ├─ risk/
│  │  ├─ risk_rules.py
│  │  ├─ instability_gate.py
│  │  └─ order_adjuster.py
│  ├─ backtest/
│  │  ├─ simulator.py
│  │  ├─ metrics.py
│  │  └─ failure_tagger.py
│  └─ config/
│     ├─ universe_static.yaml
│     ├─ source_config.yaml
│     ├─ risk_policy.yaml
│     └─ experiment_modes.yaml
│
├─ frontend/
│  ├─ dashboard/
│  ├─ cards/
│  ├─ experiments/
│  └─ portfolio/
│
└─ notebooks/
   ├─ data_validation/
   ├─ prefilter_benchmarks/
   ├─ umi_factor_studies/
   ├─ mlf_feature_bank/
   └─ rl_allocator_trials/
```

---

## 3. 핵심 클래스 청사진

## 3.1 BaseAgent
```python
class BaseAgent:
    name: str
    input_artifacts: list[str]
    output_artifacts: list[str]

    def can_run(self, registry, run_context) -> bool:
        ...

    def run(self, registry, run_context) -> dict:
        ...
```

## 3.2 ArtifactRegistry
```python
class ArtifactRegistry:
    def save(self, artifact_type: str, payload: dict, version: str, run_id: str): ...
    def latest(self, artifact_type: str, run_id: str | None = None): ...
    def history(self, artifact_type: str, key: str | None = None): ...
    def exists(self, artifact_type: str, run_id: str | None = None) -> bool: ...
    def compare_versions(self, artifact_type: str, run_id: str): ...
```

### 공식 저장 대상
- `DailyMarketPacket`
- `StrategyCard`
- `RiskCard`
- `OrderPlan`
- `BacktestReport`
- `FailureCaseCard`

### 선택 저장 대상
- `QuantEvidenceCard`
- `MarketAnalysis`
- `TextSignalCard`

## 3.3 Orchestrator
```python
class Orchestrator:
    def run_daily(self, trade_date, experiment_mode): ...
    def run_feedback_retry(self, failure_card): ...
    def should_retry(self, failure_card, retry_policy): ...
```

### 책임
- 실행 순서 제어
- dependency 체크
- experiment mode(S0~S4, R0~R3) 반영
- feedback retry 1회
- artifact routing

---

## 4. KOSPI 공통 정책 모듈

## 4.1 DataClockPolicy
```python
class DataClockPolicy:
    snapshot_time = "18:00:00"
    market_close = "15:30:00"
    next_open = "09:00:00"

    def is_available_for_trade_date(self, ts) -> bool: ...
    def assign_trade_date(self, ts): ...
```

### 규칙
- `timestamp <= T일 18:00 KST` -> T일 정보셋
- `timestamp > 18:00 KST` -> T+1 정보셋
- 가격 feature는 정규시장 마감 기준
- 집행은 T+1 open

## 4.2 SecurityMasterService
```python
class SecurityMasterService:
    def load_static_universe(self): ...
    def map_ticker_to_corp_code(self, ticker): ...
    def enrich_names(self, rows): ...
```

### 책임
- `krx_ticker <-> corp_code <-> company_name` 매핑
- universe_static.yaml 로딩
- 모든 데이터 소스 공통 key 제공

---

## 5. Quant core 구현 모듈

## 5.1 MLF-lite 구현 범위

### MultiPeriodFeatureBank
```python
class MultiPeriodFeatureBank:
    periods = [5, 20, 60]
    def build(self, price_df): ...
```

### IRF-lite
```python
class InterPeriodRedundancyFilter:
    def fit_transform(self, feature_df): ...
```

#### 구현 원칙
- period별 feature group 생성
- 상관 높은 feature prune
- importance / SHAP 기반 중복 제거 가능

### LWI-lite
```python
class LearnableWeightedIntegrator:
    def fit(self, score_5, score_20, score_60, y): ...
    def predict(self, score_5, score_20, score_60): ...
```

#### 구현 원칙
```text
score = w5 * score_5 + w20 * score_20 + w60 * score_60
```
- scalar gate 또는 작은 linear layer 수준으로 제한

### 이번 학기 보류
- full MAP reproduction
- full Patch Squeeze
- full end-to-end MLF 재현

## 5.2 UMI-lite 구현 범위

### RationalPriceProxyBuilder
```python
class RationalPriceProxyBuilder:
    def build_proxy(self, prices, corr_mat, universe_meta): ...
```

#### 기본 아이디어
```text
p_tilde(i,t) = Σ_j att(i,j) * p(j,t)
```
- `att(i,j)`는 rolling correlation 또는 lightweight attention
- peer basket + market index 사용

### IrrationalityFactorEngine
```python
class IrrationalityFactorEngine:
    def stock_factor(self, p_tilde, p, vol20): ...
    def market_factor(self, corr_surge, dispersion, co_move): ...
```

#### 산출
- `u_stock(i,t) = (p_tilde(i,t) - p(i,t)) / vol20(i,t)`
- `u_market(t) = anomalous synchronism score`

### 사용 위치
- Quant Prefilter feature
- Market Analyst 보조 state
- Risk Agent stress gate 보조 signal

## 5.3 Quant Prefilter
### 기본선
- `LightGBM LambdaMART ranker`

### 입력
- manual factor bank
- MLF-lite feature bank
- UMI-lite factors

### 출력
- 종목별 ranking score
- Top 5~8 shortlist
- `QuantEvidenceCard` (debug/experiment mode)

---

## 6. Reasoning layer 구현 모듈

## 6.1 Market Analyst
입력:
- shortlist
- top features / UMI-lite stress / trend context

출력:
- `MarketAnalysis` (내부 카드)
- StrategyCard용 evidence fragment

## 6.2 News/Disclosure Analyst Lite
입력:
- 공시 제목 / 공시 유형 / 헤드라인 / 핵심 문장(선택)

정책:
- AAPM식 relevance check
- refined text
- article full parse는 보류

출력:
- `TextSignalCard` (내부 카드)
- StrategyCard용 evidence fragment

## 6.3 General Synthesizer
입력:
- `QuantEvidenceCard`
- `MarketAnalysis`
- `TextSignalCard`

출력:
- `StrategyCard`

---

## 7. Risk / Backtest / Feedback

## 7.1 RiskAgent
- base rules: position cap / stop-loss / turnover / cash ratio
- local gate: InstabilityIndexGate
- optional stress: UMI-lite market factor

### 모듈
```python
class InstabilityIndexGate:
    def score(self, market_state): ...
    def adjust_exposure(self, current_score, plan): ...
```

## 7.2 BacktestAgent
### Simulator
- T+1 open execution
- fee/slippage
- walk-forward split

### FailureTagger
- failure window labeling
- broken assumption detection
- retry hint 생성

### 출력
- `BacktestReport`
- `FailureCaseCard`

## 7.3 Feedback retry
```text
Backtest 실패
  -> FailureCaseCard 발행
  -> Orchestrator가 retry_policy 확인
  -> Strategy 1회 재실행
  -> 재실행 결과는 별도 version 저장
```

---

## 8. RL allocator 구현 정책

## 8.1 이번 학기 실제 구현 후보 (Layer 6a)

### 후보 1: AlphaGAT Stage-II inspired PPO allocator
- state: Top-K signal vector + risk state
- action: weight simplex + cash ratio
- reward: return - cost - risk penalty

### 후보 2: HSTP-lite PPO allocator
- stage 1 signal extraction은 기존 mainline 사용
- PPO로 allocation만 학습
- mean-CVaR inspired reward 채용 가능

### 후보 3: Smart Tangency baseline
- PPO/A2C actor-critic practical baseline
- 구현 난도 가장 낮음

## 8.2 이번 학기 규칙
- RL은 **full-universe selector 금지**
- RL은 **Top-K allocator로만 사용**
- RL은 `must-have`가 아니라 **prototype / 초기 실험**

## 8.3 reserve 문헌 (실구현 강제 아님)
- MetaTrader
- Heuristic-guided IRL + Graph Policy
- Attention-Enhanced Dirichlet RL
- Can DRL Beat 1/N?

---

## 9. 일일 실행 의사코드

```python
def run_daily(trade_date, experiment_mode):
    packet = DataAgent.run(...)
    registry.save("DailyMarketPacket", packet, ...)

    strategy = StrategyAgent.run(packet, ...)
    registry.save("StrategyCard", strategy, ...)

    risk = RiskAgent.run(strategy, ...)
    registry.save("RiskCard", risk.card, ...)
    registry.save("OrderPlan", risk.order_plan, ...)

    report = BacktestAgent.run(risk.order_plan, ...)
    registry.save("BacktestReport", report.summary, ...)
    registry.save("FailureCaseCard", report.failure, ...)

    if should_retry(report.failure):
        retry_strategy = StrategyAgent.run(packet, constraints=report.failure["suggested_fixes"])
        registry.save("StrategyCard", retry_strategy, version="retry1", ...)
```

---

## 10. W1~W11 현실 로드맵

## W1
- v5 문서 freeze
- ownership freeze
- 공식 카드 6종 schema freeze
- 내부 카드 저장 정책 freeze

## W2
- security_master
- price/disclosure/news loader
- DailyMarketPacket 생성

## W3
- manual factor bank
- MLF-lite bank
- LightGBM ranker baseline

## W4 (중간발표) — C' demo
### 반드시 실제로 보여줄 것
- KOSPI 고정 유니버스
- DailyMarketPacket
- shortlist 결과
- StrategyCard v0
- artifact trace viewer

### thin baseline 허용
- RiskCard v0
- BacktestReport v0
- FailureCaseCard v0

## W5
- UMI-lite factor
- Market Analyst 연결
- QuantEvidenceCard debug 저장

## W6
- News/Disclosure Analyst Lite
- General Synthesizer
- StrategyCard 안정화

## W7
- Risk Agent + Instability gate
- RiskCard / OrderPlan 연결

## W8
- Backtest Agent
- FailureCaseCard
- feedback loop 1회

## W9
- RL allocator **설계 확정 + 프로토타입 + 초기 실험**
- 후보: HSTP-lite PPO 또는 Smart Tangency baseline
- fallback: RL은 최종 발표에서 초기 실험 + 향후 연구로 제시

## W10
- S0~S3, R0~R2 ablation
- RL on/off 비교(프로토타입이면 appendix)

## W11
- 안정화
- 발표자료 / 표 / 차트
- appendix baseline(HMM, Low-pass LSTM) 정리
- RL reserve 문헌 정리

---

## 11. 다음 회의에서 반드시 freeze할 것

1. KOSPI 고정 유니버스 최종 리스트
2. security_master 스키마
3. MLF-lite의 feature group 수
4. UMI-lite의 peer basket 구성 방식
5. LightGBM ranker feature set
6. Layer 6a에서 실제로 구현할 RL allocator 1개
7. 내부 중간 카드(QuantEvidenceCard, MarketAnalysis, TextSignalCard)의 registry 저장 정책
8. W4 데모에서 Risk/Backtest를 thin baseline으로 어느 수준까지 보여줄지

---

## 12. 이 문서의 한 줄 역할

> **v5 구현 청사진은 “KOSPI mainline을 안정적으로 완성하기 위한 engineering plan”이며, RL은 코어가 아니라 allocator prototype으로만 다루고, UMI-lite/MLF-lite를 실제 quant backbone에 넣는 방법을 구체화한 문서다.**

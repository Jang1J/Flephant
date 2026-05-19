# KOSPI Decision OS v3 — 상세 아키텍처 설계서

> KOSPI 1분봉을 위한 연구형/모의운용형 event-aware multi-agent Decision OS
>
> 작성일: 2026-04-05 (v2.1) → 2026-04-09 (v2.2) → **2026-04-10 (v3 팀 흡수 반영)**
>
> v3.1 (2026-04-20): C17 ModelRegistryContract 신설
> v3.2 (2026-04-21): C18 AgentPerformanceContract 신설 + agent_performance_id (APM-)
> v3.3 (2026-04-21): C9 input uncertainty_score non-breaking extension + uncertainty_signal channel
> v3.4 (2026-04-21): MSG/APM/DEC/OP ID UUID8 정정 (seq 충돌 방지)
> v3.5 (2026-04-21): PP/BUNDLE/BT/RPT/FCC/RGC ID UUID8 정정 (전수 통일). BT {tool} 컴포넌트 제거.
> v3.6 (2026-05-02): Sprint 4 반영. dual_source 피처 표기 통일 (언더스코어) / Persistent Cache S4-7 SQLite 명시 / §5.6 prediction_history KB 범위 명확화 / DQR stage_0 / Memory Restorer Bootstrap / E2E Profiler.
> v3.7 (2026-05-02): 전수 리뷰 fix. §8.0.1 타임라인 8단계 (stage_0 DQR) 명시 / §3.2 LightGBM n_estimators=500 (risk_config SSOT) 정정.
> v3.8 (2026-05-03): §7.4 DYNAMIC_OVERLAY_ACTIVE / DYNAMIC_OVERLAY_DISABLED 2 상태 추가 + §14 신규 3 ID (ADM/EXT/WS) 등록. Sprint 5 진입.
> 근거: 교수님 피드백 + 6개 논문 분석 (AAPM, AlphaGAT, MetaGPT, RD-Agent, TradeXpert, AlphaAgent)
> 외부 AI 검증 (v2.1): GPT Pro 평균 8.6/10 (멀티에이전트 정체성 9.1, RL 배치 8.9, 실시간성 8.7, 아키텍처 8.6, 명세 7.2, 실거래 7.1)
> 위치: /Elephant_Lab/new/

## v3 Executive Summary

이 문서는 KOSPI 1분봉 멀티에이전트 Decision OS의 상세 설계서이다. v2.1(commit a61d094)에서 확정된 구조 위에, v2.2에서 **Mode B 시스템 검증축(Backtest Agent)** 을 신설했고, v3.0에서는 **팀 병합 결과로 Dual-Source Temporal Signal(뉴스↔커뮤니티 divergence)** 을 즉시 흡수하며 **Dynamic Event Universe(Sprint 5)** 를 후속 확장으로 분리해 코어 정체성을 유지한다.

### 한 문장 포지셔닝

> **장중에는 Quant Stream이 빠르게 돌고, 사건이 생기면 Event Stream의 LLM 전문가 집단이 개입하며, 밤에는 Alpha Factor Engine이 새 factor/model을 만들고 Backtest Agent가 시스템 전체를 검증한 뒤에야 내일의 시스템이 배포된다.**

### 4개의 가치 제안

1. **퀀트가 빠르게 시그널을 생성**한다 (LightGBM, 0.3ms 추론)
2. **정성 에이전트가 이벤트에 reasoning**한다 (뉴스/공시/리스크 → CoT)
3. **FDA가 설명 가능하게 승인/거부**한다 (cause 중심, 비중 수정 불가)
4. **밤에 factor/model이 자동 진화**하고 **Backtest가 회귀 위험을 차단**한다 (Alpha Factor Engine + Co-STEER + Backtest Agent)

### 6개 논문 → 설계 매핑 (요약)

| 논문 | 핵심 아이디어 | 적용 위치 |
|------|-------------|---------|
| AAPM | 기억 + 반복정제 + RAG + 자율판단 | Layer 4+5 (News/Risk Agent memory, FDA RAG) |
| AlphaGAT | alpha factor 자동채굴 + RL 비중 | Layer 3 (LightGBM 입력 + PPO Allocator) |
| MetaGPT | SOP + Message Pool + Pub/Sub + Feedback | Layer 4 (Blackboard 통신) |
| RD-Agent(Q) | factor-model 공동최적화 + bandit 자동화 | Mode B (Alpha Factor Engine + Thompson Sampling) |
| TradeXpert | 데이터별 전문 LLM + MoE + pairwise comparator | Layer 2+4 (News/Risk/Quant 분업 + Debate) |
| AlphaAgent | alpha decay 3중 정규화 | Layer 3+5 (Idea/Factor/Eval + Alpha Decay Monitor) |

### v2.1 → v2.2 주요 변경점 (16항목 계획 + 사후 보강)

| 범위 | 건수 | 핵심 |
|------|:---:|------|
| architecture.md | 11 + **3 보강** + **v3 통합** | Backtest Agent 추가, Mode B 타임라인 세분화, Hot/Cold Path 재강조, KP-7 공식화, 6논문 매핑 강화, **§7.4 MODE_B 상태 5개 추가, §8.0 Mode B Scheduler 주체 명시, §8.6 Mode B Deployer 배포 주체 명시, §2/§3/§7에 Dual-Source + Sprint 5 Dynamic Event Universe 추가** |
| architecture_visual.md | 2 + **1 보강** + **v3 시각화** | Hot/Cold Path 분기 시각화 보강, Mode B 타임라인 시각화 신규, **§3.0에 Mode B Scheduler/Deployer 주체 명시**, v3 Dual-Source/동적 유니버스 도식 추가 |
| api_contracts.md | 2 + **1 보강** + **v3 계약 추가** | **C12 BacktestAgentContract + C13 ValidationToolsContract + C14 ModeBSchedulerContract + C3A DualSourceScoreContract + C15 DynamicUniverseContract + C16 WatchUniverseSnapshotContract** |
| memory / presentation | **확장** | KP-7 공식 등록 + AI 파트 발표용 문서/시각자료 추가 |
| **risk_config.yaml** (v2.2 사후) | **v3 보강** | **backtest_agent + validation_tools + execution_cost_model + llm_budget.mode_b_gpt4o + dual_source + dynamic_universe 섹션** |

> **중요**: Backtest Agent는 **Mode B 전용**이며 장중 경로에 절대 개입하지 않는다 (Hot Path, Cold Path, FDA, order_deltas, target_weights 전부 불변). 권한 제약 세부는 §4.2 Backtest Agent Profile + new/specs/api_contracts.md C12 참조.

> **Eval Agent ≠ Backtest Agent**: 기존 Eval Agent는 Alpha Factor Engine 내부의 로컬 factor evaluator(§8.3)이며, v2.2의 Backtest Agent는 시스템 전체 검증자이다. 두 역할은 스코프와 권한이 다르다.

---

## 0. 설계 원칙

### 0.0 가치 제안 (Elevator Pitch)

1. **퀀트가 빠르게 시그널을 생성**한다 (LightGBM, 0.3ms 추론)
2. **정성 에이전트가 이벤트에 reasoning**한다 (뉴스/공시/리스크 → CoT)
3. **FDA가 설명 가능하게 승인/거부**한다 (cause 중심, Thompson Sampling)
4. **밤에 factor/model이 자동 진화**한다 (Alpha Factor Engine + Co-STEER)

### 0.1 교수님 피드백 (2026-04-03)
1. 아이디어/전략은 좋다 — 방향성 승인
2. 데이터 주기를 분단위로 줄여라 — 1분봉
3. 즉시 반응 가능한 구조로 — "전쟁이 터지면 반응할 수 있어야"
4. 퀀트 모델에만 의존하지 말고 멀티에이전트의 강점을 살려라
5. 중간발표는 cause 중심으로

### 0.2 핵심 설계 철학
- **퀀트 모델 = 정량적 예측** (가격이 어떻게 될까)
- **멀티에이전트 = 정성적 판단** (지금 사야 하나, 왜)
- **멀티에이전트 선택 근거**: 주식 시장에는 가격/거래량 같은 정량 데이터로 포착되지 않는 리스크가 존재한다 (뉴스의 의미, 외국인 이탈의 이유, 커뮤니티 테마의 진위, 지정학적 사건의 영향). 퀀트 모델은 이 영역에서 구조적으로 무력하다. 멀티에이전트는 이 **"정량화 불가능한 리스크 공백"**을 채우기 위해 선택되었다.
- **타이밍 우위**: VIX, 감성지표 같은 정량 리스크 지표는 후행(lagging)한다 — 사건이 발생하고 수 시간~수 일이 지나야 수치에 반영된다. 1분봉에서는 이미 늦다. 반면 Risk Agent는 뉴스/공시 텍스트를 수 초~수 분 내에 읽고 즉각 반응한다. **에이전트의 핵심 가치는 정량 지표보다 빠른 선행(leading) 리스크 감지**에 있다.
- 각 에이전트는 특정 유형의 비정량 리스크를 전담한다:
  - News Agent → 뉴스/공시 텍스트의 투자 영향 해석
  - Risk Agent → 외국인 이탈의 이유, 지정학/거시 리스크 판단
  - Debate Agent → 퀀트와 에이전트 의견 충돌 시 맥락 기반 판단
  - FDA → 정량+정성 종합 후 설명 가능한 최종 판단
- **LLM = 보조 도구가 아닌 핵심 엔진**
- **"과거에 갇히지 않는 시스템"** (KP-7, v2.2 공식 등록): 과거 데이터만으로 학습한 모델은 과거 패턴만 안다. 고정된 키워드 목록은 과거 키워드만 잡는다. 정적인 alpha factor는 시간이 지나면 decay한다. 세상은 빠르게 변하므로, 시스템의 모든 구성요소가 스스로 진화해야 한다. Mode B가 매일 밤 갱신하는 것: ① 모델 재학습 ② alpha factor 진화 ③ 뉴스 필터 키워드 갱신 ④ 에이전트 자기 개선 ⑤ **Backtest Agent 시스템 검증 (v2.2 추가)**. **내일의 시스템은 오늘과 다르다** — 그리고 v2.2부터는, Backtest Agent가 "다르게 만든 시스템"이 어제보다 나은지 재검증한 뒤에야 배포된다.
- **역할 분리 원칙**: 숫자와 텍스트는 다른 처리자가 담당한다.
  - **숫자** (가격, 거래량, 지표) → LightGBM이 예측. 에이전트 불필요.
  - **텍스트** (뉴스, 공시, 커뮤니티) → LLM pretrained 규칙이 필터링 + Kanana-o가 해석.
  - **Quant Agent** = 숫자 세계와 텍스트 세계의 다리. 숫자 이상 감지 시 에이전트를 깨운다.
  - **FDA** = 숫자(퀀트) + 텍스트(에이전트) 종합 판단.
  - **Mode B** = 숫자 쪽(모델/팩터) + 텍스트 쪽(필터 규칙) + 에이전트 전부 매일 밤 갱신.
- 에이전트는 진짜 멀티에이전트 4요건 충족: 자율성, 반응성, 능동성, 사회성
- **해석력 vs 최적화력 비충돌 원칙**: RL을 전략 전체로 확장하면 설명 가능성과 통제 가능성이 약해진다. 멀티에이전트의 해석력과 RL의 최적화력이 충돌하지 않도록, RL은 allocator(PPO 비중 배분) + scheduler(Thompson Sampling 방향 선택)에 한정한다.
- **시스템 토폴로지: 중앙 오케스트레이션형 MAS** (분산형 아님). FDA가 모든 판단의 최종 게이트웨이. 장점: 감사 용이(누가 왜 결정했는지 추적 가능). 단점: single bottleneck. 이 트레이드오프를 인지한 의도적 설계 선택. (M-1)

> **경고: FDA에 계산 로직을 넣으면 1분봉에서 즉시 병목이 된다.** FDA는 읽기-승인자로만 유지하고, 계산은 반드시 Quant/Debate/PPO/PM에서 끝내야 한다.

- **에이전트 조직: 기능 기반** (섹터 기반 아님). 5에이전트가 기능별로 분업(뉴스/리스크/퀀트/토론/판단). 섹터 기반 pod(반도체 Pod, 조선 Pod 등)는 에이전트 수 급증 → 연구 범위 초과. Phase 2에서 유니버스 확장 시 재검토. (M-2)

> 재검토 트리거: 유니버스가 50종목 이상으로 확장될 때 섹터 pod 구조 재검토.
- **에이전트 통신: Blackboard Pattern** (Erman et al., 1980). Shared Message Pool에 에이전트가 읽고 쓰는 공유 작업공간. 단순 prompt chaining이 아닌 구조화된 멀티에이전트 통신. (M-3)
- **1분봉 특유 원칙: 체결 품질 > 알파**. 1분봉 환경에서는 시그널 정확도보다 체결 품질(슬리피지, 부분체결, 타이밍)이 계좌 성과에 더 큰 영향. 이 원칙은 일봉/주봉에는 해당하지 않을 수 있다. (M-4)

> **현재 적용 범위 (M-5)**: 연구형/모의운용형. 실계좌 자동매매에는 OMS 한계(§6.0), 이벤트 백프레셔 정책은 정의 완료(§7.2 C-2, C11) — 운영 검증/튜닝 미완, FDA 단일 병목 등으로 인해 아직 부적합. 상용 배포는 Phase 2 이후.

### 0.3 6개 논문 설계 원칙 매핑
| 논문 | 설계 원칙 | 적용 위치 |
|------|---------|---------|
| AAPM | 에이전트에 기억/반복정제/RAG/자율판단 | Layer 4+5 |
| AlphaGAT | alpha factor 자동채굴 + cross-asset + RL 비중 | Layer 3 |
| MetaGPT | SOP + Message Pool + Pub/Sub + Feedback | Layer 4 |
| RD-Agent(Q) | factor-model 공동최적화 + bandit + 자동화 루프 | Mode B + Layer 4 |
| TradeXpert | 데이터별 전문 LLM + MoE + Reprogramming | Layer 2+4 |
| AlphaAgent | alpha decay 방지 3중 정규화 | Layer 3+5 |

---

## 1. 전체 구조: 2모드 × 5레이어

```
Mode A: 장중 실시간 매매 (09:00~15:30, 매 1분)
Mode B: 장마감 자동 진화 (18:00~, 매일 밤)

Layer 5: Knowledge (지식 축적 + 검색)
Layer 4: Agent (판단 + 소통 + 자기 개선)
Layer 3: Model (예측 + 팩터 진화)
Layer 2: Data (수집 + 전처리)
Layer 1: Execution (실행 + 피드백)
```

---

## 2. Layer 2: Data Layer (수집 + 전처리)

### 2.1 데이터 소스

| 소스 | API | 주기 | 데이터 | 비고 |
|------|-----|------|-------|------|
| KIS | REST + WebSocket | 1분 (실시간) | OHLCV + vwap + turnover + change (8개) | 메인 가격 데이터 |
| KRX 투자자별 | REST | 수집 1분 / freshness 30분 TTL (`risk_config.yaml:473` SSOT) | 외국인/기관/개인 순매수 (종목별) | IFP — 외국인 이탈 시 Risk Agent 경고 |
| Naver News | 크롤링 | 수집 1분 / freshness 30분 TTL | 한국어 뉴스 기사 | LLM으로 분석 |
| DART | Open API | 수집 1분 / freshness 1시간 TTL | 공시 (한국어) | LLM으로 분석 |
| 커뮤니티 | 크롤링 | 수집 5분 / freshness 15분 TTL | 투자자 심리 | 보조 데이터 — RTG: 테마 점수 산출 (종목별 언급 빈도 + 감성 분석) |
| US Market | yfinance / KIS | 08:30 1회 (전날 미국장 종가) | S&P500, NASDAQ, VIX, SOXX | 야간 리스크 |
| ECOS | API | 일별 | 금리, 환율, 거시지표 | 매크로 context |

### 2.1.1 Dual-Source Temporal Signal (v3 즉시 반영)

팀 병합 결과로, 뉴스와 커뮤니티를 **같은 텍스트로 합치지 않고 서로 다른 소스**로 취급한다.

- **뉴스**: 신뢰도 높고 반영 속도 빠름 → `news_score_t`, 빠른 decay (`lambda_news = 0.8`)
- **커뮤니티**: 노이즈 높고 개인 투자자 반응이 늦음 → `comm_score_t_1`, `comm_score_t_2`, 느린 decay (`lambda_comm = 0.4`, `peak_lag_days = 2`)
- **소스 간 불일치**: `news_comm_divergence = |news_score_t - comm_score_t_1|`
- **노이즈 제어**: `community_noise_multiplier` (게시량 z-score 기반 감쇠)

핵심 의미는 **"뉴스는 긍정인데 커뮤니티는 부정" 같은 방향 불일치 자체를 불확실성(UQ) 신호로 본다**는 것이다. 이 divergence는 가격/거래량만으로 포착하기 어렵고, 현재 프로젝트의 핵심 철학인 **정량화 불가능한 리스크 공백**을 채우는 대표 사례다.

적용 위치는 세 곳이다.
1. **08:00~08:30 장전 배치**: 뉴스/커뮤니티 점수 산출
2. **Hot Path LightGBM 입력 피처**: 숫자 피처로만 편입 (LLM raw input 아님)
3. **Risk Fast sidecar**: divergence가 threshold를 넘으면 uncertainty penalty / size cut rule 발동

### 2.2 유니버스
- **Trade Universe SSOT = 목표 30종목 (현재 active 20 + pending 10)**
- 확정 섹터: 반도체, 조선, 2차전지, 방산
- 미확정: 나머지 2섹터 (바이오시밀러, K-콘텐츠 후보)
- 종목 선정 기준: 섹터 내 시총 Top 5 (단, 유동성/거래량 고려)
- **장중 Hot/Cold core는 active 20 기준**으로 동작한다. pending 10은 `universe_config.yaml`에서 수동 승인 전까지 Hot Path/Backtest 기본 대상이 아니다.
- **active 20 의미**: 종목 선정 완료된 30종목 중 현재 운용 가능한 20개 (pending 10은 데이터 미충족/수동 승인 대기). **매 1분 LightGBM 추론 후 Top-10 을 랭킹 선정**하며, "Top-10"은 active 20 중 당일 신호 상위 10개를 뜻한다. active/pending 자체는 매분 변경되지 않는다.
- **Sprint 5**에서 trade universe와 별도로 `watch_universe_kospi200.yaml`을 도입하여, KOSPI200 전체를 **이벤트 감시 전용 watch universe**로 확장한다. trade universe와 watch universe를 분리해 SSOT 충돌을 막는다.

**`new/config/sector_config.yaml`**: KOSPI 6 섹터 (IT/반도체, 2차전지, 방산, 조선, 기타 2개 pending) + active 20 ticker 매핑. L2 `sector_tracking_error` 집계 전제 (`new/docs/evaluation_metrics.md` §3). Sprint 4 pending 10 섹터 확정 후 보강 예정.

### 2.3 전처리 파이프라인

```
Raw 1분봉 (8 features × active 20종목)
    ↓
① Robust Z-score 정규화 (MAD 기반)
   x̃ = (x - Median_i(x)) / (MAD_i(x) + ε)
   ※ 표준편차 대신 MAD → 이상치에 강건 (RD-Agent)
    ↓
② 결측치 처리
   1차: forward-fill (이전 값으로 채움)
   2차: cross-sectional mean (횡단면 평균)
   (RD-Agent)
    ↓
③ Multi-scale 분해
   1분 → 5분 → 30분 → 60분 (average pooling)
   각 스케일에서 Top-down Conv1D (트렌드) + Bottom-up Conv1D (계절성)
   (AlphaGAT CATimeMixer)
    ↓
④ Dual-Source 점수 생성 (08:00 batch, v3)
   news_score_t / comm_score_t_1 / comm_score_t_2 / news_comm_divergence / community_noise_multiplier
   ※ 뉴스는 FinBERT/로컬 분류기, 커뮤니티는 spam/manipulation/sentiment_dict 기반 점수화
    ↓
⑤ TSFresh 통계 추출 → 자연어 변환 (에이전트용)
   최근 30분 통계: min, max, median, trend, volume_ratio
   → "최근 30분 삼성전자: 최저 71,500원, 최고 72,300원, 추세 하락, 거래량 급증"
   (TradeXpert)
    ↓
   **[Cold Path TextPack 경로 — S2-6 실구현]**
   NaverNewsClient / CommunityCrawler → `NewsFilter.filter()` (ticker/sector/market 3-level 매칭)
   → BarBuffer 30분 윈도우 → TSFresh `extract_features`
   → `TextPackBuilder.build()` (13 자연어 템플릿, `news_filter.yaml text_pack_templates` SSOT)
   → `NewsAgent.consume_text_pack()` (S2-7 LLMRouter 실구현 완료 (2026-04-23))
    ↓
⑥ Reprogramming (선택, 고급)
   OHLCV → patch embeddings → cross-attention → LLM 어휘의 text prototype 매핑
   → LLM이 시계열을 "텍스트처럼" 이해
   (TradeXpert)
```

### 2.4 PIT-Safety 원칙
- **LLM에 raw 가격 데이터 직접 노출 금지** (RD-Agent)
- schema-level 정보 + TSFresh 통계 자연어만 전달
- 퀀트 모델만 raw data 직접 처리
- 미래 데이터 사용 절대 금지 (기존 원칙 유지)

---

## 3. Layer 3: Model Layer (예측 + 팩터 진화)

### 3.1 퀀트 모델 (장중 추론)

#### 입출력
```
입력: 8 features × active 20종목 × k timesteps (multi-scale) + dual-source 5피처
출력: active 20종목 예측 시그널 + confidence
```

#### 모델: LightGBM (확정)

**선택 근거**:
- RD-Agent(Q) 증명: LightGBM + 자동 팩터 → IC 0.0532, ARR 14.21% (Transformer/LSTM 전부 능가)
- 추론 속도: ~0.3ms (active 20 기준) — 1분봉 병목 제로
- 해석 가능: feature importance → cause 설명 (교수님 요구)
- GPU 불필요: CPU만으로 학습 + 추론
- 매일 재학습: 1~5분 (장마감 후 빠른 진화 루프)
- 이미 구현됨: 기존 코드베이스에 LightGBM 존재
- 4개 논문 검증: RD-Agent, AlphaAgent, TradeXpert, AlphaGAT

**LightGBM의 시계열 약점 → alpha factor가 보완**:
- LightGBM 자체는 시계열 못 봄 (각 행 독립 처리)
- 해결: LLM이 자동 생성하는 alpha factor가 과거 정보를 피처로 변환
- 예: SMA_5min, rolling_max_60min, volume_ratio_1m_vs_20m, sector_corr
- RD-Agent/AlphaAgent 증명: "모델이 단순해도 팩터가 좋으면 이긴다"

**역할 분담**:
- LLM(GPT-4o) = 팩터 생성 (LightGBM의 약점 보완)
- RL(bandit+PPO) = 프로세스 관리 (에이전트 조율 + 진화 방향) + 비중 배분 (PPO Allocator)
- LightGBM = 예측 실행 (빠르고 해석 가능)
- 에이전트 = 판단 주도권 (핵심 차별점)

**v3 즉시 반영 — Dual-Source Feature Pack**
- `news_score_t`: 당일 뉴스/공시 점수 (빠른 decay)
- `comm_score_t_1`, `comm_score_t_2`: 전일/전전일 커뮤니티 점수 (지연 반영)
- `news_comm_divergence`: 두 소스 간 방향 불일치 → uncertainty
- `community_noise_multiplier`: 게시량 급증 시 커뮤니티 가중치 감쇠

이 5개는 **텍스트를 직접 LightGBM에 넣는 것이 아니라, 텍스트를 숫자로 변환한 후 편입하는 방식**이라 역할 분리 원칙을 깨지 않는다. 장중 LLM budget도 증가시키지 않는다.

```
Alpha Factor (LLM 자동 생성) × active 20종목
    ↓
Multi-scale 피처 (1분/5분/30분/60분 rolling stats)
Cross-Asset 피처 (종목 간 correlation, sector mean)
미국 시장 피처 (US Overnight, 매일 08:30 수집):
  - us_sp500_change: S&P500 전일 대비 변화율
  - us_nasdaq_change: NASDAQ 전일 대비 변화율
  - us_vix: VIX 종가 (변동성 지수)
  - us_soxx_change: SOXX (반도체 지수) 전일 대비 변화율
  → LightGBM 입력 피처로 포함. "어제 밤 미국 → 오늘 한국" 패턴 학습.
  → Risk Agent도 동일 피처 사용 (us_vix → Regime Gate 판단).
    ↓
LightGBM (n_estimators=500, depth=4, early_stop)  # risk_config.yaml SSOT
    ↓
출력: active 20종목 예측 시그널 + confidence
```

**장중 재학습 안 함**:
- 모델: 어젯밤 학습한 것 고정, 추론만 (0.3ms)
- 적응: 에이전트가 실시간 담당 (이게 멀티에이전트의 존재 이유)
- 재학습: 장마감 후 1~5분

#### RL 기법 차용 전체 목록 (모델 자체는 RL 아님)
| RL 기법 | 용도 | 출처 | 위치 |
|--------|------|------|------|
| Thompson Sampling | 에이전트 신뢰도 동적 조율 | RD-Agent | §4.5⑥ |
| Thompson Sampling | 팩터/모델 개선 방향 선택 | RD-Agent | §8.2 |
| feature interpolation | 1분봉 feature 부족 해결 | tIRDPG-LID | §3.1 전처리 |
| 슬리피지 reward | 거래비용을 학습에 반영 (한국시장 근거) | KOSPI200 PPO | §3.1 학습, §6.5 |
| PPO Allocator | 종목별 비중 최적화 학습 | AlphaGAT Stage II | §6.5 |

**RL 모델 자체(tIRDPG-LID 등)는 사용하지 않음** — 멀티에이전트 판단 의의 유지

#### can_change_weight 재정의
기존 시스템: `can_change_weight = false` (FDA가 비중 수정 불가, approve/veto만)
새 시스템: **PPO Allocator가 비중을 결정하고, FDA는 approve/veto만**
- 비중 결정 주체: FDA → **PPO Allocator**로 이전
- FDA 역할: 비중은 건드리지 않음. 에이전트 보고서 기반 최종 승인/거부만
- can_change_weight = false 원칙 **유지** (FDA 관점에서). 비중은 PPO가 별도 결정.

#### 학습 설정
- **Loss**: 예측력(L) + λ·정규화(R_g) 교대 최적화 (AlphaAgent)
- **거래비용 반영**: 슬리피지+수수료를 loss에 포함 (KOSPI200 PPO 근거)
- **Covariance regularization**: L = L_ic + λ·L_cov, λ=0.1 (AlphaGAT)
- **재학습**: 매일 장마감 후 sliding window (1~5분)
- **평가**: 5회 독립 실행 median 보고 (RD-Agent)
- **통계 검증**: paired t-test p < 0.05 필수 (AlphaGAT, AlphaAgent)

### 3.2 Alpha Factor Engine (장마감 진화)

#### Operator Library (AlphaAgent)
```
표준 연산 목록:
  rolling_min, rolling_max, rolling_mean (SMA)
  EMA, RSI, VWAP_deviation
  volume_ratio, price_range, turnover_rate
  conditional (if-then), correlation
  rank, ts_argmax, ts_argmin
  sector_mean, sector_std (섹터 기준)
  cross_asset_corr (종목 간 상관)
```

#### Factor Zoo 관리
```
각 팩터 저장 형식:
{
  hypothesis: "가설 4요소",
  description: "자연어 설명",
  expression: "수식",
  ast: "Abstract Syntax Tree 표현",
  ast_hash: "AST 해시 (빠른 비교용)",
  ic_history: [월별 IC 추이],
  created_at: "생성일",
  status: "active / decayed / retired"
}

중복 탐지:
  s(f_i, f_j) = max_{subtrees} {|t_i| : t_i ≅ t_j}  (AST subtree isomorphism)
  S(f) = max_{φ∈Z} s(f, φ)
  IC_max ≥ 0.99면 중복 → 자동 제거 (RD-Agent)

통합 저장 (교차 시너지 #1):
  KB와 Factor Zoo를 통합 — AST를 KB 항목에 포함
  → 별도 Factor Zoo 불필요
```

#### 3중 정규화 (AlphaAgent)
```
R_g(f,h) = α₁·SL(f) + α₂·PC(f) + α₃·ER(f,h)

SL(f): AST 노드 수 (symbolic length) → 복잡도
PC(f): 자유 파라미터 수 (윈도우 크기 등) → 과적합 위험
ER(f,h) = β₁·S(f) + β₂·C(h,d,f) + β₃·log(1+|F_f|)
  β₁: 유사도 페널티 (독창성)
  β₂: 정합 페널티 (가설↔수식)
  β₃: 피처 수 페널티 (간결성)

C(h,d,f) = α·c₁(h,d) + (1-α)·c₂(d,f), α=0.5
  c₁: 가설→설명 정합 (LLM 평가)
  c₂: 설명→수식 정합 (LLM 평가)
```

#### Alpha Decay 모니터링
```
팩터별 monthly IC 추적
  → IC 3개월 연속 하락 → decay 경고
  → IC 6개월 연속 하락 → 자동 은퇴 (retired)
  → 은퇴 팩터는 Factor Zoo에서 비활성화 (삭제는 안 함)
```

> Alpha Decay 임계값(3개월 경고, 6개월 은퇴)은 risk_config.yaml에서 로드. 하드코딩 금지.

---

## 4. Layer 4: Agent Layer (판단 + 소통 + 자기 개선)

### 4.1 에이전트 구성 (v3: 5 core + 1 Mode B validator = 6개)

**장중 5 Core Agents (Hot Path + Cold Path)**

| 에이전트 | 전문 데이터 | 역할 | LLM | 핵심 능력 |
|---------|----------|------|-----|---------|
| News Agent | 뉴스/공시/커뮤니티 | 텍스트 → 투자 영향 판단 | Kanana-o | CoT + Skip + Micro Notes |
| Risk Agent | US 야간/거시/지정학/커뮤니티 수급 | **Hot: Fast rule sidecar (<50ms, LLM 미호출)** + **Cold: Slow 해석 (Kanana-o)** | Hot: 없음 / Cold: Kanana-o | **v3 Fast/Slow 하위 루프** + CoT + Macro Notes + Regime |
| Quant Agent | 1분봉 OHLCV | 모델 래핑 + 시그널 | 불필요 | 빠른 추론 + Anomaly 탐지 (숫자 전담) |
| Debate Agent | 에이전트 보고서 | 의견 충돌 해소 (충돌 시만) | Kanana-o | CoT + 양측 근거 분석 |
| FDA | 모든 보고서 | 오케스트레이터 + 최종 판단 | Kanana-o / GPT-4o (Cold) / 비LLM (Hot) | CoT + Bandit + 종합 |

> **v3 변경 — Risk Agent 내부 Fast/Slow 하위 루프**:
> - **Risk Fast Path**: rule-based event-driven **sidecar** (<50ms, LLM 미호출). Hot Path **본선이 아닌 병렬 sidecar**로, 주 코어(Quant→PPO→PM→FDA)를 block하지 않는다. 커뮤니티/수급 이상 감지 (comm_volume_spike, comm_sentiment_delta, 급락 이상치). trigger rule은 `risk_config.yaml trigger_catalog`에서 로드.
> - **Risk Slow Path**: 기존 Cold Path LLM 해석자 유지 (Kanana-o 비동기, 수초). "왜 이것이 risk인가"를 CoT로 해석.
> - **역할 분리**: Quant Agent = 숫자 anomaly 전담, Risk Fast = 커뮤니티/수급 rule 전담. 탐지 도메인이 다르므로 중복 아님.

**Mode B 1 Validator Agent (v2.2 신규)**

| 에이전트 | 작동 시점 | 역할 | LLM | 핵심 능력 |
|---------|---------|------|-----|---------|
| Backtest Agent | Mode B only (18:00~22:00) | factor/model/allocator 후보의 시스템 전체 검증 | GPT-4o | walk-forward + replay + ablation + 회귀 위험 탐지 → 22:00 배포 게이트 |

> **v2.2 경계 원칙**: Backtest Agent는 장중 경로에 절대 개입하지 않는다. Shared Message Pool(Mode A)과는 별도 루프로 동작하며, C12/C13 계약서에 forbidden_permissions으로 명시되어 있다.
>
> 참고: Alpha Factor Engine 내부의 **Idea / Factor / Eval Agent** 3개는 §8.3에서 별도 다룬다 (Mode B 팩터 진화 루프). 이들은 factor 단위 로컬 평가자이며, Backtest Agent(시스템 검증자)와 역할이 다르다.

### 4.2 Agent Profile 상세 정의 (MetaGPT)

```python
NewsAgent = {
    "name": "News Agent",
    "profile": "한국 주식 뉴스/공시 분석 전문가",
    "goal": "뉴스/공시의 투자 영향을 판단하고 CoT reasoning 제시",
    "constraint": [
        "투자 무관 뉴스는 스스로 skip (AAPM 자율판단)",
        "판단에 반드시 Reasoning + Evidence + Prediction 포함",
        "확신 없으면 uncertainty 명시 (MetaGPT 불확실성)",
        "raw 가격 데이터 직접 접근 금지 (PIT-Safety)"
    ],
    "subscribes_to": ["naver_news", "dart_disclosure", "community_signal"],
    "publishes": ["news_signal", "dart_alert", "sentiment_update", "theme_score"],
    "theme_analysis": {
        "description": "커뮤니티(종토방/테마주 게시판) 언급 빈도 + 감성 → 종목별 테마 점수",
        "output": "theme_score: float (-1~+1). 양수=테마 관심 급증, 음수=관심 하락",
        "decay": "TTL < 1일 (RTG는 빠르게 소멸)"
    },
    "memory": {
        "type": "micro_notes",
        "scope": "종목별 관찰 메모 누적",
        "decay": "지수 감쇠 커널 η∈(0.95,0.99)",
        "performance_metrics": {
            "anomaly_precision": "이벤트 예측 정밀도 (TP / (TP+FP))",
            "false_positive_event_trigger_rate": "잘못된 이벤트 트리거 비율",
            "sector_contribution": "섹터별 뉴스 판단 기여도"
        }
    }
}
```

**NewsAgent (S2-7 실구현, 2026-04-23, Cold Path)**

- 이벤트 수신: `EventGateway.register_handler`로 event_type 3종 (`news` / `dart` / `community`) 자동 구독 (`attach_to_gateway`)
- LLM: `LLMRouter.call(mode='cold', caller='news_agent')` Kanana-o CoT. DI 주입 (`__init__(llm_router, pubsub, memory_root)`)
- 출력: C5 `news_signal` payload (`stance` + `impacted_tickers` + `impacted_sectors` + `narrative`)
- publish channel 분기: `news`/`community` → `news_signal`, `dart` → `dart_alert`
- Memory JSONL:
  - micro: `artifacts/agent_memory/news_agent/{ticker}/{YYYYMMDD}.jsonl`
  - macro: `artifacts/agent_memory/macro/{YYYYMMDD}.jsonl`
- Dual-Source: 뉴스 vs 커뮤니티 divergence 감지는 Sprint 4 S4-1 `DualSourceScorer`와 연계
- LLM content 파싱: Kanana prompt-level JSON instruction + local parser fail-closed. JSON parse 실패 시 heuristic fallback (`buy`/`sell`/`neutral` 키워드)
- narrative 최대 길이: `text_pack_settings.narrative_max_chars` (`news_filter.yaml` SSOT, 기본 200). 하드코딩 금지 원칙 5 준수
- 구현 파일: `new/src/agents/cold/news.py` (S2-7, pytest 612 → 640)

```python
RiskAgent = {
    "name": "Risk Agent",
    "profile": "시장 리스크 감시 전문가",
    "goal": "퀀트 모델이 포착 못하는 리스크를 감지하고 경고",
    "constraint": [
        "정량 데이터 판단은 퀀트에 위임, 정성적 판단만 수행",
        "반드시 Risk Level + Reasoning 출력",
        "US 야간 시장 데이터를 장 시작 전에 분석"
    ],
    "subscribes_to": ["us_market", "ecos_macro", "news_signal", "quant_alert", "investor_flow"],
    "publishes": ["risk_warning", "regime_change", "veto_recommendation"],
    "investor_flow_analysis": {
        "description": "외국인/기관/개인 순매수 모니터링",
        "trigger": "외국인 순매도 급증 (종목별 또는 시장 전체)",
        "action": "risk_warning publish + veto_recommendation 고려",
        "example": "삼성전자 외국인 순매도 500억 이상 → risk_level: high"
    },
    "memory": {
        "type": "macro_notes",
        "scope": "거시경제 상황 누적 요약",
        "decay": "지수 감쇠 커널 η∈(0.95,0.99)",
        "performance_metrics": {
            "veto_post_performance": "veto 이후 실제 가격 변동 (veto가 정확했는가)",
            "anomaly_precision": "리스크 경고 정밀도",
            "false_positive_event_trigger_rate": "잘못된 리스크 경고 비율",
            "sector_contribution": "섹터별 리스크 판단 기여도"
        }
    }
}

QuantAgent = {
    "name": "Quant Agent",
    "profile": "퀀트 모델 래퍼 + 시그널 생성기",
    "goal": "1분봉 예측 시그널 생성 + 이상 탐지",
    "constraint": [
        "모델 추론만 수행, 정성적 판단은 다른 에이전트에 위임",
        "이상 탐지(변동성 급증 등) 시 anomaly_detected publish",
        "추론 속도 최우선 (밀리초 단위)"
    ],
    "subscribes_to": ["kis_1min_bar"],
    "publishes": ["quant_signal", "quant_alert", "anomaly_detected"],
    "memory": {
        "type": "prediction_history",
        "scope": "예측 이력 + 정확도 추적",
        "decay": "없음 (예측 이력 전체 보존, 장기 정확도 추적용)",
        "performance_metrics": {
            "prediction_accuracy": "예측 시그널 vs 실현 수익 정확도",
            "realized_pnl_contribution": "실현 수익 기여도 (attribution)",
            "slippage_execution_shortfall": "예측 가격 vs 체결 가격 차이",
            "anomaly_precision": "이상 탐지 정밀도"
        }
    }
}

DebateAgent = {
    "name": "Debate Agent",
    "profile": "의견 충돌 해소 + 종목 pairwise 비교 전문가",
    "goal": "에이전트 간 의견 충돌 해소 + 충돌 시 Top-10 종목 pairwise 재랭킹",
    "constraint": [
        "퀀트 시그널과 에이전트 보고서 충돌 감지 시에만 활성화 (평상시 미호출)",
        "충돌 기준: quant_top10 vs agent_veto, quant_buy vs risk_high, news_sell vs quant_top5",
        "양측 의견을 공정하게 분석",
        "반드시 근거 기반 결론 제시",
        "pairwise 비교는 모든 쌍(45회) 수행 (비이행적 comparator)",
        "비충돌 시: 에이전트 보고서를 FDA 참고 정보로 전달, 랭킹 변경 없음"
    ],
    "subscribes_to": ["conflict_detected", "pairwise_request"],
    "publishes": ["debate_resolution", "pairwise_ranking"],
    "memory": {
        "type": "debate_history",
        "scope": "과거 논쟁 이력 + 결과 + pairwise 정확도",
        "decay": "없음 (전체 이력 보존, 세션 단위 아닌 장기 누적)",
        "performance_metrics": {
            "prediction_accuracy": "pairwise 랭킹 vs 실현 수익 일치도",
            "sector_contribution": "섹터별 랭킹 정확도"
        }
    }
}

FDA = {
    "name": "FDA (Orchestrator)",
    "profile": "최종 투자 판단 오케스트레이터",
    "goal": "모든 에이전트 보고서 종합 → approve/veto + CoT reasoning",
    "constraint": [
        "모든 활성 에이전트 보고서 수신 후 판단 (의존성)",
        "반드시 CoT reasoning 포함 (cause 중심)",
        "Thompson Sampling으로 에이전트 신뢰도 반영",
        "불확실하면 veto 우선",
        "비중 수정 불가 (can_change_weight = false). approve/veto만."
    ],
    "subscribes_to": ["news_signal", "risk_warning", "quant_signal",
                       "debate_resolution", "regime_change", "dart_alert"],
    "publishes": ["final_decision"],
    "action_schema": {
        "approved": "bool — 주문 계획 승인 여부",
        "target_weights": "{ticker: weight} — PPO Allocator가 결정한 비중 (FDA는 읽기만)",
        "order_deltas": "[{ticker, side, qty, reason}] — Portfolio Manager가 생성, FDA는 검토 후 approved 여부만 결정. FDA가 order_deltas를 직접 생성/수정하면 can_change_weight=false 원칙 위반.",
        "veto_reason": "string | null — 거부 시 CoT reasoning",
        "reason_code": "string | null — cause 분류 코드 (approve/veto 양쪽 필수). S2-9 완료(2026-04-26) 7종 최종 확정: NEWS_DIVERGENCE/RISK_FAST_TRIGGER/DEBATE_CONFLICT/NORMAL_APPROVE/TIMEOUT/QUANT_ANOMALY/MISSING_PORTFOLIO_PATCH. SSOT: risk_config.yaml reason_code_catalog(status=final). veto_reason과 분리: reason_code = 원인 코드, veto_reason = 거부 사유 텍스트. 교수님 피드백 #5 cause 중심 대응 필드.",
        "risk_overrides": "[{rule, original, override, justification}] — audit metadata 전용. FDA가 실제 리스크 규칙을 변경하는 경로로 사용 금지. 승인/거부 근거 기록용.",
        "confidence": "float [0,1] — FDA 판단 확신도",
        "expiry": "ISO8601 — 이 판단의 유효 기한"
    },
    "runtime_guard": {
        "ILLEGAL_DELTA_MODIFICATION_ATTEMPT": "FDA가 order_deltas를 수정하려 하면 런타임 거부. can_change_weight=false 강제.",
        "MISSING_REASON_CODE": "final_decision 출력 시 reason_code null + approved=true 조합은 cause 누락으로 런타임 거부."
    },
    "memory": {
        "type": "decision_history",
        "scope": "판단 이력 + 결과 피드백",
        "performance_metrics": {
            "prediction_accuracy": "승인/거부 판단 vs 실현 수익 정확도",
            "realized_pnl_contribution": "FDA 판단의 수익 기여도",
            "veto_post_performance": "veto 후 해당 종목 실제 변동",
            "slippage_execution_shortfall": "판단→체결 간 가격 차이",
            "sector_contribution": "섹터별 판단 정확도",
            "false_positive_event_trigger_rate": "불필요한 veto 비율"
        }
    }
}

# ─────────────────────────────────────────────────────────────────────
# v2.2 신규 — Backtest Agent (Mode B 전용 시스템 검증자)
# ─────────────────────────────────────────────────────────────────────
#
# CROSS-CONFLICT 해소:
#   - 교수님(2026-04-03): "백테스트 에이전트로 만들어라"
#   - GPT Pro(2026-04-05): "validation tooling으로 빼라"
#   → v2.2 하이브리드: Backtest Agent(C12, 판단 주체) + ValidationTools(C13, 계산 실행자)
#   → 에이전트가 LLM reasoning으로 verdict + diagnostic_notes 생성, 결정론적 계산은 tools가 수행
#   → 두 권고를 동시에 만족하는 단일 설계
#

BacktestAgent = {
    "name": "Backtest Agent",
    "profile": "Mode B 시스템 전체 검증자",
    "goal": "Alpha Factor Engine + Co-STEER + PPO retrain의 출력물을 walk-forward + replay + ablation으로 검증하고, 회귀 위험 시 22:00 배포를 차단한다",
    "mode": "Mode B only (18:00~22:00 KST)",
    "llm": "GPT-4o (Mode B 전용). 장중 Kanana-o 100회/일 예산 보존",
    "constraint": [
        "Mode B 전용. 장중 경로(Hot/Cold)에 절대 개입하지 않는다",
        "target_weights 수정 금지 (PPO Allocator의 고유 권한)",
        "order_deltas 생성 금지 (Portfolio Manager의 고유 권한)",
        "FDA 우회 금지 (FDA = 최종 게이트 원칙 유지)",
        "Hot Path 개입 금지 (장중 1분봉 루프 완전 차단)",
        "production 직접 쓰기 금지 (배포는 별도 게이트 + operator 승인 가능)",
        "결정론적 계산은 C13 ValidationTools에 위임 (LLM은 reasoning만)",
        "verdict는 pass / fail / warn 3개 enum만"
    ],
    "subscribes_to": [
        "factor_candidate",          # Alpha Factor Engine → §8.3
        "model_candidate",           # Co-STEER → §8.4
        "allocator_candidate",       # PPO retrain
        "mode_b_performance_vector"  # 8차원 성과 벡터 → §8.1
    ],
    "publishes": [
        "backtest_report",           # 시스템 검증 결과 (pass|fail|warn)
        "regression_flag",           # 회귀 위험 감지 시 경고
        "deploy_recommendation"      # 22:00 배포 게이트 권고
    ],
    "tools": [
        "BacktestEngine",            # walk-forward 백테스트 (C13)
        "ReplayRunner",              # 1분봉 + 이벤트 deterministic replay (C13)
        "PerformanceAnalyzer"        # regime breakdown + ablation + baseline 비교 (C13)
    ],
    "deploy_decision_gate": {
        # Codex 권고 3 (2026-05-09): C12 spec 정합. minute_bar_leakage_check.verdict == "pass" 조건 추가.
        # 이전: verdict + regression_risk 만 검증 → leakage 위반도 통과 가능 (PIT-Safety 우려).
        "condition": "verdict == 'pass' AND regression_risk.flagged == false AND minute_bar_leakage_check.verdict == 'pass'",
        "on_pass": "22:00 배포 승인 (human_approval flag에 따라 자동 또는 operator 확인)",
        "on_warn": "operator 수동 확인 필수 (human_approval=true)",
        "on_fail": "22:00 배포 차단 + dead_letter_log 기록 + baseline 유지"
    },
    "memory": {
        "type": "backtest_history",
        "scope": "과거 검증 bundle + 회귀 위험 기록 + 배포 후 실 성능 추적",
        "decay": "없음 (전체 이력 보존)",
        "performance_metrics": {
            "backtest_precision": "pass 판정 후 실제 prod에서 기대 성능 유지 비율 (precision of 'pass')",
            "regression_catch_rate": "회귀 위험 조기 탐지율 (recall of regression)",
            "verdict_accuracy": "pass|fail|warn 판정의 사후 정확도"
        }
    },
    "forbidden_permissions": [
        "target_weights_modification",
        "order_deltas_generation",
        "fda_bypass",
        "hot_path_intervention",
        "shared_message_pool_publish_during_market_hours",
        "production_direct_write"
    ],
    "_note_c12_vs_c14": "C12 BacktestAgent forbidden_permissions = 6개 (위). C14 ModeBScheduler forbidden_permissions = 4개 (별도 집합, api_contracts.md C14 참조). 두 집합은 다른 권한 경계이므로 혼용 금지.",
    "runtime_guard": {
        "HOT_PATH_INTERVENTION_ATTEMPT": "Backtest Agent가 장중 시간대에 publish 시도 시 런타임 거부",
        "FORBIDDEN_FIELD_IN_OUTPUT": "output에 target_weights/order_deltas/approved/veto_reason/portfolio_patch_id 포함 시 런타임 거부"
    },
    "sla": {
        "max_runtime": 3600,
        "deadline": "21:30 KST (22:00 배포 게이트 30분 전)",
        "retry_policy": "1회 재시도 후 fail → baseline 유지"
    }
}
```

> **v2.2 설계 원칙 (Eval ≠ Backtest)**: 기존 Eval Agent (§8.3 Alpha Factor Engine 내부)는 단건 factor에 대한
> **로컬 평가자** (IC/RankIC/AR/IR/MDD 3차원 평가 + 중복 제거)이며, Backtest Agent는 Alpha Factor Engine + Co-STEER + PPO retrain
> **전체 결과**에 대한 **시스템 검증자** (walk-forward + replay + ablation + 회귀 위험 탐지)이다. 두 에이전트는
> 스코프, 입력, 출력, 권한이 모두 다르다. 이름이 비슷하다는 이유로 통합하면 v2.1의 Idea→Factor→Eval 루프가 깨진다.

> **v2.2 의도적 드롭 (Model Monitor)**: 이전 라운드에서 검토되었던 "Model Monitor" 별도 에이전트는 추가하지 않는다.
> Quant Agent memory(§4.2)에 이미 `prediction_accuracy`, `realized_pnl_contribution`, `slippage_execution_shortfall`,
> `anomaly_precision` 지표가 있으므로 내부 모니터링은 충족된다. 단, 사용자-facing metric surface
> (최근 20거래일 예측 신뢰도, top-k precision, confidence calibration 등 UI 노출 레이어)는 추후 UI layer에서 재도입 가능하다.

#### Agent Memory 성과 지표 종합 (GPT Pro 피드백 #5)

| 지표 | 설명 | 관련 에이전트 |
|------|------|-------------|
| `prediction_accuracy` | 예측/판단 vs 실현 수익 정확도 | Quant, Debate, FDA |
| `realized_pnl_contribution` | 실현 수익 기여도 (attribution) | Quant, FDA |
| `veto_post_performance` | veto 이후 해당 종목 실제 가격 변동 | Risk, FDA |
| `slippage_execution_shortfall` | 예측/판단 가격 vs 실제 체결 가격 차이 | Quant, FDA |
| `anomaly_precision` | 이상 탐지/이벤트 예측 정밀도 (TP/(TP+FP)) | News, Risk, Quant |
| `sector_contribution` | 섹터별 판단 정확도/기여도 | 전체 |
| `false_positive_event_trigger_rate` | 잘못된 이벤트 트리거 비율 | News, Risk, FDA |

> 장마감 시 이 지표들이 자동 집계되어 8차원 벡터로 변환 → 에이전트 기여도 분해 → 다음 날 constraint 반영 (§5.6, §8.5 참조)

> **참조**: 이 표의 각 지표는 `new/specs/api_contracts.md` C18 AgentPerformanceContract 에 공식 필드로 등록되어 있다.
> 계산 공식, PIT-Safety 제약, threshold 는 `new/docs/evaluation_metrics.md` §3 "Layer 2" 참조.
> 18:00 KST 이후 배치 집계 + `audit_log.jsonl` schema 는 C18 을 SSOT 로 한다.

### 4.3 구조화된 메시지 프로토콜 (MetaGPT)

```json
{
    "message_id": "MSG-{yyyymmdd}-{UUID8}",
    "content": "삼성전자 긴급 리스크: 실적 하향 공시 + US 반도체 급락",
    "cause_by": "NewsAnalysis + USMarketCheck",
    "sent_from": "RiskAgent",
    "send_to": "FDA",
    "priority": "urgent | normal | low",
    "confidence": 0.85,
    "reasoning": "DART 공시 영업이익 전년 대비 -15%. US SOXX 야간 -2.5%. 동시 발생은 강한 하방 시그널.",
    "evidence_ids": ["DART-20260405-005930", "USM-20260404"],
    "uncertainty": "공시 해석의 정확도 중간. 시장 반응은 아직 미확인.",
    "prediction": "SELL",
    "risk_level": "high",
    "timestamp": "2026-04-05T10:30:00+09:00",

    // --- v2.1 추가 필드 (GPT Pro 피드백 #4) ---
    "ttl": 300,
    "expires_at": "2026-04-05T10:35:00+09:00",
    "scope": "ticker:005930",
    "event_id": "EVT-20260405-DART-005930",
    "supersedes": "MSG-20260405-A1B2C3D4",
    "action_type": "veto_recommendation",
    "portfolio_patch_id": "PP-20260405-A1B2C3D4"
}
```

#### v2.1 추가 필드 설명 (필드 총 20개: 기존 13 + 신규 7)
| 필드 | 타입 | 용도 |
|------|------|------|
| `ttl` | int (초) | 메시지 유효 시간. 초과 시 자동 만료 |
| `expires_at` | ISO8601 | ttl의 절대시각 표현. 캐시 TTL과 연동 |
| `scope` | enum | 메시지 적용 범위: `ticker:{code}` / `sector:{name}` / `market` |
| `event_id` | string | 동일 이벤트에서 파생된 메시지들을 묶는 ID |
| `supersedes` | string \| null | 이 메시지가 대체하는 이전 message_id |
| `action_type` | enum | `signal` / `alert` / `veto_recommendation` / `regime_change` / `resolution` |
| `portfolio_patch_id` | string \| null | 이 메시지가 유발한 포트폴리오 변경 추적 ID |

에이전트 소통과 KB 저장에 **동일 형식 사용** (교차 시너지 #3).

### 4.4 Shared Message Pool + Pub/Sub (MetaGPT)

> Shared Message Pool은 **blackboard pattern** (Erman et al., 1980)의 구현. 에이전트들이 공유 작업공간에 읽고 쓰며, 단순 prompt chaining이 아닌 운영 가능한 멀티에이전트 통신 기반. (M-3)

```
┌─────────────────────────────────────┐
│         Shared Message Pool          │
│                                      │
│  News Agent ──publish──→ [Pool]      │
│  Risk Agent ──publish──→ [Pool]      │
│  Quant Agent ─publish──→ [Pool]      │
│                                      │
│  [Pool] ──subscribe──→ FDA           │
│  [Pool] ──subscribe──→ Debate Agent  │
│                   (conflict 시에만)   │
└─────────────────────────────────────┘

메시지 전달 보장: at-least-once. 의존성 기반 활성화(dependency_activation) 지원.
필터링: 각 에이전트는 subscribes_to에 정의된 메시지 유형만 수신
의존성: FDA는 News+Risk+Quant 모두 완료 후 활성화
```

> **v2.2 경계 명시**: Backtest Agent는 **이 Shared Message Pool과 무관**하다.
> Backtest는 Mode B 전용이며 자체 KB artifact 레퍼런스(`bundle_id`, `backtest_run_id`, `replay_trace_ref`)로
> Alpha Factor Engine + Co-STEER + PPO retrain의 출력물을 받는다. 장중에는 publish/subscribe 권한이 없다 (C12 forbidden_permissions 참조).

### 4.5 에이전트 작동 메커니즘

#### ① 환경 모니터링 + 자율 행동 (MetaGPT)
- 각 에이전트가 Message Pool을 상시 관찰
- 관련 메시지 감지 시 자동 활성화
- 스크립트 호출이 아닌 **이벤트 기반 트리거**

#### ② 의존성 기반 병렬 실행 (MetaGPT)
```
News Agent ──┐
Risk Agent ──┼── 병렬 (의존성 없음)
Quant Agent ─┘
       ↓ (모두 완료 후)
   FDA 활성화
       ↓ (충돌 감지 시)
   Debate Agent 활성화
```

#### ③ 반복 정제 + pairwise 비교 통합 (교차 시너지 #2)
```
FDA 판단 루프 (bounded, max 3회):
  Round 1: 에이전트 보고서 종합 → 1차 판단
  Round 2: RAG로 과거 유사 상황 검색 (AAPM) → 판단 보정
  Round 3: [충돌 감지 시] Debate Agent pairwise 비교 (TradeXpert 착안) → 재랭킹
           비충돌 시: 퀀트 점수 순 랭킹 유지, Debate 미호출
```

#### ④ 3단계 텍스트 필터링 (LLM pretrained 규칙)

모든 텍스트 데이터(뉴스/DART/커뮤니티)는 동일한 3단계 구조로 처리한다.
LLM은 장중에 텍스트를 직접 읽지 않는다. LLM이 만든 규칙이 읽는다.

```
1차: 규칙 매칭 (LLM 없음, ms 단위)
  뉴스: news_filter.yaml 키워드 3-Level 매칭
  DART: dart_rules.yaml 공시 유형별 중요도 분류
  커뮤니티: spam_rules.yaml 스팸 제거 + manipulation_rules.yaml 조작 탐지
  → 매칭 없음/스팸/조작 → 버림. LLM 안 부름.

2차: 통계 집계 (LLM 없음, 커뮤니티 전용)
  종목별 mention_count, simple_sentiment (sentiment_dict.yaml 기반)
  spike 감지 (spike_ratio > 3.0), 감성 급변, 신규 키워드
  → 이상 없음 → theme_score만 갱신하고 끝.

3차: Kanana-o 해석 (이벤트 시만)
  1차/2차 통과 + 이상 감지된 텍스트만 → Kanana-o 심층 분석
  뉴스: "투자에 실질적 영향 있나?"
  DART: "이 공시의 영향은?"
  커뮤니티: "진짜 모멘텀인가 조작인가?"

효과: 하루 수천 건 텍스트 → 1차 ~150건 → 2차 ~30건 → 3차 Kanana-o ~50회

LLM pretrained 규칙 5종 (Mode B에서 GPT-4o가 매일 밤 갱신):
  ① news_filter.yaml — 뉴스 키워드 (놓친 뉴스 분석 → 키워드 추가)
  ② dart_rules.yaml — 공시 중요도 (잘못 분류한 공시 재평가)
  ③ spam_rules.yaml — 스팸 패턴 (새 스팸 패턴 추가)
  ④ sentiment_dict.yaml — 감성 사전 (신조어/은어 추가)
  ⑤ manipulation_rules.yaml — 조작 패턴 (새 조작 패턴 추가)

에러 처리:
  크롤링 실패 → 해당 소스 skip. 다른 소스 정상 진행. 3회 연속 실패 시 알림.
  규칙 매칭 오류 → pass-through (의심스러우면 버리지 말고 Kanana-o에 넘김).
```

#### ④-1 커뮤니티 데이터 처리 세부

커뮤니티(종토방)는 노이즈가 극심하므로 특별 처리한다.

```
1단계 집계 (LLM 없음, 매 5분):
  종목별: mention_count_1h, avg_mention_30d, spike_ratio
  감성: sentiment_dict.yaml 기반 simple_sentiment
  필터: spam_rules.yaml + manipulation_rules.yaml

2단계 이상 감지 트리거 (LLM 없음):
  T1: 언급량 급증 (spike_ratio > 3.0)
  T2: 감성 급변 (sentiment 변화 > 0.3 / 1시간)
  T3: 신규 키워드 (30일 미출현 키워드가 10회+)
  T4: 다종목 동시 급증 (같은 섹터 3종목+ 동시 spike)
  → 트리거 없음 → theme_score만 갱신. LLM 안 부름.

3단계 Kanana-o (~5회/일):
  트리거 발생 시 → Kanana-o에 상위 10개 글 제목 + 통계 전달
  → "진짜 모멘텀 / 루머 / 조작 / 공포" 판단
  → theme_score 조정 + Message Pool publish

핵심 원칙: 커뮤니티는 보조 신호. 퀀트를 대체하지 않는다.
  theme_score 높음 + 퀀트 매수 → 매수 확신 강화
  theme_score 높음 + 퀀트 무시그널 → 매수 안 함
```

#### ④-2 DART 공시 처리 세부

DART는 양은 적지만 (하루 0~3건) 하나하나가 중요하다.

```
1차: dart_rules.yaml 중요도 분류 (LLM 없음):
  critical (즉시 Kanana-o): 영업이익 하향, 대표이사 변경, 합병, 유상증자
  important (Kanana-o): 분기 실적, 수주, 자사주
  low (무시): 임원 보수, 주총 소집, 사업보고서

2차: critical/important만 Kanana-o 분석 (~3회/일)

폴링: 수집 시도 1분 / freshness 1시간 TTL (`risk_config.yaml:472` dart=3600 SSOT, 공시 타이밍 불규칙)
```

#### ⑤ CoT Reasoning 필수 (TradeXpert + 교수님)
```
모든 에이전트 출력 필수 포함:
  - Reasoning: "왜 이 판단을 내렸는가" (cause)
  - Evidence: 근거 데이터/소스
  - Prediction: 에이전트별 판단 (※ FDA 최종 출력은 action_schema §4.2 참조)
  - Uncertainty: 확신 없는 부분 명시 (MetaGPT)
```

#### ⑥ 에이전트 신뢰도 Bandit — 장중 (RD-Agent)
```
Thompson Sampling:
  x_t = [변동성, 거래량, 뉴스 유무, 시간대, regime]
  A = {Quant 우선, Risk 우선, 균등}

  평상시 → Quant 시그널 우선 (정량 패턴 잘 맞는 시간)
  급변 시 → Risk Agent 우선 (뉴스/급락 더 중요)
  장마감 전 → 균등 (불확실성 높음)

  과거 성과로 Bayesian posterior 갱신 → 적응적 학습
```

#### ⑦ Economy of Minds (MetaGPT)
```
에이전트 기여도 측정:
  "Risk Agent veto가 -3% 손실 방지" → 영향력 +
  "News Agent 잘못된 경고로 기회 손실" → 영향력 -
  → 다음 판단 시 FDA의 가중치에 반영
```

#### ⑧ 동적 LLM 라우팅 (교차 시너지 #5)
```
Thompson Sampling이 "어떤 LLM을 호출할지"도 결정:
  x_t = [시장 상태, 이벤트 유형, 시간대, 변동성]
  A = {Kanana-o만, GPT-4o만, 둘 다, 호출 안 함}

  한국어 뉴스 감지 → Kanana-o
  팩터 이상 감지  → GPT-4o (추론)
  급락 + 뉴스 동시 → 둘 다
  평상시          → 호출 안 함 (퀀트만)
```

### 4.6 1분봉 병목 해결 전략 (TradeXpert)

```
문제: 30종목 × LLM 호출 4.7초 = 141초 > 60초

해결 전략:

[평상시 — 퀀트만 (빠름)]
  퀀트 모델 추론: 수 밀리초
  LLM 에이전트: 호출 안 함
  → 1분 안에 충분

[이벤트 발생 시 — 선택적 LLM 호출]
  트리거 조건:
    - 뉴스/공시 감지
    - 급등/급락 (변동성 > threshold)
    - Regime 변화 감지
    - Quant Agent anomaly 감지

  최적화:
    - 30종목 전부가 아닌 변화 감지된 종목만 LLM 처리
    - 뉴스/펀더멘탈 분석 결과 캐싱 (같은 뉴스 재분석 안 함)
    - 에이전트 보고서 유효 기간 설정 (캐시 TTL)
    - 퀀트 모델로 1차 필터링(Top 10) → LLM은 10종목만

  랭킹:
    - Top 10 필터링 후 pairwise 비교 (45회)
    - 30종목 전체 비교(435회)는 비용 과다 → 2단계 필터링
```

---

## 5. Layer 5: Knowledge Layer (지식 축적 + 검색)

### 5.1 Macro/Micro 이중 노트 (AAPM)

```
Macro Note (Risk Agent 관리, 누적 갱신):
  "2026-04-05: 미중 무역갈등 심화, 반도체 섹터 리스크 높음.
   US 금리 동결 예상. VIX 상승 추세."
  → 매 이벤트마다 누적 갱신
  → 장마감 후에도 보존

Micro Note (News Agent 관리, 종목별 누적):
  005930: "삼성전자 실적 하향 3일 연속. 외국인 순매도. DART 영업이익 -15%."
  000660: "SK하이닉스 HBM 수주 기대감. 긍정 뉴스 2건."
  → 종목별 독립 관리, 누적 갱신

지수 감쇠 커널 (시간 가중치):
  s_d = Σ κ(L,i) · e_{d-L+i}
  κ(L,i) = η^{L-i} / Σ η^{L-j}
  일봉: η∈(0.9, 1.0), L_W∈{7~180일}
  1분봉: η∈(0.95, 0.99), L_W∈{30,60,120,390분} (추정, config 관리)
```

### 5.2 Knowledge Base — 통합 형식 (교차 시너지 #1, #3)

```
모든 경험을 MetaGPT 메시지 형식으로 통합 저장:
{
  "message_id": "KB-{YYYYMMDD}-{UUID8}",
  "content": "반도체 -2% + 외국인 매도 시 SELL이 정답",
  "cause_by": "EvalAgent_backtest",
  "sent_from": "EvalAgent",
  "situation": "반도체 섹터 -2% + VIX 급등 + 외국인 순매도",
  "decision": "SK하이닉스 HOLD",
  "outcome": "30분 후 추가 -1.5% → 잘못된 판단",
  "lesson": "이 패턴에서는 즉시 SELL",
  "hypothesis": "관련 팩터 가설 (있으면)",
  "ast_hash": "관련 팩터 AST 해시 (있으면)",
  "timestamp": "2026-04-05T10:30:00"
}

→ Vector DB에 저장 (similarity 검색용)
→ RAG: 유사 상황 발생 시 Top-K 검색 (AAPM)
→ 에이전트 소통(Message Pool)과 지식 축적(KB)이 같은 형식
```

> ※ KB의 decision 필드는 레거시 자연어 형식(BUY/HOLD/SELL). FDA 최종 출력은 v3 (api_contracts.md C9, approved/veto) 참조.

### 5.3 Knowledge Forest (RD-Agent)
```
팩터 가설 체계적 관리:
  Idea 1: "거래량 수축 + 가격 수렴 = 변동성 확대 전조"
    ├── Area 1: Bollinger Band Squeeze 계열
    │   ├── Factor A: BB_width < threshold (성공, IC 0.03)
    │   └── Factor B: ATR/BB ratio (실패 - complexity_violation)
    └── Area 2: Volume Contraction 계열
        └── Factor C: vol_5d / vol_20d (성공, IC 0.025)

  Idea 2: "외국인 순매도 + 환율 급등 = 하방 리스크"
    └── ...
```

### 5.4 실패 카테고리별 학습 (AlphaAgent)
```
failure_categories = {
  "hypothesis_misalignment": [가설과 수식 불일치 사례],
  "complexity_violation": [AST 과복잡 사례],
  "decay_detected": [초기 IC 좋았지만 급감 사례],
  "crowding_risk": [기존 팩터와 유사도 높은 사례],
  "execution_failure": [코드 에러/실행 불가 사례]
}
→ 같은 유형 반복 방지
→ Factor Agent가 실패 카테고리 참조 후 회피
```

### 5.5 Persistent Caching (RD-Agent)
```
캐싱 대상:
  - 뉴스 분석 결과 (동일 뉴스 재분석 방지, TTL: 장중)
  - 팩터 IC 계산 결과 (매일 갱신)
  - 에이전트 보고서 (TTL: risk_config.yaml `cache.agent_report_ttl_seconds`, 기본 1800초 = 30분)
  - Cross-Asset Attention 결과 (TTL: 1분)
```

> S4-7 구현: backend=sqlite, storage_path=artifacts/cache/persistent_cache.db (risk_config.yaml cache 섹션 SSOT).

### 5.6 장중↔장마감 Memory 순환 연결 (GPT Pro 피드백 #6)

```
┌── 장중 (09:00~15:30) ──────────────────────────────┐
│                                                      │
│  에이전트별 memory 실시간 축적:                       │
│  - News: micro_notes (종목별 이벤트 기록)             │
│  - Risk: macro_notes (거시 상황 갱신)                 │
│  - Quant: prediction_history (시그널 vs 실현, Quant Agent 로컬 JSONL, KB storage_types 미포함)│
│  - Debate: debate_history (pairwise 결과)             │
│  - FDA: decision_history (승인/거부 + 결과)           │
│                                                      │
│  + performance_metrics 7항목 실시간 갱신              │
│                                                      │
└──────────────────────┬───────────────────────────────┘
                       │ 장마감 (15:30)
                       ↓
┌── 자동 집계 (18:00) ────────────────────────────────┐
│                                                      │
│  ① 에이전트별 performance_metrics → 8차원 벡터 변환  │
│     v_agent = [pred_acc, pnl_contrib, veto_perf,     │
│                slippage, anomaly_prec, sector_contrib,│
│                fp_rate, overall_score]                │
│                                                      │
│  ② 에이전트 기여도 분해 (ablation 방식)              │
│     "Risk Agent veto가 -3% 손실 방지" → 기여도 +     │
│     "News Agent 잘못된 경고로 기회 손실" → 기여도 -   │
│                                                      │
│  ③ 8차원 벡터 → Knowledge Base에 저장                │
│     → RAG: 과거 유사 시장 상태에서 어떤 에이전트가    │
│       가장 정확했는지 검색 가능                        │
│                                                      │
└──────────────────────┬───────────────────────────────┘
                       │
                       ↓
┌── 다음 날 반영 (08:30) ────────────────────────────┐
│                                                      │
│  ④ 기여도 기반 constraint 자동 갱신:                  │
│     - Thompson Sampling posterior 업데이트            │
│     - 저성과 에이전트 → 신뢰도 하향                  │
│     - 고성과 에이전트 → 신뢰도 상향                  │
│     - 예: "어제 Risk veto 3건 중 2건 오판             │
│            → 임계값 0.7→0.8 상향"                    │
│                                                      │
│  ⑤ 에이전트 Constraint Prompt 자기 갱신 (MetaGPT)    │
│     - 과거 피드백 리뷰 + 행동 규칙 조정              │
│                                                      │
└──────────────────────────────────────────────────────┘
```

**Performance Vector 변환식**:

```
입력 (7개 에이전트 metrics):
  m1: prediction_accuracy     [0, 1]     높을수록 좋음
  m2: realized_pnl_contribution [-∞, +∞]  양수가 좋음
  m3: veto_post_performance   [-∞, +∞]   양수 = veto가 정확
  m4: slippage_execution_shortfall [0, +∞] 낮을수록 좋음 (부호 반전)
  m5: anomaly_precision       [0, 1]     높을수록 좋음
  m6: sector_contribution     [0, 1]     높을수록 좋음
  m7: false_positive_rate     [0, 1]     낮을수록 좋음 (부호 반전)

정규화: min-max scaling per metric, 30일 rolling window
부호 반전: m4, m7은 (1 - normalized_value)로 변환
클리핑: [0.01, 0.99] (극단값 방지)

8차원 벡터:
  v = [m1_norm, m2_norm, m3_norm, 1-m4_norm, m5_norm, m6_norm, 1-m7_norm, overall_score]
  overall_score = weighted_mean(v[0:7], weights=에이전트별 Thompson posterior)

→ Thompson Sampling Beta(α, β) 업데이트:
  성공(v[i] > 0.5): α += 1
  실패(v[i] ≤ 0.5): β += 1
```

> **순환 루프**: 장중 memory → 장마감 집계 → 8차원 벡터 → 기여도 분해 → 다음 날 constraint 반영 → 장중 memory ...

---

## 6. Layer 1: Execution Layer (실행 + 피드백)

### 6.0 현재 한계 — OMS (DEFER, GPT Pro 피드백 #7)

> 아래 7항목은 현재 미구현이며, 실거래 배포 전 반드시 해결해야 한다. 현 단계에서는 연구용 시뮬레이션으로 제한.

| # | 한계 | 설명 | 해결 시기 |
|---|------|------|---------|
| 1 | 부분 체결 | 주문 수량 전부가 체결되지 않을 때의 잔여 처리 로직 미구현 | Phase 2 |
| 2 | 정정/취소 | 미체결 주문의 정정·취소 API 연동 미구현 | Phase 2 |
| 3 | 주문 분할 | 대량 주문의 TWAP/VWAP 분할 실행 미구현 | Phase 2 |
| 4 | Order Queue | 주문 우선순위 큐 + 순차 실행 관리 미구현 | Phase 2 |
| 5 | 장시작/마감 처리 | 동시호가(08:30~09:00, 15:20~15:30) 별도 로직 미구현 | Phase 2 |
| 6 | WebSocket Failover | KIS WS 연결 끊김 시 재연결 + 데이터 복구 미구현 | Phase 2 |
| 7 | API Rate Limit | KIS REST API 호출 제한(초당 20회) 관리 미구현 | Phase 2 |

### 6.0.1 트레이딩 안전 장치 (실거래 필수)

| # | 안전 장치 | 설명 | 구현 시기 |
|---|---------|------|---------|
| S-1 | **Kill Switch** | 일일 최대 손실 도달 시 전량 시장가 청산 + 시스템 자동 중단. default 임계: 5% (양수 절대값, risk_config.yaml). 트리거: daily_pnl <= -0.05 | Phase 1 (실거래 전 필수) |
| S-2 | **OAuth Token 자동 갱신** | KIS access token 만료 전 자동 refresh. 갱신 실패 시 주문 중단 + 알림 | Phase 1 (실거래 전 필수) |
| S-3 | **주문 검증 (Sanity Check)** | 주문 제출 전: 가격 > 0, 수량 > 0, 종목코드 유효, 주문금액 < 포지션 리밋 확인 | Phase 1 |
| S-4 | **잔고 대조 (Reconciliation)** | 매 사이클 후 시스템 내부 포지션 vs KIS 실제 잔고 비교. 불일치 시 알림 + 시스템 잔고를 KIS 기준으로 리셋 | Phase 1 |
| S-5 | **감사 추적 (Audit Log)** | 모든 주문 제출/체결/취소/오류를 타임스탬프와 함께 JSON 로그. 사후 분석 + 문제 추적용 | Phase 1 |
| S-6 | **수동 개입 (Manual Override)** | CLI 명령 2종: (1) manual_pause → 신규 주문 중단, 포지션 유지 (MANUAL_OVERRIDE). (2) manual_emergency_halt → 전량 청산 + 시스템 중단 (EMERGENCY_HALT) | Phase 1 |

> 이 6항목은 OMS 7건(DEFER)과 별도. OMS는 체결 품질, 안전 장치는 시스템 생존.

### 6.1 주문 실행
```
FDA 최종 판단 (approved/veto + confidence + reasoning). order_deltas는 Portfolio Manager가 생성한 것을 FDA가 검토.
    ↓
approved = true → order_deltas 기반 KIS API 주문 (REST)
approved = false → veto_reason 기록, 실행 안 함
    ↓
체결 확인 + 슬리피지/비용 기록
    ↓
Knowledge Base에 (판단, 실행 결과) 피드백
    ↓
에이전트 Memory 갱신
```

> **FDA 액션 스키마 (v2.1)**: 기존 BUY/HOLD/SELL 3값 → 구조화된 스키마로 통일.
> FDA는 비중 결정 권한 없음 (can_change_weight = false). PPO Allocator가 비중을 결정하고, FDA는 approve/veto만 수행.

> **Mock 체결 모델** (`execution_gateway.py:148-179`): `snapshot_vwap` ±3bps 기준 (vwap_noise uniform), `realized_slippage = (fill_price - snapshot_vwap) / snapshot_vwap` 실계산. C18 audit log `fill_price` / `slippage_execution_shortfall` 연계.

### 6.2 Executable Feedback Loop — Bounded (MetaGPT)
```
판단 → 실행 → 결과 확인
  → 예상과 다르면?
    → 원인 분석 ("왜 틀렸나" — LLM)
    → 에이전트 Memory에 기록
    → 다음 판단에 반영
  → max 3회 수정 시도 (bounded)
  → 시간 초과 시 현재까지 결과로 판단
```

### 6.3 종목 랭킹 — 3단계 확정 파이프라인

```
Step 1: LightGBM → active 20종목 예측 시그널 (0.3ms, dual-source 5피처 포함)
Step 2: Top-10 필터링 (퀀트 점수 순)
Step 3: [충돌 시만] Debate Agent pairwise 비교 (45회) → win count → 재랭킹
         충돌 없으면: 퀀트 점수 순 랭킹 유지, Debate 미호출
         LLM comparator 비이행적 → 충돌 시 모든 쌍 비교 필요
```

> **GPT Pro는 "FDA가 pairwise"를 제안했으나**, 6개 논문 모두 "1에이전트=1역할" 원칙.
> FDA에 pairwise까지 넣으면 역할 비대 → **Debate Agent가 pairwise 전담**.
> TradeXpert의 General Expert도 종합만 하고 분석은 안 함 — 같은 원칙 적용.
> **B안 적용**: Debate는 고정 파이프라인에서 제외. 퀀트 vs 에이전트 충돌(예: 퀀트 BUY vs Risk veto) 감지 시에만 활성화.

> B안 선택 근거: Hot Path에서 퀀트 점수 순 ranking(quant-only fallback)으로 충분한 랭킹 품질 확보.
> Debate는 충돌 시에만 활성화하여 LLM 비용과 레이턴시를 절약.

> **보유종목 처리 (C-3 / held-position review)**: PPO Allocator는 state에 "현재 포지션"을 포함하므로, Top-K에서 제외된 보유종목에는 weight=0이 자연스럽게 할당된다 → 별도 청산 lane 불필요. 단, Top-10 밖 보유종목에 대해 Quant Agent의 anomaly 감지 또는 Risk Agent의 veto 권고가 Cold Path 청산 트리거가 될 수 있음을 명시한다.

보유종목 청산 정책 (C8 계약서 연동):
  - excluded_holding_policy: weight=0 → Portfolio Manager가 sell delta 자동 생성
  - cold_path_exit_trigger: quant_anomaly 또는 risk_veto 시 즉시 청산 경로 진입
  - exit priority > entry priority: 1분봉에서 기존 포지션 축소/청산이 신규 매수보다 우선

**전체 파이프라인 순서 (확정)**:
```
LightGBM active 20종목 시그널
    → Top-10 필터링 (퀀트 점수 순)
    → [충돌 시] Debate Agent pairwise → 재랭킹
    → PPO Allocator → 종목별 비중 결정
    → 상한 규칙 적용 (단일≤20%, 섹터≤40%, 현금≥10% — default)
    → Portfolio Manager → order_deltas 생성  ← C-1: PM이 주문 변경분 생성
    → FDA approve/veto (에이전트 보고서 있으면 참고, 비중 수정 불가)
    → KIS API 주문 실행
```

### 6.4 Bounded Execution (RD-Agent)
```
시간 제한:
  - News Agent: max 10초
  - Risk Agent: max 10초
  - Debate Agent: max 15초
  - FDA 종합 판단: max 30초
  - 반복 정제: max N=3회
  - 팩터 구현 (장마감): max 600초
  - 백테스트 (장마감): max 3600초
→ 시간 초과 시 현재까지 결과로 진행
```

### 6.5 포트폴리오 관리 — PPO Allocator + 상한 규칙 (확정)

**하이브리드 방식**: PPO가 비중을 학습하되, 규칙으로 상한 제약

```
LightGBM → active 20종목 예측 시그널
    ↓
Top-10 선정
    ↓
[충돌 시] Debate Agent pairwise (45회) → 재랭킹 (평상시 퀀트 점수 순 유지)
    ↓
PPO Allocator: K종목에 최적 비중 학습 (AlphaGAT Stage II 차용)
    state  = [퀀트 시그널, 현재 포지션, 시장 상태]
    action = [종목별 비중] (연속값, softmax 정규화)
    reward = (수익 - 거래비용)  ← 슬리피지 포함 (KOSPI200 PPO 근거)
    구현   = stable-baselines3 PPO
    ↓
상한 규칙 적용 (안전장치, 현재 default — final freeze 아님):
    - max 종목 수: 10 (default)
    - 단일 종목 비중 상한: 20% (default)
    - 섹터 비중 상한: 40% (default)
    - 최소 현금: 10% (default)

추가 리스크 규칙 (risk_config.yaml 기반):
    - Regime Gate: VIX proxy ≥90 → red (신규 진입 금지), ≥70 → yellow (비중 50% 감축)
    - Regime Gate: Market Breadth <0.30 → red, <0.45 → yellow
    - Turnover Cap: 일일 최대 회전율 30% (과도한 매매 방지)
    - Min Confidence: LightGBM confidence < 0.03 → Top-10 제외 (S1-2 SSOT)

> 이 값들도 risk_config.yaml에서 로드. 하드코딩 금지.
    ↓
FDA approve/veto (비중 수정 불가)
    ↓
KIS API 주문 실행
```

**PPO가 학습하는 것**: "어떤 배분이 수익을 최대화하는가"
**규칙이 제한하는 것**: "아무리 좋아도 한 종목에 20% 넘게 넣지 마라" (default, 1분봉 백테스트 후 재조정 가능)
**FDA가 하는 것**: "에이전트 판단으로 최종 승인/거부 (비중 수정 없음)"

PPO Allocator 학습:
- 매일 장마감 후 재학습 (LightGBM과 함께)
- 장중에는 학습된 policy로 추론만
- 거래비용 reward에 반드시 포함 (KOSPI 슬리피지 근거)

---

## 7. Mode A: 장중 실시간 매매 루프

### 7.0 2스트림 병렬 구조

시스템은 2개의 독립 스트림이 병렬로 돌고 FDA에서 합류한다:

```
Stream 1 — 퀀트 (매 1분, 독립):
  KIS 1분봉 → 전처리 → LightGBM 추론 → Top-10 → PPO → PM → FDA → 실행

Stream 2 — 이벤트 (상시, 독립):
  Naver News / DART / US Market / ECOS / 커뮤니티 크롤링
  → 이벤트 감지 시 News/Risk Agent 분석 → Message Pool 게시

합류점:
  FDA가 Stream 1(퀀트 시그널) + Stream 2(에이전트 보고서) 종합 → approve/veto

충돌 시:
  퀀트와 에이전트 의견이 상반 → Debate Agent 활성화 → pairwise 재랭킹
```

### 7.1 Hot Path (매 1분 — 퀀트 + Risk Fast sidecar, LLM 미호출) — v3 업데이트
```
② Data: 1분봉 수집 (8 features × active 20종목) + 전처리 + dual-source 5피처 결합
③ Model: 퀀트 모델 추론 → active 20종목 시그널 (0.3ms)
④ Agent 주 코어: Quant Agent → 시그널 publish + 숫자 anomaly 감시
   (PPO Allocator가 현재 포지션 포함 state 기반으로 Top-K 외 보유종목에 weight=0 할당 → 자연 청산)
④ Agent sidecar (v3): Risk Agent Fast Path → rule-based 커뮤니티/수급 이상 감지 (병렬, <50ms, LLM 미호출)
   → 이상 감지 시 risk_warning publish. 주 코어를 block하지 않음.
   → trigger rule은 risk_config.yaml trigger_catalog에서 로드
⑥ Ranking: 퀀트 점수 순 Top-10 → PPO 비중 결정
⑧ PM: order_deltas 생성
④ FDA: 퀀트 시그널 + Risk Fast 경고 기반 approve/veto (빠름, LLM 미호출)
① Exec: approved → PM이 생성한 order_deltas 실행 → 결과 피드백
⑤ Knowledge: 예측 이력 기록
```

> **Hot Path**: 퀀트 추론 + Risk Fast sidecar + 점수 순 랭킹. Debate 미호출. 전체 레이턴시 < 100ms.
> **v3 변경**: Risk Agent Fast Path가 sidecar로 병렬 동작. **Hot Path 주 코어(Quant→PPO→PM→FDA)를 block하지 않음**. 필요할 때만 끼어드는 event-driven rule path.

> **Hot Path file_io 원칙 (v3)**: 장중(09:00~15:30)에는 **파일 I/O 금지**. 장전 산출물은 파일/버전관리, 장중 참조는 메모리/캐시. Hot Path <100ms 보장을 위한 필수 원칙.

> **Hot Path FDA = 비LLM deterministic validator.** FDA는 Hot Path에서도 최종 게이트로 존재한다.
> 다만 LLM이 아닌 규칙 기반 체크리스트로 판단한다:
> ① Regime Gate (green/yellow/red) ② Kill Switch (daily_pnl) ③ Position Limit (단일 20%)
> ④ Sector Limit (섹터 40%) ⑤ Cash Minimum (10%) ⑥ Turnover Cap (30%) ⑦ Min Confidence (0.03)
> 전부 통과 → approve. 하나라도 실패 → veto. <10ms.
> "Hot에서 FDA 없음"이 아니라 "Hot에서 FDA는 비LLM". FDA는 모든 경로의 최종 문지기.

### 7.2 Cold Path (이벤트 트리거 발생 시 — LLM 호출)
```
트리거: 뉴스/공시 감지 | 급등락(σ>threshold) | regime 변화 | anomaly

② Data: 이벤트 데이터 수집 + TSFresh 자연어 변환
④ Agents 활성화 (병렬):
   News Agent → 뉴스 분석 + CoT → publish
   Risk Agent → 리스크 판단 + CoT → publish
   Quant Agent → 이상 탐지 → publish
④ Debate Agent: 퀀트 시그널과 에이전트 보고서가 충돌할 때만 활성화
   - 충돌 기준: 퀀트 Top-10과 에이전트 권고가 상반 (예: 퀀트 BUY vs Risk veto)
   - 활성화 시: pairwise 45회 → 재랭킹 → PPO에 전달
   - 비충돌 시: 에이전트 보고서를 FDA에 참고 정보로 전달, 랭킹 변경 없음
④ FDA: Message Pool에서 구독 → 종합 판단
   - Thompson Sampling 에이전트 신뢰도 반영
   - 반복 정제 (Round 1~3, bounded)
   - CoT reasoning 필수 → approve/veto 출력 (order_deltas는 PM 생성분을 검토)
⑤ Knowledge: (상황, 판단, 결과) 저장 + Notes 갱신
① Exec: approved → 주문 → 체결 → 피드백 → Memory 갱신
```

> **Cold Path**: 이벤트 발생 시. LLM 에이전트 전체 활성화. 레이턴시 10~30초. 변화 감지 종목만 처리.

**Cold Path 이벤트 관리 정책 (C-2)**:
```
(a) Priority Queue: urgent > normal > low 순서 처리 (메시지 priority 필드 활용)
(b) Backlog 제한: 동시 이벤트 최대 3건. 초과 시 priority 낮은 이벤트 드롭
(c) Stale Cancel: expires_at 초과 이벤트 자동 폐기 (ttl/expires_at 필드 활용)
(d) 동일 ticker 중복: supersedes 필드로 최신 이벤트만 유지, 이전 이벤트 자동 취소
(e) 트리거 우선순위: 급락(σ>threshold) > 공시(DART) > 뉴스 > regime > anomaly
(f) Admission Comparator (정렬 기준):
    sort_key = (priority, trigger_type, scope, recency)
    1순위: priority (urgent > normal > low)
    2순위: trigger_type (급락 > DART > 뉴스 > regime > anomaly)
    3순위: scope (market > sector > ticker)
    4순위: recency (최신 우선)

    max_backlog(§C-2 (b))와 max_cold_path_jobs_per_minute(C11)의 관계:
    - max_backlog = 동시 처리 가능한 이벤트 수 (3)
    - max_cold_path_jobs_per_minute = 1분 내 총 처리량 상한
    - backlog가 가득 차면 comparator 하위 이벤트는 drop → dead_letter_log
```

> 이 정책의 공식 계약서: C11 EventAdmissionControlContract (new/specs/api_contracts.md 참조)

#### Cold Path 디스패치 흐름 (S2-0 구현, 2026-04-21 명시화)

```
connector polling (Naver/DART/Community/ECOS/US Market 6소스)
    ↓ raw_event
EventGateway.ingest(raw_event, source)
    ↓ EventNormalizer.normalize() (C2)
    ↓ normalized event
EventAdmission.admit(event) (C11 3 필터 + backlog)
    ↓ admitted
[backlog Priority Queue]
    ↓ EventGateway.dispatch_next()
handler(event) (News/Risk Slow/Debate Agent, register_handler() 등록)
    ↓ 처리 결과
MessagePool.publish(channel, message) (C4, 에이전트 내부에서)
    ↓ subscribe
FDA Cold Path (C4 구독 → Thompson Sampling + CoT → C9 approve/veto)
```

**역할 경계**:
- `EventGateway`: Cold Path 진입 디스패처. handler 직접 호출 (Direct Dispatch).
- `MessagePool` (C4): Gateway 이후 에이전트 간 Pub/Sub 브로커. FDA 는 MessagePool 구독.
- **Risk Fast sidecar 예외**: Hot Path 병렬 sidecar. EventGateway 를 bypass 하고 bar_buffer 에서 직접 trigger_catalog rule 감지 (S2-8 완료, `new/src/agents/hot/risk_fast.py`). Cold Path 이벤트 큐에 들어가지 않는다. Cold Path 전용 이벤트 기반 규칙 평가는 `new/src/agents/cold/risk_fast.py` (이벤트 payload 기반, 6규칙).

**Dropped Events 감사 경로**:
```
C11에서 드롭된 이벤트는 dead_letter_log에 보존한다.
  - 저장: JSON Lines (timestamp, event_id, drop_reason, original_event)
  - 용도 1: §8.5.1 뉴스 키워드 갱신 시 "놓친 뉴스" 분석 소스
  - 용도 2: 사후 감사 (왜 이 이벤트를 버렸는가)
  - 보존 기간: 30일
```

### 7.3 Emergency Path (긴급 경로)

트리거: kill switch 발동 | manual_emergency_halt 명령 | OAuth token 갱신 실패 | 잔고 불일치 감지

동작:
  ① 신규 주문 즉시 중단
  ② 보유 전종목 시장가 청산 주문
  ③ 시스템 상태를 "EMERGENCY_HALT"로 전환
  ④ 알림 발송 (Slack/SMS/이메일)
  ⑤ audit log에 긴급 중단 기록
  ⑥ 수동 해제 전까지 재시작 불가

### 7.4 Runtime State Machine

시스템은 아래 상태 중 하나에 있으며, 상태 전이는 명시적 이벤트로만 발생한다.

**Mode A (장중) 상태 9개 (v3.8: DYNAMIC_OVERLAY_* 2개 추가)**

| 상태 | 설명 | 전이 조건 |
|------|------|---------|
| BOOTSTRAP | 시스템 시작, 모델/config 로딩 중 | 로딩 완료 → HOT_RUNNING |
| HOT_RUNNING | 정상 매매 (Hot Path) | 이벤트 감지 → COLD_RUNNING<br>장 마감(15:30) → MODE_B_IDLE |
| COLD_RUNNING | Cold Path 활성 (LLM 에이전트 동작 중) | 처리 완료 → HOT_RUNNING |
| DEGRADED | 일부 에이전트/커넥터 장애. 퀀트만으로 운영 | 장애 복구 → HOT_RUNNING |
| EMERGENCY_HALT | 긴급 전량 청산 + 시스템 중단. kill switch 또는 manual_emergency_halt. | kill_switch/manual_emergency_halt/OAuth 실패/잔고 불일치 → EMERGENCY_HALT |
| MANUAL_OVERRIDE | 수동 일시정지 (manual_pause). 신규 주문 중단, 기존 포지션 유지. | manual_pause 명령 → MANUAL_OVERRIDE |
| RECOVERY | 긴급 중단 후 복구 중. 포지션 확인 + 잔고 대조 | 검증 완료 → HOT_RUNNING |
| DYNAMIC_OVERLAY_ACTIVE | `risk_config.yaml dynamic_universe.enabled=true` AND `candidate_pool_count >= 1` | `candidate_pool_count == 0` OR `enabled=false` → HOT_RUNNING |
| DYNAMIC_OVERLAY_DISABLED | `risk_config.yaml dynamic_universe.enabled=false` | `enabled=true` AND operator 승인 → HOT_RUNNING |

> **DYNAMIC_OVERLAY_ACTIVE 비고 (Sprint 5)**: Hot Path (LightGBM/PPO/PM/FDA)는 변경 없이 동작한다. overlay는 별도 pub/sub 채널 (`dynamic_overlay_update`) 비동기 발행. trade universe는 기존 20종목과 격리 유지.

**Mode B (장마감) 상태 6개 — v2.2 신규 5개 + v3 MODE_B_BACKTEST 추가**

| 상태 | 설명 | 전이 조건 |
|------|------|---------|
| MODE_B_IDLE | 장 마감 직후, 18:00 Mode B Scheduler 대기 | 18:00 → MODE_B_EVOLVING<br>stage_0 DQR CRITICAL alert → MODE_B_BLOCKED |
| MODE_B_EVOLVING | Alpha Factor Engine + Co-STEER + 에이전트 자기 개선 실행 (18:00~21:00) | 21:00 → MODE_B_BACKTEST |
| MODE_B_BACKTEST | Backtest Agent 검증 실행 (21:00~21:30, v2.2 게이트) | verdict==pass → MODE_B_DEPLOY<br>verdict==warn → MODE_B_OPERATOR_REVIEW<br>verdict==fail → MODE_B_BLOCKED |
| MODE_B_OPERATOR_REVIEW | human_approval 대기 | 승인 → MODE_B_DEPLOY<br>거부 → MODE_B_BLOCKED |
| MODE_B_DEPLOY | 22:00 배포 실행 (model_registry 교체, Factor Zoo 활성화, audit_log 기록) | 완료 → MODE_B_IDLE (다음 날 09:00 → BOOTSTRAP) |
| MODE_B_BLOCKED | Backtest fail 또는 operator 거부. baseline 유지, dead_letter_log 기록 | 완료 → MODE_B_IDLE (다음 날 09:00 → BOOTSTRAP, baseline로 시작) |

상태 전이도:
```
정상 전이 (Mode A):
  BOOTSTRAP → HOT_RUNNING ⇄ COLD_RUNNING
  HOT_RUNNING → DEGRADED → HOT_RUNNING
  ANY → EMERGENCY_HALT → RECOVERY → HOT_RUNNING
  ANY → MANUAL_OVERRIDE → HOT_RUNNING

Sprint 5 Dynamic Overlay 전이:
  HOT_RUNNING + dynamic_universe.enabled=true + candidate_pool_count >= 1 → DYNAMIC_OVERLAY_ACTIVE
  DYNAMIC_OVERLAY_ACTIVE + (candidate_pool_count == 0 OR enabled=false) → HOT_RUNNING
  enabled=false 상태에서는 DYNAMIC_OVERLAY_ACTIVE 전이 자체가 차단됨 (gate.py)

장 마감 전이 (v2.2 신규):
  HOT_RUNNING → MODE_B_IDLE (15:30 장 마감)
  MODE_B_IDLE → MODE_B_EVOLVING (18:00 Mode B Scheduler)
  MODE_B_IDLE → MODE_B_BLOCKED (stage_0 DQR CRITICAL alert 시, 18:00 Scheduler 진입 전 차단)
  MODE_B_EVOLVING → MODE_B_BACKTEST (21:00)
  MODE_B_BACKTEST → MODE_B_DEPLOY (verdict==pass + no regression)
  MODE_B_BACKTEST → MODE_B_OPERATOR_REVIEW (verdict==warn)
  MODE_B_BACKTEST → MODE_B_BLOCKED (verdict==fail OR regression detected)
  MODE_B_OPERATOR_REVIEW → MODE_B_DEPLOY (operator 승인)
  MODE_B_OPERATOR_REVIEW → MODE_B_BLOCKED (operator 거부)
  MODE_B_DEPLOY / MODE_B_BLOCKED → MODE_B_IDLE (각 단계 완료)
  MODE_B_IDLE → BOOTSTRAP (다음 날 09:00 장 시작 직전)

실패 전이:
  BOOTSTRAP → DEGRADED (모델/config 로딩 부분 실패)
  BOOTSTRAP → EMERGENCY_HALT (치명적 실패, 시작 불가)
  COLD_RUNNING → DEGRADED (에이전트 timeout, LLM 장애)
  RECOVERY → EMERGENCY_HALT (복구 실패, 잔고 불일치 지속)
  MANUAL_OVERRIDE → RECOVERY (수동 해제 후 안전 검증 필요 시)
  MODE_B_EVOLVING → MODE_B_BLOCKED (Alpha Factor Engine timeout, Co-STEER 실패, GPT-4o 장애)
  MODE_B_BACKTEST → MODE_B_BLOCKED (SLA 초과, validation tool crash)
  MODE_B_DEPLOY → MODE_B_BLOCKED (model_registry 교체 실패 → baseline 롤백)
```

> **v2.2 원칙**: Mode B 상태는 Mode A 상태와 **물리적으로 분리**된다. 장중 9:00~15:30은 HOT/COLD_RUNNING, 장 마감 후 18:00~22:00은 MODE_B_*, 이 사이(15:30~18:00, 22:00~다음 날 09:00)는 MODE_B_IDLE. 이 분리로 Backtest Agent가 장중 경로에 절대 개입할 수 없음을 state machine 레벨에서 강제한다.

---

## 8. Mode B: 장마감 자동 진화 루프

### 8.0 Mode B Scheduler (v2.2 — 호출 trigger + Orchestrator)

> **누가 18:00/18:30/19:00/20:00/21:00/21:30/22:00 각 단계를 깨우는가?**

Mode B는 **Mode B Scheduler**라는 전용 cron-style orchestrator가 관리한다:

- **Owner**: Mode B Scheduler (BootStrap 시 기동되며, 시스템 생명주기 동안 상주)
- **Trigger 방식**: 시각 기반 cron (`0 18 * * MON-FRI`, `30 18 * * MON-FRI`, ...)
- **Scheduler 책임**:
  1. 매 단계별 상태 전이 발행 (§7.4 MODE_B_* state)
  2. Alpha Factor Engine / Co-STEER / Backtest Agent / Deployer 순차 호출
  3. 단계 실패 시 `MODE_B_BLOCKED` 전이 + dead_letter_log 기록
  4. `mode_b_audit_log` JSONL 기록 (단계, 시작/종료 ts, bundle_id, verdict, operator)
- **Backtest Agent 호출 조건**:
  - `MODE_B_EVOLVING` 완료 AND (factor_candidate OR model_candidate OR allocator_candidate) 존재 시 21:00에 호출
  - 후보가 하나도 없으면 Backtest 건너뛰고 MODE_B_IDLE로 직행 (baseline 유지)
- **배포 실제 주체**: **Mode B Deployer** (Scheduler의 sub-component, Backtest Agent와 별개)
  - Backtest verdict=pass + regression_risk=false 시 22:00 호출
  - model_registry 교체 + Factor Zoo 활성화 + constraint 갱신
  - 실패 시 baseline 롤백 + `MODE_B_BLOCKED`

> **권한 분리 (v2.2)**: Backtest Agent = 검증자(verdict만), Mode B Deployer = 배포 실행자(actual swap).
> Backtest Agent가 verdict=pass를 내도 실제 배포는 Deployer가 수행한다. 이는 C12 `can_write_to_production: false` 원칙과 일관된다.

> **C12 vs C14 권한 경계 (SHIP-fix NEW-3, 2026-05-06)**: C14 ModeBScheduler `forbidden_permissions = 4개` (별도 집합, api_contracts.md C14 참조). §4.2 C12 BacktestAgent `forbidden_permissions = 6개`와 **다른 집합**이므로 혼용 금지. C12는 검증자 권한 경계, C14는 orchestrator 권한 경계.

**bundle_id 생성 주체**: **Mode B Scheduler**가 `MODE_B_EVOLVING` 단계 시작 시 `BUNDLE-{yyyymmdd}-{UUID8}`를 발급하고, Alpha Factor Engine/Co-STEER/PPO retrain 출력물을 하나의 bundle로 묶어 Backtest Agent에 전달한다. (v3.5: {seq} → {UUID8} 정정)

**backtest_history 저장**: KB (Knowledge Base)의 `backtest_history` 컬렉션. TTL 없음 (전체 이력 보존). Validation Tools 결과는 `result_persistence.ttl_days: 30` (risk_config.yaml).

**mode_b_audit_log**: JSON Lines 포맷, Mode B Scheduler가 기록. 필드: `[timestamp, stage, duration_sec, bundle_id, verdict, regression_severity, deploy_result, operator_approval, error]`. §6.0.1 S-5 audit_log와 **별도 파일**로 분리 (장중 주문 audit과 Mode B 배포 audit의 관심사가 다름).

**Mode B yaml 갱신 정책 (v2.2, §8.7 ④~⑧ 관련)**:

Mode B Scheduler가 갱신하는 대상 yaml과 권한:

| 파일 | `mode_b_editable` | `human_approval_required` | 갱신 주체 | 갱신 시점 |
|------|:---:|:---:|------|------|
| `news_filter.yaml` | true | false (default) | Mode B Scheduler | 21:30 (§8.5.1) |
| `dart_rules.yaml` | true | false | Mode B Scheduler | 21:30 |
| `spam_rules.yaml` | true | false | Mode B Scheduler | 21:30 |
| `sentiment_dict.yaml` | true | false | Mode B Scheduler | 21:30 |
| `manipulation_rules.yaml` | true | false | Mode B Scheduler | 21:30 |
| `universe_config.yaml` | **false** | **true** | **operator only** | **수동** |
| `risk_config.yaml` | **false** | **true** | **operator only** | **수동** |

**yaml 갱신 절차 (Mode B Scheduler)**:

```
① pre-update snapshot:
   - 기존 yaml을 `{filename}.backup.{yyyymmdd}` 로 복사
   - backup_retention_days (각 파일 mode_b_metadata 참조) 초과 시 자동 삭제

② update 실행:
   - Mode B가 생성한 새 키워드/규칙을 yaml에 merge
   - yaml 파싱 검증 (schema_version 일치 확인)
   - human_approval_required == true 이면 operator 확인 대기

③ post-update 검증:
   - 변경 diff를 mode_b_audit_log에 기록
   - 다음 날 Sprint 1 Hot Path에서 로드 실패 시 → baseline 자동 rollback

④ rollback 조건:
   - 새 yaml 파싱 실패
   - 다음 날 장 시작 후 1시간 내 Hot Path 이상 감지
   - operator가 수동으로 rollback 명령 발행
   rollback 시 `{filename}.backup.{yyyymmdd}`로 복원 + mode_b_audit_log 기록
```

**금지 사항**:
- `mode_b_editable: false` 인 파일(universe_config, risk_config)을 Mode B Scheduler가 수정하려 시도하면 런타임 거부 + MODE_B_BLOCKED 전이
- backup 없이 원본을 덮어쓰는 행위 금지 (atomic swap 필수)

### 8.0.1 Mode B 타임라인 세분화

> **v2.2 타임라인 세분화**: Mode B는 18:00~22:00 사이에 8단계 (stage_0 DQR + stage_1~7)로 진행된다.
> 각 단계는 직렬로 이어지며, Backtest Agent(§8.5.2)는 21:00~21:30의 시스템 검증 게이트이다.
>
> ```
> 18:00       § stage_0  DQR (Data Quality Review, 2분 SLA)
> 18:00       § 8.1  성과 분석 (8차원 벡터)
> 18:30       § 8.2  방향 결정 (Thompson Sampling: factor vs model)
> 19:00~20:00 § 8.3  팩터 진화 (Alpha Factor Engine: Idea/Factor/Eval)
> 20:00~20:30 § 8.4  모델 진화 (Co-STEER) + § 8.4.1 재학습 데이터 구성
> 20:30~21:00 § 8.5  에이전트 자기 개선 (Handover Feedback + Memory 순환)
> 21:00~21:30 § 8.5.2 Backtest Agent 시스템 검증 (v2.2 신규 게이트)
> 21:30~22:00 § 8.5.1 뉴스 필터 키워드 갱신 (prefilter_drop_log 기반)
> 22:00       § 8.6  배포 (Backtest pass + operator 확인)
> ```
>
> **v2.2 피드백 루프**: Alpha Factor Engine(§8.3) + Co-STEER(§8.4) + PPO retrain의 출력물은
> 모두 Backtest Agent(§8.5.2)의 candidate_bundle로 수렴한다. Backtest Agent가 `verdict=pass` +
> `regression_risk.flagged=false`를 내야만 §8.6 배포 단계로 넘어간다. fail/warn이면 22:00 배포 차단.

### 8.1 성과 분석 (18:00)
```
⑤ Knowledge: 오늘 성과 8차원 벡터 평가
   x_t = [IC, ICIR, Rank(IC), Rank(ICIR), ARR, IR, -MDD, SR]
```

> **참조**: 구현 = `new/src/models/metrics.py::MetricsBundle.to_performance_vector()` (2026-04-21 신설).
> 정규화 = `normalize_performance_vector(vec, history)` (30일 rolling min-max, clip [0.01, 0.99]).
> 3-Layer 평가 매트릭 전체 스펙은 `new/docs/evaluation_metrics.md` 참조.

> **집계 주체**: `ModeBPerformanceAggregator` (Sprint 2 S2-10 실구현 완료 2026-04-26, `new/src/mode_b/performance_aggregator.py`). L2 9지표 일별 집계 + 8d 벡터 proxy. AuditLogger 18필드 (C18 `AgentPerformanceContract`) 연계. PIT-Safety: 18:00 KST 이후만 실행.

### 8.2 방향 결정 (18:30)
```
Thompson Sampling (RD-Agent):
   A = {factor 개선, model 개선}
   Bayesian posterior 기반 → 유망한 방향 자동 선택
   같은 시간 대비 Bandit이 33% 더 많은 탐색 가능
```

### 8.3 팩터 진화 (19:00, 선택 시)
```
Idea Agent (GPT-4o): 4요소 가설 생성 (AlphaAgent)
  - observation: 오늘 시장 관찰
  - knowledge: 금융 이론 참조
  - justification: 이론적 근거
  - specification: 구현 제약
  - evolving anchor: 전일 가설 기반 진화

Factor Agent (GPT-4o): 가설 → Operator Library로 구현
  - AST 파싱: key phrases → operators → parameters → AST
  - 복잡도 체크: SL(f), PC(f)
  - 독창성 검증: Alpha Zoo/KB와 AST 유사도 비교
  - 정합 검증: 가설↔수식 LLM 평가
  - 3중 정규화 통과 시에만 채택
  - 실패 시 카테고리별 분류 저장

Eval Agent (GPT-4o): 백테스트 + 3차원 평가 (C12/C13 metrics 7종 정렬, S1-0 Batch B 2026-04-20)
  - Predictive: IC, ICIR, RankIC        # ICIR = IC / std(IC), 안정성 측정
  - Return: AR, IR, SR                  # SR = Sharpe Ratio, 위험조정 수익률
  - Risk: MDD, stability                # MDD = Max Drawdown (음수 규약)
  - IC ≥ 0.99 기존 팩터와 중복이면 제거
  - 피드백 → Idea Agent에 전달 → 다음 라운드
```

### 8.4 모델 진화 (20:00, 선택 시)
```
Co-STEER (RD-Agent):
  - 아키텍처/하이퍼파라미터 가설 생성
  - DAG 기반 태스크 스케줄링
  - 실패 시 복잡도 α ← α+δ → 쉬운 것 먼저
  - 구현 → 백테스트 → 평가
  - Knowledge Base에 결과 저장
```

### 8.4.1 재학습 데이터 구성
```
재학습 데이터 구성:
  - 한국 피처: 오늘 KRX 1분봉 데이터 (30종목 × multi-scale)
  - 미국 피처: 어제 밤 미국장 종가 (S&P500, NASDAQ, VIX, SOXX)
  - 이벤트: 오늘 뉴스/공시/투자자 매매 요약

  ※ 오늘 밤 미국장 데이터는 재학습 시점(18:00)에 아직 없음.
     내일 08:30에 수집하여 추론 시 피처로 사용.

  학습: us_close(d-1) → krx_data(d) 패턴
  추론: us_close(d) → krx_prediction(d+1)
```

> **§8.5 하위 섹션 읽기 순서 주의 (v2.2)**: 아래 §8.5.x 서브섹션은 **물리적 문서 순서와 실행 시간 순서가 다르다**.
> - 실제 실행 순서: §8.5 (20:30~21:00 에이전트 자기 개선) → §8.5.2 (21:00~21:30 Backtest Agent, v2.2 신규) → §8.5.1 (21:30~22:00 뉴스 필터 갱신) → §8.6 (22:00 배포)
> - 문서 순서: §8.5 → §8.5.2 (신규) → §8.5.1 → §8.6
> - 이유: §8.5.1은 v2.1에서 이미 존재하던 섹션이고, §8.5.2는 v2.2에서 Backtest Agent 게이트로 신규 추가됨. 기존 번호를 유지하고 신규 섹션만 삽입했다.
> - Canonical 시간 순서는 §8.0 타임라인 다이어그램 + §7.4 State Machine MODE_B_* 전이를 참조하라.

### 8.5 에이전트 자기 개선 (20:30~21:00) — Memory 순환 연결 (MetaGPT + GPT Pro #6)
```
Handover Feedback (§5.6 자동 집계 연동):
  - 오늘의 모든 판단 + 결과 정리
  - performance_metrics 7항목 → 8차원 벡터 자동 변환
  - 에이전트별 기여도 측정 (ablation 방식)
  - Long-term Memory에 저장 + KB에 벡터 저장

React Action (다음 날 08:30):
  - 각 에이전트가 과거 피드백 리뷰
  - 기여도 기반 Thompson Sampling posterior 업데이트
  - Constraint prompt 자기 갱신
  - 예: "어제 Risk Agent veto 3건 중 2건 오판 → 임계값 0.7→0.8 상향"
  - 저성과 에이전트 → 자동 신뢰도 하향, 고성과 → 상향
```

> 장중 memory → 장마감 자동 집계 → 8차원 벡터 → 에이전트 기여도 분해 → 다음 날 constraint 반영 (§5.6 참조)

### 8.5.2 Backtest Agent 시스템 검증 게이트 (21:00~21:30) — v2.2 신규

> **v2.2에서 신설**. Alpha Factor Engine + Co-STEER + PPO retrain이 만든 "내일의 시스템"이
> 어제보다 나은지, 또는 회귀 위험이 있는지를 시스템 레벨에서 검증하는 마지막 게이트.

```
입력 (candidate_bundle):
  - factor_ref: Alpha Factor Engine이 통과시킨 새 팩터 (§8.3)
  - model_ref: Co-STEER가 재학습한 모델 (§8.4)
  - allocator_ref: PPO retrain 결과
  - baseline_ref: 직전 배포 버전 (비교 대상)

실행 (3개 Validation Tools, C13):
  ① BacktestEngine: 후보 bundle의 walk-forward 백테스트 → IC/ICIR/ARR/IR/MDD/SR
  ② ReplayRunner: 과거 N거래일 1분봉 + 이벤트 deterministic replay →
                   Hot Path/Cold Path latency 분포 + 에이전트 activation count
  ③ PerformanceAnalyzer: regime breakdown (bull/bear/sideways/volatile) +
                          ablation (factor/model/allocator 분리 기여도) +
                          baseline 대비 delta_sharpe/delta_mdd

LLM reasoning (GPT-4o):
  - 계산 결과에 CoT reasoning을 붙여 diagnostic_notes 생성
  - "이번 factor는 bear regime에서 sharpe -0.3 → 회귀 위험" 같은 자연어 해석
  - Kanana-o는 사용하지 않음 (장중 100회/일 예산 보존)

출력 (backtest_report):
  verdict: pass | fail | warn
  regression_risk: {flagged: bool, evidence: [...], severity: low|medium|high}
  diagnostic_notes: string
  deploy_recommendation: auto_deploy | operator_required | blocked

배포 게이트 규칙:
  verdict == 'pass' AND regression_risk.flagged == false
    AND minute_bar_leakage_check == pass                   → §8.6 배포 진행
  verdict == 'warn'                                        → operator 수동 확인 (human_approval=true)
  verdict == 'fail' OR regression_risk.flagged == true
    OR minute_bar_leakage_check == fail                    → 22:00 배포 차단, baseline 유지,
                                                              dead_letter_log에 기록

v3 추가 — 분��� leakage control:
  purge_bars: config에서 로드 (Sprint 4 pilot 실험 결정)
  embargo_bars: config에서 로드
  replay_unit: "1m" (분��� 기준)
  minute_bar_leakage_check: purge/embargo/replay_unit 기준 준수 여부 자동 검증

v3 추가 — 산출물 분리:
  backtest_report: 전체 성과 요약 (기존)
  failure_case_cards: [FailureCaseCard] — 일반 실패/교훈 카드 (v3 신규)
    {case_id, regime, trade_id, expected_pnl, actual_pnl, root_cause, severity, lesson}
  regression_cases: [RegressionCase] — 회귀 위험 전용 증거 (v3 신규)
    업데이트 후 성능 퇴화 사례만 분리
```

> **권한 제약 (C12 계약서 + forbidden_permissions)**: Backtest Agent는
> target_weights 수정 / order_deltas 생성 / FDA 우회 / Hot Path 개입 / production 직접 쓰기
> 전부 불가. 판단만 하고, 실제 배포는 §8.6의 별도 게이트에서 수행한다.

> **피드백 루프**: Backtest Agent의 memory는 과거 검증 이력 + 배포 후 실 성능 추적을 포함한다.
> `backtest_precision` (pass 판정 후 실제 prod 성능 유지 비율)과 `regression_catch_rate` (회귀 조기 탐지율)
> 지표는 Mode B §8.5.1 뉴스 필터 키워드 갱신과 동일하게 self-tuning 근거가 된다.

### 8.5.1 뉴스 필터 키워드 갱신 (Mode B)
```
매일 장마감 후 실행:
  ① 오늘 prefilter_drop_log에서 "1차 필터에서 버린 뉴스 중 실제로는 영향이 있었던 것" 분석
     (C11 dead_letter_log와 별도 — prefilter_drop은 키워드 미매칭, dead_letter는 admission backlog/stale 드롭)
     - 가격 변동이 컸는데 관련 뉴스를 놓친 종목 식별
  ② 놓친 뉴스의 키워드를 news_filter.yaml에 추가 제안
  ③ 30일간 매칭 0건인 키워드는 제거 후보로 표시
  ④ 갱신된 키워드 목록을 다음 날부터 적용
```

> 이 단계 덕분에 키워드 목록은 시장 변화에 적응한다. "전력반도체"가 새 테마로 부상하면 다음 날부터 자동 포착.
> `human_approval: false`이면 자동 반영. 운용자가 검토를 원하면 `human_approval: true`로 변경하여 사람 승인 후 반영.

### 8.6 배포 (22:00) — Mode B Deployer가 수행 (v2.2)
```
주체: Mode B Deployer (§8.0 Mode B Scheduler의 sub-component)
입력: bundle_id (Backtest Agent가 pass 판정한 candidate_bundle)
상태: MODE_B_DEPLOY (§7.4)

①  §8.5.2 Backtest Agent verdict 확인:
    - verdict == 'pass' AND regression_risk.flagged == false  → Deployer 호출 진행
    - verdict == 'warn'                                        → MODE_B_OPERATOR_REVIEW 상태로 전이, human_approval 대기
    - verdict == 'fail' OR regression_risk.flagged == true     → MODE_B_BLOCKED 상태로 전이, baseline 유지, dead_letter_log 기록

②  sanity check (Mode B Deployer가 실행):
    - 팩터/모델/에이전트 constraint 형식 검증
    - C8/C9/C10 계약서 호환성 확인
    - config drift 검사 (risk_config.yaml vs api_contracts.md SSOT 일치)

③  배포 실행 (Mode B Deployer가 수행):
    - 배포 전 `artifacts/bundles/{bundle_id}` candidate 4종 존재/비어있지 않음 검증
    - source와 live dest 경로가 같으면 즉시 차단 (live artifact 보호)
    - 개선된 팩터 → `artifacts/alpha_factor/factor_zoo.jsonl` 활성화 (atomic swap)
    - 재학습된 모델 → `artifacts/lgbm/latest_model.pkl` 교체 (rollback 가능)
    - PPO Allocator → `artifacts/ppo/latest_policy.pkl` 적용
    - 에이전트 constraint → 다음 날 08:30 React Action에 반영
    - 배포 완료 ts + bundle_id + verdict → mode_b_audit_log 기록
    - 성공 → MODE_B_IDLE 전이
    - 실패 → baseline 롤백 + MODE_B_BLOCKED 전이
```

> **권한 원칙**: Backtest Agent(C12)는 **판단만**, Mode B Deployer는 **실행만**.
> Backtest Agent가 pass를 내지 않으면 Deployer는 호출되지 않는다.
> Deployer가 배포 후 실패 감지 시 자동 롤백하여 baseline로 복원한다.

> **v2.2 변경점**: v2.1에서는 sanity check만 수행했으나, v2.2부터는 Backtest Agent의 시스템 레벨 검증이 선행된다.
> 이 게이트가 없으면 "오늘의 실패를 내일의 모델 변화로 연결한다"는 Mode B 철학이 **잘못된 방향의 변화**까지
> 무방비로 배포하는 위험이 있다. Backtest Agent는 그 방향이 baseline 대비 개선인지 회귀인지 판단한다.

### 8.7 Mode B 야간 갱신 전체 목록 (v3: 12개)

Mode B는 매일 밤 아래 항목을 갱신한다. "내일의 시스템은 오늘과 다르다." (KP-7)

숫자 처리 (LightGBM 영역):
  ① LightGBM 재학습 (오늘 1분봉 + 어제 밤 미국)
  ② Alpha Factor 진화 (Idea→Factor→Eval 루프)
  ③ PPO Allocator 재학습 (거래비용 포함 reward)

텍스트 처리 (LLM pretrained 규칙):
  ④ news_filter.yaml 갱신 (놓친 뉴스 → 키워드 추가/제거)
  ⑤ dart_rules.yaml 갱신 (잘못 분류한 공시 → 중요도 조정)
  ⑥ spam_rules.yaml 갱신 (새 스팸 패턴 추가)
  ⑦ sentiment_dict.yaml 갱신 (신조어/은어 추가)
  ⑧ manipulation_rules.yaml 갱신 (새 조작 패턴 추가)
  **⑨ trigger_catalog 갱신 (v3 추가)**: trigger → action 매핑 ���칙 재평가. 어제 False Positive 높았던 rule 임계값 조정.

에이전트:
  ⑩ 에이전트 constraint 자기 개선 (어제 틀린 판단 반성)
  ⑪ Thompson Sampling posterior 업데이트 (에이전트 신뢰도 갱신)

**v2.2 추가 — 시스템 검증 게이트**:
  ⑫ **Backtest Agent 시스템 검증** (§8.5.2, 21:00~21:30): factor/model/allocator 후보를
     walk-forward + replay + ablation으로 검증. verdict=pass + 회귀 위험 없음 + **minute_bar_leakage_check pass** (v3)이어야 22:00 배포 진행.

### 8.5.3 Dual-Source 검증 (v3 즉시 반영)

Backtest Agent는 v3부터 **dual-source ablation**을 기본 검증 항목으로 사용한다.

- `with_dual_source` vs `without_dual_source` 비교
- 비교 지표: IC / RankIC / ARR / SR / MDD
- 목적: divergence 피처가 실제로 불확실성 축소와 과대진입 억제에 기여하는지 검증
- 결과는 `FailureCaseCard` 및 `regression_case`와 함께 저장하여, 다음 날 배포 여부 판단의 근거로 사용

---

## 9. LLM 역할 분배

### 9.1 장중 (Kanana-o 위주)
```
Kanana-o (한국어 전문):
  - News Agent: 뉴스/공시/커뮤니티 분석
  - Risk Agent: 리스크 판단 + macro note 갱신
  - Debate Agent: 갈등 해소 + 양측 근거
  - FDA: 최종 판단 + reasoning
```

### 9.2 장마감 (GPT-4o 위주)
```
GPT-4o / o3-mini (추론/코드 전문):
  - Idea Agent: 팩터 가설 생성
  - Factor Agent: 팩터 코드 구현
  - Eval Agent: 결과 분석 + 피드백 (Alpha Factor Engine 내부 로컬 평가)
  - 가설-수식 정합성 검증
  - Backtest Agent (v2.2): 시스템 전체 검증 결과에 CoT reasoning 부여
    → walk-forward/replay/ablation 계산은 C13 tools가 수행 (LLM 없음)
    → GPT-4o는 diagnostic_notes + regression_risk evidence 생성만
```

> **v2.2 예산 원칙**: Backtest Agent는 GPT-4o만 사용한다. Kanana-o 일일 100회 예산(§9.4)은
> 장중 경로(News/Risk/Debate/FDA Cold Path) 전용으로 보존한다.

### 9.3 LLM Router
```
# v3 S2-2 실구현 인터페이스:
result = router.call(prompt, mode="cold", caller="news_agent")
# mode: 'cold' (Kanana 우선) | 'mode_b' (GPT-4o 전용) | 'hot' (raise)
# caller: risk_config.yaml llm_budget.budget_allocation 키
# mode 값은 exact enum만 허용. "HOT", "hot ", "mode_a" 등은 UNKNOWN_LLM_MODE fail-closed
# Cold Path에서 force_model="gpt-4o" 금지. GPT는 fallback 또는 Mode B에서만 사용
# Kanana: OpenAI-compatible chat.completions.create(model="kanana-o", messages=[...])
# KANANA_API_URL: full endpoint가 아니라 /v1 base URL
# 로컬 legacy 호환: KANANA_API_URL 없으면 KANANA_BASE_URL alias 사용
# GPT-4o: store=false, structured_schema 제공 시 strict json_schema response_format
# schema: Kanana는 prompt-level JSON 지시 + local parser fail-closed, GPT는 strict schema

# 예시:
result = router.call(prompt, mode="cold", caller="news_agent")   # Kanana-o 우선
result = router.call(prompt, mode="mode_b", caller="backtest_reasoning")  # GPT-4o 전용
router.call(prompt, mode="hot")  # → RuntimeError (불변 원칙 4)

동적 LLM 라우팅 (교차 시너지 #5):
  Thompson Sampling이 호출 여부+LLM 선택 자동 결정 (Sprint 4 S4-6 예정)
```

### 9.4 Kanana-o 일일 예산 (100회)

| 용도 | 할당 | 비고 |
|------|:----:|------|
| 뉴스 심층 분석 | 30회 | 3단계 통과 뉴스만 |
| DART 공시 분석 | 3회 | 전수 분석 (드물지만 중요) |
| Risk Agent | 15회 | 미국 맥락/외국인/지정학 |
| 커뮤니티 이상 분석 | 5회 | spike/감성급변/조작 의심 시만 |
| FDA Cold Path | 12회 | 종합 판단 + CoT |
| Debate Agent | 5회 | 충돌 시만 |
| 버퍼 | 30회 | 급변 시 추가 (평상시 미사용) |
| **합계** | **100회** | |

> 100회 소진 시 GPT-4o로 자동 전환 (llm_router fallback).
> 평상시 ~40-60회 사용. 급변일 ~80-100회.

### 9.5 확장 전략
```
Phase 1: 프롬프트 전문화 (MetaGPT 방식 — 즉시 가능)
  → 같은 LLM에 역할별 다른 프롬프트

Phase 2: LoRA fine-tuning (TradeXpert 방식 — 여유 시)
  → 한국어 소형 LLM을 도메인별 fine-tune
  → fine-tuned 7B > 범용 GPT-4 (TradeXpert 증명)
```

---

## 10. 확정 / 미확정 사항

### 확정됨
| 항목 | 결정 | 근거 |
|------|------|------|
| 퀀트 모델 | **LightGBM** | 4개 논문 검증, 빠름, 해석 가능, GPU 불필요 |
| 포트폴리오 비중 | **PPO Allocator + 상한 규칙** | AlphaGAT Stage II + 안전장치 |
| RL 모델 사용 | **안 함** (기법만 차용) | 멀티에이전트 판단 의의 유지 |
| 장중 재학습 | **안 함** (추론만) | 에이전트가 실시간 적응 담당 |

### 미확정 (default, final freeze 아님)

> **리스크 상수 표기 원칙**: 아래 수치는 모두 초기값(default)이며, 1분봉 백테스트 결과에 따라 재조정 가능. Final freeze는 Phase 2 이후.
>
> **freeze 조건 (I-2)**: 1분봉 백테스트 50거래일 이상 + walk-forward 검증 후 재조정. 그 이전은 default 유지.

| 항목 | 현재 default 값 | 결정 시기 |
|------|---------------|---------|
| 단일 종목 비중 상한 | 20% (default) | 1분봉 백테스트 후 |
| 섹터 비중 상한 | 40% (default) | 1분봉 백테스트 후 |
| 최소 현금 | 10% (default) | 1분봉 백테스트 후 |
| max 종목 수 | 10 (default) | 1분봉 백테스트 후 |
| 나머지 2섹터 | 바이오시밀러/K-콘텐츠/금융 | 종목 분석 후 |
| LLM 조합 | Kanana-o + GPT-4o? + Gemini? | 비용/성능 비교 후 |

### 구현 착수 준비 상태 (GPT Pro 판정)

| 목표 | 판정 |
|------|------|
| 연구형/모의운용형 구현 착수 | **가능** |
| 논문/중간발표용 데모 시스템 | **가능** |
| 실거래 소액 paper-to-live | 불가 (OMS + 백프레셔 미완) |
| 상용 실계좌 자동매매 | 불가 |

> API Contracts 11개 명세: new/specs/api_contracts.md
> 구현 순서: C1→C4→C5→C8→C9→C10 skeleton → inference 연결 → 검증 → Mode B

---

## 11. 살릴 수 있는 기존 자산

| 기존 코드 | 재활용 방법 |
|---------|----------|
| connectors/kis_mock.py | KIS 실제 API로 확장 |
| connectors/dart.py | 공시 수집 로직 재사용 |
| connectors/naver_news.py | 뉴스 크롤링 로직 재사용 |
| connectors/ecos.py | 거시 데이터 수집 재사용 |
| connectors/us_market.py | US 야간 데이터 재사용 |
| connectors/llm_router.py | 멀티LLM 라우팅으로 확장 |
| 리스크 정책 개념 | 구체 수치는 재조정 |
| 스키마 기반 artifact 관리 | 메시지 프로토콜로 확장 |

---

## 12. R&D Backlog (v3 신설)

향후 확장을 위해 기록해두는 연구 주제. v3 core 반영은 아니지만 문서에 명시.

| # | 주제 | 출처 | 방향 |
|---|------|------|------|
| R1 | **커뮤니티 특화 LLM** | 팀원안 | Phase 2A: small LLM (처음부터 학습) vs Phase 2B: adapter/LoRA (기존 Kanana-o/GPT-4o에 레이어) |
| R2 | **soft focus priority score** | GPT Pro v3 수정 | cold_path_priority_score로 LLM 예산 배분 참고. hard gate가 아닌 soft score. 장중 Kanana-o 100회 예산 부족 시 검토 |
| R3 | **동적 유니버스 확장 (Sprint 5 예정)** | 팀원안 | KOSPI 200 전체 rule-based 스캔 → 이벤트 감지 시 동적 편입 (3~5종목 동시) → 당일 청산. Cold Path 스코프 확장 |
| R4 | **STGNN (Spatio-Temporal GNN)** | Gemini 연구 방향 | 대학원 R&D 대상. 종설 범위는 LightGBM + AlphaGAT Committee (S3-8)로 대응. 26종목 섹터 그래프는 `sector_config.yaml` 구조 기반. |
| R5 | **Temporal CP (Copula Processes)** | Gemini 연구 방향 | 대학원 R&D 대상. 종설 범위는 Dual-Source UQ (S4-1) + Uncertainty Score (C9 extension, v3.3)로 커버. |

---

## 13. 구현 우선순위 (안)

```
Phase 1: 기반 구축
  - KIS 1분봉 커넥터
  - 데이터 전처리 파이프라인
  - Message Pool + 메시지 프로토콜
  - Agent Profile 정의

Phase 2: 퀀트 모델
  - 모델 선택 + 학습
  - Alpha Factor 기본 세트
  - 백테스트 프레임워크

Phase 3: 에이전트 구현
  - 5개 에이전트 + 프롬프트 전문화
  - Pub/Sub + 의존성 실행
  - CoT reasoning

Phase 4: 진화 루프
  - 장마감 Factor-Model 공동 최적화
  - Thompson Sampling
  - 3중 정규화
  - Self-Improvement

Phase 5: 통합 + 최적화
  - 장중 매매 루프 완성
  - 병목 해결 (선택적 LLM, 캐싱)
  - Ablation 실험
  - 성능 평가
```

---

## 14. ID Convention (API Contracts 공통 규약)

| ID 유형 | 형식 | 예시 | 사용처 |
|---------|------|------|--------|
| ticker | 6자리 KRX 코드 | 005930 | 모든 계약 |
| event_id | EVT-{yyyymmdd}-{source}-{scope} | EVT-20260405-DART-005930 | C2 |
| message_id | MSG-{yyyymmdd}-{UUID8} | MSG-20260421-A1B2C3D4 | C4 |
| portfolio_patch_id | PP-{yyyymmdd}-{UUID8} | PP-20260421-A1B2C3D4 | C8 |
| decision_id | DEC-{yyyymmdd}-{UUID8} | DEC-20260421-E5F6G7H8 | C9 |
| order_plan_id | OP-{yyyymmdd}-{UUID8} | OP-20260421-I9J0K1L2 | C10 |
| **bundle_id** (v2.2, v3.5 UUID8) | **BUNDLE-{yyyymmdd}-{UUID8}** | **BUNDLE-20260421-A1B2C3D4** | **C12 candidate_bundle** |
| **backtest_run_id** (v2.2, v3.5 UUID8) | **BT-{yyyymmdd}-{UUID8}** | **BT-20260421-E5F6G7H8** | **C13 ({tool} 컴포넌트 제거)** |
| **replay_trace_ref** (v2.2, v3.5 UUID8) | **RPT-{yyyymmdd}-{UUID8}** | **RPT-20260421-A1B2C3D4** | **C13 ReplayRunner 출력** |
| **failure_case_id** (v3, v3.5 UUID8) | **FCC-{yyyymmdd}-{UUID8}** | **FCC-20260421-E5F6G7H8** | **C12 FailureCaseCard** |
| **regression_case_id** (v3, v3.5 UUID8) | **RGC-{yyyymmdd}-{UUID8}** | **RGC-20260421-A1B2C3D4** | **C12 RegressionCase** |
| **model_version** (v3.1) | **baseline \| v{n}** | **baseline / v2 / v3** | **C17 ModelRegistryContract (artifacts/lgbm/)** |
| **agent_performance_id** (v3.2, v3.4 포맷 정정) | **APM-{yyyymmdd}-{UUID8}** | **APM-20260421-A1B2C3D4** | **C18 AgentPerformanceContract (daily L2 rollup)** |
| **kb_message_id** (v3.6, 2026-05-01) | **KB-{yyyymmdd}-{UUID8}** | **KB-20260501-A1B2C3D4** | **S3-11 KnowledgeBase.write() 반환값 (Layer 5)** |
| **admission_event_id** (v3.8, Sprint 5) | **ADM-{yyyymmdd}-{UUID8}** | **ADM-20260503-A1B2C3D4** | **C15 candidate_pool 편입 이벤트** |
| **exit_event_id** (v3.8, Sprint 5) | **EXT-{yyyymmdd}-{UUID8}** | **EXT-20260503-E5F6G7H8** | **C15 dynamic_holdings 청산 이벤트** |
| **watch_snapshot_id** (v3.8, Sprint 5) | **WS-{yyyymmdd}-{UUID8}** | **WS-20260503-I9J0K1L2** | **C16 KOSPI200 60초 snapshot** |

> 상세 계약서: new/specs/api_contracts.md 참조

> **SSOT 원칙**: 스키마/필드 정의의 최종 권위는 `new/specs/api_contracts.md`. architecture.md와 visual.md는 파생 문서로, 불일치 시 contracts가 우선한다.

---

## 15. 평가 매트릭스 & 성능 지표

평가 3-Layer (Sprint 2 S2-10 기준):
- Layer 1: 모델 성능 (IC, ICIR, RankIC, AR, IR, MDD, SR)
- Layer 2: 에이전트 기여도 (C18 9지표: prediction_accuracy, slippage, anomaly_precision/recall, sector_tracking_error, veto_precision/recall, false_positive_rate)
- Layer 3: 시스템 성과 (Sharpe, MDD, Cause Attribution)

상세: `new/docs/evaluation_metrics.md` 참조.

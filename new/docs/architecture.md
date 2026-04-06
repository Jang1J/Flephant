# KOSPI Decision OS v2.1 — 상세 아키텍처 설계서

> KOSPI 1분봉을 위한 연구형/모의운용형 event-aware multi-agent Decision OS
>
> 작성일: 2026-04-05 (GPT Pro v2.1 피드백 반영: 2026-04-05)
> 근거: 교수님 피드백 + 6개 논문 분석 (AAPM, AlphaGAT, MetaGPT, RD-Agent, TradeXpert, AlphaAgent)
> 외부 AI 검증: GPT Pro 평균 8.6/10 (멀티에이전트 정체성 9.1, RL 배치 8.9, 실시간성 8.7, 아키텍처 8.6, 명세 7.2, 실거래 7.1)
> 위치: /Elephant_Lab/new/

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
- **"과거에 갇히지 않는 시스템"**: 과거 데이터만으로 학습한 모델은 과거 패턴만 안다. 고정된 키워드 목록은 과거 키워드만 잡는다. 정적인 alpha factor는 시간이 지나면 decay한다. 세상은 빠르게 변하므로, 시스템의 모든 구성요소가 스스로 진화해야 한다. Mode B가 매일 밤 갱신하는 것: ① 모델 재학습 ② alpha factor 진화 ③ 뉴스 필터 키워드 갱신 ④ 에이전트 자기 개선. 내일의 시스템은 오늘과 다르다.
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
| KIS | REST + WebSocket | 1분 | OHLCV + vwap + turnover + change (8개) | 메인 가격 데이터 |
| KRX 투자자별 | REST | 1분 폴링 | 외국인/기관/개인 순매수 (종목별) | IFP — 외국인 이탈 시 Risk Agent 경고 |
| Naver News | 크롤링 | 1분 폴링 | 한국어 뉴스 기사 | LLM으로 분석 |
| DART | Open API | 1분 폴링 | 공시 (한국어) | LLM으로 분석 |
| 커뮤니티 | 크롤링 | 5분 폴링 | 투자자 심리 | 보조 데이터 — RTG: 테마 점수 산출 (종목별 언급 빈도 + 감성 분석) |
| US Market | yfinance / KIS | 08:30 1회 (전날 미국장 종가) | S&P500, NASDAQ, VIX, SOXX | 야간 리스크 |
| ECOS | API | 일별 | 금리, 환율, 거시지표 | 매크로 context |

### 2.2 유니버스
- **6개 한국 대표 섹터 × 5종목 = 30종목**
- 확정 섹터: 반도체, 조선, 2차전지, 방산
- 미확정: 나머지 2섹터 (바이오시밀러, K-콘텐츠 후보)
- 종목 선정 기준: 섹터 내 시총 Top 5 (단, 유동성/거래량 고려)

### 2.3 전처리 파이프라인

```
Raw 1분봉 (8 features × 30종목)
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
④ TSFresh 통계 추출 → 자연어 변환 (에이전트용)
   최근 30분 통계: min, max, median, trend, volume_ratio
   → "최근 30분 삼성전자: 최저 71,500원, 최고 72,300원, 추세 하락, 거래량 급증"
   (TradeXpert)
    ↓
⑤ Reprogramming (선택, 고급)
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
입력: 8 features × 30종목 × k timesteps (multi-scale)
출력: 30종목 예측 시그널 + confidence
```

#### 모델: LightGBM (확정)

**선택 근거**:
- RD-Agent(Q) 증명: LightGBM + 자동 팩터 → IC 0.0532, ARR 14.21% (Transformer/LSTM 전부 능가)
- 추론 속도: ~0.3ms (30종목) — 1분봉 병목 제로
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

```
Alpha Factor (LLM 자동 생성) × 30종목
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
LightGBM (n_estimators=200~2000, depth=4, early_stop)
    ↓
출력: 30종목 예측 시그널 + confidence
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

### 4.1 에이전트 구성 (5개)

| 에이전트 | 전문 데이터 | 역할 | LLM | 핵심 능력 |
|---------|----------|------|-----|---------|
| News Agent | 뉴스/공시/커뮤니티 | 텍스트 → 투자 영향 판단 | Kanana-o | CoT + Skip + Micro Notes |
| Risk Agent | US 야간/거시/지정학 | 리스크 감지 + 경고 | Kanana-o | CoT + Macro Notes + Regime |
| Quant Agent | 1분봉 OHLCV | 모델 래핑 + 시그널 | 불필요 | 빠른 추론 + Anomaly 탐지 |
| Debate Agent | 에이전트 보고서 | 의견 충돌 해소 | Kanana-o | CoT + 양측 근거 분석 |
| FDA | 모든 보고서 | 오케스트레이터 + 최종 판단 | Kanana-o / GPT-4o | CoT + Bandit + 종합 |

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
        "risk_overrides": "[{rule, original, override, justification}] — audit metadata 전용. FDA가 실제 리스크 규칙을 변경하는 경로로 사용 금지. 승인/거부 근거 기록용.",
        "confidence": "float [0,1] — FDA 판단 확신도",
        "expiry": "ISO8601 — 이 판단의 유효 기한"
    },
    "runtime_guard": {
        "ILLEGAL_DELTA_MODIFICATION_ATTEMPT": "FDA가 order_deltas를 수정하려 하면 런타임 거부. can_change_weight=false 강제."
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
```

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

### 4.3 구조화된 메시지 프로토콜 (MetaGPT)

```json
{
    "message_id": "MSG-{YYYYMMDD}-{HHMM}-{SEQ}",
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
    "supersedes": "MSG-20260405-1025-003",
    "action_type": "veto_recommendation",
    "portfolio_patch_id": "PP-20260405-001"
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

폴링: 1분 폴링 (공시 타이밍 불규칙, API 비용 미미, 1분봉 시스템에서 10분 지연은 위험)
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
  "message_id": "KB-{YYYYMMDD}-{SEQ}",
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

> ※ KB의 decision 필드는 레거시 자연어 형식(BUY/HOLD/SELL). FDA 최종 출력은 action_schema v2.1(approved/veto) 참조.

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
  - 에이전트 보고서 (TTL: 5분 or 이벤트까지)
  - Cross-Asset Attention 결과 (TTL: 1분)
```

### 5.6 장중↔장마감 Memory 순환 연결 (GPT Pro 피드백 #6)

```
┌── 장중 (09:00~15:30) ──────────────────────────────┐
│                                                      │
│  에이전트별 memory 실시간 축적:                       │
│  - News: micro_notes (종목별 이벤트 기록)             │
│  - Risk: macro_notes (거시 상황 갱신)                 │
│  - Quant: prediction_history (시그널 vs 실현)         │
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
Step 1: LightGBM → 30종목 예측 시그널 (0.3ms)
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
LightGBM 30종목 시그널
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
LightGBM → 30종목 예측 시그널
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

추가 리스크 규칙 (기존 risk_policy_v0.yaml에서 계승):
    - Regime Gate: VIX proxy ≥90 → red (신규 진입 금지), ≥70 → yellow (비중 50% 감축)
    - Regime Gate: Market Breadth <0.30 → red, <0.45 → yellow
    - Turnover Cap: 일일 최대 회전율 30% (과도한 매매 방지)
    - Min Confidence: LightGBM confidence < 0.3 → Top-10 제외

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

### 7.1 Hot Path (매 1분 — 퀀트만, LLM 미호출)
```
② Data: 1분봉 수집 (8 features × 30종목) + 전처리
③ Model: 퀀트 모델 추론 → 30종목 시그널 (0.3ms)
④ Agent: Quant Agent만 → 시그널 publish + anomaly 감시
   (PPO Allocator가 현재 포지션 포함 state 기반으로 Top-K 외 보유종목에 weight=0 할당 → 자연 청산)
⑥ Ranking: 퀀트 점수 순 Top-10 → PPO 비중 결정
⑧ PM: order_deltas 생성
④ FDA: 퀀트 시그널 기반 approve/veto (빠름, LLM 미호출)
① Exec: approved → PM이 생성한 order_deltas 실행 → 결과 피드백
⑤ Knowledge: 예측 이력 기록
```

> **Hot Path**: 퀀트 추론 + 점수 순 랭킹. Debate 미호출. 전체 레이턴시 < 100ms.

> **Hot Path FDA = 비LLM deterministic validator.** FDA는 Hot Path에서도 최종 게이트로 존재한다.
> 다만 LLM이 아닌 규칙 기반 체크리스트로 판단한다:
> ① Regime Gate (green/yellow/red) ② Kill Switch (daily_pnl) ③ Position Limit (단일 20%)
> ④ Sector Limit (섹터 40%) ⑤ Cash Minimum (10%) ⑥ Turnover Cap (30%) ⑦ Min Confidence (0.3)
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

| 상태 | 설명 | 전이 조건 |
|------|------|---------|
| BOOTSTRAP | 시스템 시작, 모델/config 로딩 중 | 로딩 완료 → HOT_RUNNING |
| HOT_RUNNING | 정상 매매 (Hot Path) | 이벤트 감지 → COLD_RUNNING |
| COLD_RUNNING | Cold Path 활성 (LLM 에이전트 동작 중) | 처리 완료 → HOT_RUNNING |
| DEGRADED | 일부 에이전트/커넥터 장애. 퀀트만으로 운영 | 장애 복구 → HOT_RUNNING |
| EMERGENCY_HALT | 긴급 전량 청산 + 시스템 중단. kill switch 또는 manual_emergency_halt. | kill_switch/manual_emergency_halt/OAuth 실패/잔고 불일치 → EMERGENCY_HALT |
| MANUAL_OVERRIDE | 수동 일시정지 (manual_pause). 신규 주문 중단, 기존 포지션 유지. | manual_pause 명령 → MANUAL_OVERRIDE |
| RECOVERY | 긴급 중단 후 복구 중. 포지션 확인 + 잔고 대조 | 검증 완료 → HOT_RUNNING |

상태 전이도:
```
정상 전이:
  BOOTSTRAP → HOT_RUNNING ⇄ COLD_RUNNING
  HOT_RUNNING → DEGRADED → HOT_RUNNING
  ANY → EMERGENCY_HALT → RECOVERY → HOT_RUNNING
  ANY → MANUAL_OVERRIDE → HOT_RUNNING

실패 전이:
  BOOTSTRAP → DEGRADED (모델/config 로딩 부분 실패)
  BOOTSTRAP → EMERGENCY_HALT (치명적 실패, 시작 불가)
  COLD_RUNNING → DEGRADED (에이전트 timeout, LLM 장애)
  RECOVERY → EMERGENCY_HALT (복구 실패, 잔고 불일치 지속)
  MANUAL_OVERRIDE → RECOVERY (수동 해제 후 안전 검증 필요 시)
```

---

## 8. Mode B: 장마감 자동 진화 루프

### 8.1 성과 분석 (18:00)
```
⑤ Knowledge: 오늘 성과 8차원 벡터 평가
   x_t = [IC, ICIR, Rank(IC), Rank(ICIR), ARR, IR, -MDD, SR]
```

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

Eval Agent (GPT-4o): 백테스트 + 3차원 평가
  - Predictive: IC, RankIC
  - Return: AR, IR
  - Risk: MDD, stability
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

### 8.5 에이전트 자기 개선 (21:00) — Memory 순환 연결 (MetaGPT + GPT Pro #6)
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

### 8.6 배포 (22:00)
```
개선된 팩터 + 모델 + 에이전트 constraint
→ 검증 (sanity check)
→ 다음 날 장 시작 전 배포 완료
```

### 8.7 Mode B 야간 갱신 전체 목록

Mode B는 매일 밤 아래 10개 항목을 갱신한다. "내일의 시스템은 오늘과 다르다."

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

에이전트:
  ⑨ 에이전트 constraint 자기 개선 (어제 틀린 판단 반성)
  ⑩ Thompson Sampling posterior 업데이트 (에이전트 신뢰도 갱신)

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
  - Eval Agent: 결과 분석 + 피드백
  - 가설-수식 정합성 검증
```

### 9.3 LLM Router
```
llm_router에서 용도별 분배:
  call_llm(purpose="korean_analysis") → Kanana-o
  call_llm(purpose="reasoning_code") → GPT-4o
  call_llm(purpose="factor_hypothesis") → GPT-4o

동적 LLM 라우팅 (교차 시너지 #5):
  Thompson Sampling이 호출 여부+LLM 선택 자동 결정
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

## 12. 구현 우선순위 (안)

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

## 13. ID Convention (API Contracts 공통 규약)

| ID 유형 | 형식 | 예시 |
|---------|------|------|
| ticker | 6자리 KRX 코드 | 005930 |
| event_id | EVT-{yyyymmdd}-{source}-{scope} | EVT-20260405-DART-005930 |
| message_id | MSG-{yyyymmdd}-{hhmm}-{seq} | MSG-20260405-1030-001 |
| portfolio_patch_id | PP-{yyyymmdd}-{seq} | PP-20260405-001 |
| decision_id | DEC-{yyyymmdd}-{hhmm}-{seq} | DEC-20260405-1030-001 |
| order_plan_id | OP-{yyyymmdd}-{hhmm}-{seq} | OP-20260405-1030-001 |

> 상세 계약서: new/specs/api_contracts.md 참조

> **SSOT 원칙**: 스키마/필드 정의의 최종 권위는 `new/specs/api_contracts.md`. architecture.md와 visual.md는 파생 문서로, 불일치 시 contracts가 우선한다.

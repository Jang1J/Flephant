# Paper 5: TradeXpert — Revolutionizing Trading with Mixture of Expert LLMs

> 데이터별 전문 LLM을 조합하는 법 | ICLR 2025 Workshop | Univ de Montréal + HKUST

## 논문 해석

### 핵심 문제
다양한 데이터 소스(뉴스, 가격, 팩터, 재무)를 효과적으로 통합하는 것이 어려움. 단일 LLM으로는 모든 데이터 유형을 잘 처리할 수 없음.

### 해법
MoE(Mixture of Experts) — 4개 전문 LLM이 각각 다른 데이터를 분석, General Expert LLM이 종합. 실제 증권사의 분석가 팀 구조와 동일.

### 아키텍처
```
News     → News Analyst LLM     → News Report ──┐
Market   → Reprogramming → Market Analyst LLM → Market Report ──┤
Alpha    → LightGBM Top-K → Alpha Expert LLM → Alpha Report ──┤
Fundmntl → Fundamental Analyst LLM → Fund Report ──┘
                                                    ↓
                                          General Expert LLM
                                          ├── Prediction Mode (Rise/Fall)
                                          └── Ranking Mode (pairwise → Top-K)
```

### 핵심 기법 상세

#### 1. 4개 전문 Expert LLM
- 모두 LLaMA-2-7B + LoRA fine-tuning (rank=8, alpha=16, dropout=0.1)
- 동일 구조, 동일 하이퍼파라미터 → **데이터만 다름**
- Expert별 학습 감독이 다름:
  - News/Fundamental: Movement + Reasoning (GPT-4 CoT)
  - Market: Movement만
  - Alpha: Movement + Comprehensive score

#### 2. Reprogramming (시계열 → LLM 입력)
```
OHLCV X → patch embeddings X_P ∈ R^{N×L_P×d_m}
LLM 어휘에서 text prototypes E' ∈ R^{V'×D} 추출 (V' << V)
Cross-attention: Z_k = Softmax(Q_k K_k^T / √d_k) V_k
  Q = patches, K/V = text prototypes
→ LLM이 시계열을 "텍스트처럼" 이해

TSFresh 통계 추가:
  "min=71500, max=72300, median=71900, trend=downward"
```

#### 3. Relaxed Comparison Sorting
```
Algorithm 2:
  For each pair (i,j):
    result ← General_Expert_LLM_Compare(r_i, r_j)
    C[winner] += 1
  ranked ← Sort(S, key=C, reverse=True)
  Top-K ← ranked[1:K]

LLM comparator는 비이행적 (A>B, B>C → A>C 아닐 수 있음)
→ O(N²) 모든 쌍 비교 > O(NlogN) QuickSort
RankIC: RelaxedSort 0.12 > BubbleSort 0.06 > QuickSort 0.03
```

#### 4. General Expert (오케스트레이터)
- Prediction Mode: 4개 보고서 요약 → Rise/Fall
- Ranking Mode: 두 종목 보고서 비교 → 승자 결정
- Multi-task fine-tuning: prediction + ranking 동시 학습

#### 5. Alpha Factor 선별
- 108개 전체 계산 → LightGBM comprehensive score
- Top-K 기여 팩터만 선별 → 수식 + GPT-4 설명 + 값을 LLM에 전달
- 전체를 넘기지 않음 → 토큰 절약 + 노이즈 감소

### 성과 수치
- AR 49.79%, SR 5.01, AV 9.95%, MD 6.56% (DOW 30)
- Prediction: Acc 0.64, MCC 0.19 (S&P500) — GPT-4 (0.58) 능가
- Ablation: w/o Market AR -19pp, w/o News AR -18pp
- Market 제거 시 AV +64% (변동성 급증)
- 종목당 4.7초 (A5000 GPU)

### 하이퍼파라미터
- LR 1e-5, batch 4, epochs 30, seq_len 2048
- LoRA: rank=8, alpha=16, dropout=0.1
- AdamW, warmup 0.1, gradient accumulation 8
- Hardware: 4× A5000, AMD Threadripper, 256GB RAM

## KOSPI 1분봉 활용 방안

### 1. MoE → 에이전트 매핑
- News Analyst → News Agent (Kanana-o)
- Market Analyst → Quant Agent (MSNet 모델)
- Alpha Expert → (퀀트 모델이 대체)
- Fundamental → Risk Agent (Kanana-o)
- General Expert → FDA (Kanana-o / GPT-4o)

### 2. 프롬프트 전문화 (즉시 적용)
```
News Agent: "너는 한국 주식 뉴스 분석 전문가..."
Risk Agent: "너는 시장 리스크 감시 전문가..."
FDA: "너는 최종 투자 판단 오케스트레이터..."
```
MetaGPT 방식(1 LLM + N prompts)으로 시작

### 3. Reprogramming / TSFresh
- 1분봉 OHLCV → TSFresh 통계 → 자연어 프롬프트에 포함
- "최근 30분 삼성전자: 최저 71,500, 추세 하락, 거래량 급증"
- 또는 full reprogramming (text prototype 변환)

### 4. Relaxed Sorting → 종목 랭킹
- 퀀트 모델로 Top 10 필터링 → LLM으로 10종목 pairwise (45회)
- 30종목 전체 비교(435회)는 비용 과다

### 5. 1분봉 병목 해결
- 평상시: 퀀트만 (빠름)
- 이벤트 시: LLM 호출 (느리지만 정확)
- 뉴스/펀더멘탈 캐싱 (매 분 변하지 않음)
- 변화 감지 종목만 선택 처리

### 6. CoT reasoning 필수 → cause 중심
- 교수님 "cause 중심" 피드백과 직결
- 모든 에이전트 출력에 Reasoning 필수

### 7. Alpha Factor Top-K만 LLM에
- 전체 팩터가 아닌 중요 팩터만 에이전트에 전달

### 8. 긴 텍스트에서 효과 극대화
- 네이버 뉴스(긴 기사) → MoE 효과 ↑ (트윗 대비)

## 핵심 발견
- fine-tuned 7B > 범용 GPT-4 (도메인 특화가 크기보다 중요)
- Market + News = 가장 중요 (각 ~18-19pp AR)
- Market 제거 시 변동성 64% 증가 → 리스크 관리에도 기여
- Non-transitive LLM comparator → 효율적 알고리즘보다 정확한 알고리즘

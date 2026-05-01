# Paper 6: AlphaAgent — LLM-Driven Alpha Mining with Regularized Exploration

> 팩터가 죽지 않게 만드는 법 | KDD 2025 | Sun Yat-Sen Univ + UNSW + NTU

## 논문 해석

### 핵심 문제
Alpha Decay — 아무리 좋은 팩터도 시간이 지나면 예측력 상실. 원인: (1) 과적합(p-hacking), (2) factor crowding (모두가 같은 팩터 사용). LLM은 RSI 같은 기존 팩터를 복제하여 crowding 가속.

### 해법
3가지 정규화로 decay-resistant alpha 채굴: (1) AST 독창성 검증, (2) 가설-팩터 정합성 LLM 검증, (3) 복잡도 제어. RD-Agent(Q)의 진화형.

### 아키텍처
```
Idea Agent (4요소 가설)
  → Factor Agent (Operator Library → AST 구현 → 3중 정규화)
  → Eval Agent (3차원 평가 → 피드백)
  → Idea Agent (evolving anchor 기반 진화)
  → ... 순환

3중 정규화:
  R_g(f,h) = α₁·SL(f) + α₂·PC(f) + α₃·ER(f,h)
  ER(f,h) = β₁·S(f) + β₂·C(h,d,f) + β₃·log(1+|F_f|)
```

### 핵심 기법 상세

#### 1. Idea Agent 가설 4요소
```
observation: "거래량 수축 + 가격 횡보 동시 발생"
knowledge: "Bollinger Band Squeeze = 변동성 확대 전조"
justification: "참여자 관망 → 방향성 결정 시 급격한 이동"
specification: "5일 윈도우, high-low range, volume MA"
```
- h₀ = evolving anchor (진화의 기준점)
- historical evolving traces 참조

#### 2. AST 기반 독창성 검증
```
s(f_i, f_j) = max_{subtrees} {|t_i| : t_i ≅ t_j}
  (largest common subtree의 크기)
S(f) = max_{φ∈Z} s(f, φ)
  (alpha zoo 내 최대 유사도)
→ S(f) 높으면 → 기존 팩터와 너무 비슷 → 거부
```

#### 3. 가설-팩터 정합성
```
C(h,d,f) = α·c₁(h,d) + (1-α)·c₂(d,f)
c₁: 가설→설명 정합 (LLM 평가)
c₂: 설명→수식 정합 (LLM 평가)
α = 0.5

예: "유동성 포착" 가설인데 수식에 volume/bid-ask 없으면 c₂ 낮음
```

#### 4. 복잡도 제어
```
R_g = α₁·SL(f) + α₂·PC(f) + α₃·ER(f,h)
SL(f): AST 노드 수 (symbolic length)
PC(f): 자유 파라미터 수 (윈도우 크기 등)
→ 너무 복잡한 팩터 = 과적합 위험 → 페널티
```

#### 5. Operator Library
- 표준 연산: rolling_min, rolling_max, SMA, EMA, conditional, correlation, rank, ts_argmax
- LLM이 이 연산들을 조합하여 팩터 구성
- AST에서 internal nodes = operator, leaf nodes = raw features ($price, $volume)

#### 6. Factor Agent 실패 카테고리화
- hypothesis_misalignment: 가설과 수식 불일치
- complexity_violation: AST 너무 복잡
- decay_detected: 초기 IC 좋았지만 빠르게 감소
- crowding_risk: 기존 팩터와 유사도 높음
- 같은 유형 반복 방지

### 성과 수치
- CSI 500: AR 11.00%, IR 1.488, MDD -9.36%
- S&P 500: AR 8.74%, IR 1.0545, MDD -9.10%
- vs RD-Agent: AR 14배, IR 20배 향상 (CSI 500)
- Hit ratio 81% 향상 (constraints 적용 시 0.16→0.29)
- 30% fewer tokens
- GPT-3.5로 GPT-4 사용 RD-Agent 능가 (정규화 > LLM 품질)
- 통계적 유의성: 모든 백엔드 p < 0.05

### 하이퍼파라미터
- LLM: GPT-3.5-turbo (AlphaAgent), GPT-4-turbo (RD-Agent)
- Base alphas: 4개 (intra-day return, daily return, 20d rel volume, norm daily range)
- LightGBM: max depth 4
- 전략: top-k dropout (top 50 buy, bottom 5 sell)
- 20회 독립 시행 × 5 라운드

## KOSPI 1분봉 활용 방안

### 1. 매일 밤 3중 정규화 적용
- 새 팩터 → AST 독창성 검증 → 가설 정합 → 복잡도 체크
- 3개 모두 통과해야 채택
- RD-Agent(Q) 자동화 + AlphaAgent 정규화 = 최적 조합

### 2. Operator Library for KOSPI 1분봉
```
KOSPI 전용 연산:
  rolling_min, rolling_max, SMA, EMA
  volume_ratio, price_range, vwap_deviation
  conditional (if-then), correlation
  rank, ts_argmax, ts_argmin
  sector_mean, sector_std (섹터 기준)
```

### 3. 팩터 IC 추이 모니터링
- 팩터별 monthly IC 추적
- IC 지속 하락 → decay 판정 → 자동 은퇴 → 새 팩터로 교체

### 4. 독창성 강제 → KOSPI crowding 방지
- KOSPI는 참여자 적어 crowding 덜하지만 장기적으로 필요
- 기존 alpha zoo와 AST 유사도 검사

### 5. Idea Agent 4요소 가설 필수화
- 모든 팩터 가설에 observation/knowledge/justification/specification

### 6. 실패 카테고리별 학습
- 실패를 분류 저장 → 같은 유형 반복 방지

## 핵심 발견
- 정규화 > LLM 품질 (GPT-3.5 + 정규화 > GPT-4 + 정규화 없음)
- DeepSeek-R1도 정규화 없이 decay → 추론만으로는 불충분
- Alpha158 = S&P 500에서 완전 사망 → 전통 팩터 의존 위험
- RD-Agent = exploitation(좁은 탐색), AlphaAgent = exploration(넓은 탐색)
- "continuous exploration capability" = alpha sustainability의 핵심
- LLM이 기존 팩터 복제 → crowding 가속 → 독창성 강제 필수

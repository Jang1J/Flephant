# Paper 1: AAPM — LLM Agent-based Asset Pricing Models

> 에이전트 하나를 제대로 만드는 법 | Dartmouth College | arXiv 2024

## 논문 해석

### 핵심 문제
기존 asset pricing은 수동 제작 팩터(Fama-French 등)에 의존. 뉴스 같은 정성적 정보가 시장을 움직이지만, 정량 모델에 통합하기 어려움.

### 해법
단일 LLM 에이전트가 뉴스를 분석 → embedding으로 변환 → 정량 팩터와 결합하여 자산 가격 예측. 에이전트에 기억/반복정제/RAG/자율판단을 부여하여 분석 품질을 높임.

### 아키텍처
```
News → LLM Agent → Analysis Report → Embedding
                                        ↓
                    Manual Factors → Hybrid State → Pricing Network → 수익률 예측
                    Asset Embedding ↗
```

### 핵심 기법 상세

#### 1. Macro/Micro 이중 노트
- Macro: 거시경제 상황 누적 요약 (매 뉴스마다 갱신)
- Micro: 종목별 투자 메모 (개별 관찰 축적)
- n_t (note)가 매 분석 후 n_{t'} 로 갱신

#### 2. 반복 정제 (N rounds)
- 1차: 뉴스 요약 → 2차: RAG로 관련 정보 검색 → 3차: 보강 분석 → ... N차
- N=5, K=3이 N=1, K=15보다 SR 5.4% 높음 → 여러 번 조금씩 > 한 번에 많이
- K×N ≈ 15에서 marginal gain 급감 (saturation)

#### 3. RAG (검색 증강 생성)
- Knowledge Base: 교과서, 백과사전, 논문 (SocioDojo)
- Embedding model: BGE (MTEB leaderboard 기준 선택)
- 분석 시 Top-K 관련 문서 검색하여 참조

#### 4. 지수 감쇠 커널
```
s_d = Σ_{i=1}^{L} κ(L,i) · e_{d-L+i}
κ(L,i) = η^{L-i} / Σ_{j=1}^{L} η^{L-j}
η ∈ (0.9, 1.0), L_W ∈ {7,15,30,45,60,90,180}
```
최근 뉴스에 높은 가중치, 오래된 뉴스는 자연 감쇠.

#### 5. Asset Embedding
- 각 종목에 학습 가능한 임베딩 E_a ∈ R^{N_A × d_model}
- Hybrid state: h_{d,a} = [s'_{d,a}; σ(E_a)]
- 같은 뉴스도 종목별로 다른 영향

#### 6. Pre-train → Fine-tune
- Historical factor만으로 pricing network 먼저 pre-train
- 뉴스 임베딩 추가하여 fine-tune (Null embedding → real embedding)

#### 7. 자율 판단 (Skip/Analyze)
- 뉴스가 투자와 무관하면 LLM이 스스로 skip
- 관련 있을 때만 분석 → LLM 호출 비용 절감

### 성과 수치
- Sharpe ratio +9.6%, |α| +10.8% (5개 baseline 대비)
- 뉴스 임베딩만으로 개별 종목 R² 0.62~0.89
- GPT-3.5 → GPT-4: SR 8.5~16.2% 추가 향상
- Ablation: Memory +6% SR, Hybrid +5%, Refine +3.8%, Notes +1.3%

### 하이퍼파라미터
- d_model: {128,256,512,768,1024}
- d_emb: {128,256,512,768,1024}
- Epochs: {50,100,150,200}
- η: U(0.9, 1), L_W: {7,15,30,45,60,90,180}
- N: {1,2,3,4,5}, K: {1,2,3,4,5}
- Hardware: 2× NVIDIA L40 Ada GPU, 512GB RAM

## KOSPI 1분봉 활용 방안

### 1. Risk Agent에 Macro/Micro Notes 도입
- Macro note: "미중 무역갈등 심화, 반도체 리스크 높음" (누적)
- Micro note(005930): "삼성전자 실적 하향 3일 연속, 외국인 순매도" (종목별)
- 장마감 후에도 보존 → 다음 날 판단에 영향

### 2. 에이전트 판단 반복 정제 (Bounded N=3)
- 1차 판단 → RAG 검색 → 보정 → 반복 (max 3회)
- 1분봉에서는 시간 제약 → 이벤트 시에만 반복, 평상시 1회

### 3. RAG over 과거 매매 기록
- Vector DB에 (상황, 판단, 결과) 저장
- "비슷한 과거에 어떤 판단이 좋았는지" Top-K 검색
- AAPM의 SocioDojo → 우리는 FailureCaseCard + BacktestReport

### 4. 지수 감쇠 커널 → 1분봉 적응
- 일봉: η∈(0.9,1.0), L_W∈{7~180일}
- 1분봉: η∈(0.95,0.99), L_W∈{30,60,120,390분} (추정)
- config에서 관리, 하드코딩 금지

### 5. Asset Embedding → 30종목 KOSPI
- 30종목 각각에 학습 임베딩 벡터 부여
- "미국 금리 인상" 뉴스 → 삼성전자 vs 한화에어로 영향 다름 → 임베딩이 차이 반영

### 6. Pre-train → Fine-tune
- MSNet을 1분봉 가격 데이터로 pre-train
- 에이전트의 뉴스/공시 임베딩을 추가 입력으로 fine-tune
- AAPM의 hybrid state 개념 직접 적용

### 7. 자율 판단 → News Agent
- 뉴스 수신 → "투자 관련?" 자율 판단 → 무관하면 skip
- LLM 호출 비용 절감 + 1분봉 시간 제약 내 처리

## 한계 → 우리 차별화
- US/영문만 → **KOSPI/한국어**
- 일봉만 → **1분봉**
- 뉴스만 → **뉴스 + DART 공시 + 커뮤니티**
- 단일 에이전트 → **멀티에이전트**

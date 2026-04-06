# Paper 4: RD-Agent-Quant — Multi-Agent Framework for Factor-Model Joint Optimization

> 퀀트 연구를 자동화하는 법 | NeurIPS 2025 | Microsoft Research + CMU

## 논문 해석

### 핵심 문제
퀀트 연구 파이프라인(팩터 발굴→모델 학습→백테스트→분석)이 사람 의존, 비자동화, 분절됨. 팩터와 모델이 따로 최적화되어 시너지 없음.

### 해법
5개 LLM-powered 유닛이 closed-loop으로 팩터+모델을 공동 최적화. Thompson Sampling bandit으로 자원 배분 자동화. Co-STEER 에이전트로 코드 생성.

### 아키텍처
```
Research Phase:
  ① Specification Unit → ② Synthesis Unit (가설 생성)
Development Phase:  
  ③ Implementation Unit (Co-STEER 코드 생성) → ④ Validation Unit (백테스트)
Feedback:
  ⑤ Analysis Unit → ② Synthesis에 피드백 → 순환

Thompson Sampling: factor 개선 vs model 개선 중 선택
```

### 핵심 기법 상세

#### 1. 5개 유닛
- **Specification**: S = (B, D, F, M). 동적 생성 (factor/model에 따라 변경)
- **Synthesis**: 과거 이력 H_t + 피드백 F_t → 새 가설 h^(t+1). Knowledge Forest 관리
- **Implementation**: Co-STEER (Scheduling + Implementation co-evolving). DAG 의존성. Knowledge transfer
- **Validation**: 팩터 IC dedup (≥0.99 제거). Qlib 백테스트
- **Analysis**: 로컬 뷰 (현재 실험) + 글로벌 뷰 (Synthesis). exploration-exploitation 균형

#### 2. Thompson Sampling Bandit
```
x_t = [IC, ICIR, Rank(IC), Rank(ICIR), ARR, IR, -MDD, SR] ∈ R^8
A = {factor, model}
Bayesian linear regression으로 posterior 갱신
→ 유망한 방향에 자원 집중
같은 12시간에 Random 33루프 vs Bandit 44루프
```

#### 3. Co-STEER
```
Algorithm 1:
  Initialize DAG G = (V, E), complexity α_j = 1
  실패 시: α_j ← α_j + δ → 순서 재조정 (쉬운 것 먼저)
  Knowledge Base K: {(task, code, feedback)} 삼중체
  유사 태스크 검색 → 과거 코드 재활용 (knowledge transfer)
```

#### 4. Knowledge Base
- (hypothesis, code, feedback) 삼중체 저장
- Knowledge Forest: Idea → Area → Knowledge Tree
- 성공 시 복잡도 증가, 실패 시 구조 변경

### 성과 수치
- R&D-Agent(Q)_o3-mini: IC 0.0532, ARR 14.21%, IR 1.7382 (CSI 300)
- vs Alpha 158: 22% 팩터로 동등 성능
- 비용 $10 이하
- PatchTST/Mamba 주식에서 부진 — 범용 시계열 ≠ 주식

### 하이퍼파라미터
- 실행: R&D-Factor 6h, R&D-Model 6h, R&D-Agent(Q) 12h
- LLM: GPT-4o temp=0.8 max=4096, o3-mini temp=1.0 max=10000
- Co-STEER: text-embedding-ada-002, 내부 루프 max 10
- Implementation max 600초, Validation max 3600초
- Hardware: 4× RTX A6000, 192 GiB

## KOSPI 1분봉 활용 방안

### 1. 장마감 자동 연구 루프
- Analysis → Thompson Sampling → 팩터 or 모델 개선 → KB 갱신 → 다음 날 배포

### 2. 장중 자동매매 루프 (같은 패턴)
- 매 1분: 가설(판단)→실행→결과→피드백→다음 판단
- Thompson Sampling으로 에이전트 신뢰도 동적 조정

### 3. Knowledge Base 실시간 축적
- (상황, 판단, 결과) 삼중체 저장
- RAG로 유사 상황 검색

### 4. 중복 팩터 자동 pruning (IC ≥ 0.99)

### 5. Robust Z-score + forward-fill 전처리

### 6. LLM 역할 분배 (Kanana-o + GPT-4o)

### 7. Bounded execution (시간 제한)

### 8. LLM에 raw data 비노출 (PIT 방지)

## 핵심 발견
- PatchTST/Mamba 주식에서 부진 → 최신 모델 맹신 금지
- IC ≠ ARR → 예측 정확도와 수익은 다른 문제
- 22% 팩터로 동등 성능 → 양보다 질
- 추론 능력 강한 LLM (o1) 유리

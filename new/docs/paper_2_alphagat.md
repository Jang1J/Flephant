# Paper 2: AlphaGAT — Two-Stage Learning for Adaptive Portfolio Selection

> 퀀트 모델을 제대로 만드는 법 | IJCAI-25 | Wuhan Univ

## 논문 해석

### 핵심 문제
raw 시장 데이터는 noise가 심하고, 시장 regime이 변하면 기존 전략이 무너짐. 기존 방법은 적응 못함.

### 해법
2단계 학습: Stage I(SL)에서 raw data를 alpha factor로 변환하여 noise 제거, Stage II(RL)에서 GAT+PPO로 팩터 가중치를 동적 조정하여 시장 변화에 적응.

### 아키텍처
```
Stage I (SL): Raw OHLCV → Downsampling → Top-down Conv1D + Bottom-up Conv1D
              → Cross-Asset Attention (MHA × 3 layers) → MLP → Alpha Factors
              Loss = L_ic + λ·L_cov

Stage II (RL): Alpha Factors → GAT (graph of factors) → PPO → Portfolio Weights
               Reward = (r̃_t + 1)(1 - c_t) - 1
```

### 핵심 기법 상세

#### 1. CATimeMixer (Alpha Factor 채굴)
- Downsampling: average pooling으로 L 스케일 생성
- Top-down Conv1D: 저주파→고주파 (매크로 트렌드)
- Bottom-up Conv1D: 고주파→저주파 (계절성/미시패턴)
- 결합: H_i = Tr_i + Se_i
- Cross-Asset Attention: MHA로 종목 간 상관 포착 (3 layer stack)
- Alpha Factor Generator: Z_t = Σ MLP(E_i) (전 스케일 합산)

#### 2. Loss Function
- L_ic: negative mean Information Coefficient → IC 최대화
- L_cov: off-diagonal covariance → 팩터 간 다양성 강제
- Total: L = L_ic + λ·L_cov (λ=0.1)

#### 3. GAT Policy Network
- State: alpha factors Z_t (raw feature 대신 → noise 감소)
- 각 alpha factor = graph node (fully connected)
- Attention: e_{i,j} = LeakyReLU(a^T[WZ_i, WZ_j])
- Factor weight: q_t^i = softmax(W_o·h_i + b_o)
- Trading signal: v = q_t * Z_t → Top-G softmax 선택

#### 4. PPO + Reward
- r̃_t = w_t^T · p_t - 1 (가격 변동)
- r_t = (r̃_t + 1)(1 - c_t) - 1 (거래비용 포함)
- PPO: 안정적 policy 학습

### 성과 수치
- DJIA: CW 1.428, APY 30.2%, ASR 1.367, CR 1.507
- CRYPTO: ASR 5.008 (압도적)
- 4개 시장(DJIA, HSI, CSI, CRYPTO) 모두 전 지표 최고
- Ablation: GAT+PPO가 MLP/random/top-IC/equal 모두 능가

### 하이퍼파라미터
- k=30 lookback, n=30 alpha factors, c=512 hidden
- λ=0.1 (cov regularization), 거래비용 0.25%
- 8 raw features: open, close, high, low, volume, vwap, turn, chg
- LR: 1e-3 (Stage I), 5e-4 (Stage II)
- NVIDIA RTX 4090 GPU

## KOSPI 1분봉 활용 방안

### 1. Alpha Factor 자동 채굴
- 수동 36피처 → CATimeMixer로 자동 학습
- raw 1분봉 8 features에서 n=30 alpha factor 추출

### 2. Cross-Asset Attention → 30종목 KOSPI
- MHA로 30종목 간 상관관계 자동 포착
- "삼성전자 급락 → SK하이닉스 연동" 학습

### 3. Multi-scale 분해 → 1분봉 적용
- 1분(미시) → 5분(단기) → 30분(중기) → 60분(장중 트렌드)
- Top-down(트렌드) + Bottom-up(계절성)

### 4. 거래비용 reward 내장
- 1분봉은 매매 빈도 높음 → 비용 영향 큼
- 학습 단계부터 슬리피지+수수료 반영 필수

### 5. 8개 raw features
- KIS API에서 OHLCV + vwap + turnover + change 확인 필요
- AlphaGAT와 동일한 입력 세트 구성

### 6. Covariance regularization
- 팩터 간 상관 최소화 강제 (λ=0.1)
- 비슷한 팩터 중복 방지

### 7. 포트폴리오 비중 학습 vs 규칙
- Option A: PPO가 비중 직접 학습 (can_change_weight 폐지)
- Option B: 상한은 규칙, 내부 배분은 학습
- 교수님 확인 필요

### 8. 통계적 유의성
- paired t-test p<0.05 필수
- 단일 수치가 아닌 통계적 검증

## 참고
- MAPS (Lee et al., 2020, IJCAI): 멀티에이전트 RL 포트폴리오. 관련 연구에 포함 필요.

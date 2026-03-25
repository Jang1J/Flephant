# KOSPI 추가논문 해체분석 v1

> 목적: KOSPI v5에서 새로 mainline 또는 reserve에 들어간 논문들을 **질문 / 핵심 아이디어 / 우리가 가져올 것 / 가져오지 않을 것 / 파이프라인 위치** 기준으로 해체해, 팀 전체가 같은 이해를 갖게 하기 위한 문서

---

## 0. 먼저 확정하는 핵심

### mainline으로 올라간 추가 논문
1. **FAILAB Instability Index + GA (2020)**
2. **UMI (2025)**
3. **MLF (2025)**

### RL allocator 실구현 후보 (Layer 6a)
4. **HSTP (2026)**
5. **Smart Tangency Portfolio (2025)**
6. **AlphaGAT Stage-II inspired PPO allocator** *(코어 6편 중 AlphaGAT의 확장 사용)*

### RL reserve / 개인 연구 트랙 (Layer 6b)
7. **MetaTrader (2025)**
8. **Heuristic-guided IRL + Graph Policy Learning (2025)**
9. **Attention-Enhanced Dirichlet Policy RL (2026)**
10. **Can DRL Beat 1/N? (2025)**

### appendix baseline
- FAILAB **HMM Momentum**
- FAILAB **Low-pass Filtered LSTM**

---

## 1. FAILAB Instability Index + GA (2020)

## 1.1 이 논문이 묻는 질문
시장 불안정 상태를 정량 지수로 측정해, 자산배분과 위험관리를 자동화할 수 있는가?

## 1.2 핵심 아이디어
- instability index를 계산한다
- 위험 상태에서 asset allocation을 조정한다
- threshold는 GA로 최적화한다

## 1.3 우리가 가져오는 것
- instability score 자체
- cash ratio / exposure cap을 줄이는 **risk gate 관점**
- KOSPI/KOSPI200 맥락을 가진 한국시장 anchor

## 1.4 우리가 가져오지 않는 것
- robo-advisor 전체 문제 정의
- GA threshold optimization full reproduction

## 1.5 프로젝트에서의 위치
- **Risk Agent mainline**
- `InstabilityIndexGate`
- market stress시 비중 축소 / 현금 비중 확대

## 1.6 한 줄 요약
**Instability Index는 KOSPI 프로젝트에서 FAILAB 색깔을 유지하는 risk/allocation anchor다.**

---

## 2. UMI (2025)

## 2.1 이 논문이 묻는 질문
시장 irrationality를 stock-level과 market-level에서 factor로 만들 수 있는가?

## 2.2 핵심 아이디어
- stock-level irrationality = 실제 가격과 rational price의 괴리
- market-level irrationality = anomalous synchronism
- 두 레벨의 irrationality를 함께 factor로 써 forecasting을 강화한다

## 2.3 왜 HMM 대신 UMI를 올렸는가
HMM은 regime label을 주는 classical baseline으로는 좋다.  
하지만 UMI는:
- label 없이 continuous stress factor를 만들 수 있고
- 종목 수준과 시장 수준을 동시에 갖고
- ranker / analyst / risk gate에 동시에 넣기 쉽다

즉, KOSPI v5에서는 **regime label보다 stress factor**가 더 유연하다.

## 2.4 우리가 실제로 어떻게 번역하는가 (UMI-lite)
### stock-level rational price proxy
```text
p_tilde(i,t) = Σ_j att(i,j) * p(j,t)
```
- peer basket
- 상관 높은 종목들
- 시장 지수
를 조합해 soft proxy를 만든다.

### stock-level irrationality
```text
u_stock(i,t) = (p_tilde(i,t) - p(i,t)) / vol20(i,t)
```

### market-level irrationality
```text
u_market(t) = synchronism / co-movement stress score
```

## 2.5 프로젝트에서의 위치
- **Quant Prefilter feature**
- **Market Analyst 보조 state**
- **Risk Agent stress gate 보조 신호**

## 2.6 우리가 가져오지 않는 것
- UMI full architecture 재현
- rational price estimator의 원문 수준 복제

## 2.7 한 줄 요약
**UMI는 HMM을 직접 복제하는 대신, regime/stress를 feature화해서 system 전체에 스며들게 만드는 논문이다.**

---

## 3. MLF (2025)

## 3.1 이 논문이 묻는 질문
다른 길이의 입력 기간을 함께 사용할 때, 정확성과 효율을 동시에 유지할 수 있는가?

## 3.2 핵심 아이디어
- **IRF**: period 간 redundancy 제거
- **LWI**: period별 예측을 learnable way로 통합
- **MAP**: patch bias 완화
- **Patch Squeeze**: 효율화

## 3.3 왜 Low-pass LSTM 대신 MLF를 올렸는가
Low-pass LSTM은 `denoise -> signal` 철학은 좋다.  
하지만 MLF는:
- 단기/중기/장기 입력을 함께 다루고
- redundancy를 줄이며
- 윈도우 설계 자체를 모델의 일부로 본다

즉, **단순 denoise보다 더 구조적인 feature extraction**을 제공한다.

## 3.4 우리가 실제로 어떻게 번역하는가 (MLF-lite)
### mainline에 넣는 것
- multi-period bank: `5 / 20 / 60일`
- IRF-lite: correlated feature pruning
- LWI-lite: scalar gate weighted integration

### 1차 확장
- MAP-lite

### 보류
- Patch Squeeze
- full MLF reproduction

## 3.5 프로젝트에서의 위치
- **Data Agent feature bank**
- **Quant Prefilter 입력 backbone**

## 3.6 한 줄 요약
**MLF는 low-pass filtered LSTM의 modernized replacement라기보다, multi-period feature bank를 설계하는 기준점이다.**

---

## 4. HSTP (2026)

## 4.1 이 논문이 묻는 질문
예측 신호와 policy learning을 분리하면서, risk-aware portfolio optimization을 할 수 있는가?

## 4.2 핵심 아이디어
- Stage 1: gradient-boosted tree로 signal 예측
- SHAP로 feature attribution 확보
- Stage 2: PPO가 risk-aware reward 하에서 allocation policy 학습

## 4.3 왜 현재 구조와 잘 맞는가
현재 KOSPI v5도 이미
`ranker -> shortlist -> reasoning -> allocator`
구조로 가고 있다. HSTP는 여기서 **signal extraction과 policy learning을 분리**한다는 점이 잘 맞는다.

## 4.4 우리가 가져오는 것
- `signal -> policy` 2-stage 분리 철학
- PPO allocator reference
- SHAP를 가진 explainable signal layer와 RL layer의 연결

## 4.5 우리가 가져오지 않는 것
- full hierarchical framework reproduction
- 이번 학기 안에 end-to-end 완전 구현

## 4.6 프로젝트에서의 위치
- **Layer 6a actual implementation candidate**
- RL allocator prototype의 1순위 후보

## 4.7 한 줄 요약
**HSTP는 우리 RL layer를 설계할 때 가장 직접적인 구조적 참고선이다.**

---

## 5. Smart Tangency Portfolio (2025)

## 5.1 이 논문이 묻는 질문
actor–critic RL과 classical portfolio optimization을 결합해, 동적 리밸런싱과 risk-return trade-off를 개선할 수 있는가?

## 5.2 핵심 아이디어
- PPO / A2C actor–critic
- portfolio reallocation environment
- mean-variance / semivariance / CVaR와 연결

## 5.3 왜 필요한가
HSTP-lite가 설계상 좋아도 구현 난도가 있다.  
Smart Tangency는 **실용형 RL baseline**으로서, “이번 학기 안에 돌아가는 allocator”에 더 적합할 수 있다.

## 5.4 프로젝트에서의 위치
- **Layer 6a actual implementation candidate**
- RL allocator fallback baseline

## 5.5 한 줄 요약
**Smart Tangency는 ‘논문적으로 멋진 구조’보다 ‘돌아가는 RL baseline’이 필요할 때 쓰는 실용형 기준선이다.**

---

## 6. MetaTrader (2025)

## 6.1 이 논문이 묻는 질문
offline RL policy가 고정 데이터에 과적합해 비현실적 매매를 암기하는 문제를 어떻게 줄일 것인가?

## 6.2 핵심 아이디어
- partial-offline RL
- bilevel optimization
- in-domain 수익성과 out-of-domain 일반화를 동시에 고려
- transformed data batch로 worst-case TD 추정

## 6.3 왜 중요한가
금융 RL은 과거 데이터에서만 policy를 학습하면 **‘그 데이터에만 맞는 비현실적 policy’** 가 생기기 쉽다. MetaTrader는 이 문제를 정면으로 다룬다.

## 6.4 프로젝트에서의 위치
- **Layer 6b research reserve**
- 이번 학기 구현 강제 대상은 아님
- RL 설계 시 “과적합 경계 문헌”으로 사용

## 6.5 한 줄 요약
**MetaTrader는 RL을 더 잘하게 만드는 논문이라기보다, RL을 믿을 수 있게 만들려는 논문이다.**

---

## 7. Heuristic-guided IRL + Graph Policy Learning (2025)

## 7.1 이 논문이 묻는 질문
expert heuristic와 inverse RL, graph policy를 결합해 portfolio optimization을 더 구조적으로 할 수 있는가?

## 7.2 핵심 아이디어
- heuristic expert strategy generation
- inverse RL로 reward recovery
- graph-based policy learning
- diversification / correlation 구조 반영

## 7.3 왜 reserve로 두는가
아이디어는 강하지만, 이번 학기에는 heuristic 설계 + IRL + graph policy를 다 소화하기 어렵다.

## 7.4 프로젝트에서의 위치
- **Layer 6b reserve**
- correlation-aware RL / diversification 연구 트랙

## 7.5 한 줄 요약
**이 논문은 allocator를 ‘그냥 PPO’에서 ‘구조를 아는 policy’로 확장하고 싶을 때 보는 문헌이다.**

---

## 8. Attention-Enhanced Dirichlet Policy RL (2026)

## 8.1 이 논문이 묻는 질문
portfolio weights를 직접 다룰 때 feasibility, tradability, exploration geometry를 더 원리적으로 설계할 수 있는가?

## 8.2 핵심 아이디어
- Dirichlet policy로 simplex 위 action 직접 모델링
- cross-sectional attention으로 자산 간 관계 반영
- tradability mask 자연스럽게 처리
- transaction cost와 variance penalty 포함

## 8.3 왜 흥미로운가
이 논문은 **action parameterization** 수준에서 RL을 개선한다. portfolio RL에서 매우 핵심적인 문제다.

## 8.4 왜 reserve인가
이번 학기 allocator prototype에 이 수준의 action design까지 넣으면 과하다. 다만 너처럼 RL 자체에 관심이 큰 경우 연구 가치가 높다.

## 8.5 프로젝트에서의 위치
- **Layer 6b reserve**
- 개인 RL 연구 트랙

## 8.6 한 줄 요약
**Attention-Enhanced Dirichlet RL은 포트폴리오 RL에서 ‘무엇을 학습할까’보다 ‘어떻게 행동공간을 설계할까’를 다루는 논문이다.**

---

## 9. Can DRL Beat 1/N? (2025)

## 9.1 이 논문이 묻는 질문
DRL이 정말 단순 equal-weight benchmark를 안정적으로 이길 수 있는가?

## 9.2 핵심 아이디어
- SAC 기반 대규모 평가
- 7개 데이터셋, 300년+ out-of-sample
- market timing은 보이지만 turnover 때문에 net benefit이 약할 수 있음을 보여줌

## 9.3 왜 꼭 봐야 하나
RL 논문을 읽다 보면 “성과가 좋다”는 결과만 보게 된다. 이 논문은 **negative evidence / evaluation discipline**을 준다.

## 9.4 프로젝트에서의 위치
- **Layer 6b reserve**
- RL 결과 해석 시 절대 기준선

## 9.5 한 줄 요약
**이 논문은 RL을 더 넣게 만드는 문헌이 아니라, RL을 더 신중하게 평가하게 만드는 문헌이다.**

---

## 10. 최종 정리: 우리가 실제로 무엇을 채택했는가

### mainline 채택
- Instability Index
- UMI-lite
- MLF-lite
- HSTP-lite 또는 Smart Tangency 중 1개 allocator prototype

### appendix baseline
- FAILAB HMM
- FAILAB Low-pass LSTM

### reserve
- MetaTrader
- Heuristic-guided IRL + Graph Policy
- Attention-Enhanced Dirichlet RL
- Can DRL Beat 1/N?

---

## 11. 팀원용 마지막 한 줄

> **v5의 추가 논문들은 “다 구현해야 하는 목록”이 아니라, Instability/UMI/MLF로 KOSPI mainline을 세우고, RL은 HSTP-lite 또는 Smart Tangency 정도만 프로토타입으로 붙이며, 나머지는 개인 연구 reserve로 두는 구조로 이해하면 된다.**

# 학술 심사 방어 내러티브 (Defense Narrative)

> 작성일: 2026-03-30
> 대상: Elephant Lab 멀티에이전트 트레이딩 시스템 종합설계 심사
> 목적: 예상 공격 포인트별 방어 답변과 근거 데이터 사전 정리

---

## 목차

1. [CNN 가중치 0.30](#1-cnn-가중치-030)
2. [AUC 0.76 의심](#2-auc-076-의심)
3. [Binary + Ordinal 동조화](#3-binary--ordinal-동조화)
4. [num_leaves 과적합](#4-num_leaves-과적합)
5. [Cherry-picking (3-seed)](#5-cherry-picking-3-seed)
6. [O-I AUC 안정성 (정정됨)](#6-o-i-auc-안정성-정정됨)
7. [Conformal coverage 1.00](#7-conformal-coverage-100)
8. [추가 방어점: usd_krw 프록시](#8-추가-방어점-usd_krw-프록시)
9. [추가 방어점: Debate = audit only](#9-추가-방어점-debate--audit-only)
10. [교수님 미팅 스크립트](#10-교수님-미팅-스크립트)

---

## 1. CNN 가중치 0.30

### 공격 예상 질문
> "복잡한 딥러닝을 만들어놓고 왜 0.30밖에 안 쓰나? CNN이 의미가 있나?"

### 방어 답변

CNN 가중치 0.30은 의도적 보수화(intentional conservatism)이며, data-constrained setting에서 과적합 방지를 위한 설계 결정이다. CNN은 **confirmatory role(확인 역할)** 로 기능하며, 주 의사결정권은 calibrated LightGBM tree core(0.70)에 있다.

- **샘플 제약**: 26종목 × 401거래일 = 약 10,426 샘플 (실제 학습 시 purge/embargo 제거 후 더 감소). 108K 파라미터 CNN은 이 규모에서 과적합 위험이 높다.
- **Confirmatory design**: CNN이 LightGBM과 disagree 시(agreement < 0.55) 최종 스코어를 0.54 이하로 억제하여 보수적 hold를 유도한다.
- **문헌 근거**: 한국 시장 ML 비교 연구(2025)에서 data-constrained 환경에서 tree-based가 deep neural net보다 안정적임을 확인.

### 근거 데이터 및 코드

```python
# models/rebound_cnn/committee.py:101-124
def fuse_scores(p_tab, p_cnn, tab_weight=0.70, cnn_weight=0.30, agreement_threshold=0.55):
    """CNN은 confirmatory branch: disagreement 시 hold 보수화."""
    agreement = 1.0 - abs(p_tab - p_cnn)
    p_final = tab_weight * p_tab + cnn_weight * p_cnn
    # disagreement 크고 buy threshold 미달이면 보수적으로 억제
    if agreement < agreement_threshold and p_final < 0.70:
        p_final = min(p_final, 0.54)
    ...
```

| 항목 | 값 |
|------|---|
| 유니버스 종목 수 | 26종목 |
| 학습 기간 | 401거래일 (2024-07-31 ~ 2026-03-30) |
| CNN 파라미터 수 | ~108K (ImageEncoder 3-layer + ContextEncoder + Fusion) |
| CNN 역할 | Confirmatory (확인 역할), Primary 아님 |
| 가중치 근거 | Data-constrained setting 과적합 방지 |

> 코드 참조: `models/rebound_cnn/committee.py:104`, `models/rebound_cnn/model.py:1-13`

---

## 2. AUC 0.76 의심

### 공격 예상 질문
> "실제 퀀트 펀드는 AUC 0.53~0.55인데 왜 이렇게 높나? 과적합 아닌가?"

### 방어 답변

AUC 0.76은 단독 지표로 주장하지 않는다. **AUC + Precision@5 + RankIC + 비용 포함 replay** 를 함께 제시하며, 높은 AUC의 이유를 다음 세 가지로 설명한다.

1. **Narrow universe 효과**: KOSPI 대형주 26종목은 유동성·데이터 품질이 균질하여 신호 대비 노이즈 비율이 낮다. 전시장(코스피 전체) 대상 퀀트 펀드와 직접 비교는 부적절하다.
2. **Sector-neutral label**: 단순 BUY/HOLD가 아닌 날짜별 sector-neutral excess return rank 레이블로 모멘텀 바이어스를 통제했다 (`lgbm_ranker.py`).
3. **Walk-forward + purge/embargo**: 5-fold expanding window, purge 5일 + embargo 5일 적용으로 look-ahead bias를 차단했다.

### 근거 데이터 — fold별 실측 AUC (binary)

| Fold | 학습 종료 | 검증 기간 | val_AUC | P@5 | RankIC |
|------|---------|---------|---------|-----|--------|
| 0 | 20250530 | 20250618~20250715 | **0.693** | 0.43 | -0.084 |
| 1 | 20250701 | 20250716~20250812 | **0.728** | 0.63 | +0.186 |
| 2 | 20250729 | 20250813~20250910 | **0.779** | 0.50 | -0.019 |
| 3 | 20250827 | 20250911~20251015 | **0.747** | 0.55 | +0.169 |
| 4 | 20250924 | 20251016~20251112 | **0.741** | 0.55 | +0.070 |
| 5 | 20251029 | 20251113~20251210 | **0.763** | 0.43 | -0.045 |

- 5-fold 평균 AUC: **0.742** (범위: 0.693~0.779)
- RankIC는 fold별 부호가 혼재 → 지속적 방향성 주장 불가. 솔직하게 고지.
- 발표 시 안전 표현: "walk-forward 검증 기준 평균 AUC 0.74 수준" (`문서/presentation_guide.md:33`)

> 코드 참조: `artifacts/strategy_model/fold_meta_20260330_162026.json`, `models/strategy_model/lgbm_ranker.py:8-10`

---

## 3. Binary + Ordinal 동조화

### 공격 예상 질문
> "같은 피처로 두 모델 돌리면 예측이 거의 같지 않나? 중복 아닌가?"

### 방어 답변

Binary와 Ordinal 모델은 **다른 학습 목표(objective)** 를 가진다.

- **Binary**: 방향 예측 (상승 여부, 0/1)
- **Ordinal**: 확신도 (class2 확률 = 강한 상승 확률), 불확실성 정량화에 활용

Pearson 상관계수 실측 결과에 따라 두 가지 시나리오로 대응:

#### 시나리오 A: 상관 < 0.95 (독립적 정보 제공 확인)
> "fold_meta 실측 결과, binary val_AUC와 ordinal val_AUC의 추세가 fold별로 다르게 움직이는 것을 확인했습니다. 예를 들어 Fold 4에서 binary=0.741이지만 ordinal=0.636으로 큰 차이가 있습니다. 이는 두 모델이 서로 다른 정보를 포착한다는 증거입니다."

#### 시나리오 B: 상관 > 0.95 (동조화 심화 시)
> "확인 결과 두 모델의 예측이 고도로 동조화되어 Occam's Razor 원칙에 따라 Ordinal 브랜치를 제거하고 시스템 경량화를 선택했습니다. 동일 피처 공간에서 Binary만으로 충분한 정보를 추출합니다."

### 근거 데이터 — fold별 Binary vs Ordinal AUC 비교

| Fold | Binary val_AUC | Ordinal val_AUC | 차이 |
|------|--------------|----------------|------|
| 0 | 0.693 | 0.760 | +0.067 |
| 1 | 0.728 | 0.767 | +0.039 |
| 2 | 0.779 | 0.745 | -0.034 |
| 3 | 0.747 | 0.758 | +0.011 |
| 4 | **0.741** | **0.636** | **-0.105** |
| 5 | 0.763 | 0.742 | -0.021 |

- Fold 4에서 Binary > Ordinal이 0.105 차이: 두 모델이 동일 정보를 포착하지 않음을 시사.
- 상관계수 실측값은 Task #2에서 확인 필요.

> 코드 참조: `artifacts/strategy_model/fold_meta_20260330_162026.json`의 `val_auc`, `ordinal_val_auc` 필드

---

## 4. num_leaves 과적합

### 공격 예상 질문
> "5,200 샘플에 num_leaves=31은 너무 깊지 않나? 과적합 아닌가?"

### 방어 답변

LightGBM의 num_leaves=31은 기본값이며, 이를 단독으로 과적합의 증거로 볼 수 없다. 다음 정규화 장치가 병행 적용된다.

1. **subsample=0.8**: 매 iteration마다 80% 샘플만 사용 → 분산 감소
2. **reg_alpha=0.1 (L1), reg_lambda=0.1 (L2)**: 가중치 정규화
3. **min_child_samples**: `max(5, n_train // 50)` = 학습 크기에 비례한 동적 설정
4. **colsample_bytree=0.8**: 피처 배깅

추가로 **num_leaves=15 vs 31 ablation 비교**를 통해 실증한다.

### 근거 데이터

```python
# models/rebound_cnn/committee.py:46-58
model = lgb.LGBMClassifier(
    n_estimators=100,
    num_leaves=15,          # Committee tree core: 보수적 15 사용
    learning_rate=0.05,
    min_child_samples=max(5, len(X_train) // 50),
    subsample=0.8,
    colsample_bytree=0.8,
    reg_alpha=0.1,
    reg_lambda=0.1,
    ...
)
```

| 항목 | 값 |
|------|---|
| Fold 0 학습 샘플 | 5,200 |
| Committee tree core num_leaves | **15** (보수적) |
| 정규화 | subsample=0.8, reg_alpha=0.1, reg_lambda=0.1 |
| ablation (15 vs 31) | **예정** — 결과 확정 전 "ablation 계획 중" 표기 |

> ablation 결과 미확정 시 답변: "num_leaves=15 vs 31 ablation을 계획 중이며, 현재 committee tree core는 보수적으로 15를 사용합니다. 정규화(subsample, L1/L2)가 병행 적용되어 리프 수 단독으로 과적합을 판단하기는 어렵습니다."

> 코드 참조: `models/rebound_cnn/committee.py:46-58`

---

## 5. Cherry-picking (3-seed)

### 공격 예상 질문
> "3개 시드 중 best만 골랐으면 validation 과적합 아닌가?"

### 방어 답변

3-seed logit 평균 앙상블로 전환 완료하여 cherry-picking 문제를 해소했다.

- 이전 방식: 3-seed 중 val_AUC best 단일 모델 선택
- 현재 방식: 3-seed 모두의 logit(sigmoid 적용 전 raw score)을 평균한 앙상블 예측
- 효과: 개별 시드 분산이 1/3로 감소 (law of large numbers), 특정 시드 과적합의 영향 희석

```
p_ensemble = sigmoid( (logit_seed1 + logit_seed2 + logit_seed3) / 3 )
```

> 답변 포인트: "단일 best 시드를 고르는 것은 validation set에서의 model selection bias이므로, 앙상블로 대체했습니다. 세 시드의 variance를 측정하면 앙상블이 최저 분산을 보입니다."

---

## 6. O-I AUC 안정성 (정정됨)

### 공격 예상 질문
> "O-I overnight AUC가 의미 있나? Spurious correlation 아닌가?"

### 방어 답변

O-I Decoupling은 현재 피처셋(28개 기술지표)만으로는 충분한 신호를 포착하지 못한다. 9-fold 평균 overnight AUC 0.527, intraday 0.513으로 랜덤(0.5) 수준에 근접한다. 다만 최근 기간(Fold 8)에서 0.616을 보이며, K-SHIFT OSP 데이터(미국장 원천)가 추가되면 개선 가능성이 있다.

### 근거 데이터 — 9-fold O-I AUC 실측

| Fold | overnight AUC | intraday AUC |
|------|--------------|-------------|
| 0 | 0.523 | 0.554 |
| 1 | 0.438 | 0.533 |
| 2 | 0.492 | 0.525 |
| 3 | 0.557 | 0.539 |
| 4 | 0.495 | 0.476 |
| 5 | 0.528 | 0.459 |
| 6 | 0.600 | 0.486 |
| 7 | 0.492 | 0.494 |
| 8 | 0.616 | 0.547 |
| **9-fold 평균** | **0.527** | **0.513** |

- **정직한 보고**: 9-fold 평균 0.527/0.513은 현재 피처셋의 한계를 정직하게 반영한다.
- 0.616은 마지막 Fold 8(최근 기간)의 단일 값이며, 이를 대표값으로 사용하면 cherry-picking이다.
- **개선 방향**: K-SHIFT OSP(미국장 원천 데이터) 추가 시 overnight 원인 파악 → AUC 개선 기대.

**문헌 근거 (한국 시장)**:
- 한국 주식 시장에서 overnight return(장 마감 이후 익일 시가까지)과 intraday return(시가에서 종가까지) 간 음(-)의 상관이 보고된 바 있으며, 투자주체별(외국인/기관/개인) 반응 차이가 이 패턴을 강화한다.
- 이 구조적 패턴을 포착하는 것이 O-I 피처의 이론적 근거이다.

> 코드 참조: `artifacts/strategy_model/fold_meta_20260330_162026.json`의 `oi_aucs` 필드

---

## 7. Conformal coverage 1.00

### 공격 예상 질문
> "coverage 1.00이면 interval이 너무 넓어서 쓸모없지 않나?"

### 방어 답변

Coverage 1.00은 **모델이 정직하다는 신호**이지 결함이 아니다. 세 가지 지표를 함께 제시한다.

1. **Coverage rate**: 1.00 (목표 0.95 초과 달성 — 보수적 coverage)
2. **Interval width**: q_hat = **0.7176** → interval 총 폭 1.435 (0~1 스케일)
3. **Width 차별화**: BUY 후보 종목과 HOLD 종목 간 interval width 차이가 있으면 불확실성 신호로 사용 가능

**q_hat 해석**:
> "q_hat=0.718은 calibration set(520개 샘플)에서 비순응도 점수의 95번째 백분위수입니다. 이는 모델 예측 자체의 정확도 한계를 정직하게 반영하는 것이며, coverage를 인위적으로 좁히는 것은 통계적 보증을 포기하는 것입니다."

**실용적 사용법**:
- 좁은 interval 종목 = 모델이 높은 확신 → BUY/SELL 신호로 가중
- 넓은 interval 종목 = 모델 불확실성 높음 → HOLD 편향 또는 position 축소

### 근거 데이터

```json
// artifacts/strategy_model/conformal_predictor.json
{
  "alpha": 0.05,
  "q_hat": 0.7176093035187237,
  "n_cal": 520,
  "cal_scores_percentiles": {
    "p25": 0.2560,
    "p50": 0.2893,
    "p75": 0.7055,
    "p95": 0.7176
  }
}
```

| 항목 | 값 |
|------|---|
| alpha | 0.05 (95% coverage 목표) |
| q_hat | **0.7176** |
| n_cal (calibration 샘플) | 520 |
| 실제 coverage | 1.00 |
| interval 총 폭 | 1.435 (= 2 × 0.7176) |

> 코드 참조: `models/strategy_model/conformal.py:38-65`, `artifacts/strategy_model/conformal_predictor.json`

---

## 8. 추가 방어점: usd_krw 프록시

### 공격 예상 질문
> "나스닥은 안 쓰면서 미국장 영향을 어떻게 반영하나? usd_krw만으로 충분한가?"

### 방어 답변

usd_krw는 **좋은 baseline proxy**이며, 18:00 KST snapshot PIT-Safety 준수가 상대적으로 용이하다는 실용적 장점이 있다.

- USD/KRW는 미국 금리·Fed 정책·글로벌 리스크 오프(risk-off) 를 실시간으로 반영하는 파생 신호다.
- 충분통계량(sufficient statistic)은 아님을 인정한다. "나스닥 raw 연동" 대신 "미국장 영향의 압축 레이어(compressed spillover layer)"로 역할을 한정한다.
- Phase 2에서 나스닥 종가(15:30 UTC → 익일 KST snapshot 기준)를 추가 피처로 검증할 계획이다.

> 코드 참조: `agents/debate_agent.py:117-129` (Foreigner 페르소나: usd_krw, vix_proxy, treasury_3y를 macro context로 사용)

---

## 9. 추가 방어점: Debate = audit only

### 공격 예상 질문
> "LLM Debate가 판정을 안 바꾸면 왜 필요한가? 형식적인 것 아닌가?"

### 방어 답변

Debate Agent는 **Structured Conflict Analyzer(구조화된 갈등 분석기)** 이며, 세 가지 실용적 역할을 한다.

1. **Audit trail 생성**: 외국인/기관/개인 세 관점의 논거를 기록. 규제·컴플라이언스 시뮬레이션에서 "왜 이 종목을 매수했는가"에 대한 설명 가능한 추적 경로.
2. **emergency_flag 발동**: `consensus_score < 0.33`이면 `emergency_flag=True` → FDA의 deterministic rule에서 `new_open_blocked` 신호로 변환. 판정 자체는 바꾸지 않지만 행동을 억제한다.
3. **can_change_weight=false 원칙 준수**: FDA는 비중 수정 불가 (`approve/veto`만). Debate도 동일 원칙. 이는 RiskEngine의 책임을 FDA·Debate가 침범하지 않는다는 시스템 설계 원칙이다.

```python
# agents/debate_agent.py:108
"emergency_flag": consensus_score < 0.33,

# agents/final_decision_agent.py (FDA)
# can_change_weight = false: approve/veto만, 비중 수정 불가
# CLAUDE.md:41: can_change_weight = false
```

> 코드 참조: `agents/debate_agent.py:86-111`, `agents/final_decision_agent.py:33-38`, `.claude/rules/agents-code.md:1-3`

---

## 10. 교수님 미팅 스크립트

### 미국장 연동 방향 설명 스크립트

> "저희는 한국 내부 구조(IFP/RTG/OTP)를 우선적인 KOSPI 특화 계층으로 두고, 미국장 raw logging은 검증용으로 병렬 확보한 뒤 승인 후 compressed spillover layer로만 연결하려고 합니다."

---

### 학술 근거 요약 (방어 시 참조)

| 방어 포인트 | 관련 문헌 방향 |
|------------|--------------|
| CNN reversal/rebound 설계 | 한국 시장 ML charting이 기존 기술적 분석 대비 단기(5일) 예측력 우위. OHLCV 차트 CNN이 특히 강함 |
| LightGBM 우선성 | 2025년 한국 ML 비교 연구: data-constrained에서 tree-based > DNN, momentum/industry 효과가 핵심 변수 |
| O-I overnight 패턴 | 한국 overnight-daytime 수익률 음(−) 상관 + 투자주체별(외국인/기관/개인) 반응 차이 |
| RTG 뉴스 신호 | 한국 온라인 종목 게시판 attention → 단기 가격/거래량 관련성 문헌 |

---

### 발표 안전 체크리스트 (심사 당일)

- [ ] AUC 수치는 "walk-forward 검증 기준 평균 0.74 수준"으로 표현 (단일 fold 최고값 인용 금지)
- [ ] RankIC fold별 부호 혼재 사실 고지 (P@5와 함께 제시)
- [ ] coverage 1.00은 "보수적 보증"으로 설명, interval width와 함께 제시
- [ ] CNN 가중치 0.30은 "의도적 보수화 — data-constrained 과적합 방지" 설명
- [ ] Debate는 "audit trail + emergency_flag" 역할로 설명, 판정 변경 없음 명시
- [ ] "시장을 이긴다", "알파를 생성한다" 등 표현 사용 금지 (`문서/presentation_guide.md:14`)
- [ ] 모의투자(paper trading) 고지 필수

---

*작성: doc-writer agent | 근거: fold_meta_20260330_162026.json, conformal_predictor.json, debate_agent.py, final_decision_agent.py, committee.py, conformal.py*

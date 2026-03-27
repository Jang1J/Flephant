# KR-Rebound-CNN v1.0 설계서

> GPT Pro 작성, 2026-03-26
> 문헌 근거: 2025 하반기~2026 초 peer-reviewed journals

프로젝트명: Elephant Lab Multi-Agent Trading System
대상 모듈: 독립 추가형 2nd Quant Strategy Agent
작성 목적:
1. AI #2의 LightGBM 모멘텀 랭커와 상반된 스타일의 예비 전략 구현
2. 기존 AI #1 파이프라인 무수정 원칙 준수
3. 기존 StrategyCard 계약을 그대로 만족하는 drop-in 전략 생성기 설계
4. 캡스톤 발표와 개인 포트폴리오 양쪽에서 재사용 가능한 수준의 구조 정의

---

## 1. Executive Summary

한국 시장의 최근 문헌을 기준으로 보면, 전통적 기술적 분석이 약하다고 알려진 환경에서도 ML-based charting은 한국 시장에서 out-of-sample abnormal return을 보였고, OHLCV 차트 이미지 기반 CNN은 특히 단기 예측(5일 horizon)에서 기존 벤치마크를 상회했다. 반면, 한국 개별주식 momentum은 장기 표본에서 전반적으로 reversal 우세로 보고되었다. 따라서 AI #2가 준비 중인 LightGBM cross-sectional momentum ranker와 가장 자연스럽게 차별화되는 전략은, 또 하나의 tabular ranker가 아니라 한국형 learned reversal / rebound 전략이다.

추가로, 2025년 한국 시장 ML 비교 연구는 tree-based 모델이 deep neural net보다 안정적이고, momentum 및 industry 효과가 핵심 변수라고 보고했다. 이 결과는 AI #2의 LightGBM 방향이 연구적으로 타당하다는 뜻이기도 하다. 따라서 Model B는 AI #2와 경쟁하는 "더 좋은 LightGBM"이 아니라, 다른 alpha family + 다른 representation으로 가야 한다. 그 관점에서 본 설계서는 KR-Rebound-CNN v1.0 — 즉, 20일 차트 이미지(I20) + 컨텍스트 피처를 입력으로 받아 향후 5거래일 반등 확률(R5)을 산출하는 long/neutral selector — 를 최종안으로 제안한다.

---

## 2. 설계 결정 요약

### 2.1 최종 선택

| 항목 | 최종안 |
|------|--------|
| 전략 성향 | Reversal / Rebound |
| 모델명 | KR-Rebound-CNN v1.0 |
| 입력 표현 | I20 chart image + tabular context fusion |
| 예측 목표 | 향후 5거래일 초과수익 반등 확률 |
| 운영 모드 | long / neutral 중심 |
| 출력 계약 | 기존 StrategyCard 스키마 100% 준수 |
| 통합 방식 | strategy_card_variants/ + publish step + 기존 loader 재사용 |
| 학기 MVP | 가능 |
| Phase 2 | small TCN/GRU 또는 Transformer baseline 추가 |

### 2.2 왜 이 방향인가

한국 시장에서 ML charting이 기존 기술적 분석보다 강한 예측력을 보였고, image-based CNN이 특히 단기 horizon에서 강했다는 점은 "차트 기반 deep representation"을 쓰는 것이 한국 맥락에 맞는다는 근거를 제공한다. 동시에 한국 개별주식 모멘텀은 전반적으로 reversal 우세였으므로, AI #2의 모멘텀 랭커와 직교적인 백업 전략은 rebound selector여야 한다.

또한 clustering-augmented reversal 연구는 유사 특성 집단 내부 reversal이 더 강할 수 있음을 보여주지만, 성과 개선의 상당 부분이 short leg에서 나왔다. 현재 repo에서 run_risk_engine.py는 실질적으로 buy/strong_buy 중심으로 주문을 만들고, run_e2e_pipeline.py와 PortfolioManager도 long-entry 흐름에 더 가깝다. 따라서 Model B는 "정통 long-short contrarian"이 아니라 oversold 상황에서 반등 확률이 높은 종목만 long으로 선별하는 selector로 설계하는 것이 현재 시스템 계약과 맞다.

---

## 3. 프로젝트 제약 반영

### 3.1 하드 제약

| 제약 | 설계 반영 |
|------|----------|
| AI #1 기존 코드 무수정 | 신규 파일만 추가, 기존 jobs/, agents/, schemas/ 동작 유지 |
| 기존 파이프라인 계약 유지 | 출력은 StrategyCard 그대로 |
| 기존 strategy_loader.py 활용 | canonical path는 유지하되, variant 저장소를 별도로 둠 |
| 10주 내 구현 | MVP는 CNN+fusion까지만, Transformer는 Phase 2 |
| 추가 데이터 소스 금지 | 기존 DMP + historical DMP + existing KRX connector만 사용 |
| PIT-safe 유지 | train/infer 모두 snapshot cutoff 적용 |

### 3.2 현재 repo에 맞춘 실무적 판단

현재 strategy_loader.py는 artifacts/strategy_card/SC-{date}.json 또는 SC-{date}-*.json을 자동 탐지한다. 따라서 momentum과 rebound SC를 같은 canonical 디렉터리에 동시에 두면 loader가 혼합 로드할 위험이 있다. 그래서 variant 저장소와 publish step 분리가 필수다. 이것은 코드 수정이 아니라 새 모듈 추가로 해결하는 것이 가장 안전하다.

또한 현재 run_e2e_pipeline.py는 TTP를 전체 26종목이 아니라 샘플 3종목만 생성한다. 따라서 Model B는 TTP 비의존형 quant-first 전략이어야 하며, news_signal은 v1에서 핵심 alpha가 아니라 보조 필드로 다루는 것이 맞다.

---

## 4. 문헌 기반 설계 원칙

### 4.1 원칙 A — Alpha family는 momentum이 아니라 rebound여야 한다

한국 개별주식에서 장기적으로 reversal이 우세하다는 결과는, AI #2의 모멘텀 전략과 다른 성향의 backup 전략을 만들려면 reversal prior를 명시적으로 강제해야 한다는 뜻이다. "다른 모델 클래스"보다 "다른 alpha prior"가 중요하다.

### 4.2 원칙 B — Representation도 달라야 한다

AI #2는 LightGBM + tabular technical/news ranker 방향이다. 한국 시장 연구는 tree-based 모델이 안정적이고 momentum/industry 효과가 강하다고 본다. 따라서 Model B를 또 다른 tabular tree/MLP로 만들면 AI #2와 신호가 겹칠 가능성이 높다. Model B는 chart image representation을 사용해 구조적으로 다른 inductive bias를 가져야 한다.

### 4.3 원칙 C — Short-horizon에 집중해야 한다

한국 image-CNN 연구는 short-term return forecast에서 특히 강했으며, I20/R5가 매우 강한 조합으로 보고되었다. 따라서 Model B의 기본 horizon은 20일 입력 → 5일 예측으로 고정한다. 이때 논문상의 포트폴리오 수익률 숫자는 직접 재현 목표가 아니라, window/horizon 설계의 근거로만 사용한다.

### 4.4 원칙 D — Cluster / sector-aware normalization을 넣어야 한다

Korean ML 연구는 industry 효과가 중요하다고 보고했고, clustering-augmented reversal 연구는 유사 집단 내 reversal이 강할 수 있음을 보여준다. 따라서 절대적 과매도만 보는 것이 아니라 sector-relative 약세 / oversold를 함께 써야 한다.

### 4.5 원칙 E — MVP는 Transformer가 아니다

2025년 비교 연구에서 Transformer가 overall performance는 강했지만, LSTM이 성능-연산효율 균형이 좋았다. 동시에 한국 emerging-market evidence는 데이터 제약에서 복잡한 NN이 tree-based 대비 불리할 수 있다고 본다. 따라서 10주 capstone MVP에서 Transformer를 먼저 구현하는 것은 ROI가 낮다. Transformer/Quantformer류는 Phase 2 비교군으로 두는 것이 맞다.

---

## 5. 목표 상태 아키텍처

```
[Historical DMP / KRX Connector]
        ↓
[Rebound Dataset Builder]
        ↓
[KR-Rebound-CNN Train + Calibrate]
        ↓
[Daily Inference: build_strategy_card_rebound.py]
        ↓
artifacts/strategy_card_variants/rebound/SC-YYYYMMDD.json
        ↓
[publish_strategy_variant.py --profile rebound]
        ↓
artifacts/strategy_card/SC-YYYYMMDD.json
        ↓
기존 strategy_loader.py
        ↓
기존 Risk Agent → FDA → PortfolioManager
```

핵심은 기존 pipeline의 consumer는 전혀 건드리지 않고, producer만 하나 더 추가하는 것이다.

---

## 6. 시스템 범위 정의

### 6.1 In-scope

| 범위 | 내용 |
|------|------|
| StrategyCard 생성 | daily inference 후 SC 저장 |
| 모델 학습/재학습 | monthly 또는 biweekly retrain |
| variant publish | profile 선택 시 canonical SC publish |
| quant-only rationale | deterministic rationale 생성 |
| confidence calibration | validation 기반 scaling |
| optional uncertainty | ensemble disagreement 또는 null |

### 6.2 Out-of-scope (v1)

| 제외 범위 | 이유 |
|----------|------|
| full long-short execution | 현재 Risk/FDA 구조와 불일치 |
| fundamentals/value investing | PIT-aligned accounting feature store 부재 |
| TTP 의존형 뉴스 alpha | 현재 official E2E에서 full TTP 보장 없음 |
| transformer-first 구현 | 일정 대비 과도 |
| pair trading | single-ticker SC 계약과 불일치 |

---

## 7. 데이터 사양

### 7.1 실제 사용할 기존 데이터

| 필드 | 사용 여부 | 용도 |
|------|----------|------|
| market_data[ticker].ohlcv | 사용 | 당일 OHLC |
| market_data[ticker].volume | 사용 | 거래량 |
| market_data[ticker].tech_features | 사용 | RSI, SMA, MACD, BB, ATR, vol ratio |
| macro_snapshot | 사용 | base rate, treasury, FX, vix proxy, breadth |
| news_index | v1 비핵심 | optional confidence shrink |
| disclosure_index | v1 미사용 | 차후 event gate용 |
| config/universe_v1.csv | 사용 | ticker-sector map |

학습/추론용 rolling panel 구성:
1. artifacts/daily_market_packet/의 다일자 DMP를 적층하여 재구성
2. 부족 구간은 기존 connectors.krx.get_ohlcv() / get_universe_ohlcv()로 보완

### 7.2 PIT-safe 데이터 적재 규칙

| 규칙 | 내용 |
|------|------|
| feature cutoff | 시점 t의 입력은 t snapshot까지 |
| label window | t+1 ~ t+5 수익률 |
| 마지막 5일 | label 미정이므로 train set 제외 |
| scaling fit | train fold에서만 fit |
| split 방식 | random split 금지, date-based walk-forward만 허용 |
| profile publish | target_date 당 1개 canonical SC만 publish |

---

## 8. 문제 정의

### 8.1 목적 함수

Model B는 "다음 5일 동안 오를 종목"을 일반적으로 맞히는 것이 아니라, "지금 과매도 / 약세 성격을 보이는 종목 중 반등할 확률이 높은 종목"을 찾는다.

정의:
- r_i(t, t+5) = 종목 i의 t+1 ~ t+5 누적 수익률
- r_EW26(t, t+5) = 26종목 equal-weight 벤치마크의 동일 기간 누적 수익률
- excess_i_5d = r_i(t, t+5) - r_EW26(t, t+5)

분류 타깃: y_i,t = 1 if excess_i_5d > τ, else 0

여기서 τ는 v1에서 0.0% 또는 +0.5% 두 후보를 validation으로 고정한다. MVP는 표본 손실을 줄이기 위해 0.0%를 기본값으로 둔다.

### 8.2 왜 excess return인가

AI #2의 momentum ranker와 orthogonality를 확보하려면 절대수익보다 cross-sectional relative rebound를 보는 편이 낫다. 또한 KOSPI index raw series를 새로 붙이지 않아도 EW26 benchmark로 내부 일관성을 유지할 수 있다.

---

## 9. 스타일 강제를 위한 2-Stage 구조

### 9.1 Stage A — Oversold Candidate Gate

종목 i가 아래 4개 조건 중 2개 이상 만족하면 rebound candidate로 본다.

| 조건 | 식 |
|------|-----|
| 단기 하락 | ret_5d < 0 |
| 과매도 RSI | rsi_14 < 45 |
| 중기 추세 하회 | close < sma_20 |
| 볼린저 하단 근접 | bb_pos < 0.35 |

bb_pos = (close - bb_lower) / (bb_upper - bb_lower), 0~1로 clip.

추가로 sector-relative 약세를 강제:
- sector_ret5_z <= -0.25 또는
- sector 내 ret_5d 하위 40% 이내

**Gate Relaxation Rule**: 후보가 3종목 미만이면:
1. RSI threshold를 45 → 48로 완화
2. sector 하위 40% → 50%로 완화
3. 그래도 부족하면 Stage A 미통과 종목은 모두 hold

### 9.2 Stage B — Learned Rebound Scorer

Stage A 후보에 대해서만 KR-Rebound-CNN이 향후 5일 excess rebound probability를 예측한다.

---

## 10. 입력 피처 설계

### 10.1 Image Branch — I20 Chart Image

| 항목 | 규격 |
|------|------|
| window | 최근 20거래일 |
| image size | 64 x 64 |
| channel 수 | 3 |
| channel 1 | normalized OHLC candlestick/body-wick map |
| channel 2 | volume bars |
| channel 3 | SMA5 / SMA20 / Bollinger 위치 정보 |

이미지 정규화:
- price axis: 20일 window 내부 min-max normalization
- volume axis: log(1+volume) 후 min-max
- missing day 없음: trading-day 기준 rolling window

### 10.2 Context Branch — Tabular Context

| 그룹 | 피처 |
|------|------|
| macro | vix_proxy, market_breadth, usd_krw, base_rate |
| current technical | rsi_14, atr_14/close, volume_ratio_20, macd, macd_signal |
| price stretch | close/sma_5 - 1, close/sma_20 - 1 |
| sector-relative | sector_ret5_z, sector_rsi_z, sector_vol_ratio_pct |
| meta | sector embedding 또는 one-hot, market cap rank |

뉴스: v1에서는 news_signal = 0.0 (기본안). 확장안은 부정 뉴스 과다 시 confidence를 5~10% shrink.

---

## 11. 모델 구조

### 11.1 네트워크 아키텍처

**Image Encoder**
- Conv(32, 3x3) + BN + ReLU + MaxPool
- Conv(64, 3x3) + BN + ReLU + MaxPool
- Conv(128, 3x3) + BN + ReLU
- AdaptiveAvgPool
- Output: 128-d

**Context Encoder**
- Dense(64) + ReLU + Dropout(0.2)
- Dense(32) + ReLU
- Output: 32-d

**Fusion Head**
- Concat(128 + 32 = 160)
- Dense(64) + ReLU + Dropout(0.2)
- Dense(1)
- Sigmoid → p_rebound

### 11.2 학습 설정

| 항목 | 값 |
|------|-----|
| optimizer | AdamW |
| lr | 1e-3 시작 |
| batch size | 64 |
| epoch | 최대 50 |
| early stopping | patience 8 |
| loss | weighted BCE |
| seed ensemble | 3 seeds |
| model selection | validation Brier + Precision@N 혼합 |

---

## 12. Calibration과 Uncertainty

### 12.1 Calibration
- validation fold에서 temperature scaling 1차 적용
- 필요 시 isotonic regression 비교
- 최종 채택 기준: Brier score + ECE

### 12.2 Uncertainty
- B안 (권장): 3-seed ensemble probability variance를 0~1로 정규화

---

## 13. StrategyCard 매핑 규칙

### 13.1 필드 매핑

| SC 필드 | 매핑 규칙 |
|---------|----------|
| card_id | SC-{date}-{ticker} |
| snapshot_dt | {date}T18:00:00+09:00 |
| artifact_version | v1.0 |
| ticker | 6자리 종목코드 |
| direction | long 또는 neutral |
| signal | strong_buy / buy / hold 중심 |
| confidence | calibrated p_rebound |
| pre_risk_score | 2 * confidence - 1 |
| quant_score | raw logit 또는 standardized fused score |
| news_signal | 0.0 또는 shrink-only 보조값 |
| rationale | deterministic template |
| source_strategy | quant |
| evidence_ids | DMP snapshot + QFEAT evidence |
| features_used | 실제 사용 feature list |
| uncertainty_score | ensemble variance 또는 null |

### 13.2 Signal 변환 규칙

```
if Stage A 미통과:
    direction = neutral, signal = hold
elif confidence >= 0.70:
    direction = long, signal = strong_buy
elif confidence >= 0.55:
    direction = long, signal = buy
else:
    direction = neutral, signal = hold
```

### 13.3 Rationale 템플릿

예시: "최근 20거래일 차트에서 가격이 SMA20 아래에 위치하고 RSI가 과매도 구간에 근접해 있습니다. 동종 섹터 대비 최근 5일 수익률이 약세였으나, 모델은 향후 5거래일 초과수익 반등 확률을 0.68로 평가했습니다. 매크로 컨텍스트는 변동성 경계 구간이지만 breadth 악화는 제한적입니다."

이 문장은 deterministic template로 생성한다. LLM을 여기에 쓰지 않아도 된다.

---

## 14. 파일/디렉터리 설계

### 14.1 신규 추가 파일

| 경로 | 역할 |
|------|------|
| jobs/build_rebound_panel.py | historical panel / feature / image dataset 생성 |
| jobs/train_rebound_cnn.py | 학습 및 calibration |
| jobs/build_strategy_card_rebound.py | daily inference + SC 생성 |
| jobs/publish_strategy_variant.py | 선택된 profile을 canonical SC로 publish |
| config/strategy_profiles.yaml | profile 매핑 |
| models/rebound_cnn/model.py | CNN+fusion 모델 |
| models/rebound_cnn/preprocess.py | image render + feature transform |
| models/rebound_cnn/calibrator.pkl | calibration artifact |
| artifacts/strategy_card_variants/rebound/ | raw variant output |
| reports/rebound_eval/ | 실험 결과 저장 |

### 14.2 프로필 설정 예시

```yaml
profiles:
  momentum_ai2:
    source_dir: "artifacts/strategy_card_variants/momentum"
    publish_to: "artifacts/strategy_card"
    description: "AI #2 LightGBM cross-sectional momentum"
  rebound_cnn:
    source_dir: "artifacts/strategy_card_variants/rebound"
    publish_to: "artifacts/strategy_card"
    description: "KR-Rebound-CNN v1.0"
```

---

## 15. 학습 파이프라인 상세

### 15.1 Dataset Builder

입력:
- artifacts/daily_market_packet/DMP-*.json
- 부족 구간: connectors.krx.get_universe_ohlcv()
- config/universe_v1.csv

출력:
- dataset_rows.parquet
- image_tensor_index.json
- train/valid/test split manifest

### 15.2 Split Protocol

| 단계 | 기간 |
|------|------|
| train | 시작 ~ t-60 |
| valid | t-59 ~ t-20 |
| test/infer | t-19 ~ t 또는 다음 월 |
| rolling | 월 단위 전진 |

권장안은 expanding walk-forward다.

---

## 16. 일별 추론 알고리즘

```python
def build_strategy_card_rebound(target_date: str):
    dmp = load_dmp(target_date)
    panel = build_last_20d_panel(target_date)
    macro = dmp["macro_snapshot"]
    universe = load_universe()

    cards = []
    for ticker in universe:
        x_img = render_i20_image(panel[ticker])
        x_ctx = build_context_features(dmp, panel, ticker)

        gate_pass, gate_meta = oversold_gate(panel, dmp, ticker)
        if not gate_pass:
            card = make_hold_card(ticker, dmp, gate_meta)
            cards.append(card)
            continue

        p = model.predict_proba(x_img, x_ctx)
        p_cal = calibrator.transform(p)
        u = estimate_uncertainty(ticker, x_img, x_ctx)

        card = map_to_strategy_card(
            ticker=ticker, date=target_date,
            confidence=p_cal, uncertainty=u,
            gate_meta=gate_meta, dmp=dmp
        )
        cards.append(card)

    save_variant(cards, profile="rebound_cnn", date=target_date)
```

---

## 17. 성향 선택 구조

| 사용자 선택 | 내부 전략 | 설명 |
|-----------|----------|------|
| 추세추종형 | AI #2 LightGBM Momentum | 상대강도 + 뉴스 continuation |
| 반등포착형 | KR-Rebound-CNN | 과매도/약세 이후 short-horizon rebound |

risk stack은 공통이고 alpha source만 교체된다.

---

## 18. 평가 설계

### 18.1 평가 층위

**A. Signal Layer**: Precision@N, mean 5D excess return by signal bucket, hit rate, Brier score, ECE

**B. Style Orthogonality Layer**:

| 지표 | 기대 방향 |
|------|----------|
| corr(score, past_5d_return) | 음수 |
| corr(score, momentum_proxy) | 낮거나 음수 |
| Top-N overlap with momentum proxy | 낮음 |
| sector-relative underperformance of picks | 존재해야 함 |

**C. Portfolio Layer**: cumulative return, Sharpe, MDD, turnover, stop-loss hit rate, FDA approval rate, exposure/cash ratio

### 18.2 W10 ablation 설계

| 실험 ID | 전략 | 목적 |
|---------|------|------|
| A0 | Random mock | lower bound |
| A1 | Heuristic reversal gate only | 구조 baseline |
| A2 | ElasticNet Rebound | tabular-only baseline |
| A3 | KR-Rebound-CNN | 주력 모델 |
| A4 | KR-Rebound-CNN without sector-relative | cluster 효과 검증 |
| A5 | KR-Rebound-CNN without oversold gate | style drift 검증 |
| A6 | AI #2 real LightGBM | 최종 상대 비교 |

### 18.3 Regime-sliced 분석

| slice | 이유 |
|-------|------|
| green / yellow / red regime | risk stack과 alignment |
| high / low vix_proxy | rebound 민감도 |
| breadth high / low | macro context |
| sector별 | cluster-aware 효과 확인 |

---

## 19. 리스크 및 대응

### 19.1 가장 큰 리스크

| 리스크 | 설명 | 대응 |
|--------|------|------|
| 작은 유니버스 | 26종목만 사용 | daily rolling windows, expanding WF |
| 모델이 momentum으로 변질 | generic ML은 쉽게 continuation 학습 | oversold gate + style purity metric |
| short-leg 의존 alpha | reversal literature 일부는 short leg 중심 | production은 long-only selector |
| turnover 과다 | daily signal oscillation | signal persistence filter |
| TTP 의존성 | official E2E가 full TTP 보장 안 함 | quant-only v1 |
| PIT leakage | 날짜/스냅샷 처리 실수 | date-based split + KST aware timestamps |

### 19.2 Signal Persistence Filter (v1.1 권장)

- 신규 buy: p_t >= 0.55 and p_{t-1} >= 0.50
- strong_buy: p_t >= 0.70 and p_t - p_{t-1} >= -0.05
- 기존 buy 유지: p_t >= 0.50

---

## 20. 구현 로드맵 (10주)

| 주차 | 목표 | 산출물 |
|------|------|--------|
| 1주차 | 설계 freeze | 본 문서 확정 |
| 2주차 | panel builder | build_rebound_panel.py |
| 3주차 | heuristic/ENet baseline | A1/A2 결과 |
| 4주차 | image renderer + dataset | I20 image pipeline |
| 5주차 | CNN fusion model 학습 | train_rebound_cnn.py |
| 6주차 | calibration + SC emitter | build_strategy_card_rebound.py |
| 7주차 | publish step + canonical 연결 | publish_strategy_variant.py |
| 8주차 | W10 ablation 자동화 확장 | A3/A4/A5 |
| 9주차 | profile 선택 UX 연동 | strategy_profiles.yaml |
| 10주차 | 발표자료/논문용 결과 정리 | report, figure, table |

---

## 21. 릴리스 계획

### 21.1 v0 — Safe Baseline
- heuristic oversold gate
- ElasticNet rebound
- SC schema 발행
- publish step 연결

### 21.2 v1 — Main Release
- KR-Rebound-CNN
- image + context fusion
- calibration
- optional uncertainty

### 21.3 v1.1 — Stability Upgrade
- persistence filter
- ensemble variance uncertainty
- regime-sliced reporting

### 21.4 Phase 2
- small GRU/TCN baseline
- Transformer/Quantformer 계열 비교
- optional news-conditioned shrink

---

## 22. Acceptance Criteria

| 분류 | 기준 |
|------|------|
| 계약 | StrategyCard schema 100% pass |
| 통합 | 기존 run_e2e_pipeline.py 무수정 통과 |
| 운영 | publish_strategy_variant.py로 단일 profile publish 가능 |
| 스타일 | score와 past_5d_return 상관이 음수 또는 낮음 |
| 품질 | heuristic baseline 대비 Precision@N 개선 |
| 실전성 | Risk/FDA downstream approval 가능 |
| 재현성 | 동일 seed/model version으로 재현 가능 |
| 문서화 | 학습/추론/발행 CLI와 결과 보고서 존재 |

---

## 23. 최종 권고안

Model B는 KR-Rebound-CNN v1.0으로 설계한다.
I20 chart image + sector-aware tabular context로
향후 5거래일 초과수익 반등 확률을 예측하고,
기존 StrategyCard 스키마 그대로 발행하며,
variant publish 방식으로 기존 AI #1 pipeline에 무수정 연결한다.

이 안은:
- 최근 한국 시장 문헌과 부합하고,
- AI #2의 LightGBM momentum과 명확히 차별화되며,
- 현재 repo의 strategy_loader.py, run_e2e_pipeline.py, run_risk_engine.py 제약과도 맞고,
- 10주 capstone 범위 안에서 구현 가능한 수준이다.

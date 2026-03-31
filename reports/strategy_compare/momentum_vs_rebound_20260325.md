# Momentum vs Rebound 전략 비교 리포트

> 기준일: 2026-03-25 (20260325)
> 생성일: 2026-03-29

---

## 1. 전략 개요

| 항목 | Momentum (AI #2) | Rebound (AI #1) |
|------|-------------------|-----------------|
| 모델 | LightGBM cross-sectional ranker | KR-Rebound-CNN (2-Stage) |
| Alpha family | Momentum (상승 추세 추종) | Reversal (과매도 반등) |
| 입력 | DMP tech_features + TTP news | DMP OHLCV chart image + context |
| SC Builder | jobs/build_strategy_card_momentum.py | jobs/build_strategy_card_rebound.py |
| 변형 | quant_only / news_only / full | committee (Tree Core+CNN) |

## 2. 신호 분포 비교 (20260325)

| Signal | Momentum | Rebound |
|--------|----------|---------|
| strong_buy | 3 | 2 |
| buy | 1 | 0 |
| hold | 22 | 24 |
| sell | 0 | 0 |
| **BUY 합계** | **4** | **2** |

## 3. BUY 종목 비교

| 항목 | Momentum | Rebound |
|------|----------|---------|
| BUY 종목 | SK하이닉스(000660), LG에너지솔루션(373220), DB손보(005830), 한화에어로(012450) | 기아(000270), 현대모비스(086790) |
| 겹치는 종목 | 없음 (0종목) | |
| 평균 confidence | 0.701 | (Rebound 별도) |

## 4. 전략 상보성 분석

두 전략의 BUY 종목이 완전히 다름 → **높은 상보성(complementarity)**

- **Momentum**: 최근 상승 추세 강한 종목 (SK하이닉스, LG에너지솔루션 등 반도체/배터리)
- **Rebound**: 최근 과매도 후 반등 신호 (기아, 현대모비스 등 자동차)
- 두 전략을 결합하면 시장 국면에 따라 다른 알파 원천 활용 가능

## 5. Regime별 기대 특성

| Market Regime | Momentum 예상 | Rebound 예상 |
|---------------|--------------|-------------|
| Green (상승장) | 강세 — 추세 추종 유리 | 약세 — 과매도 종목 적음 |
| Yellow (횡보장) | 보통 — 모멘텀 약화 | 강세 — 섹터 로테이션 반등 |
| Red (하락장) | 약세 — 하락 추세 매수 위험 | 강세 — 급락 후 기술적 반등 |

## 6. 파이프라인 통합 현황

| 단계 | Momentum | Rebound |
|------|----------|---------|
| SC 생성 | PASS (26카드, 스키마 검증 통과) | PASS (모델 학습 시) |
| Publish → strategy_card/ | PASS (--publish 플래그) | PASS (publish_strategy_variant.py) |
| RiskEngine 소비 | PASS | PASS |
| FDA 소비 | PASS (E2E 검증) | PASS (E2E 검증) |
| PortfolioState 반영 | PASS | PASS |
| Backtest | jobs/run_backtest.py | jobs/run_backtest_replay.py |

## 7. 제한 사항

- Momentum LightGBM은 아직 fallback heuristic으로 동작 (모델 학습 필요)
- Rebound는 Tree Core + CNN 2-Stage committee 구조 (v1.1). model.pt 학습 완료 시에만 SC 생성 가능
- 두 전략의 실제 백테스트 성과 비교는 모델 학습 + 전체 기간 backtest 후 가능
- 현재 비교는 단일 날짜(20260325) 기준 신호 분포 비교

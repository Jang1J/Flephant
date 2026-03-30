# Gemini Pro 진단 프롬프트 — Elephant Lab 전체 코드 구현도 + 성능 진단

아래 프롬프트를 Gemini Pro에 붙여넣으세요. 코드 파일은 각 섹션의 파일 경로를 참고하여 첨부하세요.

---

## 프롬프트 시작

```
당신은 금융 ML 시스템 아키텍트이자 퀀트 엔지니어입니다.
아래 프로젝트의 전체 코드베이스를 진단하고, 구현 완성도와 ML 성능을 체계적으로 평가해 주세요.

# 프로젝트 개요

KOSPI 대형주 26종목 대상 멀티에이전트 트레이딩 알고리즘 (대학원 종합설계 프로젝트).
t일 장마감 데이터로 t+1 거래일 종목별 BUY/HOLD/SELL 판단을 내리는 시스템.

## 파이프라인 (7단계)
DailyMarketPacket(DMP) → TickerTextPack(TTP) → StrategyCard(SC)
→ RiskEngine → RiskCard(RC) + CandidateOrderPlan(COP)
→ FinalDecisionAgent(FDA) → FinalDecisionCard(FDC)
→ PortfolioManager → PortfolioState(PFS)

## 코드베이스 통계
- Python 파일: 42개 / 총 16,612줄
- JSON 스키마: 17개 (schemas/*.json)
- 아티팩트 디렉토리: 15개 (artifacts/)
- DMP 데이터: 401일 (2024-07-31 ~ 2026-03-27)
- 유니버스: KOSPI 대형주 26종목

## AI #2 전략 엔진 (2-branch 구조)

### Branch 1: LightGBM Momentum Ranker
- 파일: models/strategy_model/lgbm_ranker.py (1,058줄)
- 피처: 28개 (manual 10 + mlf_lite 2 + umi_lite 3 + cross_sectional_pct 8 + ohlcv_micro 5)
- 레이블: sector-neutral excess return, dual binary(0/1) + ordinal(0/1/2)
- 학습: walk-forward expanding window + purge(5d) + embargo(5d)
- 성능 (9-fold):
  - Binary AUC: 0.682 ~ 0.761 (평균 ~0.734)
  - Ordinal AUC(OVR): 0.728
- 추론: binary(0.6) + ordinal class2(0.4) 가중 평균
- SC 출력: build_strategy_card_momentum.py (632줄)
  - 3-branch: synthesized, quant_only, news_only
  - Top-K shortlist: max_position_count(10)까지만 BUY 허용

### Branch 2: KR-Rebound-CNN + Committee
- 모델: 3-channel 64x64 chart tensor + context vector (39-dim)
  - ImageEncoder: Conv32→64→128 + AdaptiveAvgPool → 128-d
  - ContextEncoder: 39-d → Dense(64) → Dense(32) → 32-d
  - Fusion Head: Concat(160) → Dense(64) → Dense(1) → logits (Sigmoid 제거)
- Context features (39-dim = 23 base + 16 sectors):
  - macro(4) + technical(5) + price_stretch(2) + sector_relative(3)
  - confirmation(8): ret_5d_rank, close_sma20_rank, bb_pos, overnight_gap, intraday_range, upper/lower_shadow, body_ratio
  - sector_onehot(16) + mktcap_rank(1)
- 학습: BCEWithLogitsLoss, AdamW(lr=3e-4, wd=0.01), batch=32
- 레이블: sector-neutral excess return (종목 ret - 섹터 평균 ret)
- 14-fold walk-forward:
  - AUC 범위: 0.446 ~ 0.872 (평균 ~0.641)
  - Calibration: Isotonic Regression winner
- 3-seed ensemble (42, 123, 456) + variance → uncertainty
- Committee v1.1: ElasticNet tabular(0.65) + CNN(0.35) fusion
- Oversold Gate: 4조건 중 2개 + sector-relative 약세 + relaxation
- SC 출력: build_strategy_card_rebound.py (1,159줄)
  - Gate 내 top-N(5) shortlist
  - Directional agreement 강화: CNN/Tab 방향 불일치 시 hold 강제
  - Persistence Filter (옵션)

## 리스크 엔진
- Regime Gate: VIX proxy >= 90 → red, >= 70 → yellow
- Position: max 10종목, 단일 <= 20%, 섹터 <= 40%
- Stop-loss: -5%, Turnover cap: 30%/일, 최소 현금: 10%
- min_confidence: 0.3

## 핵심 제약
- PIT-Safety: 미래 데이터 사용 금지. snapshot 기준 18:00 KST
- can_change_weight = false: FDA는 비중 수정 불가 (approve/veto만)
- 스키마 준수: 17개 JSON 스키마 strict validation
- 정책 동기화: risk_policy_v0.yaml에서 값 로드 (하드코딩 금지)

---

# 진단 요청 (5개 섹션)

## 섹션 1: 구현 완성도 진단
아래 체크리스트 기준으로 각 항목의 구현 상태를 [완료/부분/미구현]으로 평가하고, 미구현 또는 개선이 필요한 항목을 우선순위(P0/P1/P2)로 분류해 주세요.

| 항목 | 세부 |
|------|------|
| 데이터 파이프라인 | DMP 수집, TTP 생성, 커넥터 (KRX/DART/Naver/ECOS) |
| AI #2 Momentum | 피처 엔지니어링, 학습, 추론, SC 생성 |
| AI #2 Rebound | chart tensor, context vector, CNN 학습, 앙상블, calibration, committee |
| 리스크 엔진 | regime gate, position sizing, stop-loss, turnover cap |
| FDA | LLM 기반 approve/veto, can_change_weight=false 준수 |
| 포트폴리오 관리 | portfolio state 갱신, 현금 비율, 수수료 |
| 백테스트 | walk-forward, 거래 비용, 벤치마크 대비 |
| 운영 자동화 | E2E pipeline, replay, backfill, 장중 사이클, paper trading |
| 스키마/정합성 | 17개 스키마 검증, config-code 동기화 |
| UQ (불확실성) | ensemble variance, calibration, uncertainty_score |

## 섹션 2: ML 모델 성능 진단
아래 메트릭에 대해 현 수준을 평가하고, 학술적/실무적 관점에서 개선 방향을 제시해 주세요.

### Momentum Ranker
- Binary AUC 0.734 (9-fold 평균) — 충분한가? 유니버스 26종목 cross-sectional ranking에서 의미있는 수준인가?
- Ordinal AUC 0.728 — binary 대비 추가 가치가 있는가?
- Precision@5 = 0.0 (binary mode) — 왜 0인가? 해결 방안은?
- cross-sectional pct features 8개와 ohlcv_micro 5개의 기대 기여도는?
- sector-neutral label의 효과 (이전 0.539 → 현재 0.734)

### Rebound CNN
- AUC 범위 0.446~0.872 (14-fold) — fold간 분산이 큰 이유는? overfitting 가능성?
- Oversold Gate 후 샘플 수 (175개/60일) — 충분한가?
- Committee fusion (ElasticNet 0.65 + CNN 0.35) — 가중치 적절한가?
- BCEWithLogitsLoss 전환의 이론적 근거와 기대 효과
- Sigmoid 제거 → logits 출력의 calibration 영향

## 섹션 3: 아키텍처 리스크 진단
다음 관점에서 시스템 취약점을 평가해 주세요:

1. **PIT-Safety 위반 가능성**: 학습/추론 경로에서 미래 데이터 유입 경로가 있는가?
2. **학습-추론 불일치 (train-serve skew)**: dataset.py와 SC emitter에서 피처 계산 로직이 분기되어 있는 부분은?
3. **과적합 리스크**: 26종목 × 401일 = 10,426 관측치로 28개 피처 + 39-dim context가 과적합되지 않는가?
4. **Regime sensitivity**: VIX proxy 기반 regime gate가 KOSPI에 적합한가? 대안은?
5. **Lookahead bias**: walk-forward purge/embargo가 충분한가? 5일이 적절한가?

## 섹션 4: 종합설계 프로젝트 관점 평가
학술적/교육적 관점에서:

1. **논문 근거**: 6편 논문(AI-Trader, StockBench, FinPos, FinTexTS, VTA, R&D-Agent-Quant) 매핑이 코드에 실제로 반영되어 있는가?
2. **발표/시연 준비도**: demo/, reports/ 아티팩트가 포트폴리오 시연에 충분한가?
3. **재현 가능성**: 제3자가 README + config + DMP 데이터로 파이프라인을 재현할 수 있는가?
4. **차별화 요소**: 기존 퀀트 시스템 대비 이 프로젝트의 학술적 기여는 무엇인가?

## 섹션 5: 개선 우선순위 Top-10
위 진단 결과를 종합하여, 향후 2주 내 구현 가능한 개선 항목을 Top-10으로 정리해 주세요.
각 항목에 [우선순위, 예상 효과, 난이도, 의존성]을 명시해 주세요.

---

# 첨부 파일 목록 (아래 파일들을 Gemini에 업로드하세요)

## 필수 첨부 (핵심 코드)
1. models/strategy_model/lgbm_ranker.py
2. models/strategy_model/config.yaml
3. models/rebound_cnn/model.py
4. models/rebound_cnn/train.py
5. models/rebound_cnn/dataset.py
6. models/rebound_cnn/preprocess.py
7. models/rebound_cnn/config.yaml
8. jobs/build_strategy_card_momentum.py
9. jobs/build_strategy_card_rebound.py
10. jobs/run_risk_engine.py
11. agents/final_decision_agent.py
12. config/risk_policy_v0.yaml
13. schemas/strategy_card.json

## 권장 첨부 (파이프라인 + 운영)
14. jobs/run_e2e_pipeline.py
15. jobs/run_backtest.py
16. jobs/portfolio_manager.py
17. jobs/strategy_loader.py
18. config/universe_v1.csv
19. 문서/3주차/KOSPI_프로젝트_제안서_v11_최종.md
20. 문서/KR_Rebound_CNN_v1_설계서.md

## 선택 첨부 (성능 데이터)
21. models/rebound_cnn/training_log.json (있는 경우)
22. artifacts/strategy_model/fold_meta_*.json (가장 최근)
23. reports/strategy_compare/momentum_vs_rebound_20260325.md
```

## 프롬프트 끝

---

## 사용 가이드

### Gemini Pro 1.5 / 2.0 사용 시
- 위 프롬프트를 **System 또는 첫 메시지**로 입력
- 필수 첨부 13개 파일을 파일 업로드로 첨부
- 응답이 길어질 경우 "섹션 1~2 먼저, 이후 3~5를 이어서" 분할 요청

### 응답 후 후속 프롬프트
1. "섹션 2에서 지적한 [항목]에 대해 구체적 코드 수정 방안을 제시해 주세요"
2. "Top-10 개선 항목 중 1~3번을 구현하기 위한 코드 diff를 작성해 주세요"
3. "발표 자료에서 강조해야 할 기술적 차별화 포인트 3가지를 정리해 주세요"

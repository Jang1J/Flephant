# GPT Pro 프롬프트 #2 — 2nd 퀀트 모델 설계 상담

아래 프롬프트를 GPT Pro에 복사해서 넣으세요.

---

## 프롬프트

나는 종합설계(캡스톤) 프로젝트에서 KOSPI 대형주 26종목 대상 멀티에이전트 트레이딩 시스템을 만들고 있어. 현재 상황과 내가 하려는 것을 설명할게.

### 현재 상황

**팀 구성**: AI 2명 + BE 2명
- **AI #1 (나)**: Data Agent + Risk Agent + Final Decision Agent → **W1~W11 구현 완료**
- **AI #2 (팀원)**: Strategy Agent (LightGBM ranker + News Strategy + Synthesizer) + Backtest Agent → **아직 미구현**

**파이프라인**:
```
Data Agent(내가 만듦) → Strategy Agent(AI #2) → Risk Agent(내가 만듦) → FDA(내가 만듦) → Portfolio
```

**문제**: AI #2가 아직 real StrategyCard를 만들지 못했어. 현재 내 파이프라인은 mock StrategyCard(랜덤)로 동작 중이야.

### 내가 하려는 것

AI #2의 모델(LightGBM cross-sectional ranker)과 **완전히 다른 성향의 2nd 퀀트 모델**을 내가 예비용으로 만들려고 해.

**목적**:
1. AI #2와 협의 전에 미리 만들어서 "이거 어때요?"라고 제안하기 위한 **예비 구현**
2. 승인되면 **사용자가 투자 성향을 2개 중 선택**하는 기능으로 확장
3. 리젝되면 **개인 프로젝트 포트폴리오**로 활용
4. AI #1이 W11까지 구현한 기존 코드를 **절대 건드리지 않고**, 독립적으로 추가

**AI #2의 모델 (예정)**:
- LightGBM cross-sectional ranker
- 기술적 지표 + 뉴스 감성 → 종목 랭킹 → top N 매수
- 특징: **상대 강도(cross-sectional)** 기반, 단기 모멘텀 추종

**내가 만들고 싶은 2nd 모델의 방향**:
- AI #2와 **성향이 반대**여야 함
- 예시: 시계열 예측 기반 (LSTM/GRU/Transformer), 평균 회귀(mean-reversion), 가치 투자 성향
- DMP에 이미 OHLCV + 기술적 지표가 있으니 **추가 데이터 수집 없이** 바로 모델링 가능
- 출력은 기존 StrategyCard 스키마와 동일 → 기존 파이프라인(Risk→FDA→PFS)에 그대로 연결 가능

### 기존 StrategyCard 스키마 (내 파이프라인이 소비하는 형태)

```json
{
  "card_id": "SC-20260325-005930",
  "snapshot_dt": "2026-03-25T18:00:00+09:00",
  "ticker": "005930",
  "direction": "long|short|neutral",
  "signal": "strong_buy|buy|hold|sell|strong_sell",
  "confidence": 0.82,
  "pre_risk_score": 0.65,
  "quant_score": 0.7,
  "news_signal": 0.5,
  "rationale": "투자 근거...",
  "source_strategy": "quant|news|synthesized",
  "evidence_ids": ["..."],
  "features_used": ["sma_5", "rsi_14", ...],
  "uncertainty_score": null
}
```

### 내가 사용 가능한 데이터 (DMP에 이미 있는 것)

- OHLCV (시가, 고가, 저가, 종가, 거래량) — 최근 60거래일
- 기술적 지표: SMA(5,20,60), RSI(14), MACD, Bollinger Band, 거래량 비율
- 매크로: 기준금리, 환율, KOSPI 변동성(VIX proxy), 시장 breadth
- 뉴스 감성: Kanana-o LLM 분석 결과 (news_sentiment -1~+1)
- 시가총액, 섹터 정보

### 질문

1. **AI #2의 LightGBM(모멘텀/랭킹)과 확실히 차별화되는 2nd 퀀트 모델을 뭘로 만들면 좋을까?**
   - LSTM/GRU 시계열 예측?
   - Transformer (PatchTST 등)?
   - 평균 회귀(mean-reversion) 전략?
   - 가치 투자 팩터 모델?
   - 앙상블 가능성?

2. **투자 성향 분리가 자연스러운 구조**를 어떻게 설계하면 좋을까?
   - 예: 모델 A = "공격적 (모멘텀)", 모델 B = "보수적 (가치/평균회귀)"
   - 사용자가 성향을 선택하면 해당 모델의 StrategyCard가 파이프라인에 투입

3. **기존 파이프라인을 건드리지 않고** 2nd 모델을 추가하는 가장 깔끔한 아키텍처는?
   - strategy_loader.py가 이미 real SC 자동 감지 + mock fallback을 지원
   - 2nd 모델의 SC도 같은 스키마로 `artifacts/strategy_card/`에 저장하면 기존 파이프라인이 그대로 소비 가능

4. **학기 내 구현 가능한 현실적 범위**는? (남은 기간 약 10주)
   - 너무 복잡한 건 안 됨 (종설 프로젝트)
   - 하지만 논문/포트폴리오에 어필 가능한 수준이어야 함

5. **W10 ablation에서 비교 실험 설계** — 모델 A vs 모델 B 비교가 자연스럽게 들어갈 수 있을까?

참고:
- Python 환경: anaconda elephant conda env
- 사용 가능 라이브러리: PyTorch, scikit-learn, LightGBM, pandas, numpy
- LLM: Kanana-o (한국어 특화) + GPT-4o (fallback)
- 프로젝트 참조 논문: MetaGPT, TradExpert, AAPM, AlphaGAT, AlphaAgent, R&D-Agent-Quant + KOSPI 보강 3편(MLF, UMI, FAILAB Instability)

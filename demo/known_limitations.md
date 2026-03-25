# Known Limitations — Phase 1 (W1~W4) + W5 진행

> 최종 업데이트: 2026-03-25
> 중간발표: 2026-04-17

## 1. 구현 완료 vs 미구현

### ✅ 구현 완료
- Data Agent: OHLCV, 기술적 지표, 뉴스/공시 인덱스, 매크로 스냅샷
- Risk Agent v3: 2-tier (Regime Gate + Position Constraints) + PFS 기반 stop-loss + turnover hard cap
- Final Decision Agent: 독립 모듈 (`agents/final_decision_agent.py`) + prompt contract + conflict taxonomy
- PortfolioState: 진입가·보유일수·실제 turnover·미실현 PnL 추적 (stateful artifact)
- HourlyMarketPatch: delta artifact 구조 + intraday cycle runner
- UQ Calibration v0: logistic regression + P85 threshold (synthetic data)
- E2E 파이프라인: 7-step (DMP → TTP → PFS load → Risk → FDA → PFS update → schema validation)
- Replay: stateful 5거래일 replay (PFS carry-forward)
- Intraday Cycle: daily backbone + hourly delta 파이프라인
- Artifact Schema: **10개** JSON schema + validation 통과
  - DMP, TTP, RC, COP, FDC, PFS, HMP, UQ, StrategyCard, DataQualityReport
  - (RiskAuditLog는 logs/risk_audit/에 저장되며 별도 JSON schema 없음)
- API 연동: KRX(pykrx+OpenAPI), DART, Naver News, ECOS 4소스 smoke test 통과
- AI #2 Handoff: `validate_ai2_handoff.py` 통합 테스트 harness
- Strategy Loader: real SC 자동 감지 + mock fallback (`strategy_loader.py`)
- Main Runner 통합: E2E/Intraday/RiskEngine 모두 real SC 자동 감지 경로 완비
- LLM Router Smoke Test: GPT-4o fallback 정상 동작 확인 (2026-03-25)

### ⚠️ 부분 구현 (Phase 2에서 완성)
- **Historical text backfill**: 과거 날짜의 뉴스/공시는 PIT-safety를 위해 비활성화
  - OHLCV/macro는 완전 PIT-safe, text는 당일 live만 안전
- **VIX proxy**: KOSPI 20일 변동성으로 구현 완료 → Phase 2에서 KOSPI200 옵션 내재변동성으로 고도화
- **Market breadth**: KOSPI 전종목 상승비율로 구현 완료 → Phase 2에서 세분화
- **UQ tail cap**: 구조는 완비, enabled=false (실제 StrategyCard 연결 후 활성화)
- **Kanana-o LLM explanation**: router + circuit breaker 완성, FDC explanation 연동 완료
  - Kanana-o API 키 미발급 → 현재 GPT-4o fallback 또는 deterministic explanation만 사용
- **시가총액**: KRX Open API (`stk_bydd_trd`) 승인 완료, 커넥터 fallback 구현 완료
  - pykrx 호환 이슈 시 자동으로 KRX Open API 사용
- **FDA backtest_summary**: 구조는 있으나 AI #2의 BacktestReport 미연결 (W6 예정)

### ❌ 의도적 미구현 (이번 학기 범위 밖)
- RL allocator / 강화학습
- Similar Case Retriever (full graph-based)
- GNN relation model
- 실계좌 주문 연동 / 실제 자동매매 실행 (모의투자 gateway까지만)
- LSTM/GNN shadow baseline (optional만)
- Real-time streaming (batch 기반 운영)
- full LoRA fine-tuning
- 전종목 pairwise ranking

## 2. 데이터 한계

| 항목 | 현재 | 개선 방향 |
|------|------|----------|
| 시가총액 | ✅ KRX Open API fallback 구현 완료 | 승인 후 자동 사용 |
| PER/PBR | KRX `stk_isu_base_info`에 미포함 | pykrx 또는 다른 소스 보완 |
| VIX proxy | KOSPI 20일 변동성 (연환산) | Phase 2: KOSPI200 옵션 내재변동성 |
| Market breadth | KOSPI 전종목 상승비율 | Phase 2: 업종별 세분화 |
| KOSPI 지수 | ✅ KRX Open API 신청 완료 | 승인 후 stress gate 연동 |
| 뉴스 historical | Naver API는 과거 정확 재현 불가 | backtest에서 text 비활성화로 leakage 차단 |
| 공시 | ✅ DART 유니버스 필터링 적용 완료 | — |
| 환율 | ✅ ECOS stat code 수정 완료 (`731Y001`) | 정상 조회 가능 |

## 3. 모델 한계

- **UQ 모델**: synthetic data로 학습 → AUC 0.55 수준 (baseline)
  - Phase 2에서 실제 StrategyCard + 실현수익률로 재학습 예정
- **LightGBM ranker**: AI #2 영역, Phase 1에서는 mock StrategyCard 사용
- **Kanana-o**: API 키 미발급 (closed beta 대기 중), GPT-4o fallback 사용 중
- **FDA conflict resolution**: deterministic 6-rule veto만 동작, LLM-based conflict 해소는 Phase 2

## 4. 실험 한계

- backtest 기간: 현재 5거래일 (demo용) → W6에서 정식 Backtest Agent 구현
- baseline 비교: 아직 미실행 (W6에서 AI #2 담당, 6종)
- ablation: 미실행 (W10에서 4종 must-have 예정)
- DSR overfitting 검증: W6~W8
- walk-forward purge/embargo: W6

## 5. 선택 확장 (진행 상태)

- [x] VIX proxy 실제 값 연동 → KOSPI 20일 변동성
- [x] Market breadth 계산 로직 → KOSPI 상승종목비율
- [x] DART 유니버스 필터링 → DMP + TTP 양쪽 적용
- [x] LLM Router circuit breaker → 연속 실패 시 자동 fallback 전환
- [x] FDC LLM explanation 연동 → Kanana-o/GPT-4o 한국어 설명
- [x] PortfolioState (진입가/보유일수/실제 turnover)
- [x] HourlyMarketPatch (1시간 delta artifact)
- [x] FDA 독립 모듈 분리 + prompt contract
- [x] Intraday cycle runner (daily backbone + hourly patch)
- [x] AI #2 handoff validation harness
- [x] strategy_loader 모듈 (real SC 자동 감지 + mock fallback)
- [x] main runner real SC 통합 (E2E/Intraday/RiskEngine)
- [x] LLM Router smoke test 실행 (GPT-4o 정상)
- [x] KRX Open API 시가총액 연동
- [x] ECOS 환율 stat code 수정
- [ ] pubDate ISO 8601 정규화
- [ ] alias normalization 실사용
- [ ] MLF-lite feature bank (W8)
- [ ] UMI-lite feature bank (W8)
- [ ] Backtest Agent 정식 구현 (W6)
- [ ] KIS 모의투자 gateway (W7)

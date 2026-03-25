# Smoke Test Report — 2026-03-22 (W1~W4 통합)

## 환경
- Python: 3.12.13 (conda elephant)
- OS: macOS (Apple Silicon)
- 패키지: requirements.txt 전체 설치 완료

## 1. API Connector Tests

| Connector | Status | Note |
|-----------|--------|------|
| KRX (pykrx) | ✅ PASS | OHLCV 26종목 정상, 시가총액은 pandas 호환 이슈 |
| DART | ✅ PASS | 공시 100건 수집 성공 |
| Naver News | ✅ PASS | 종목별 3건씩 뉴스 수집 |
| ECOS | ✅ PASS | 기준금리 2.5%, 국고채 3Y/10Y 정상 |
| LLM Router | ⚠️ 구조완성 | API 키 미연결 (Kanana→GPT-4o fallback + retry 구현) |

## 2. Pipeline Tests

| Pipeline | Status | Note |
|----------|--------|------|
| DailyMarketPacket 생성 | ✅ PASS | 26종목, PIT-safe 필터 적용 |
| TickerTextPack 생성 | ✅ PASS | 3-level, dedup, alias normalization |
| DataQualityReport | ✅ PASS | missing/stale/dedup 정상 |
| Risk Engine v3 | ✅ PASS | Regime Gate + Position + Stop-Loss + UQ |
| E2E Pipeline | ✅ PASS | DMP→TTP→SC(mock)→RC→COP→FDC 전체 통과 |
| 5거래일 Replay | ✅ PASS | 수동 수정 없이 연속 성공 |
| Backfill 5일 | ✅ PASS | 26/26 종목 커버리지 |

## 3. Schema Validation

| Schema | Status |
|--------|--------|
| daily_market_packet.json | ✅ PASS |
| ticker_text_pack.json | ✅ PASS |
| risk_card.json | ✅ PASS |
| candidate_order_plan.json | ✅ PASS |
| final_decision_card.json | ✅ PASS |
| strategy_card.json | ✅ PASS |
| uq_model_io_v1.json | ✅ PASS |

## 4. UQ Model

| Metric | Value |
|--------|-------|
| Model | Logistic Regression |
| Training data | 500 synthetic samples |
| CV AUC | 0.5492 ± 0.0359 |
| P85 threshold | 0.3587 |

## 5. Demo Scenarios

| # | Scenario | Regime | Result |
|---|----------|--------|--------|
| 1 | 정상 시장 | 🟢 Green | 3종목 승인, 60% 노출 |
| 2 | 경계 시장 | 🟡 Yellow | 2종목 축소 승인, 20% 노출 |
| 3 | 위기 시장 | 🔴 Red | 전면 차단, 100% 현금 |

## 6. 확인된 이슈 (Phase 2 이관)

- 시가총액: pykrx + pandas 호환 → null
- VIX proxy / Market breadth: null (Phase 2)
- 뉴스 PIT-safe: 과거 날짜 미래 뉴스 제거 동작 확인
- UQ: synthetic data → 실제 데이터 재학습 필요

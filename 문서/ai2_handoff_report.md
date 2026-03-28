# AI #2 Handoff Report — W5

> 작성일: 2026-03-25
> AI #1 측 초안 (AI #2 합동 확인 필요)

---

## 1. Handoff 개요

| 항목 | 내용 |
|------|------|
| 인터페이스 | StrategyCard (SC) |
| 방향 | AI #2 (Strategy Agent) → AI #1 (Risk/FDA) |
| 스키마 | `schemas/strategy_card.json` |
| 계약서 | `문서/strategy_contract_v1.md` |
| 검증 도구 | `jobs/validate_ai2_handoff.py` |

---

## 2. AI #1 측 준비 완료 항목

### 2.1 Strategy Loader (`jobs/strategy_loader.py`)
- `has_real_sc(date)`: real SC 파일 존재 여부 확인
- `load_strategy_cards(date)`: 단일 파일 / 개별 파일 자동 감지 로드
- `generate_mock_strategy_cards(date, dmp)`: mock SC 생성 (개발용)

### 2.2 Main Runner 통합
| 파일 | real SC 자동 감지 | mock fallback |
|------|------------------|---------------|
| `run_e2e_pipeline.py` | ✅ | ✅ |
| `run_risk_engine.py` | ✅ | ✅ |
| `run_intraday_cycle.py` | ✅ | ✅ |

### 2.3 Validation Harness (`jobs/validate_ai2_handoff.py`)
4단계 자동 검증:
1. SC 파일 감지
2. Schema Validation (`schemas/strategy_card.json`)
3. E2E Pipeline (real SC → Risk → FDA → FDC)
4. 결과 요약 + JSON 리포트 저장

validate_ai2_handoff.py는 다음 semantic 검증을 수행한다: (1) ticker unique (2) ticker ∈ universe (3) snapshot_dt PIT-Safety (4) signal/direction 일관성 (5) confidence ∈ [0,1] (6) evidence_ids 비어있지 않음 (7) 26종목 전체 coverage

현재 AI #1의 backup strategy(KR-Rebound-Committee)도 동일한 SC contract를 사용하며, 동일한 handoff validator를 통과해야 한다.

SC는 유니버스 26종목 전체에 대해 생성되어야 한다.

### 2.4 FDA Real SC 처리
- `final_decision_agent.py`가 `strategy_cards: list`를 직접 수신
- SC의 `signal`, `confidence`, `source_strategy`를 conflict detection에 활용
- 6가지 conflict rule 적용 (quant↔news 방향 불일치, 낮은 confidence 등)

### 2.5 LLM Router
- Kanana-o (primary) + GPT-4o (fallback) 자동 전환
- Circuit breaker: 연속 3회 실패 → 5분 cooldown
- Smoke test 통과 (2026-03-25): GPT-4o 정상 동작 확인
- Kanana-o: API 키 미발급 (closed beta 대기 중)

---

## 3. AI #2 측 필요 작업

### 3.1 Real StrategyCard v1 생성
- 실제 LightGBM + LLM 출력 기반 SC 파일 생성
- `artifacts/strategy_card/SC-{YYYYMMDD}.json` 형식으로 배치

### 3.2 Branch Output 3종
- `strategy_card_quant_only.json` — Quant만 사용
- `strategy_card_news_only.json` — News만 사용
- `strategy_card_full.json` — full synthesized

### 3.3 Schema/필드 Freeze 확인
- `strategy_contract_v1.md` 합의

### 3.4 5거래일 Replay용 샘플
- 최소 5거래일분 real SC 생성 (E2E replay 검증용)

---

## 4. 공동 작업 체크리스트

- [ ] real SC 기반 E2E 1회 성공
- [ ] real SC 기반 5거래일 replay 1회 성공
- [ ] `strategy_contract_v1.md` 양측 합의
- [ ] 이 리포트 양측 확인

---

## 5. W5 종료 기준

| 기준 | 상태 |
|------|------|
| DMP + TTP + **real** SC → Risk → FDA → FDC 1회 E2E 성공 | ⏳ AI #2 SC 대기 |
| quant-only / news-only / full branch artifact 3종 저장 | ⏳ AI #2 생성 대기 |
| `strategy_contract_v1.md` 합의 완료 | ⏳ 초안 작성 완료, 합의 대기 |

---

## 6. 검증 실행 방법

```bash
# AI #2가 SC 파일을 배치한 후:
python jobs/validate_ai2_handoff.py YYYYMMDD

# 출력 예시:
# [1/4] StrategyCard 파일 감지...
# [2/4] Schema Validation...
# [3/4] E2E Pipeline (real StrategyCard)...
# [4/4] 결과 요약
# [PASS] detect / schema / e2e
```

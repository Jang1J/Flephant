# StrategyCard Contract v1

> 합의일: 2026-03-25 (W5)
> 상태: **FREEZE** — 이후 필드 변경 금지 (v2 시 별도 버전)

---

## 1. 목적

AI #1(Risk/FDA)과 AI #2(Strategy Agent) 간 **StrategyCard handoff 인터페이스**를 고정한다.
이 문서에 정의된 스키마·파일 규격·전달 방식이 W5 이후 변경 없이 유지됨을 양측이 합의한다.

---

## 2. 스키마 (schemas/strategy_card.json)

| 필드 | 타입 | required | 설명 |
|------|------|----------|------|
| `card_id` | string | ✅ | `SC-{YYYYMMDD}-{TICKER}` 형식 |
| `snapshot_dt` | string (ISO 8601) | ✅ | PIT-safe snapshot 시각 (18:00 KST) |
| `artifact_version` | string | ✅ | 스키마 버전 (v1.0) |
| `ticker` | string | ✅ | 6자리 zero-padded 종목코드 |
| `direction` | enum | ✅ | `long` / `short` / `neutral` |
| `signal` | enum | ✅ | `strong_buy` / `buy` / `hold` / `sell` / `strong_sell` |
| `confidence` | number [0,1] | ✅ | 신호 확신도 |
| `pre_risk_score` | number | ✅ | Quant + News 결합 점수 (Risk 적용 전) |
| `quant_score` | number | — | Quant Strategy 점수 |
| `news_signal` | number | — | News Strategy 점수 |
| `rationale` | string | ✅ | 투자 근거 요약 |
| `source_strategy` | enum | ✅ | `quant` / `news` / `synthesized` |
| `evidence_ids` | string[] | ✅ | 근거 데이터 ID (DMP/TTP 참조) |
| `features_used` | string[] | — | 사용된 feature 목록 |
| `uncertainty_score` | number \| null | — | UQ 추정치 (Phase 1: null, Phase 2+: 활성화) |

---

## 3. 파일 규격

### 파일명 패턴
```
artifacts/strategy_card/SC-{YYYYMMDD}.json          # 단일 파일 (배열)
artifacts/strategy_card/SC-{YYYYMMDD}-{TICKER}.json  # 종목별 개별 파일
```

- 두 가지 패턴 모두 허용. AI #1 측 `strategy_loader.py`가 자동 감지.
- 단일 파일 형태를 **권장** (관리 편의).

### 내용 형식
- 단일 파일: JSON 배열 `[{SC}, {SC}, ...]`
- 개별 파일: JSON 객체 `{SC}`

### PIT-Safety
- `snapshot_dt`는 반드시 해당 거래일 18:00 KST 이전 시각이어야 한다.
- 미래 데이터 사용 금지.

---

## 4. 전달 방식

1. AI #2가 `artifacts/strategy_card/` 디렉토리에 SC 파일을 배치
2. AI #1 측 파이프라인이 `strategy_loader.has_real_sc(date)` → `load_strategy_cards(date)` 로 자동 로드
3. real SC 미존재 시 자동으로 mock SC fallback (개발/테스트용)

---

## 5. Validation

- `validate_ai2_handoff.py`가 공식 통합 테스트 harness
- 검증 단계: 파일 감지 → schema validation → Risk Engine → FDA → FDC 생성
- 리포트: `artifacts/validation_report/VR-{YYYYMMDD}.json`

---

## 6. 변경 관리

- **v1 freeze 이후 필드 추가/삭제/타입 변경 금지**
- 변경 필요 시: `strategy_card_v2.json` 별도 스키마 + 양측 합의 필요
- `artifact_version` 필드로 버전 구분

---

## 7. Branch Output (W10 Ablation 대비)

AI #2는 아래 3종 branch output을 함께 저장한다:

| 파일 | 내용 |
|------|------|
| `strategy_card_quant_only.json` | Quant Strategy만 사용한 SC |
| `strategy_card_news_only.json` | News Strategy만 사용한 SC |
| `strategy_card_full.json` | full synthesized SC (= main SC) |

---

## 합의

- [ ] AI #1 확인
- [ ] AI #2 확인

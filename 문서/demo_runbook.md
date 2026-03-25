# Demo Runbook — Elephant Lab

> 최종발표 데모 실행 가이드
> 작성일: 2026-03-25

---

## 사전 준비

### 환경
- Python 환경: `/opt/anaconda3/envs/elephant/bin/python`
- 프로젝트 루트: `/Users/jangjaewon/Desktop/Elephant_Lab`

### 필수 확인 사항
- `.env` 파일에 API 키 설정 확인 (DART, Naver, ECOS, KRX, Kanana-o, OpenAI)
- `artifacts/` 디렉토리 존재 확인
- `config/universe_v1.csv` 존재 확인 (26종목)
- `config/risk_policy_v0.yaml` 존재 확인

### 커넥터 smoke test (데모 직전 실행 권장)
```bash
/opt/anaconda3/envs/elephant/bin/python connectors/krx.py
/opt/anaconda3/envs/elephant/bin/python connectors/dart.py
/opt/anaconda3/envs/elephant/bin/python connectors/naver_news.py
/opt/anaconda3/envs/elephant/bin/python connectors/ecos.py
/opt/anaconda3/envs/elephant/bin/python connectors/llm_router.py
```

---

## 데모 시나리오 A: 정상 시장 (Green Regime)

### 목적
VIX proxy < 70, market breadth >= 0.45 상태의 정상 시장에서 파이프라인이 E2E로 정상 동작함을 보여준다.

### 실행 명령어
```bash
# 시나리오 A 전용 데모 CLI (권장)
/opt/anaconda3/envs/elephant/bin/python jobs/run_demo_scenario.py A

# 또는 E2E 파이프라인 직접 실행 (날짜는 발표 전날 거래일로 교체)
/opt/anaconda3/envs/elephant/bin/python jobs/run_e2e_pipeline.py 20260604
```

### 예상 결과
- `artifacts/daily_market_packet/DMP-20260604-180000.json` 생성
- `artifacts/ttp/TTP-20260604-*.json` 26종목 생성
- `artifacts/sc/SC-20260604-*.json` 생성
- `artifacts/rc/RC-20260604-*.json` 생성 → regime: "green"
- `artifacts/cop/COP-20260604-*.json` 생성
- `artifacts/fdc/FDC-20260604-*.json` 생성 → 다수 approve
- `artifacts/pfs/PFS-20260604-*.json` 생성

### 시연 포인트
- DMP → TTP → SC → RC → COP → FDC → PFS 전체 artifact 생성 확인
- FDC의 `execution_summary.explanation` 한국어 서술 (Kanana-o 생성)
- RC의 `llm_risk_analysis.risk_narrative` 한국어 리스크 내러티브
- regime = "green", tier1_pass = true 확인

---

## 데모 시나리오 B: 스트레스 시장 (Yellow/Red Regime)

### 목적
시장 스트레스 상황에서 Risk Engine의 Regime Gate가 작동하고, FDA가 거부(veto)를 적절히 수행함을 보여준다.

### 실행 명령어
```bash
# 시나리오 B (Yellow) 전용 데모 CLI
/opt/anaconda3/envs/elephant/bin/python jobs/run_demo_scenario.py B

# 시나리오 B-red (Red) 전용 데모 CLI
/opt/anaconda3/envs/elephant/bin/python jobs/run_demo_scenario.py B-red

# A/B 연속 실행 (비교 시연용)
/opt/anaconda3/envs/elephant/bin/python jobs/run_demo_scenario.py --both
```

### 예상 결과 (Yellow)
- RC regime: "yellow"
- `position_risks` 일부 종목 risk_flag: "cap" (비중 제한)
- FDC `decisions` 중 일부 veto 발생

### 예상 결과 (Red)
- RC tier1_pass: false → 신규 진입 금지
- COP orders 전체 action: "hold" 또는 "sell" 위주
- FDC `emergency_veto_count` 증가

### 시연 포인트
- VIX proxy >= 70 또는 market breadth < 0.45 시 Yellow 전환 확인
- VIX proxy >= 90 또는 market breadth < 0.30 시 Red 전환 확인
- Kanana-o가 생성한 `llm_risk_analysis.risk_narrative` 내용 확인 (위험 경고 한국어 서술)
- Stop-loss -5% 도달 종목 자동 처리 확인

---

## 데모 시나리오 C: 5거래일 Replay

### 목적
시스템이 연속 5거래일에 걸쳐 일관되게 동작함을 보여준다.

### 실행 명령어
```bash
/opt/anaconda3/envs/elephant/bin/python jobs/run_replay.py --days 5
```

### 예상 결과
- 5거래일치 DMP/TTP/SC/RC/COP/FDC/PFS artifact 일괄 생성
- `artifacts/pfs/` 에서 포트폴리오 상태 변화 추이 확인
- 각 거래일 FDC의 approve/veto 내역 비교

---

## 대체 시나리오 (네트워크 장애 시)

### 오프라인 데모 방법 — 기존 아티팩트 사용

네트워크 장애 또는 API 장애 시, 사전에 생성해둔 아티팩트를 직접 열어 시연한다.

1. 사전 준비: 데모 전날 E2E 파이프라인을 실행하여 결과 아티팩트를 저장
2. 오프라인 시연 경로:
   - `artifacts/daily_market_packet/` — DailyMarketPacket 시연
   - `artifacts/fdc/` — FinalDecisionCard 시연 (최종 판단 결과)
   - `artifacts/pfs/` — PortfolioState 시연 (포트폴리오 상태)
3. 아티팩트 내용을 직접 JSON으로 열어 필드별 설명:
   ```bash
   cat artifacts/fdc/FDC-YYYYMMDD-HHMMSS.json
   ```

### 부분 오프라인 실행
```bash
# API 없이 Risk Engine만 mock 모드로 실행
/opt/anaconda3/envs/elephant/bin/python jobs/run_risk_engine.py YYYYMMDD --mock
```

---

## Kanana-o LLM 데모

### 시연 항목

| # | 적용 위치 | 시연 내용 | artifact 필드 |
|---|----------|---------|--------------|
| 1 | DailyMarketPacket | 일일 시장 코멘터리 (시황 분석) | `llm_market_analysis.market_commentary` |
| 2 | TickerTextPack | 뉴스 요약 + 감성 점수 | `llm_news_analysis.news_summary`, `news_sentiment` |
| 3 | TickerTextPack | 공시 해석 + 리스크 플래그 | `llm_disclosure_analysis.disclosure_summary` |
| 4 | RiskCard | 리스크 내러티브 (Regime 근거 한국어) | `llm_risk_analysis.risk_narrative` |
| 5 | FinalDecisionCard | 갈등 해소 분석 | `conflicts[].llm_conflict_analysis.conflict_analysis` |

### 확인 방법
```bash
# FDC의 LLM 생성 explanation 확인
cat artifacts/fdc/FDC-YYYYMMDD-HHMMSS.json | python -c "import json,sys; d=json.load(sys.stdin); print(d['execution_summary']['explanation'])"
```

### fallback 동작 확인
- FDC의 `fallback_used: true` 이면 Kanana-o 실패 → GPT-4o 자동 전환
- `llm_market_analysis.model_used` 필드에서 실제 사용 모델 확인

---

## 트러블슈팅

### API 오류

| 오류 유형 | 대처법 |
|---------|-------|
| Kanana-o 429 (rate limit) | GPT-4o fallback 자동 전환. `fallback_used: true` 로 기록 |
| Kanana-o 연속 3회 실패 | Circuit breaker 5분 cooldown 후 자동 복구 |
| DART API 오류 | `disclosure_index` 빈 배열로 처리. 파이프라인 계속 진행 |
| Naver News API 오류 | `news_index` 빈 배열로 처리. 파이프라인 계속 진행 |
| KRX/pykrx 오류 | `mktcap: null` 허용 (Phase 1 정상 동작) |
| ECOS API 오류 | macro_snapshot 일부 필드 null 허용 |

### 타임아웃
- LLM 응답 지연: 각 LLM 호출은 독립적. 실패해도 해당 필드 null로 처리, 파이프라인 정상 진행
- 전체 E2E 소요시간 p95 기준 확인: `artifacts/ops_metrics/OPM-YYYYMMDD.json`의 `p95_latency_sec`

### 아티팩트 미생성 시
```bash
# 개별 단계 수동 실행
/opt/anaconda3/envs/elephant/bin/python jobs/build_daily_market_packet.py YYYYMMDD
/opt/anaconda3/envs/elephant/bin/python jobs/build_ticker_text_pack.py YYYYMMDD
/opt/anaconda3/envs/elephant/bin/python jobs/run_risk_engine.py YYYYMMDD
```

### 환경 이슈
- Python 경로 오류: `/opt/anaconda3/envs/elephant/bin/python` 직접 지정
- 모듈 import 오류: `sys.path.insert(0, ...)` 설정 확인 (jobs/ 스크립트 내 첫 줄)
- `.env` 파일 없음: 프로젝트 루트에 `.env` 파일 존재 여부 확인 (`.env` 직접 수정 금지)

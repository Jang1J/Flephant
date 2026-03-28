# Operations Runbook — Elephant Lab

> 일일 운영 가이드
> 작성일: 2026-03-25

---

## 환경

- Python 환경: `/opt/anaconda3/envs/elephant/bin/python`
- 프로젝트 루트: `/Users/jangjaewon/Desktop/Elephant_Lab`
- 리스크 정책: `config/risk_policy_v0.yaml`
- 유니버스: `config/universe_v1.csv` (26종목)

---

## 일일 운영 사이클

### 1. 장마감 후 자동 트레이딩 (18:00 KST 이후)

PIT-Safety 기준: 18:00 KST 스냅샷. `available_at >= 18:00 KST` 확인 후 실행.

```bash
/opt/anaconda3/envs/elephant/bin/python jobs/daily_auto_trading.py YYYYMMDD
```

내부 순서:
1. `build_daily_market_packet.py` → DMP-{date}.json 생성
2. `build_ticker_text_pack.py` → TTP-{date}-{ticker}.json 26종목 생성
3. Strategy Agent (AI #2) → SC-{date}-{ticker}.json 생성
4. `run_risk_engine.py` → RC-{date}.json + COP-{date}.json 생성
5. FinalDecisionAgent → FDC-{date}.json 생성
6. PortfolioManager → PFS-{date}.json 생성

### 2. 장중 모니터링 (09:30 ~ 15:30)

```bash
# 장중 특정 시점 사이클 실행
/opt/anaconda3/envs/elephant/bin/python jobs/run_intraday_cycle.py YYYYMMDD HHMM

# 전체 유니버스 장중 업데이트
/opt/anaconda3/envs/elephant/bin/python jobs/run_intraday_cycle.py YYYYMMDD HHMM --all
```

HourlyMarketPatch(HMP) 생성: `artifacts/hmp/HMP-{date}-{time}.json`
- `base_dmp_id`: 당일 DMP ID 참조
- `changed_tickers`: 가격/뉴스 변동 종목
- `market_stress_update`: 장중 VIX proxy, market breadth 업데이트

### 3. 데이터 품질 확인

```bash
# DMP + TTP 데이터 품질 리포트 생성
/opt/anaconda3/envs/elephant/bin/python jobs/build_daily_market_packet.py YYYYMMDD
```

DQR 확인: `artifacts/dqr/DQR-{date}.json`
- `overall_pass: false` 이면 `dmp.issues` / `ttp.issues` 항목 검토
- `missing_tickers`, `stale_tickers` 확인 → 재수집 또는 허용 여부 판단

---

## 주간 운영

### 5거래일 Replay

```bash
/opt/anaconda3/envs/elephant/bin/python jobs/run_replay.py --days 5
```

- 연속 5거래일 E2E 파이프라인 일괄 실행
- 각 거래일 artifact 생성 및 PFS 상태 누적

### 주간 운영 리포트

```bash
/opt/anaconda3/envs/elephant/bin/python jobs/ops_metrics_collector.py --weekly-days 5
```

운영 메트릭 7종 수집 (`artifacts/ops_metrics/OPM-{date}.json`):

| 메트릭 | 필드 | WARN 기준 |
|--------|------|---------|
| 주문 성공률 | `order_success_rate` | < 0.9 |
| 주문 거부율 | `order_reject_rate` | > 0.1 |
| LLM fallback 비율 | `llm_fallback_rate` | > 0.3 (30% 이상 GPT-4o 전환) |
| 일일 회전율 | `daily_turnover` | > 30% (risk_policy turnover cap) |
| 현금 비율 변화 | `cash_ratio_drift` | 절대값 > 10%p |
| PFS↔PTL 불일치율 | `reconciliation_mismatch_rate` | > 0 (불일치 발생 시 즉시 조사) |
| 긴급 거부 건수 | `emergency_veto_count` | > 3 (Red regime 시 정상) |

Latency 참고:
- `p50_latency_sec`: E2E 소요 시간 중앙값
- `p95_latency_sec`: E2E 소요 시간 95th percentile

---

## 과거 데이터 Backfill

```bash
# 최근 20거래일 DMP/TTP 소급 생성
/opt/anaconda3/envs/elephant/bin/python jobs/backfill_packets.py --days 20
```

---

## Ablation 실험

### UQ on/off

```bash
/opt/anaconda3/envs/elephant/bin/python jobs/run_ablation.py YYYYMMDD --experiment uq
```

- baseline: UQ tail cap 활성화 (`uq_enabled: true`)
- variant: UQ tail cap 비활성화 (`uq_enabled: false`)
- 결과: `artifacts/ablation/ABL-uq-{date}.json`
- 비교 필드: `comparison.exposure_delta`, `comparison.approval_diff`, `comparison.weight_changes`

### PFS stateful/stateless

```bash
/opt/anaconda3/envs/elephant/bin/python jobs/run_ablation.py YYYYMMDD --experiment pfs
```

- baseline: PFS stateful (진입가 기반 stop-loss, 보유일수 추적)
- variant: stateless (PFS 없이 독립 판단)
- 결과: `artifacts/ablation/ABL-pfs-{date}.json`

---

## UQ 모델 운영

### 학습 (offline, AI #2 담당)

```bash
/opt/anaconda3/envs/elephant/bin/python jobs/uq_calibration.py --train
```

### 추론 (online, AI #1 담당)

```bash
/opt/anaconda3/envs/elephant/bin/python jobs/uq_calibration.py --predict YYYYMMDD
```

UQ 모델 I/O:
- 입력 feature: `strategy_confidence`, `signal_agreement`, `volume_ratio`, `volatility_20d`, `regime_label`, `sector_momentum`, `mktcap_rank`
- 출력: `uncertainty_score` (0~1), `p85_value` → Risk Agent Tier 2 적용

P85 threshold 기준: `config/risk_policy_v0.yaml` 참조 (하드코딩 금지)

---

## Paper Trading

### 모의투자 실행 (dry_run 모드)

```bash
/opt/anaconda3/envs/elephant/bin/python jobs/paper_trading_executor.py YYYYMMDD
```

- `dry_run: true` 플래그로 실제 주문 없이 시뮬레이션
- 결과: `artifacts/ptl/PTL-{date}.json`
- 주문 상태: `executed` | `dry_run` | `rejected` | `cancelled` | `cancel_failed`

### 주문 취소

```bash
/opt/anaconda3/envs/elephant/bin/python jobs/paper_trading_executor.py YYYYMMDD --cancel 005930
```

### Reconciliation (PFS ↔ PTL 대조)

- 결과: `artifacts/brr/BRR-{date}.json`
- `status: "pass"` 확인
- `mismatches` 배열이 비어있으면 정상
- `reconciliation_mismatch_rate > 0` 시 `mismatches[].description` 조사

---

## BE Handoff

### AI → BE 대시보드 payload 생성

```bash
/opt/anaconda3/envs/elephant/bin/python jobs/build_be_handoff_payload.py --days 5
```

생성 payload (`schemas/dashboard_payload_contract.json`):
- `recommendation_history`: FDC 기반 종목별 추천 이력 (ID 패턴: `RH-{시작날짜}-{종료날짜}`)
- `portfolio_nav`: PFS 기반 포트폴리오 순자산 추이 (ID 패턴: `NAV-{시작날짜}-{종료날짜}`)
- `paper_trading_log_summary`: PTL 기반 거래 요약 (ID 패턴: `PTS-{시작날짜}-{종료날짜}`)
- `risk_trace`: RC 기반 리스크 추적 (ID 패턴: `RT-{시작날짜}-{종료날짜}`)

---

## Rebound Profile 운영 명령

### Rebound Profile 운영 명령

# SC 생성
```bash
/opt/anaconda3/envs/elephant/bin/python jobs/build_strategy_card_rebound.py YYYYMMDD
```

# Publish
```bash
/opt/anaconda3/envs/elephant/bin/python jobs/publish_strategy_variant.py --profile rebound --date YYYYMMDD
```

# Backtest
```bash
/opt/anaconda3/envs/elephant/bin/python jobs/run_backtest_replay.py --days 100
```

SC 생성 후 반드시 26종목 전체 카드가 생성되었는지 확인한다.

---

## 장애 대응

### DMP 생성 실패
```bash
# 개별 재실행
/opt/anaconda3/envs/elephant/bin/python jobs/build_daily_market_packet.py YYYYMMDD
```
- KRX API 실패: `mktcap: null` 허용 (Phase 1 정상)
- ECOS API 실패: `macro_snapshot` 일부 필드 null 허용
- DQR `overall_pass: false` 이어도 파이프라인 진행 가능 (판단 후 결정)
- DMP macro_snapshot의 필드가 None인 경우 SC 생성 시 float(None) 에러가 발생할 수 있다. backfill 후 DMP 재생성을 권장한다.

### TTP 생성 실패
```bash
# 특정 종목만 재실행
/opt/anaconda3/envs/elephant/bin/python jobs/build_ticker_text_pack.py YYYYMMDD 005930
```
- Naver News API 실패: `target_company_docs` 빈 배열 허용
- DART API 실패: `target_company_docs` 빈 배열 허용

### Risk Engine 오류
```bash
# mock 모드로 실행 (실제 API 없이 테스트)
/opt/anaconda3/envs/elephant/bin/python jobs/run_risk_engine.py YYYYMMDD --mock
```
- risk_policy_v0.yaml 로드 실패: 파일 존재 및 YAML 문법 확인

### LLM 장애
- Kanana-o 연속 3회 실패 → 자동 5분 cooldown 후 재시도
- GPT-4o fallback: `FDC.fallback_used: true`로 기록됨
- 두 모델 모두 실패: LLM 관련 필드 null 처리, 파이프라인 정상 진행

### PFS ↔ PTL 불일치
1. `BRR-{date}.json` 의 `mismatches` 항목 확인
2. `pfs_value` vs `ptl_value` 비교
3. `ticker`, `field` 기준으로 원인 조사
4. 수동 보정 후 재실행

---

## 모니터링 지표 요약

| 지표 | 정상 범위 | WARN 기준 | 대응 |
|------|---------|---------|------|
| order_success_rate | >= 0.9 | < 0.9 | PTL `rejected` 주문 사유 확인 |
| order_reject_rate | <= 0.1 | > 0.1 | COP → FDC → PTL 연결 확인 |
| llm_fallback_rate | <= 0.3 | > 0.3 | Kanana-o API 상태 확인 |
| daily_turnover | <= 30% | > 30% | risk_policy_v0.yaml turnover cap 확인 |
| cash_ratio_drift | ±10%p | > ±10%p | PFS cash_ratio 이상 확인 |
| reconciliation_mismatch_rate | 0 | > 0 | BRR mismatches 즉시 조사 |
| emergency_veto_count | 0 (정상 시장) | > 3 (비 Red) | FDC veto 사유 + regime 상태 확인 |

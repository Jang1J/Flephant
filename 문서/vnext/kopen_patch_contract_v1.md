# K-OPEN Pulse / K-SHIFT 데이터 계층 정의서 v1

> 기준일: 2026-03-29
> 작성: AI #1 (장재원)
> 상태: DRAFT — vnext 설계 문서

---

## 1. 명칭 정의

### K-OPEN Pulse (서비스 명칭)

사용자에게 노출되는 **한국장 개장 해석 서비스**의 공식 명칭이다.
"K-OPEN"은 Korean market OPENing을 의미하며, "Pulse"는 시장의 실시간 맥박 정보를 함축한다.
사용자는 전날 밤/새벽에 발생한 해외 충격이 오늘 한국 장 개장에 어떻게 번역되는지를 이 서비스를 통해 확인한다.

### K-SHIFT (내부 엔진 명칭)

**Korean Shock-Hype-Investor-Flow Translator**의 약자로, K-OPEN Pulse를 생성하는 내부 데이터 엔진의 공식 코드명이다.
K-SHIFT는 4개의 patch artifact를 생산하며, 각 patch는 DailyMarketPacket(DMP) 또는 HourlyMarketPatch(HMP)에 summary 형태로 포함되거나 standalone artifact로 독립 저장된다.

---

## 2. 아키텍처 개요

```
외부 데이터 소스
  ├── 미국장 OHLCV (장마감 후) ──────────────────────┐
  ├── USDKRW 환율, KOSPI200 선물                       │
  ├── 외국인/기관/개인 순매수 (KRX Daily)              │  K-SHIFT Engine
  ├── 종토방 게시글 (실시간)                            │  (4개 Patch Producer)
  └── 뉴스 헤드라인 (Naver)                            │
                                                        ▼
                             InvestorFlowPatch       ─── P0 daily
                             OvernightSpilloverPatch ─── P0 daily
                             RetailThemeGraph        ─── P1 hourly
                             OpenTrapRiskPatch       ─── P1 hourly
                                        │
                    ┌───────────────────┴────────────────────┐
                    ▼                                        ▼
             DMP summary 필드                     standalone artifact
           (dmp.kopen_summary)              (artifacts/kopen/{date}/*.json)
                    │
                    ▼
             K-OPEN Pulse (서비스)
```

---

## 3. Patch 정의

### 3.1 InvestorFlowPatch

| 항목 | 값 |
|------|-----|
| **Artifact ID** | `IFP-{YYYYMMDD}` |
| **Cadence** | daily (D+0, 18:00 KST 이후 확정) |
| **우선순위** | P0 |
| **Producer** | `jobs/build_investor_flow_patch.py` (신규) |
| **Consumer** | RiskEngine (regime gate 보조), FDA (설명 생성) |

**내용 정의:**
- 외국인 순매수 금액 / 순매수 상위 5종목
- 기관 순매수 금액 / 순매수 상위 5종목
- 개인 순매수 금액 / 순매수 상위 5종목
- **동조 이탈 지수 (Divergence Score)**: 외국인↑ + 기관↑ + 개인↓ 같은 투자자 간 방향 불일치를 수치화한 값 (0~1, 높을수록 이탈 심화)
- 전일 대비 플로우 모멘텀 변화

**DMP 연동 방식:**
```json
"kopen_summary": {
  "investor_flow": {
    "patch_id": "IFP-20260329",
    "foreign_net_buy_bn": 3.2,
    "institutional_net_buy_bn": -1.1,
    "retail_net_buy_bn": -2.1,
    "divergence_score": 0.72,
    "top_buy_tickers": ["005930", "000660", "035420"],
    "flow_regime": "foreign_led"
  }
}
```

**HMP 연동 방식:**
장중 누적 순매수 데이터가 업데이트되면 HourlyMarketPatch의 `investor_flow_update` 필드를 통해 delta 값만 반영한다.

---

### 3.2 OvernightSpilloverPatch

| 항목 | 값 |
|------|-----|
| **Artifact ID** | `OSP-{YYYYMMDD}` |
| **Cadence** | daily (D+0, 07:00 KST 이후 — 미국장 마감 후 한국 조회 가능 시점) |
| **우선순위** | P0 |
| **Producer** | `jobs/build_overnight_spillover_patch.py` (신규) |
| **Consumer** | RiskEngine (Tier 1 regime gate), FDA (개장 리스크 내러티브) |

**내용 정의:**
- 나스닥 / S&P500 / 다우지수 전일 등락률
- USDKRW 환율 변화 (D-1 대비 %)
- KOSPI200 야간선물 등락률
- **충격 번역 등급 (Spillover Grade)**: 위 3개 신호를 종합한 개장 예상 충격 수준 (LOW / MODERATE / HIGH / SEVERE)
- Spillover 근거 요약 텍스트 (Kanana-o 생성, optional)

**DMP 연동 방식:**
```json
"kopen_summary": {
  "overnight_spillover": {
    "patch_id": "OSP-20260329",
    "nasdaq_change_pct": -1.8,
    "usdkrw_change_pct": 0.4,
    "kospi200_futures_change_pct": -1.2,
    "spillover_grade": "HIGH",
    "llm_summary": "미국 기술주 급락 + 달러 강세로 개장 하방 압력 예상"
  }
}
```

**HMP 연동 방식:**
본 patch는 일별 snapshot으로 생성되므로 장중 HMP delta에 반영되지 않는다.
단, 장 시작 이후 환율/선물 급변 시 HMP의 `market_stress_update` 필드를 통해 별도 반영한다.

---

### 3.3 RetailThemeGraph

| 항목 | 값 |
|------|-----|
| **Artifact ID** | `RTG-{YYYYMMDD}-{HH}` |
| **Cadence** | hourly (장중 09:00~15:30 KST, 1시간 단위) |
| **우선순위** | P1 |
| **Producer** | `jobs/build_retail_theme_graph.py` (신규) |
| **Consumer** | OpenTrapRiskPatch (테마 추격 판단), FDA (개인 수급 테마 해석) |

**내용 정의:**
- 종토방 (종목토론방) 게시글 수 급증 종목 Top 10
- 뉴스 헤드라인에서 추출된 테마 키워드 클러스터 (예: "AI", "2차전지", "방산")
- 테마 간 전염 관계: 테마 A에서 테마 B로 검색/언급이 전파되는 구조 (graph 형태로 저장)
- 각 테마의 시간대별 언급 급증 여부 (spike flag)

**DMP 연동 방식:**
RetailThemeGraph는 장중 hourly artifact이므로 DMP(daily)에는 **전일 마감 기준 일별 요약**만 포함된다.
```json
"kopen_summary": {
  "retail_theme_yesterday": {
    "top_themes": ["AI반도체", "방산", "바이오"],
    "spike_tickers": ["000660", "012450"],
    "theme_fever_score": 0.61
  }
}
```

**HMP 연동 방식:**
장중 매시간 HourlyMarketPatch에 `retail_theme_patch` 필드로 신규 스파이크 테마와 언급 증가 종목을 추가 반영한다.

---

### 3.4 OpenTrapRiskPatch

| 항목 | 값 |
|------|-----|
| **Artifact ID** | `OTP-{YYYYMMDD}-{HH}` |
| **Cadence** | hourly (장중 09:00~10:30 KST 집중, 이후 필요 시) |
| **우선순위** | P1 |
| **Producer** | `jobs/build_open_trap_risk_patch.py` (신규) |
| **Consumer** | RiskEngine (veto 판단), FinalDecisionAgent (추격 경보) |

**내용 정의:**
- **갭상승 위험 종목**: 전일 종가 대비 당일 시가 +3% 이상 종목 목록
- **VI (변동성 완화장치) 발동 종목**: 당일 VI 발동 이력
- **가짜 테마 추격 경보 (Hype Trap Score)**: RetailThemeGraph의 스파이크 테마와 해당 종목 펀더멘털 간 乖離度를 0~1로 수치화. 높을수록 테마 과열 추격 위험
- 개장 30분 내 급등 종목 중 거래량 이상 징후 (평균 거래량 대비 5배 이상)

**DMP 연동 방식:**
OpenTrapRiskPatch는 장중 hourly이므로 DMP에는 전일 오픈 트랩 이력 요약만 포함된다.
```json
"kopen_summary": {
  "open_trap_yesterday": {
    "gap_up_tickers": ["005380", "035720"],
    "vi_triggered_tickers": ["035420"],
    "hype_trap_max_score": 0.83
  }
}
```

**HMP 연동 방식:**
장 개시 후 매시간 HourlyMarketPatch에 `open_trap_update` 필드로 신규 갭상승/VI 발동 정보를 추가한다.
RiskEngine은 HMP의 OpenTrapRiskPatch 데이터를 수신하면 해당 종목에 대해 `추격_경보` 태그를 붙이고 진입 차단 여부를 재판단한다.

---

## 4. Standalone Artifact + DMP/HMP Summary 이중 구조

각 patch는 두 가지 경로로 저장된다.

### 경로 1: Standalone Artifact

```
artifacts/kopen/{YYYYMMDD}/
  ├── IFP-20260329.json
  ├── OSP-20260329.json
  ├── RTG-20260329-09.json
  ├── RTG-20260329-10.json
  ├── OTP-20260329-09.json
  └── OTP-20260329-10.json
```

standalone artifact는 전체 데이터를 보존하며, AI #2 전략 모델 학습에서 KOPEN feature로 소비된다.
스키마 파일: `schemas/kopen_patch_v1.json` (P0 patch 2종) 및 `schemas/kopen_patch_hourly_v1.json` (P1 patch 2종)

### 경로 2: DMP / HMP Summary 필드

DMP의 `kopen_summary` 필드와 HMP의 `kopen_delta` 필드에는 **요약 값만** 포함된다.
이 요약은 RiskEngine, FinalDecisionAgent, 서비스 레이어가 직접 소비한다.
full standalone을 읽지 않아도 파이프라인이 정상 동작하도록 설계한다 (graceful degradation).

### PIT-Safety 원칙

- IFP, OSP: 18:00 KST snapshot 이후 생성 (D+0 장마감 확정 데이터 사용)
- RTG, OTP: 장중 hourly이므로 생성 시각 기준 `available_at` 필드 강제 기록
- 미래 데이터 유입 차단: `is_within_snapshot()` 필터 적용 동일

---

## 5. 변경 이력

| 버전 | 날짜 | 내용 |
|------|------|------|
| v1.0 | 2026-03-29 | 최초 작성 (vnext 설계 초안) |
| v1.1 | 2026-03-29 | GPT Pro #7 반영: 필드명/API 소스 확정 |

---

## 6. 필드명 상세 (GPT Pro #7 확정)

### 6-1. InvestorFlowPatch 필드

| 필드명 | 타입 | 설명 | 소스 API |
|--------|------|------|---------|
| foreign_net_buy_1d | number | 외국인 당일 순매수 (억원) | KRX 투자자별 매매동향 |
| foreign_net_buy_3d | number | 외국인 3일 누적 순매수 | KRX |
| foreign_net_buy_5d | number | 외국인 5일 누적 순매수 | KRX |
| inst_net_buy_1d | number | 기관 당일 순매수 | KRX |
| inst_net_buy_3d | number | 기관 3일 누적 순매수 | KRX |
| inst_net_buy_5d | number | 기관 5일 누적 순매수 | KRX |
| foreign_inst_sync_sell_flag | boolean | 외국인+기관 동시 순매도 여부 | 계산 |
| retail_absorption_ratio | number | 개인 흡수율 (개인 순매수 / 전체 거래량) | 계산 |
| foreign_ownership_change | number | 외국인 보유비율 변동 (%p) | KRX 외국인 보유 |

### 6-2. OvernightSpilloverPatch 필드

| 필드명 | 타입 | 설명 | 소스 API |
|--------|------|------|---------|
| spy_ret_prev | number | 전일 S&P500 수익률 (%) | Alpha Vantage |
| qqq_ret_prev | number | 전일 NASDAQ 수익률 (%) | Alpha Vantage |
| soxx_ret_prev | number | 전일 반도체 ETF 수익률 (%) | Alpha Vantage |
| usdkrw_change_prev | number | 전일 원달러 변동 (%) | ECOS or Alpha Vantage |
| us_sector_shock_score | number | 미국 섹터 충격 점수 (-1~1) | 계산 |
| kr_sector_translation_score | number | 한국 섹터 번역 점수 | 계산 |
| overnight_reversal_risk | number | 야간 충격 반전 위험 (0~1) | 계산 |

### 6-3. RetailThemeGraph 필드

| 필드명 | 타입 | 설명 | 소스 API |
|--------|------|------|---------|
| attention_spike | boolean | 관심 급증 여부 | Naver DataLab + News API |
| attention_rank | number | 유니버스 내 관심 순위 (1~26) | Naver DataLab |
| co_mention_strength | number | 동시 언급 강도 (0~1) | Naver News/Blog/Cafe |
| theme_heat | number | 테마 과열도 (0~1) | 계산 |
| theme_core_stock | boolean | 테마 중심주 여부 | 계산 |
| theme_satellite_stock | boolean | 테마 위성주 여부 | 계산 |
| crowd_news_divergence | number | 군중-뉴스 괴리도 | 계산 |
| retail_hype_score | number | 리테일 과열 점수 (0~1) | 계산 |

### 6-4. OpenTrapRiskPatch 필드

| 필드명 | 타입 | 설명 | 소스 |
|--------|------|------|------|
| gap_chase_risk | number | 갭상승 추격 위험 (0~1) | 계산 (개장갭 + 거래량) |
| vi_proximity_score | number | VI 근접도 (0~1) | 계산 (가격제한폭 ±30% 기준) |
| limit_move_risk | number | 상하한가 근접 위험 | 계산 |
| theme_fakeout_risk | number | 가짜 테마 위험 (0~1) | 계산 (RTG + 뉴스 확인) |
| retail_trap_risk | number | 개미 물림 종합 위험 (0~1) | 계산 (종합) |

### 6-5. API 소스 요약

| API | 용도 | 현재 사용 | 신규 추가 |
|-----|------|----------|----------|
| KRX (pykrx + Open API) | OHLCV, 시총 | 사용 중 | + 투자자별 매매, 외국인 보유 |
| OpenDART | 공시 | 사용 중 | 변경 없음 |
| Naver Search | 뉴스 | 사용 중 | + Blog/Cafe 검색 |
| Naver DataLab | 검색 트렌드 | 미사용 | **신규** (attention proxy) |
| ECOS | 금리/환율 | 사용 중 | 변경 없음 |
| Alpha Vantage | 미국장 | 미사용 | **신규** (SPY/QQQ/SOXX) |

### 6-6. 구현 방식 분류

| Patch | 신규 API 필요 | 계산형 | cadence |
|-------|-------------|--------|---------|
| InvestorFlowPatch | KRX 확장 | 일부 계산 | daily |
| OvernightSpilloverPatch | Alpha Vantage **신규** | 일부 계산 | daily + hourly |
| RetailThemeGraph | Naver DataLab **신규** | 대부분 계산 | hourly |
| OpenTrapRiskPatch | 없음 | **전부 계산** | hourly |

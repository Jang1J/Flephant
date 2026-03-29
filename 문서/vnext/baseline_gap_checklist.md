# Baseline Gap Checklist

생성일: 2026-03-29
검증자: QA Inspector (qa-inspector agent)
대상 브랜치: main (7a26575)

---

## 요약 테이블

| # | 항목 | 판정 | 핵심 불일치 |
|---|------|------|------------|
| 1 | momentum 학습-추론 artifact path mismatch | **GAP** | 저장 경로 `artifacts/strategy_model/lgbm_ranker_{run_id}.pkl` vs 로드 경로 `models/strategy_model/model.pkl` |
| 2 | backtest placeholder 위치 | **GAP** | run_e2e_pipeline.py:91–101, run_intraday_cycle.py:131–142, run_ablation.py:38–48 전부 하드코딩 |
| 3 | hourly patch live 미구현 상태 | **GAP** | `_build_live_patch`가 `NotImplementedError` 발생. live path 동작 불가. |
| 4 | default_profile 의미 불일치 | **GAP** | `strategy_profiles.yaml` default_profile = "rebound" vs 제안서 "AI #2 공식 Strategy Agent = momentum" |
| 5 | full-universe TTP 현황 | **GAP** | run_e2e_pipeline.py가 3종목만 생성. --full-ttp 옵션 없음. |
| 6 | E2E pipeline 완전성 | **CONDITIONAL OK** | has_real_sc 동작은 정상. 그러나 real SC 소비는 --publish 선행 실행 없으면 자동 mock 전환됨. |

---

## 상세 점검

### GAP 1 — momentum 학습-추론 artifact path mismatch

**저장 경로 (학습기)**
- 파일: `models/strategy_model/lgbm_ranker.py`
- 라인 36–37: `OUTPUT_DIR = _BASE_DIR / "artifacts" / "strategy_model"`
- 라인 570: `model_path = OUTPUT_DIR / f"lgbm_ranker_{run_id}.pkl"`
- 실제 저장 위치: `artifacts/strategy_model/lgbm_ranker_{run_id}.pkl`
  (run_id는 학습 시 타임스탬프 기반 문자열)

**로드 경로 (추론기)**
- 파일: `jobs/build_strategy_card_momentum.py`
- 라인 39: `MODEL_PKL_PATH = MODEL_DIR / "model.pkl"`
  (MODEL_DIR = `models/strategy_model/`)
- 실제 탐색 위치: `models/strategy_model/model.pkl`

**불일치 요약**
| 측면 | 경로 |
|------|------|
| 학습기 저장 | `artifacts/strategy_model/lgbm_ranker_{run_id}.pkl` |
| 추론기 기대 | `models/strategy_model/model.pkl` |

두 경로는 디렉토리도, 파일명도 모두 다르다. 학습 직후 `model.pkl` 심볼릭 링크 또는 복사 단계가 없으면 추론기는 항상 fallback(휴리스틱) 경로로 진입한다. `build_strategy_card_momentum.py:221`의 "모델 파일 없음" 경고가 항상 출력되는 원인이 이 불일치다.

---

### GAP 2 — backtest placeholder 위치

세 파일 모두에 `"status": "phase1_placeholder"` dict가 코드에 직접 인라인으로 작성되어 있다.

**run_e2e_pipeline.py**
- 라인 90–101: `backtest_summary = {"status": "phase1_placeholder", ...}` 딕셔너리 하드코딩
- 주석: `# Phase 1 backtest_summary placeholder (W6에서 AI #2 Backtest Agent 연결 예정)`

**run_intraday_cycle.py**
- 라인 131–142: `backtest_summary={"status": "phase1_placeholder", ...}` 인라인 전달
- fda.run() 호출 시 keyword argument로 직접 삽입

**run_ablation.py**
- 라인 38–48: `_BACKTEST_PLACEHOLDER = {"status": "phase1_placeholder", ...}` 모듈 레벨 상수 정의
- 이후 `run_uq_ablation`, `run_pfs_ablation` 등 모든 실험 함수에서 동일 상수 참조

세 위치가 서로 독립적으로 중복 정의되어 있다. run_ablation.py의 `_BACKTEST_PLACEHOLDER`는 상수 추출이 된 것이지만, run_e2e_pipeline.py와 run_intraday_cycle.py는 완전 인라인으로 남아 있어 향후 Backtest Agent 연결 시 세 곳을 각각 수정해야 한다.

---

### GAP 3 — hourly patch live 미구현 상태

- 파일: `jobs/build_hourly_patch.py`
- 라인 116–120:
  ```python
  def _build_live_patch(...) -> dict:
      """Live HourlyMarketPatch 생성 (실시간 API 기반) — Phase 2"""
      # Phase 2: pykrx 실시간 / KRX Open API / Naver 실시간 시세 연동
      raise NotImplementedError("Live HourlyMarketPatch는 Phase 2에서 구현 예정")
  ```
- 라인 26–53: `build_hourly_patch(use_mock=True)`가 기본값이며, `use_mock=False` 호출 시 즉시 `NotImplementedError` 발생
- `run_intraday_cycle.py:59`: 함수 시그니처 `run_intraday_cycle(... use_mock_hmp: bool = True)`로 기본값도 mock

**결론**: live path는 코드 경로만 존재하고 함수 본문이 NotImplementedError이므로 현재 동작 불가능하다. Phase 2 구현 전까지 장중 실거래 연동은 불가.

---

### GAP 4 — default_profile 의미 불일치

**config/strategy_profiles.yaml**
- 라인 17: `default_profile: "rebound"`
- `rebound` 프로파일은 `jobs/build_strategy_card_rebound.py` (KR-Rebound-CNN) 를 가리킨다.

**제안서/문서 서술**
- `문서/3주차/KOSPI_프로젝트_제안서_v11_최종.md:530`:
  "현재 AI #2의 공식 Strategy Agent(LightGBM + News + Synthesizer)는 구현 진행 중이다."
- `문서/3주차/AI_파트_분배_v1.md:99`:
  "AI #2의 공식 momentum/news strategy와 true backtest suite는 구현 진행 중이다."
- `문서/KR_Rebound_CNN_v1_설계서.md:374–376`:
  strategy_profiles.yaml 스니펫에서 `lgbm_momentum`의 default 위치가 예시로 제시됨.

**불일치 요약**
| 측면 | 값 |
|------|---|
| config/strategy_profiles.yaml default_profile | `"rebound"` |
| 제안서의 "공식 Strategy Agent" 서술 | AI #2 LightGBM momentum |

제안서는 LightGBM momentum을 공식 Strategy Agent로 서술하지만, 실제 config의 default_profile은 rebound로 설정되어 있다. 실험/발표 시 "공식 전략"이 무엇인지 혼선을 유발할 수 있다.

---

### GAP 5 — full-universe TTP 현황

**run_e2e_pipeline.py**
- 라인 51–53:
  ```python
  # 전체 유니버스 TTP는 build_ticker_text_pack.py로 별도 실행:
  #   python jobs/build_ticker_text_pack.py YYYYMMDD
  print(f"\n[Step 2/7] TickerTextPack 생성 (샘플 3종목)...")
  ```
- 라인 54: `sample_tickers = ["005930", "000660", "005380"]` — 고정 3종목만 처리
- `--full-ttp` 또는 유사 옵션 없음. argparse에 해당 플래그가 정의되어 있지 않다.

**결론**: E2E 파이프라인이 단일 커맨드로 26종목 전체 TTP를 생성하는 경로가 없다. 전체 유니버스 TTP가 필요하면 `build_ticker_text_pack.py`를 종목별로 별도 실행해야 한다. 발표/데모 시 TTP 누락으로 SC의 news_signal이 0.0으로 고정되는 시나리오가 발생할 수 있다.

---

### CONDITIONAL OK 6 — E2E pipeline 완전성 (real SC 소비 경로)

**has_real_sc 동작**
- 파일: `jobs/strategy_loader.py:23–29`
- `artifacts/strategy_card/SC-{date}.json` 또는 `SC-{date}-*.json` 존재 여부로 판정
- 로직 자체는 정상 동작

**real SC 소비 경로**
- `run_e2e_pipeline.py:87–88`:
  ```python
  if not use_mock:
      use_mock = not has_real_sc(target_date)
  ```
- `has_real_sc`가 True이면 `load_strategy_cards`로 real SC를 소비한다.

**조건부 제약**
- real SC가 `artifacts/strategy_card/`에 존재하려면 `build_strategy_card_momentum.py --publish` 또는 `build_strategy_card_rebound.py --publish`가 사전에 실행되어야 한다.
- publish 없이 `run_e2e_pipeline.py`만 실행하면 SC 파일이 없으므로 항상 mock 전환된다.
- 이 의존 관계가 README/ops_runbook에 명시적으로 기술되어 있는지 여부와 무관하게, E2E 단일 실행으로는 real SC가 자동 생성·소비되지 않는다.

**판정**: has_real_sc 로직 자체는 OK이나, real SC를 실제로 소비하는 전제 조건(--publish 사전 실행)이 E2E 파이프라인 외부에 있다는 점에서 완전한 PASS는 아니다.

---

## 수정 우선순위 제안

| 우선순위 | 항목 | 제안 조치 |
|---------|------|----------|
| P0 | GAP 1 (path mismatch) | `lgbm_ranker.py` 학습 완료 후 `models/strategy_model/model.pkl`로 복사/symlink하는 단계 추가, 또는 `build_strategy_card_momentum.py`의 `MODEL_PKL_PATH`를 `artifacts/strategy_model/`을 탐색하도록 변경 |
| P1 | GAP 2 (backtest placeholder) | 공통 상수 `BACKTEST_PLACEHOLDER_V1`을 `jobs/constants.py` 또는 `config/` 에 단일 정의 후 세 파일에서 import |
| P1 | GAP 4 (default_profile) | 제안서와 config 중 하나를 통일. 발표 전 합의 필요. |
| P2 | GAP 5 (full-universe TTP) | `run_e2e_pipeline.py`에 `--full-ttp` 플래그 추가하여 유니버스 전체 TTP 생성 경로 제공 |
| P3 | GAP 3 (live patch) | Phase 2 일정 명확화. 현재 상태는 "Phase 2 미구현"으로 문서에만 명시하면 충분. |
| P3 | CONDITIONAL OK 6 | ops_runbook에 "real SC 소비 전 --publish 선행 실행" 단계 명시 |


# Elephant Lab — KOSPI Multi-Agent Trading Algorithm

KOSPI 대형주 26종목 대상 멀티에이전트 트레이딩 알고리즘 (종합설계 프로젝트).

## 시스템 개요

```
DMP → TTP → StrategyCard → RiskEngine → FDA → PortfolioState
```

- **AI #2 Branch 1**: LightGBM Momentum Ranker (AUC 0.750, P@5 0.54, 28 features)
- **AI #2 Branch 2**: KR-Rebound-CNN + Committee (AUC 0.831, 39-dim context, tree 0.7 / CNN 0.3)
- **Risk Engine**: Regime Gate + Position Sizing + Stop-loss
- **FDA**: LLM 기반 approve/veto (can_change_weight=false)

## 빠른 시작

```bash
# 환경 설정
conda create -n elephant python=3.11
conda activate elephant
pip install -r requirements.txt

# .env 파일 설정 (API 키)
cp .env.example .env  # DART_API_KEY, NAVER_CLIENT_ID 등

# E2E 파이프라인 1회 실행
python jobs/run_e2e_pipeline.py YYYYMMDD

# 백테스트
python jobs/run_backtest.py
```

## 주요 명령어

| 명령어 | 설명 |
|--------|------|
| `python jobs/run_e2e_pipeline.py YYYYMMDD` | E2E 1회 실행 |
| `python jobs/run_replay.py --days 5` | 연속 N거래일 replay |
| `python jobs/backfill_packets.py --days 20` | 과거 데이터 backfill |
| `python jobs/run_backtest.py` | 백테스트 (거래세+슬리피지 포함) |
| `python models/strategy_model/lgbm_ranker.py` | Momentum 모델 학습 |
| `python models/rebound_cnn/train.py` | CNN 모델 학습 |

## 프로젝트 구조

```
Elephant_Lab/
├── agents/          # FDA (Final Decision Agent)
├── connectors/      # KRX, DART, Naver News, ECOS, LLM Router
├── config/          # risk_policy, universe, strategy_profiles
├── jobs/            # 파이프라인 작업 스크립트
├── models/          # AI #2 모델 (LightGBM, CNN, Committee)
├── schemas/         # 17개 JSON 스키마
├── artifacts/       # 실행 결과 아티팩트 (DMP, TTP, SC, RC, FDC, PFS)
└── prompts/         # FDA 프롬프트 계약서
```

## 핵심 제약

- **PIT-Safety**: 미래 데이터 사용 금지 (snapshot 18:00 KST)
- **can_change_weight=false**: FDA는 비중 수정 불가 (approve/veto만)
- **스키마 준수**: 17개 JSON 스키마 strict validation
- **정책 동기화**: risk_policy_v0.yaml에서 값 로드 (하드코딩 금지)

## 라이선스

종합설계 프로젝트 — 비공개

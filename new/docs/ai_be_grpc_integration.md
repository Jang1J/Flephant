# AI-BE gRPC Integration

이 문서는 백엔드-AI 연결을 HTTP/OpenAPI 대신 gRPC로 붙이기 위한 AI 쪽 계약과 실행 방법을 정리한다. BE 코드는 이 문서에서 수정하지 않고, 양쪽이 공유할 proto 파일만 AI repo에 둔다.

## 핵심 파일

| 구분 | 경로 | 설명 |
|---|---|---|
| Proto SSOT | `new/proto/elephant_ai_bridge.proto` | BE/AI가 공유할 gRPC 인터페이스 |
| Python stub 생성 | `new/scripts/generate_ai_grpc_stubs.py` | proto → Python gRPC stub 생성 |
| AI gRPC 서버 | `new/scripts/run_ai_grpc_server.py` | read-only AI bridge 서버 실행 |
| 서버 구현 | `new/src/integration/grpc/server.py` | generated stub에 붙는 AI-side servicer |
| Payload 변환 | `new/src/integration/grpc/payloads.py` | service_readiness 등 로컬 산출물을 gRPC 응답으로 변환 |

## gRPC 서비스

`AiBeBridgeService`는 기존 `new/GPT/ai_be_openapi_reviewed.yaml`의 HTTP bridge 의미를 gRPC 메서드로 옮긴 것이다.

| RPC | HTTP 대응 | 목적 |
|---|---|---|
| `HealthCheck` | health/status | AI 서버 liveness + live disabled 확인 |
| `GetServiceReadiness` | service readiness | BE 대시보드가 읽을 수 있는 배포/브로커 readiness |
| `PublishPortfolioPatch` | `/v1/portfolio-patches` | C8 Portfolio Manager patch 전달 |
| `PublishFinalDecision` | `/v1/final-decisions` | C9 FDA approve/veto 전달 |
| `PublishExecutionFeedback` | `/v1/execution-feedback` | C10 execution feedback 전달 |
| `PublishInternalMessage` | `/v1/internal/messages` | C4 blackboard message 전달 |
| `PublishAgentReport` | `/v1/internal/agent-reports` | C5 agent report 전달 |
| `GetRecommendations` | recommendation list | BE 추천종목 화면용 read-only 모델 랭킹 조회 |

## 안전 정책

- gRPC transport는 live trading 권한을 열지 않는다.
- `HealthCheckResponse.live_trading_allowed=false`가 기본이다.
- `ExecutionFeedbackEnvelope.live_enabled=true`가 들어오면 AI 서버는 `REJECTED_LIVE_DISABLED`로 거부한다.
- production registry를 수정하지 않는다.
- `.env` 파일을 직접 읽지 않는다. 추천 RPC는 실행 프로세스에 이미 주입된 환경으로 KIS read-only 시세 조회를 사용할 수 있지만, 주문 API는 호출하지 않는다.
- FDA는 `target_weights`와 `order_deltas`를 read-only echo로만 가진다. 비중은 PPO, 주문 변경분은 Portfolio Manager 소유다.
- `GetRecommendations`는 추천종목 표시용 신호만 반환한다. `target_weights`, `order_deltas`, 주문 수량, 주문 방향은 반환하지 않으며 주문 승인으로 해석하면 안 된다.

## 추천종목 RPC

BE 추천종목 화면은 `GetRecommendations`를 호출한다.

요청 필드:

| 필드 | 설명 |
|---|---|
| `request_id` | BE 요청 추적 ID. 비어 있으면 AI 서버가 응답용 ID를 생성 |
| `bundle_id` | 추천에 사용할 paper baseline bundle. 비어 있으면 `risk_config.yaml.grpc_recommendations.default_bundle_id` 사용 |
| `asof` | 선택. 비어 있으면 조회한 최신 1분봉 시각을 사용 |
| `tickers` | 선택. 비어 있으면 `new/config/universe_config.yaml`의 active 30종목 전체 |
| `top_k` | 선택. 기본 10, 최대 30 (`risk_config.yaml`의 `grpc_recommendations`) |
| `include_diagnostics` | 장애 원인/quant mode 등 진단 JSON 포함 여부 |

응답의 `RecommendationItem`은 다음 필드를 포함한다.

| 필드 | 설명 |
|---|---|
| `recommendation_id`, `request_id` | 추천 항목/요청 추적 ID |
| `stock_code`, `ticker`, `stock_name` | 종목 식별자와 이름 |
| `ranking`, `score` | LightGBM cross-sectional ranking 순위와 raw score |
| `reason` | 추천 사유 코드. 현재는 ranking signal이며 기대수익률은 미보정 |
| `expected_return`, `expected_return_available` | 현재 모델 score는 보정된 수익률이 아니므로 `expected_return_available=false` |
| `risk_level` | confidence 기반 `low|medium|high` 표시 |
| `model_version`, `bundle_id` | 사용한 모델/번들 식별자 |

추천 RPC는 read-only다. 필요한 Dual-Source artifact가 없거나 Quant가 `active`가 아니면 `status=BLOCKED`와 원인만 반환하고, 더미 추천종목을 만들지 않는다.

## AI 쪽 실행 방법

먼저 gRPC 의존성이 설치되어 있어야 한다.

```bash
/opt/anaconda3/envs/elephant/bin/python -m pip install -r requirements.txt
```

Python stub 생성:

```bash
PYTHONPATH=/Users/jangjaewon/Desktop/Elephant_Lab/new \
/opt/anaconda3/envs/elephant/bin/python \
/Users/jangjaewon/Desktop/Elephant_Lab/new/scripts/generate_ai_grpc_stubs.py
```

AI gRPC 서버 실행:

```bash
PYTHONPATH=/Users/jangjaewon/Desktop/Elephant_Lab/new \
/opt/anaconda3/envs/elephant/bin/python \
/Users/jangjaewon/Desktop/Elephant_Lab/new/scripts/run_ai_grpc_server.py \
  --host 127.0.0.1 \
  --port 50051 \
  --bundle-id BUNDLE-20260521-POSTCLOSE
```

## BE 쪽 공유 포인트

BE 팀에는 `new/proto/elephant_ai_bridge.proto` 하나를 공유하면 된다. Java/Spring gRPC라면 같은 proto에서 Java stub을 생성하면 된다.

예시:

```bash
protoc \
  -I new/proto \
  --java_out=<be-generated-dir> \
  --grpc-java_out=<be-generated-dir> \
  new/proto/elephant_ai_bridge.proto
```

## 현재 범위

이번 작업은 HTTP 계약을 gRPC 계약으로 병렬 제공하는 작업이다. 실제 paper-auto 주문 로직, 모델 재학습, C12 backtest 정책, production/live gate는 변경하지 않는다.

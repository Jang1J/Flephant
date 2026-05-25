"""AI-side gRPC server wrapper for the shared AI-BE proto contract."""
from __future__ import annotations

import importlib
import sys
import threading
import uuid
from concurrent import futures
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.integration.grpc.payloads import (
    build_ack_payload,
    build_health_payload,
    build_service_readiness_payload,
)
from src.integration.kafka.producer import KafkaEventProducer
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger

logger = get_logger("grpc_server")
_KST = ZoneInfo("Asia/Seoul")

_GENERATED_DIR = Path(__file__).resolve().parent / "generated"
_PROJECT_ROOT = Path(__file__).resolve().parents[4]


class _PaperAutoSession:
    """동시 1세션만 허용하는 paper auto trading 세션 관리.

    BE에서 Start 요청 → 비동기 스레드 실행 → Stop 요청 또는 자동 종료.
    """

    def __init__(self, kafka: KafkaEventProducer) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._kafka = kafka
        self.session_id: str = ""
        self.bundle_id: str = ""
        self.running: bool = False
        self.completed_cycles: int = 0
        self.total_cycles: int = 0
        self.started_at: str = ""
        self.last_cycle_at: str = ""

    def start(
        self,
        *,
        bundle_id: str,
        cycles: int,
        interval_sec: int,
        tickers: list[str],
        confirm_phrase: str,
        root: Path | None = None,
    ) -> dict[str, Any]:
        """세션 시작. 이미 실행 중이면 거부."""
        with self._lock:
            if self.running:
                return {
                    "accepted": False,
                    "status": "ALREADY_RUNNING",
                    "reason": f"session {self.session_id} 실행 중",
                    "session_id": self.session_id,
                }

            self.session_id = uuid.uuid4().hex[:8]
            self.bundle_id = bundle_id
            self.total_cycles = cycles
            self.completed_cycles = 0
            self.started_at = datetime.now(_KST).isoformat()
            self.last_cycle_at = ""
            self.running = True
            self._stop_event.clear()

            self._thread = threading.Thread(
                target=self._run_loop,
                kwargs={
                    "bundle_id": bundle_id,
                    "cycles": cycles,
                    "interval_sec": interval_sec,
                    "tickers": tickers,
                    "confirm_phrase": confirm_phrase,
                    "root": root,
                },
                daemon=True,
            )
            self._thread.start()

            self._kafka.emit(
                "AUTO_TRADING_STARTED",
                session_id=self.session_id,
                bundle_id=bundle_id,
                payload={"cycles": cycles, "tickers": tickers},
            )
            return {
                "accepted": True,
                "status": "STARTED",
                "reason": "",
                "session_id": self.session_id,
            }

    def stop(self, session_id: str = "") -> dict[str, Any]:
        """세션 중지 요청."""
        with self._lock:
            if not self.running:
                return {
                    "accepted": False,
                    "status": "NOT_RUNNING",
                    "reason": "실행 중인 세션 없음",
                }
            if session_id and session_id != self.session_id:
                return {
                    "accepted": False,
                    "status": "SESSION_MISMATCH",
                    "reason": f"요청 {session_id} != 실행 {self.session_id}",
                }
            self._stop_event.set()
            return {
                "accepted": True,
                "status": "STOPPING",
                "reason": "",
            }

    def status(self) -> dict[str, Any]:
        """현재 상태 반환. lock으로 백그라운드 스레드와 일관성 보장."""
        with self._lock:
            return {
                "running": self.running,
                "session_id": self.session_id,
                "status": "RUNNING" if self.running else "IDLE",
                "completed_cycles": self.completed_cycles,
                "total_cycles": self.total_cycles,
                "started_at": self.started_at,
                "bundle_id": self.bundle_id,
                "last_cycle_at": self.last_cycle_at,
            }

    def _run_loop(
        self,
        *,
        bundle_id: str,
        cycles: int,
        interval_sec: int,
        tickers: list[str],
        confirm_phrase: str,
        root: Path | None,
    ) -> None:
        """비동기 스레드에서 PaperAutoTrader.run()을 실행.

        _GrpcPaperAutoTrader(상속)로 run_once()를 override해서 매 cycle마다
        실시간 kafka 이벤트 발행. sleep_fn 주입으로 stop 요청 시 조기 종료.
        """
        try:
            stop_event = self._stop_event

            def interruptible_sleep(sec: float) -> None:
                if stop_event.wait(timeout=sec):
                    raise InterruptedError("stop requested via gRPC")

            trader = _make_grpc_paper_auto_trader(
                kafka=self._kafka,
                session_ref=self,
                required_bundle_id=bundle_id,
                sleep_fn=interruptible_sleep,
            )

            trader.run(
                tickers=tickers,
                cycles=cycles,
                interval_sec=interval_sec,
                confirm_phrase=confirm_phrase,
            )

            self._kafka.emit(
                "AUTO_TRADING_STOPPED",
                session_id=self.session_id,
                bundle_id=bundle_id,
                payload={"completed_cycles": self.completed_cycles},
            )
            self._kafka.flush()
        except Exception as e:
            logger.error("[grpc] auto trading 비정상 종료: %s", e)
            self._kafka.emit(
                "AUTO_TRADING_FAILED",
                session_id=self.session_id,
                bundle_id=bundle_id,
                payload={"error": str(e)},
            )
            self._kafka.flush()
        finally:
            with self._lock:
                self.running = False


def _make_grpc_paper_auto_trader(
    *,
    kafka: KafkaEventProducer,
    session_ref: _PaperAutoSession,
    **kwargs: Any,
) -> Any:
    """PaperAutoTrader를 상속해 run_once()마다 실시간 kafka 이벤트 발행.

    run() 내부에서 매 cycle마다 run_once()를 호출하므로,
    override한 run_once()에서 super() 호출 후 kafka emit → 실시간.
    PaperAutoTrader 원본 코드 수정 없음.

    lazy import로 circular import 회피.
    """
    from src.execution.paper_auto_trading import PaperAutoTrader

    class _GrpcPaperAutoTrader(PaperAutoTrader):

        def __init__(self, *, _kafka: KafkaEventProducer, _session: _PaperAutoSession, **kw: Any) -> None:
            super().__init__(**kw)
            self._grpc_kafka = _kafka
            self._grpc_session = _session

        def run_once(self, *, tickers: list[str], cycle_index: int = 0, risk_warnings: Any = None) -> dict[str, Any]:
            result = super().run_once(
                tickers=tickers,
                cycle_index=cycle_index,
                risk_warnings=risk_warnings,
            )

            with self._grpc_session._lock:
                self._grpc_session.completed_cycles = cycle_index + 1
                self._grpc_session.last_cycle_at = datetime.now(_KST).isoformat()

            sid = self._grpc_session.session_id
            bid = self._grpc_session.bundle_id

            self._grpc_kafka.emit(
                "DECISION_COMPLETED",
                session_id=sid,
                bundle_id=bid,
                payload={"cycle": cycle_index + 1, "status": result.get("status", "")},
            )

            execution = result.get("stages", {}).get("execution", {})
            if isinstance(execution, dict):
                for od in execution.get("order_deltas", []):
                    self._grpc_kafka.emit(
                        "PAPER_ORDER_SUBMITTED",
                        session_id=sid,
                        bundle_id=bid,
                        payload={"ticker": od.get("ticker", ""), "side": od.get("side", "")},
                    )
                for fill in execution.get("fills", []):
                    self._grpc_kafka.emit(
                        "PAPER_ORDER_FILLED",
                        session_id=sid,
                        bundle_id=bid,
                        payload={"ticker": fill.get("ticker", "")},
                    )
                for rej in execution.get("rejections", []):
                    self._grpc_kafka.emit(
                        "PAPER_ORDER_FAILED",
                        session_id=sid,
                        bundle_id=bid,
                        payload={"ticker": rej.get("ticker", ""), "reason": rej.get("reason", "")},
                    )

            return result

    return _GrpcPaperAutoTrader(_kafka=kafka, _session=session_ref, **kwargs)


def _load_grpc_modules() -> tuple[Any, Any, Any]:
    """Load grpc + generated modules with a clear setup error."""
    try:
        import grpc  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "grpcio is not installed. Install requirements.txt before running the AI gRPC server."
        ) from e

    if str(_GENERATED_DIR) not in sys.path:
        sys.path.insert(0, str(_GENERATED_DIR))
    try:
        pb2 = importlib.import_module("elephant_ai_bridge_pb2")
        pb2_grpc = importlib.import_module("elephant_ai_bridge_pb2_grpc")
    except ImportError as e:
        raise RuntimeError(
            "Generated gRPC stubs are missing. Run "
            "`PYTHONPATH=new python new/scripts/generate_ai_grpc_stubs.py` first."
        ) from e
    return grpc, pb2, pb2_grpc


def _make_servicer(
    grpc: Any,
    pb2: Any,
    pb2_grpc: Any,
    *,
    bundle_id: str,
    root: Path | None,
    session: _PaperAutoSession,
) -> Any:
    """Create a generated servicer instance bound to read-only payload builders."""

    class AiBeBridgeServicer(pb2_grpc.AiBeBridgeServiceServicer):  # type: ignore[misc, name-defined]
        def HealthCheck(self, request: Any, context: Any) -> Any:  # noqa: N802
            payload = build_health_payload(
                request_id=str(getattr(request, "request_id", "")),
                bundle_id=str(getattr(request, "bundle_id", "") or bundle_id),
                root=root,
            )
            return pb2.HealthCheckResponse(**payload)

        def GetServiceReadiness(self, request: Any, context: Any) -> Any:  # noqa: N802
            requested_bundle_id = str(getattr(request, "bundle_id", "") or bundle_id)
            if not requested_bundle_id:
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details("bundle_id is required")
                requested_bundle_id = ""
            payload = build_service_readiness_payload(
                request_id=str(getattr(request, "request_id", "")),
                bundle_id=requested_bundle_id,
                include_details=bool(getattr(request, "include_details", False)),
                root=root,
            )
            return pb2.ServiceReadinessResponse(**payload)

        def PublishPortfolioPatch(self, request: Any, context: Any) -> Any:  # noqa: N802
            return pb2.Ack(**_ack_from_request(request))

        def PublishFinalDecision(self, request: Any, context: Any) -> Any:  # noqa: N802
            return pb2.Ack(**_ack_from_request(request))

        def PublishExecutionFeedback(self, request: Any, context: Any) -> Any:  # noqa: N802
            if bool(getattr(request, "live_enabled", False)):
                return pb2.Ack(
                    **_ack_from_request(
                        request,
                        accepted=False,
                        status="REJECTED_LIVE_DISABLED",
                        reason="AI gRPC bridge rejects live_enabled=true by default",
                    )
                )
            return pb2.Ack(**_ack_from_request(request))

        def PublishInternalMessage(self, request: Any, context: Any) -> Any:  # noqa: N802
            return pb2.Ack(**_ack_from_request(request))

        def PublishAgentReport(self, request: Any, context: Any) -> Any:  # noqa: N802
            return pb2.Ack(**_ack_from_request(request))

        def StartPaperAutoTrading(self, request: Any, context: Any) -> Any:  # noqa: N802
            pa_cfg = config_load("risk_config.yaml", "paper_auto_trading") or {}
            default_cycles = int(pa_cfg.get("default_max_cycles", 1))
            default_interval = int(pa_cfg.get("default_interval_sec", 60))
            result = session.start(
                bundle_id=str(getattr(request, "bundle_id", "") or bundle_id),
                cycles=int(getattr(request, "cycles", 0) or default_cycles),
                interval_sec=int(getattr(request, "interval_sec", 0) or default_interval),
                tickers=list(getattr(request, "tickers", [])),
                confirm_phrase=str(getattr(request, "confirm_phrase", "")),
                root=root,
            )
            return pb2.StartPaperAutoTradingResponse(
                request_id=str(getattr(request, "request_id", "")),
                accepted=bool(result["accepted"]),
                status=str(result["status"]),
                reason=str(result.get("reason", "")),
                session_id=str(result.get("session_id", "")),
                started_at=session.started_at if result["accepted"] else "",
            )

        def StopPaperAutoTrading(self, request: Any, context: Any) -> Any:  # noqa: N802
            result = session.stop(
                session_id=str(getattr(request, "session_id", "")),
            )
            return pb2.StopPaperAutoTradingResponse(
                request_id=str(getattr(request, "request_id", "")),
                accepted=bool(result["accepted"]),
                status=str(result["status"]),
                reason=str(result.get("reason", "")),
                stopped_at=datetime.now(_KST).isoformat() if result["accepted"] else "",
            )

        def GetPaperAutoTradingStatus(self, request: Any, context: Any) -> Any:  # noqa: N802
            s = session.status()
            return pb2.PaperAutoTradingStatusResponse(
                request_id=str(getattr(request, "request_id", "")),
                running=bool(s["running"]),
                session_id=str(s["session_id"]),
                status=str(s["status"]),
                completed_cycles=int(s["completed_cycles"]),
                total_cycles=int(s["total_cycles"]),
                started_at=str(s["started_at"]),
                bundle_id=str(s["bundle_id"]),
                last_cycle_at=str(s["last_cycle_at"]),
            )

    return AiBeBridgeServicer()


def _ack_from_request(
    request: Any,
    *,
    accepted: bool = True,
    status: str = "ACK_READ_ONLY",
    reason: str = "validated by AI gRPC bridge; no live trading or registry mutation",
) -> dict[str, Any]:
    return build_ack_payload(
        request_id=str(getattr(request, "request_id", "")),
        idempotency_key=str(getattr(request, "idempotency_key", "")),
        accepted=accepted,
        status=status,
        reason=reason,
    )


def serve(
    *,
    host: str,
    port: int,
    bundle_id: str,
    root: Path | None = None,
    max_workers: int = 4,
) -> Any:
    """Start the AI gRPC server and return the grpc.Server instance."""
    grpc, pb2, pb2_grpc = _load_grpc_modules()
    kafka = KafkaEventProducer()
    session = _PaperAutoSession(kafka=kafka)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    pb2_grpc.add_AiBeBridgeServiceServicer_to_server(
        _make_servicer(
            grpc, pb2, pb2_grpc,
            bundle_id=bundle_id, root=root, session=session,
        ),
        server,
    )
    server.add_insecure_port(f"{host}:{int(port)}")
    server.start()
    return server

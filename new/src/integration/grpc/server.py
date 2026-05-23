"""AI-side gRPC server wrapper for the shared AI-BE proto contract."""
from __future__ import annotations

import importlib
import sys
from concurrent import futures
from pathlib import Path
from typing import Any

from src.integration.grpc.payloads import (
    build_ack_payload,
    build_health_payload,
    build_service_readiness_payload,
)

_GENERATED_DIR = Path(__file__).resolve().parent / "generated"


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
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    pb2_grpc.add_AiBeBridgeServiceServicer_to_server(
        _make_servicer(grpc, pb2, pb2_grpc, bundle_id=bundle_id, root=root),
        server,
    )
    server.add_insecure_port(f"{host}:{int(port)}")
    server.start()
    return server

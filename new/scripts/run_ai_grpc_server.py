#!/usr/bin/env python
"""Run the operational AI-BE gRPC bridge server.

The server reads only the current process environment. Deployment wrappers may
source a server-only env file before launching this script, but this script
never reads `.env` by itself and never prints secret values.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.integration.grpc.server import serve  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.safe_cast import safe_int  # noqa: E402


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, min_value: int | None = None) -> int:
    return safe_int(os.environ.get(name), default=default, min_value=min_value)


def _default_bundle_id() -> str:
    cfg = config_load("risk_config.yaml", "grpc_recommendations") or {}
    return str(cfg.get("default_bundle_id") or "").strip()


def _env_readiness_report() -> dict:
    from scripts.print_env_readiness import build_report

    return build_report()


def _require_env_readiness() -> dict:
    report = _env_readiness_report()
    if report.get("status") != "PASS":
        raise RuntimeError(
            "KIS paper env readiness failed; source server env before starting AI gRPC.",
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("AI_GRPC_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=_env_int("AI_GRPC_PORT", 50051, min_value=1))
    parser.add_argument(
        "--bundle-id",
        default=os.environ.get("AI_BUNDLE_ID", _default_bundle_id()),
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=_env_int("AI_GRPC_MAX_WORKERS", 8, min_value=1),
    )
    parser.add_argument(
        "--no-env-readiness-check",
        action="store_true",
        help="Skip process-env readiness check. Use only for local unit smoke.",
    )
    parser.add_argument(
        "--require-kafka",
        action="store_true",
        default=_env_bool("KAFKA_REQUIRED", False),
        help="Fail startup if Kafka producer cannot connect.",
    )
    args = parser.parse_args(argv)

    readiness = {}
    if not args.no_env_readiness_check:
        readiness = _require_env_readiness()
    if not str(args.bundle_id).strip():
        raise RuntimeError("bundle_id is required for operational AI gRPC server")

    server = serve(
        host=str(args.host),
        port=int(args.port),
        bundle_id=str(args.bundle_id),
        root=ROOT,
        max_workers=int(args.max_workers),
        kafka_required=bool(args.require_kafka),
    )
    stopped = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    startup_payload = {
        "status": "SERVING",
        "host": str(args.host),
        "port": int(args.port),
        "bundle_id": str(args.bundle_id),
        "max_workers": int(args.max_workers),
        "kafka_required": bool(args.require_kafka),
        "env_readiness_status": readiness.get("status", "SKIP"),
        "live_trading_allowed": False,
        "production_registry_mutated": False,
    }
    print(
        "[ai_grpc] "
        + json.dumps(startup_payload, ensure_ascii=False, sort_keys=True),
        flush=True,
    )
    while not stopped:
        time.sleep(1)
    server.stop(grace=5)
    print("[ai_grpc] 서버 중지 완료", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

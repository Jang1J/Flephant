"""Read-only payload builders for the AI-BE gRPC bridge.

The helpers in this module intentionally do not call brokers, do not read
secrets, and do not mutate registries. They translate local artifact status
into gRPC-friendly dictionaries used by the generated service wrapper.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.ops.service_readiness_status import build_service_status

_KST = ZoneInfo("Asia/Seoul")
_REPO_ROOT = Path(__file__).resolve().parents[4]


def _now_iso() -> str:
    return datetime.now(_KST).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        return {"_read_error": f"json_decode_error: {e}"}
    except OSError as e:
        return {"_read_error": f"os_error: {e}"}
    return data if isinstance(data, dict) else {}


def _production_active_version(root: Path) -> str:
    registry = _read_json(root / "artifacts" / "lgbm" / "registry.json")
    active = registry.get("active_version")
    return str(active) if active else ""


def build_health_payload(
    *,
    request_id: str = "",
    bundle_id: str = "",
    root: Path | None = None,
) -> dict[str, Any]:
    """Build a lightweight health response for BE gRPC liveness checks."""
    repo_root = root or _REPO_ROOT
    active_version = _production_active_version(repo_root)
    status = "PASS"
    message = "AI gRPC bridge is reachable; live trading remains disabled."
    if bundle_id:
        readiness = build_service_status(bundle_id=bundle_id, root=repo_root)
        status = str(readiness.get("status") or "BLOCKED")
        message = "Service readiness reflected from local read-only artifacts."
    return {
        "request_id": request_id,
        "status": status,
        "generated_at": _now_iso(),
        "transport": "grpc",
        "bundle_id": bundle_id,
        "live_trading_allowed": False,
        "production_registry_mutated": False,
        "production_active_version": active_version,
        "message": message,
    }


def build_service_readiness_payload(
    *,
    request_id: str = "",
    bundle_id: str,
    include_details: bool = False,
    root: Path | None = None,
) -> dict[str, Any]:
    """Translate service_readiness_status into a gRPC response dictionary."""
    repo_root = root or _REPO_ROOT
    readiness = build_service_status(bundle_id=bundle_id, root=repo_root)
    be_contract = readiness.get("be_contract")
    if not isinstance(be_contract, dict):
        be_contract = {}
    details_json = ""
    if include_details:
        details_json = json.dumps(readiness, ensure_ascii=False, sort_keys=True)
    return {
        "request_id": request_id,
        "status": str(readiness.get("status") or "BLOCKED"),
        "generated_at": str(readiness.get("generated_at") or _now_iso()),
        "bundle_id": str(readiness.get("bundle_id") or bundle_id),
        "deploy_quality": str(readiness.get("deploy_quality") or "BLOCKED"),
        "broker_evidence": str(readiness.get("broker_evidence") or "BLOCKED"),
        "live_trading_allowed": False,
        "registry_mutated": bool(readiness.get("registry_mutated")),
        "safe_to_show_dashboard": bool(be_contract.get("safe_to_show_dashboard", True)),
        "safe_to_enable_order_actions": bool(be_contract.get("safe_to_enable_order_actions")),
        "safe_to_enable_live_actions": bool(be_contract.get("safe_to_enable_live_actions")),
        "details_json": details_json,
    }


def build_ack_payload(
    *,
    request_id: str = "",
    idempotency_key: str = "",
    accepted: bool = True,
    status: str = "ACK_READ_ONLY",
    reason: str = "validated by AI gRPC bridge; no live trading or registry mutation",
) -> dict[str, Any]:
    """Build a transport-level ACK without persisting or mutating state."""
    return {
        "request_id": request_id,
        "accepted": bool(accepted),
        "status": status,
        "reason": reason,
        "received_at": _now_iso(),
        "idempotency_key": idempotency_key,
    }

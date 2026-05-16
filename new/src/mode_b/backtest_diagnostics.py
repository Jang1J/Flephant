"""Backtest diagnostic helpers for service-policy evidence."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

from src.mode_b.service_policy_verifier import verify_service_policy_evidence
from src.utils.safe_cast import safe_bool

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_SERVICE_POLICY_DIR = (
    _REPO_ROOT / "artifacts" / "reports" / "service_policy_replay"
)


def latest_service_policy_report_path(
    bundle_id: str,
    *,
    reports_dir: Path | None = None,
) -> Path | None:
    """Return the newest persisted service-policy replay report for a bundle."""
    root = reports_dir or _DEFAULT_SERVICE_POLICY_DIR
    if not root.exists():
        return None
    candidates = sorted(
        root.glob(f"service_policy_replay_{bundle_id}_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def load_service_policy_evidence(
    bundle_id: str,
    *,
    reports_dir: Path | None = None,
    expected_date_range: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Load compact service-policy evidence for C12 diagnostics."""
    path = latest_service_policy_report_path(bundle_id, reports_dir=reports_dir)
    if path is None:
        return None
    try:
        raw = path.read_bytes()
        report_sha256 = hashlib.sha256(raw).hexdigest()
        report = json.loads(raw.decode("utf-8"))
    except Exception as e:
        try:
            raw = path.read_bytes()
            report_sha256 = hashlib.sha256(raw).hexdigest()
        except Exception:
            report_sha256 = ""
        return {
            "status": "UNREADABLE",
            "report_path": str(path),
            "report_path_relative": _repo_relative(path),
            "service_policy_report_path": str(path),
            "service_policy_report_path_relative": _repo_relative(path),
            "service_policy_report_sha256": report_sha256,
            "error": str(e),
            "error_type": type(e).__name__,
        }
    evidence = {
        "status": str(report.get("status", "")),
        "bundle_id": str(report.get("bundle_id", bundle_id)),
        "date_range": report.get("date_range"),
        "service_policy_generated_at": report.get("generated_at"),
        "service_policy_report_path": str(path),
        "service_policy_report_path_relative": _repo_relative(path),
        "service_policy_report_sha256": report_sha256,
        "report_path": str(path),
        "report_path_relative": _repo_relative(path),
        "gate": report.get("gate", {}),
        "metrics": report.get("metrics", {}),
        "order_stats": report.get("order_stats", {}),
        "policy_checks": report.get("policy_checks", {}),
        "external_kis_api": safe_bool(report.get("external_kis_api", False), default=False),
        "registry_mutated": safe_bool(report.get("registry_mutated", False), default=False),
    }
    verification = verify_service_policy_evidence(
        evidence,
        bundle_id=bundle_id,
        repo_root=_REPO_ROOT,
        expected_date_range=expected_date_range,
    )
    evidence["verification"] = {
        "status": verification.status,
        "blockers": verification.blockers,
        "report_sha256": verification.report_sha256,
    }
    if not verification.passed:
        evidence["status"] = "BLOCKED"
    return evidence


def attach_service_policy_evidence(
    backtest_report: dict[str, Any],
    *,
    reports_dir: Path | None = None,
    expected_date_range: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach latest service-policy evidence to a C12 report copy."""
    bundle_id = str(backtest_report.get("bundle_id", ""))
    if not bundle_id:
        return dict(backtest_report)
    evidence = load_service_policy_evidence(
        bundle_id,
        reports_dir=reports_dir,
        expected_date_range=(
            expected_date_range
            or backtest_report.get("service_policy_expected_date_range")
            or backtest_report.get("date_range")
        ),
    )
    merged = dict(backtest_report)
    if evidence is None:
        merged["service_policy_replay"] = {
            "status": "MISSING",
            "diagnostic": "No persisted service-policy replay report found.",
            "verification": {
                "status": "MISSING",
                "blockers": ["service_policy_report_missing"],
            },
        }
        return merged
    merged["service_policy_replay"] = evidence
    notes = str(merged.get("diagnostic_notes", "") or "")
    service_status = evidence.get("status", "")
    if service_status and service_status != "PASS":
        suffix = f" service_policy_replay={service_status}"
        merged["diagnostic_notes"] = f"{notes}{suffix}".strip()
    return merged


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)

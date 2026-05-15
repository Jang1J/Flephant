"""Service-policy evidence verifier shared by C12/C14 gates."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ServicePolicyVerification:
    status: str
    blockers: list[str]
    report_path: str | None = None
    report_sha256: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == "PASS"


def verify_service_policy_evidence(
    evidence: dict[str, Any] | None,
    *,
    bundle_id: str,
    repo_root: Path | None = None,
    expected_date_range: dict[str, Any] | None = None,
) -> ServicePolicyVerification:
    """Validate C12-embedded service-policy evidence against its report file."""
    root = repo_root or _REPO_ROOT
    blockers: list[str] = []
    if not isinstance(evidence, dict):
        return ServicePolicyVerification("MISSING", ["service_policy_evidence_missing"])

    evidence_bundle = evidence.get("bundle_id")
    if evidence_bundle not in (None, "", bundle_id):
        blockers.append("service_policy_bundle_mismatch")

    expected_sha = str(evidence.get("service_policy_report_sha256") or "")
    report_path = _resolve_report_path(evidence, root)
    report: dict[str, Any] = {}
    actual_sha = ""

    if report_path is None:
        blockers.append("service_policy_report_path_missing")
    elif not report_path.exists():
        blockers.append("service_policy_report_missing")
    else:
        try:
            raw = report_path.read_bytes()
            actual_sha = hashlib.sha256(raw).hexdigest()
            report = json.loads(raw.decode("utf-8"))
            if not isinstance(report, dict):
                blockers.append("service_policy_report_not_object")
                report = {}
        except Exception:
            blockers.append("service_policy_report_unreadable")
            report = {}

    if not expected_sha:
        blockers.append("service_policy_report_sha_missing")
    elif actual_sha and actual_sha != expected_sha:
        blockers.append("service_policy_report_sha_mismatch")

    report_bundle = report.get("bundle_id")
    if report and report_bundle != bundle_id:
        blockers.append("service_policy_report_bundle_mismatch")

    if expected_date_range:
        report_range = report.get("date_range") if report else None
        if not _date_ranges_equal(report_range, expected_date_range):
            blockers.append("service_policy_date_range_mismatch")

    status = str(evidence.get("status") or report.get("status") or "")
    gate = _merge_mapping(report.get("gate"), evidence.get("gate"))
    checks = _merge_mapping(report.get("policy_checks"), evidence.get("policy_checks"))
    stats = _merge_mapping(report.get("order_stats"), evidence.get("order_stats"))

    if status != "PASS":
        blockers.append("service_policy_status_not_pass")
    if gate.get("status") != "PASS":
        blockers.append("service_policy_gate_not_pass")
    for key in (
        "deploy_candidate_by_service_policy",
        "no_naked_short_exposure",
        "order_caps_respected",
        "cash_guard_respected",
    ):
        if checks.get(key) is not True:
            blockers.append(f"service_policy_check_failed:{key}")
    if int(stats.get("naked_short_attempts", 0) or 0) != 0:
        blockers.append("service_policy_naked_short_attempts")

    blockers = sorted(set(blockers))
    return ServicePolicyVerification(
        status="PASS" if not blockers else "BLOCKED",
        blockers=blockers,
        report_path=str(report_path) if report_path is not None else None,
        report_sha256=actual_sha or None,
    )


def service_policy_gate_pass(
    evidence: dict[str, Any] | None,
    *,
    bundle_id: str,
    repo_root: Path | None = None,
    expected_date_range: dict[str, Any] | None = None,
) -> bool:
    return verify_service_policy_evidence(
        evidence,
        bundle_id=bundle_id,
        repo_root=repo_root,
        expected_date_range=expected_date_range,
    ).passed


def _resolve_report_path(evidence: dict[str, Any], root: Path) -> Path | None:
    """Resolve persisted service-policy report path with portable fallbacks.

    Older C12 reports embed both an absolute path from the producer machine and
    a repo-relative path.  A sanitized zip or another checkout may not have the
    absolute path, so try every declared candidate and prefer the first existing
    file.  If none exists, return the first syntactically valid path so callers
    can still report `service_policy_report_missing`.
    """
    candidates: list[Path] = []
    for key in (
        "service_policy_report_path",
        "service_policy_report_path_relative",
        "report_path",
        "report_path_relative",
    ):
        raw = evidence.get(key)
        if raw in (None, ""):
            continue
        path = Path(str(raw))
        candidates.append(path if path.is_absolute() else root / path)

    for path in candidates:
        if path.exists():
            return path
    return candidates[0] if candidates else None


def _merge_mapping(primary: Any, fallback: Any) -> dict[str, Any]:
    if isinstance(primary, dict) and primary:
        return primary
    if isinstance(fallback, dict):
        return fallback
    return {}


def _date_ranges_equal(left: Any, right: Any) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return _compact_date(left.get("start")) == _compact_date(right.get("start")) and (
        _compact_date(left.get("end")) == _compact_date(right.get("end"))
    )


def _compact_date(value: Any) -> str:
    raw = str(value or "")[:10]
    return raw.replace("-", "")

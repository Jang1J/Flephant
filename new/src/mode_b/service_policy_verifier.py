"""Service-policy evidence verifier shared by C12/C14 gates."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.utils.config_loader import load as config_load
from src.utils.safe_cast import safe_bool, safe_float, safe_int
from src.utils.ticker_utils import pad_ticker

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
    expected_universe: list[str] | None = None,
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

    if expected_universe is not None:
        universe_blockers = _verify_universe_binding(
            report=report,
            evidence=evidence,
            expected_universe=expected_universe,
        )
        blockers.extend(universe_blockers)

    blockers.extend(_verify_current_policy_binding(report))

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
        if not safe_bool(checks.get(key), default=False):
            blockers.append(f"service_policy_check_failed:{key}")
    naked_raw = stats.get("naked_short_attempts", 0)
    naked_default = 0 if naked_raw is None else -1
    if safe_int(naked_raw, default=naked_default) != 0:
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
    expected_universe: list[str] | None = None,
) -> bool:
    return verify_service_policy_evidence(
        evidence,
        bundle_id=bundle_id,
        repo_root=repo_root,
        expected_date_range=expected_date_range,
        expected_universe=expected_universe,
    ).passed


def normalize_service_policy_universe(tickers: list[str] | None) -> list[str]:
    """Canonical final-universe representation used by replay and gates."""
    normalized = {
        pad_ticker(str(ticker))
        for ticker in (tickers or [])
        if str(ticker).strip()
    }
    return sorted(normalized)


def service_policy_universe_hash(tickers: list[str] | None) -> str:
    """Stable SHA-256 hash for a service-policy replay universe."""
    universe = normalize_service_policy_universe(tickers)
    payload = json.dumps(universe, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def current_service_policy_snapshot() -> dict[str, Any]:
    """Return the current SSOT service-policy knobs used by deploy gates.

    This intentionally duplicates only the gate-relevant subset instead of
    importing ``ServicePolicyConfig`` to avoid a circular dependency:
    ``service_policy_replay`` imports universe helpers from this module.
    """
    replay_cfg = config_load("risk_config.yaml", "service_policy_replay") or {}
    paper_auto_cfg = config_load("risk_config.yaml", "paper_auto_trading") or {}
    eval_cfg = config_load("risk_config.yaml", "evaluation") or {}
    return {
        "top_k_fraction": safe_float(
            replay_cfg.get("top_k_fraction", eval_cfg.get("top_k_fraction", 0.25)),
            default=0.25,
            min_value=0.0,
        ),
        "max_orders_per_cycle": safe_int(
            paper_auto_cfg.get("max_orders_per_cycle", 3),
            default=3,
            min_value=1,
        ),
        "max_order_qty_per_order": safe_int(
            paper_auto_cfg.get("max_order_qty_per_order", 1),
            default=1,
            min_value=1,
        ),
        "decision_stride_bars": safe_int(
            replay_cfg.get("decision_stride_bars", 1),
            default=1,
            min_value=1,
        ),
        "min_holding_bars": safe_int(
            replay_cfg.get("min_holding_bars", 0),
            default=0,
            min_value=0,
        ),
        "rebalance_cooldown_bars": safe_int(
            replay_cfg.get("rebalance_cooldown_bars", 0),
            default=0,
            min_value=0,
        ),
        "no_trade_score_spread": safe_float(
            replay_cfg.get("no_trade_score_spread", 0.0),
            default=0.0,
            min_value=0.0,
        ),
        "allow_position_pyramiding": safe_bool(
            replay_cfg.get("allow_position_pyramiding", False),
            default=False,
        ),
        "turnover_budget_hard_stop": safe_bool(
            replay_cfg.get("turnover_budget_hard_stop", True),
            default=True,
        ),
        "min_expected_net_alpha_bps": safe_float(
            replay_cfg.get("min_expected_net_alpha_bps", 0.0),
            default=0.0,
            min_value=0.0,
        ),
        "expected_net_alpha_source": str(
            replay_cfg.get("expected_net_alpha_source", "rank_score")
        ),
        "min_service_policy_sharpe": safe_float(
            replay_cfg.get("min_service_policy_sharpe", 0.0),
            default=0.0,
        ),
        "trade_probability_gate_enabled": safe_bool(
            replay_cfg.get("trade_probability_gate_enabled", False),
            default=False,
        ),
        "min_trade_probability": safe_float(
            replay_cfg.get("min_trade_probability", 0.5),
            default=0.5,
            min_value=0.0,
            max_value=1.0,
        ),
    }


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


def _verify_current_policy_binding(report: dict[str, Any]) -> list[str]:
    policy = report.get("policy") if isinstance(report, dict) else None
    if not isinstance(policy, dict) or not policy:
        return []

    expected = current_service_policy_snapshot()
    blockers: list[str] = []
    for key, expected_value in expected.items():
        if key not in policy:
            blockers.append(f"service_policy_policy_key_missing:{key}")
            continue
        observed_value = policy.get(key)
        if not _policy_values_equal(observed_value, expected_value):
            blockers.append(f"service_policy_policy_stale:{key}")
    return blockers


def _policy_values_equal(left: Any, right: Any) -> bool:
    if isinstance(right, bool):
        return safe_bool(left, default=not right) is right
    if isinstance(right, int) and not isinstance(right, bool):
        return safe_int(left, default=right - 1) == right
    if isinstance(right, float):
        return abs(safe_float(left, default=right + 1.0) - right) <= 1e-12
    return str(left) == str(right)


def _merge_mapping(primary: Any, fallback: Any) -> dict[str, Any]:
    if isinstance(primary, dict) and primary:
        return primary
    if isinstance(fallback, dict):
        return fallback
    return {}


def _verify_universe_binding(
    *,
    report: dict[str, Any],
    evidence: dict[str, Any],
    expected_universe: list[str],
) -> list[str]:
    expected = normalize_service_policy_universe(expected_universe)
    if not expected:
        return ["service_policy_expected_universe_empty"]

    blockers: list[str] = []
    expected_count = len(expected)
    expected_hash = service_policy_universe_hash(expected)
    observed_universe = normalize_service_policy_universe(
        _first_list(report.get("universe"), evidence.get("universe"))
    )
    observed_hash = str(report.get("universe_hash") or evidence.get("universe_hash") or "")
    observed_count_raw = report.get("universe_count", evidence.get("universe_count"))

    if observed_count_raw in (None, "") and not observed_universe:
        blockers.append("service_policy_universe_count_missing")
    else:
        observed_count = (
            len(observed_universe)
            if observed_count_raw in (None, "")
            else safe_int(observed_count_raw, default=-1, min_value=0)
        )
        if observed_count != expected_count:
            blockers.append("service_policy_universe_count_mismatch")

    if observed_hash:
        if observed_hash != expected_hash:
            blockers.append("service_policy_universe_hash_mismatch")
    elif observed_universe:
        if service_policy_universe_hash(observed_universe) != expected_hash:
            blockers.append("service_policy_universe_hash_mismatch")
    else:
        blockers.append("service_policy_universe_hash_missing")

    return blockers


def _first_list(*values: Any) -> list[Any]:
    for value in values:
        if isinstance(value, list):
            return value
    return []


def _date_ranges_equal(left: Any, right: Any) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return _compact_date(left.get("start")) == _compact_date(right.get("start")) and (
        _compact_date(left.get("end")) == _compact_date(right.get("end"))
    )


def _compact_date(value: Any) -> str:
    raw = str(value or "")[:10]
    return raw.replace("-", "")

"""Validate paper-auto reports for false PASS semantics.

This script is read-only:
- does not read .env
- does not call KIS or external APIs
- does not mutate registries
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parents[2]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate local paper-auto report PASS semantics",
    )
    parser.add_argument("--bundle-id", default="")
    parser.add_argument(
        "--report-dir",
        default="artifacts/reports/paper_auto_trading",
    )
    parser.add_argument("--pattern", default="paper_auto_trade_*.json")
    parser.add_argument(
        "--generated-date",
        default="",
        help="Filter reports by KST date YYYYMMDD. Useful to exclude historical false-PASS fixtures.",
    )
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument(
        "--no-write-report",
        action="store_true",
        help="Accepted for CLI consistency; this validator is read-only unless --write-report is set.",
    )
    parser.add_argument(
        "--strict-cadence",
        action="store_true",
        help="Apply 30-stock cadence proof checks to matching reports.",
    )
    parser.add_argument("--min-cycles", type=int, default=2)
    parser.add_argument("--expected-ticker-count", type=int, default=30)
    parser.add_argument("--expected-cold-fetch-n", type=int, default=61)
    parser.add_argument("--expected-incremental-fetch-n", type=int, default=6)
    parser.add_argument("--incremental-after-cycle", type=int, default=1)
    parser.add_argument("--max-cycle-start-gap-sec", type=float, default=75.0)
    parser.add_argument("--require-shadow-only", action="store_true")
    return parser.parse_args(argv)


def _score_stats(scores: Any) -> dict[str, Any]:
    if not isinstance(scores, dict):
        return {
            "score_count": 0,
            "finite_score_count": 0,
            "nonzero_score_count": 0,
            "score_std": 0.0,
            "rankable": False,
        }
    values: list[float] = []
    for value in scores.values():
        try:
            parsed = float(value)
        except Exception:
            continue
        if math.isfinite(parsed):
            values.append(parsed)
    nonzero = [value for value in values if abs(value) > 1e-12]
    if len(values) > 1:
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        std = math.sqrt(variance)
    else:
        std = 0.0
    return {
        "score_count": len(scores),
        "finite_score_count": len(values),
        "nonzero_score_count": len(nonzero),
        "score_std": float(std),
        "rankable": bool(len(values) == 1 and nonzero)
        or bool(len(values) > 1 and std > 1e-12),
    }


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"report must be a JSON object: {path}")
    return data


def _iter_reports(report_dir: Path, pattern: str) -> list[Path]:
    if not report_dir.exists():
        return []
    return sorted(path for path in report_dir.rglob(pattern) if path.is_file())


def _matches_generated_date(path: Path, report: dict[str, Any], generated_date: str) -> bool:
    target = str(generated_date or "").strip()
    if not target:
        return True
    generated_at = str(report.get("generated_at") or "")
    if len(target) == 8:
        iso_date = f"{target[:4]}-{target[4:6]}-{target[6:8]}"
        if generated_at.startswith(iso_date):
            return True
        if path.name.startswith(f"paper_auto_trade_{target}_"):
            return True
    return False


def _cycle_quant_output(cycle: dict[str, Any]) -> dict[str, Any]:
    hot_result = cycle.get("hot_result") if isinstance(cycle.get("hot_result"), dict) else {}
    quant_output = hot_result.get("quant_output") if isinstance(hot_result, dict) else {}
    return quant_output if isinstance(quant_output, dict) else {}


def _cycle_order_deltas(cycle: dict[str, Any]) -> list[dict[str, Any]]:
    direct = cycle.get("final_decision")
    if isinstance(direct, dict):
        deltas = direct.get("order_deltas")
        return [delta for delta in deltas if isinstance(delta, dict)] if isinstance(deltas, list) else []
    hot_result = cycle.get("hot_result") if isinstance(cycle.get("hot_result"), dict) else {}
    decision = hot_result.get("final_decision") if isinstance(hot_result, dict) else {}
    deltas = decision.get("order_deltas") if isinstance(decision, dict) else []
    return [delta for delta in deltas if isinstance(delta, dict)] if isinstance(deltas, list) else []


def _cycle_fill_count(cycle: dict[str, Any]) -> int:
    execution = cycle.get("execution") if isinstance(cycle.get("execution"), dict) else {}
    execution_report = (
        execution.get("execution_report") if isinstance(execution, dict) else {}
    )
    fills = execution_report.get("fills") if isinstance(execution_report, dict) else []
    return len(fills) if isinstance(fills, list) else 0


def _cycle_rejection_count(cycle: dict[str, Any]) -> int:
    execution = cycle.get("execution") if isinstance(cycle.get("execution"), dict) else {}
    execution_report = (
        execution.get("execution_report") if isinstance(execution, dict) else {}
    )
    rejections = (
        execution_report.get("rejections") if isinstance(execution_report, dict) else []
    )
    return len(rejections) if isinstance(rejections, list) else 0


def _cycle_has_broker_execution(cycle: dict[str, Any]) -> bool:
    execution = cycle.get("execution") if isinstance(cycle.get("execution"), dict) else {}
    if not execution:
        return False
    execution_report = (
        execution.get("execution_report") if isinstance(execution, dict) else {}
    )
    report_status = (
        execution_report.get("status") if isinstance(execution_report, dict) else None
    )
    status = str(report_status or execution.get("status") or "").upper()
    if status in {"NOT_SUBMITTED_SHADOW", "SKIPPED", "NO_ORDERS", "FAIL", "FAILED", "REJECTED"}:
        return False
    if status not in {"SUBMITTED", "FILLED", "PARTIAL_FILLED", "PASS"}:
        return False
    return _cycle_fill_count(cycle) > 0 or _cycle_rejection_count(cycle) > 0


def _cycle_is_shadow_execution(cycle: dict[str, Any]) -> bool:
    execution = cycle.get("execution") if isinstance(cycle.get("execution"), dict) else {}
    report = execution.get("execution_report") if isinstance(execution.get("execution_report"), dict) else {}
    statuses = {
        str(execution.get("status") or "").upper(),
        str(report.get("status") or "").upper(),
    }
    return "NOT_SUBMITTED_SHADOW" in statuses


def _cycle_explained_no_submit_reason(cycle: dict[str, Any]) -> str:
    if _cycle_is_shadow_execution(cycle):
        return "shadow_only_order_deltas_not_broker_execution"
    order_guard = cycle.get("order_guard") if isinstance(cycle.get("order_guard"), dict) else {}
    guard_status = str(order_guard.get("status") or "").upper()
    guard_reason = str(order_guard.get("reason") or "").strip()
    if guard_status == "SKIP" and bool(order_guard.get("safe_skip")):
        if guard_reason in {
            "fda_veto",
            "runtime_service_policy_filtered_all_orders",
            "no_order_deltas",
            "quant_scores_not_rankable_no_broker_submit",
            "outside_market_session",
            "market_session_skip",
        }:
            return guard_reason
    status = str(cycle.get("status") or "").upper()
    reason = str(cycle.get("reason") or "").strip()
    if status == "SKIP" and reason in {
        "outside_market_session",
        "quant_signal_readiness",
        "market_session_skip",
    }:
        return reason
    return ""


def _cycle_bar_readiness(cycle: dict[str, Any]) -> dict[str, Any] | None:
    readiness = cycle.get("hot_path_bar_readiness")
    return readiness if isinstance(readiness, dict) else None


def _parse_dt(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _cycle_cache_meta(cycle: dict[str, Any]) -> dict[str, Any]:
    bar_readiness = _cycle_bar_readiness(cycle) or {}
    topup = bar_readiness.get("bar_warmup_topup")
    topup = topup if isinstance(topup, dict) else {}
    cache_meta = topup.get("minute_bar_window_cache")
    return cache_meta if isinstance(cache_meta, dict) else {}


def _strict_cadence_summary(
    *,
    report: dict[str, Any],
    cycles: list[Any],
    min_cycles: int,
    expected_ticker_count: int,
    expected_cold_fetch_n: int,
    expected_incremental_fetch_n: int,
    incremental_after_cycle: int,
    max_cycle_start_gap_sec: float,
    require_shadow_only: bool,
) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    cycle_summaries: list[dict[str, Any]] = []

    runtime = report.get("runtime") if isinstance(report.get("runtime"), dict) else {}
    runtime_checks = {
        "kis_mode_virtual": str(runtime.get("kis_mode") or "") == "virtual",
        "live_enabled_false": runtime.get("live_enabled") is False,
        "broker_submit_disabled": runtime.get("broker_submit_enabled") is False,
        "shadow_only": runtime.get("shadow_only") is True,
    }
    if not runtime_checks["kis_mode_virtual"]:
        blockers.append("cadence_runtime_not_kis_virtual")
    if not runtime_checks["live_enabled_false"]:
        blockers.append("cadence_runtime_live_enabled_not_false")
    if require_shadow_only and not runtime_checks["broker_submit_disabled"]:
        blockers.append("cadence_runtime_broker_submit_enabled")
    if require_shadow_only and not runtime_checks["shadow_only"]:
        blockers.append("cadence_runtime_not_shadow_only")

    if len(cycles) < max(1, int(min_cycles)):
        blockers.append("cadence_insufficient_cycles")

    started_at_values: list[tuple[int, datetime]] = []
    previous_cycle_index: int | None = None
    for idx, raw_cycle in enumerate(cycles):
        if not isinstance(raw_cycle, dict):
            blockers.append(f"cadence_cycle_{idx}_not_object")
            continue
        raw_cycle_index = raw_cycle.get("cycle_index")
        if raw_cycle_index is None:
            cycle_index = idx
        else:
            try:
                cycle_index = int(raw_cycle_index)
            except (TypeError, ValueError):
                cycle_index = idx
                blockers.append(f"cadence_cycle_{idx}_cycle_index_invalid")
        if previous_cycle_index is not None and cycle_index != previous_cycle_index + 1:
            blockers.append("cadence_cycle_index_non_contiguous")
        previous_cycle_index = cycle_index
        started_at = _parse_dt(raw_cycle.get("started_at"))
        if started_at is not None:
            started_at_values.append((cycle_index, started_at))
        else:
            blockers.append(f"cadence_cycle_{cycle_index}_started_at_missing_or_invalid")
        status = str(raw_cycle.get("status") or "").upper()
        readiness = _cycle_bar_readiness(raw_cycle)
        readiness_status = str((readiness or {}).get("status") or "").upper()
        cache_meta = _cycle_cache_meta(raw_cycle)
        cache_tickers = cache_meta.get("tickers")
        cache_tickers = cache_tickers if isinstance(cache_tickers, dict) else {}
        failed_tickers = cache_meta.get("failed_tickers")
        failed_tickers = failed_tickers if isinstance(failed_tickers, dict) else {}
        policies = {
            str(ticker): str(meta.get("fetch_policy") or "")
            for ticker, meta in cache_tickers.items()
            if isinstance(meta, dict)
        }
        fetch_ns = {
            str(ticker): meta.get("fetch_n")
            for ticker, meta in cache_tickers.items()
            if isinstance(meta, dict)
        }
        reasons = {
            str(ticker): str(meta.get("reason") or "")
            for ticker, meta in cache_tickers.items()
            if isinstance(meta, dict)
        }
        cycle_summary = {
            "cycle_index": cycle_index,
            "status": status,
            "started_at": raw_cycle.get("started_at"),
            "bar_readiness_status": readiness_status,
            "cache_status": cache_meta.get("status"),
            "cache_reason": cache_meta.get("reason"),
            "ticker_count": len(cache_tickers),
            "failed_tickers": failed_tickers,
            "fetch_policy_counts": {
                policy: list(policies.values()).count(policy)
                for policy in sorted(set(policies.values()))
            },
            "fetch_n_values": sorted({str(value) for value in fetch_ns.values()}),
        }
        cycle_summaries.append(cycle_summary)

        if status != "PASS":
            blockers.append(f"cadence_cycle_{cycle_index}_status_not_pass")
        if readiness is None:
            blockers.append(f"cadence_cycle_{cycle_index}_bar_readiness_missing")
        elif readiness_status != "PASS":
            blockers.append(f"cadence_cycle_{cycle_index}_bar_readiness_not_pass")
        if not cache_meta:
            blockers.append(f"cadence_cycle_{cycle_index}_cache_meta_missing")
            continue
        if str(cache_meta.get("status") or "").upper() != "PASS":
            blockers.append(f"cadence_cycle_{cycle_index}_cache_status_not_pass")
        if failed_tickers:
            blockers.append(f"cadence_cycle_{cycle_index}_cache_failed_tickers")
        if len(cache_tickers) != int(expected_ticker_count):
            blockers.append(f"cadence_cycle_{cycle_index}_ticker_count_mismatch")
        if "fetch_timeout" in set(failed_tickers.values()) or "fetch_timeout" in set(reasons.values()):
            blockers.append(f"cadence_cycle_{cycle_index}_fetch_timeout")

        expected_policy = "incremental" if cycle_index >= int(incremental_after_cycle) else "cold"
        expected_fetch_n = (
            int(expected_incremental_fetch_n)
            if expected_policy == "incremental"
            else int(expected_cold_fetch_n)
        )
        if cache_tickers:
            bad_policy = [
                ticker for ticker, policy in policies.items() if policy != expected_policy
            ]
            bad_fetch_n = [
                ticker
                for ticker, value in fetch_ns.items()
                if str(value) != str(expected_fetch_n)
            ]
            if bad_policy:
                blockers.append(f"cadence_cycle_{cycle_index}_{expected_policy}_policy_mismatch")
            if bad_fetch_n:
                blockers.append(f"cadence_cycle_{cycle_index}_fetch_n_mismatch")

    start_gaps: list[float] = []
    for (left_index, left), (right_index, right) in zip(
        started_at_values,
        started_at_values[1:],
    ):
        if right_index != left_index + 1:
            continue
        gap = (right - left).total_seconds()
        start_gaps.append(float(gap))
        if gap > float(max_cycle_start_gap_sec):
            blockers.append("cadence_cycle_start_gap_exceeds_limit")

    return {
        "strict_enabled": True,
        "runtime_checks": runtime_checks,
        "min_cycles": int(min_cycles),
        "expected_ticker_count": int(expected_ticker_count),
        "expected_cold_fetch_n": int(expected_cold_fetch_n),
        "expected_incremental_fetch_n": int(expected_incremental_fetch_n),
        "incremental_after_cycle": int(incremental_after_cycle),
        "max_cycle_start_gap_sec": float(max_cycle_start_gap_sec),
        "start_gaps_sec": start_gaps,
        "cycles": cycle_summaries,
        "blockers": sorted(set(blockers)),
        "warnings": warnings,
    }


def _summarize_report(
    path: Path,
    report: dict[str, Any],
    *,
    strict_cadence: bool = False,
    min_cycles: int = 2,
    expected_ticker_count: int = 30,
    expected_cold_fetch_n: int = 61,
    expected_incremental_fetch_n: int = 6,
    incremental_after_cycle: int = 1,
    max_cycle_start_gap_sec: float = 75.0,
    require_shadow_only: bool = False,
) -> dict[str, Any]:
    cycles = (((report.get("stages") or {}).get("cycles") or {}).get("items") or [])
    if not isinstance(cycles, list):
        cycles = []
    score_nonempty_cycles = 0
    score_rankable_cycles = 0
    warmup_or_blocked_cycles = 0
    no_order_delta_cycles = 0
    order_delta_count = 0
    execution_cycles = 0
    fill_count = 0
    rejection_count = 0
    shadow_order_delta_cycles = 0
    explained_no_submit_order_delta_cycles = 0
    unexplained_order_delta_no_broker_cycles = 0
    explained_no_submit_reasons: dict[str, int] = {}
    hot_path_bar_readiness_present_cycles = 0
    hot_path_bar_readiness_pass_cycles = 0
    for cycle in cycles:
        if not isinstance(cycle, dict):
            continue
        quant_output = _cycle_quant_output(cycle)
        stats = _score_stats(quant_output.get("scores"))
        if stats["finite_score_count"] > 0:
            score_nonempty_cycles += 1
        if stats["rankable"]:
            score_rankable_cycles += 1
        mode = str(quant_output.get("mode") or "").lower()
        if mode in {"warmup", "blocked", "passive"}:
            warmup_or_blocked_cycles += 1
        deltas = _cycle_order_deltas(cycle)
        order_delta_count += len(deltas)
        order_guard = cycle.get("order_guard") if isinstance(cycle.get("order_guard"), dict) else {}
        if order_guard.get("reason") == "no_order_deltas":
            no_order_delta_cycles += 1
        if _cycle_has_broker_execution(cycle):
            execution_cycles += 1
        if deltas and _cycle_is_shadow_execution(cycle):
            shadow_order_delta_cycles += 1
        if deltas and not _cycle_has_broker_execution(cycle):
            explained_reason = _cycle_explained_no_submit_reason(cycle)
            if explained_reason:
                explained_no_submit_order_delta_cycles += 1
                explained_no_submit_reasons[explained_reason] = (
                    explained_no_submit_reasons.get(explained_reason, 0) + 1
                )
            else:
                unexplained_order_delta_no_broker_cycles += 1
        fill_count += _cycle_fill_count(cycle)
        rejection_count += _cycle_rejection_count(cycle)
        bar_readiness = _cycle_bar_readiness(cycle)
        if bar_readiness is not None:
            hot_path_bar_readiness_present_cycles += 1
            if str(bar_readiness.get("status") or "").upper() == "PASS":
                hot_path_bar_readiness_pass_cycles += 1

    hot_path_bar_readiness_missing_cycles = (
        len(cycles) - hot_path_bar_readiness_present_cycles
    )
    required_bundle_id = ((report.get("params") or {}).get("required_bundle_id") or "")
    summary = {
        "path": str(path.relative_to(REPO_ROOT)),
        "status": report.get("status"),
        "generated_at": report.get("generated_at"),
        "required_bundle_id": required_bundle_id,
        "cycle_count": len(cycles),
        "score_nonempty_cycles": score_nonempty_cycles,
        "score_rankable_cycles": score_rankable_cycles,
        "warmup_or_blocked_cycles": warmup_or_blocked_cycles,
        "no_order_delta_cycles": no_order_delta_cycles,
        "order_delta_count": order_delta_count,
        "execution_cycles": execution_cycles,
        "fill_count": fill_count,
        "rejection_count": rejection_count,
        "shadow_order_delta_cycles": shadow_order_delta_cycles,
        "explained_no_submit_order_delta_cycles": explained_no_submit_order_delta_cycles,
        "unexplained_order_delta_no_broker_cycles": unexplained_order_delta_no_broker_cycles,
        "explained_no_submit_reasons": explained_no_submit_reasons,
        "hot_path_bar_readiness_present_cycles": hot_path_bar_readiness_present_cycles,
        "hot_path_bar_readiness_pass_cycles": hot_path_bar_readiness_pass_cycles,
        "hot_path_bar_readiness_missing_cycles": hot_path_bar_readiness_missing_cycles,
        "cadence": {
            "strict_enabled": False,
        },
        "blockers": [],
        "warnings": [],
    }
    if report.get("status") == "PASS" and required_bundle_id:
        if cycles and score_nonempty_cycles == 0:
            summary["blockers"].append("pass_with_zero_quant_scores")
        if cycles and score_rankable_cycles == 0:
            summary["blockers"].append("pass_with_no_rankable_quant_scores")
        if cycles and warmup_or_blocked_cycles == len(cycles):
            summary["blockers"].append("pass_with_all_quant_warmup_or_blocked")
        if cycles and no_order_delta_cycles == len(cycles) and execution_cycles == 0:
            if score_rankable_cycles == 0 or warmup_or_blocked_cycles == len(cycles):
                summary["blockers"].append("pass_with_only_no_order_delta_cycles")
            else:
                summary["warnings"].append(
                    "pass_with_only_no_order_delta_cycles_evidence_limited"
                )
        if cycles and order_delta_count > 0 and execution_cycles == 0:
            if unexplained_order_delta_no_broker_cycles > 0:
                summary["blockers"].append("pass_with_order_deltas_without_broker_execution")
            elif shadow_order_delta_cycles > 0:
                summary["warnings"].append("shadow_only_order_deltas_not_broker_execution")
            else:
                summary["blockers"].append(
                    "pass_with_order_deltas_no_broker_execution_explained_by_guard"
                )
        if execution_cycles > 0 and hot_path_bar_readiness_missing_cycles > 0:
            summary["warnings"].append("hot_path_bar_readiness_missing_legacy_report")
    if strict_cadence:
        cadence = _strict_cadence_summary(
            report=report,
            cycles=cycles,
            min_cycles=min_cycles,
            expected_ticker_count=expected_ticker_count,
            expected_cold_fetch_n=expected_cold_fetch_n,
            expected_incremental_fetch_n=expected_incremental_fetch_n,
            incremental_after_cycle=incremental_after_cycle,
            max_cycle_start_gap_sec=max_cycle_start_gap_sec,
            require_shadow_only=require_shadow_only,
        )
        summary["cadence"] = cadence
        summary["blockers"].extend(cadence["blockers"])
        summary["warnings"].extend(cadence["warnings"])
    return summary


def build_report(
    *,
    bundle_id: str = "",
    report_dir: str | Path = "artifacts/reports/paper_auto_trading",
    pattern: str = "paper_auto_trade_*.json",
    generated_date: str = "",
    strict_cadence: bool = False,
    min_cycles: int = 2,
    expected_ticker_count: int = 30,
    expected_cold_fetch_n: int = 61,
    expected_incremental_fetch_n: int = 6,
    incremental_after_cycle: int = 1,
    max_cycle_start_gap_sec: float = 75.0,
    require_shadow_only: bool = False,
) -> dict[str, Any]:
    base_dir = Path(report_dir)
    if not base_dir.is_absolute():
        base_dir = REPO_ROOT / base_dir
    report_summaries: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for path in _iter_reports(base_dir, pattern):
        try:
            report = _load_json(path)
            required_bundle_id = str(
                ((report.get("params") or {}).get("required_bundle_id") or "")
            )
            if bundle_id and required_bundle_id != bundle_id:
                continue
            if not _matches_generated_date(path, report, generated_date):
                continue
            summary = _summarize_report(
                path,
                report,
                strict_cadence=strict_cadence,
                min_cycles=min_cycles,
                expected_ticker_count=expected_ticker_count,
                expected_cold_fetch_n=expected_cold_fetch_n,
                expected_incremental_fetch_n=expected_incremental_fetch_n,
                incremental_after_cycle=incremental_after_cycle,
                max_cycle_start_gap_sec=max_cycle_start_gap_sec,
                require_shadow_only=require_shadow_only,
            )
            report_summaries.append(summary)
            if summary["blockers"]:
                failures.append(summary)
        except Exception as e:
            failures.append({
                "path": str(path.relative_to(REPO_ROOT)),
                "error_type": type(e).__name__,
                "error": str(e),
            })
    if bundle_id and not report_summaries:
        failures.append({
            "reason": "paper_auto_report_missing",
            "bundle_id": bundle_id,
            "report_dir": str(base_dir.relative_to(REPO_ROOT)),
            "pattern": pattern,
            "generated_date": str(generated_date or ""),
        })
    return {
        "schema_version": "1.0.0",
        "status": "PASS" if not failures else "BLOCKED",
        "action": "validate_paper_auto_report_semantics",
        "generated_at": datetime.now(KST).isoformat(),
        "bundle_id": bundle_id,
        "report_dir": str(base_dir.relative_to(REPO_ROOT)),
        "pattern": pattern,
        "scan_mode": "recursive",
        "generated_date": str(generated_date or ""),
        "strict_cadence": {
            "enabled": bool(strict_cadence),
            "min_cycles": int(min_cycles),
            "expected_ticker_count": int(expected_ticker_count),
            "expected_cold_fetch_n": int(expected_cold_fetch_n),
            "expected_incremental_fetch_n": int(expected_incremental_fetch_n),
            "incremental_after_cycle": int(incremental_after_cycle),
            "max_cycle_start_gap_sec": float(max_cycle_start_gap_sec),
            "require_shadow_only": bool(require_shadow_only),
        },
        "report_count": len(report_summaries),
        "failure_count": len(failures),
        "failures": failures,
        "reports": report_summaries,
        "safety": {
            "external_api_called": False,
            "env_read": False,
            "registry_mutated": False,
            "live_trading_allowed": False,
        },
    }


def _write_report(report: dict[str, Any]) -> Path:
    out_dir = REPO_ROOT / "artifacts" / "reports" / "paper_auto_semantics"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    suffix = report["bundle_id"] or "ALL"
    path = out_dir / f"paper_auto_semantics_{suffix}_{ts}.json"
    report["report_path"] = str(path)
    report["report_path_relative"] = str(path.relative_to(REPO_ROOT))
    with path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = build_report(
        bundle_id=str(args.bundle_id),
        report_dir=args.report_dir,
        pattern=str(args.pattern),
        generated_date=str(args.generated_date),
        strict_cadence=bool(args.strict_cadence),
        min_cycles=int(args.min_cycles),
        expected_ticker_count=int(args.expected_ticker_count),
        expected_cold_fetch_n=int(args.expected_cold_fetch_n),
        expected_incremental_fetch_n=int(args.expected_incremental_fetch_n),
        incremental_after_cycle=int(args.incremental_after_cycle),
        max_cycle_start_gap_sec=float(args.max_cycle_start_gap_sec),
        require_shadow_only=bool(args.require_shadow_only),
    )
    if args.write_report:
        _write_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

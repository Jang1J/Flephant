#!/usr/bin/env python
"""Summarize one paper-auto trading day from local reports.

This is a post-close, read-only artifact summarizer. It never reads `.env`,
never calls external APIs, and never mutates registries.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_KST = ZoneInfo("Asia/Seoul")
_DEFAULT_REPORT_ROOT = ROOT / "artifacts" / "reports" / "paper_auto_trading"
_DEFAULT_OUTPUT_DIR = ROOT / "artifacts" / "reports" / "paper_auto_daily_summary"
_EXPLAINED_NO_SUBMIT_REASONS = frozenset({
    "fda_veto",
    "runtime_service_policy_filtered_all_orders",
    "quant_signal_readiness",
    "no_order_deltas",
    "RISK_FAST_TRIGGER",
    "QUANT_ANOMALY",
    "outside_market_session",
    "market_session_skip",
})


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"paper-auto report must be a JSON object: {path}")
    return data


def _report_matches_date(path: Path, data: dict[str, Any], generated_date: str) -> bool:
    if path.name.startswith(f"paper_auto_trade_{generated_date}_"):
        return True
    generated_at = str(data.get("generated_at") or "")
    return generated_at.startswith(
        f"{generated_date[:4]}-{generated_date[4:6]}-{generated_date[6:8]}"
    )


def _iter_reports(report_root: Path, generated_date: str) -> list[tuple[Path, dict[str, Any]]]:
    reports: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(report_root.rglob("paper_auto_trade_*.json")):
        data = _load_json(path)
        if _report_matches_date(path, data, generated_date):
            reports.append((path, data))
    return reports


def _report_bundle_id(data: dict[str, Any]) -> str:
    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    return str(params.get("required_bundle_id") or "").strip()


def _cycle_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    cycles_stage = ((data.get("stages") or {}).get("cycles") or {})
    if isinstance(cycles_stage.get("items"), list):
        return [item for item in cycles_stage["items"] if isinstance(item, dict)]
    if isinstance(cycles_stage.get("cycle_reports"), list):
        return [item for item in cycles_stage["cycle_reports"] if isinstance(item, dict)]
    if isinstance(data.get("cycles"), list):
        return [item for item in data["cycles"] if isinstance(item, dict)]
    return []


def _final_decision(cycle: dict[str, Any]) -> dict[str, Any]:
    direct = cycle.get("final_decision")
    if isinstance(direct, dict):
        return direct
    hot_result = cycle.get("hot_result") if isinstance(cycle.get("hot_result"), dict) else {}
    direct_hot = hot_result.get("final_decision") if isinstance(hot_result, dict) else {}
    if isinstance(direct_hot, dict):
        return direct_hot
    nested = (((cycle.get("hot_result") or {}).get("fda_result") or {}).get("final_decision") or {})
    return nested if isinstance(nested, dict) else {}


def _quant_output(cycle: dict[str, Any]) -> dict[str, Any]:
    quant = ((cycle.get("hot_result") or {}).get("quant_output") or {})
    return quant if isinstance(quant, dict) else {}


def _execution_report(cycle: dict[str, Any]) -> dict[str, Any]:
    report = ((cycle.get("execution") or {}).get("execution_report") or {})
    return report if isinstance(report, dict) else {}


def _execution_lists(cycle: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    report = _execution_report(cycle)
    fills = report.get("fills") if isinstance(report.get("fills"), list) else []
    rejections = (
        report.get("rejections") if isinstance(report.get("rejections"), list) else []
    )
    return fills, rejections


def _is_broker_execution(cycle: dict[str, Any]) -> bool:
    status = str(_execution_report(cycle).get("status") or "").upper()
    if status in {"NOT_SUBMITTED_SHADOW", "SKIPPED", "NO_ORDERS", "FAIL", "FAILED", "REJECTED"}:
        return False
    if status not in {"SUBMITTED", "FILLED", "PARTIAL_FILLED", "PASS"}:
        return False
    fills, rejections = _execution_lists(cycle)
    return bool(fills or rejections)


def _submitted_count_and_source(cycle: dict[str, Any]) -> tuple[int, str]:
    if "submitted_order_deltas" in cycle:
        submitted = cycle.get("submitted_order_deltas")
        if isinstance(submitted, list):
            return len(submitted), "submitted_order_deltas"
        return 0, "submitted_order_deltas_invalid"
    if _is_broker_execution(cycle):
        fills, rejections = _execution_lists(cycle)
        return len(fills) + len(rejections), "legacy_execution_report_fills_rejections"
    return 0, "none"


def _is_explained_no_submit_reason(reason: str) -> bool:
    return str(reason or "").strip() in _EXPLAINED_NO_SUBMIT_REASONS


def summarize_report(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    cycles = _cycle_items(data)
    cycle_status_counts = Counter(str(item.get("status") or "null") for item in cycles)
    quant_mode_counts = Counter(str(_quant_output(item).get("mode") or "null") for item in cycles)
    score_nonempty_cycles = 0
    rankable_score_cycles = 0
    order_delta_count = 0
    submitted_order_delta_count = 0
    execution_cycles = 0
    fill_count = 0
    rejection_count = 0
    order_delta_no_submit_cycles = 0
    explained_order_delta_no_submit_cycles = 0
    unexplained_order_delta_no_submit_cycles = 0
    order_delta_no_submit_reasons: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    submitted_count_sources: Counter[str] = Counter()
    hot_path_bar_readiness_present_cycles = 0
    hot_path_bar_readiness_pass_cycles = 0

    for cycle in cycles:
        scores = _quant_output(cycle).get("scores") or {}
        if isinstance(scores, dict) and scores:
            score_nonempty_cycles += 1
            if len(set(scores.values())) > 1:
                rankable_score_cycles += 1
        decision = _final_decision(cycle)
        order_deltas = decision.get("order_deltas") or []
        if isinstance(order_deltas, list):
            order_delta_count += len(order_deltas)
        submitted_count, submitted_source = _submitted_count_and_source(cycle)
        submitted_order_delta_count += submitted_count
        submitted_count_sources[submitted_source] += 1
        execution = _execution_report(cycle)
        if _is_broker_execution(cycle):
            execution_cycles += 1
        readiness = cycle.get("hot_path_bar_readiness")
        if isinstance(readiness, dict):
            hot_path_bar_readiness_present_cycles += 1
            if str(readiness.get("status") or "").upper() == "PASS":
                hot_path_bar_readiness_pass_cycles += 1
        fills = execution.get("fills") or []
        rejections = execution.get("rejections") or []
        if isinstance(fills, list):
            fill_count += len(fills)
        if isinstance(rejections, list):
            rejection_count += len(rejections)
        reason = (
            ((cycle.get("order_guard") or {}).get("reason"))
            or decision.get("reason_code")
            or "none"
        )
        reason_counts[str(reason)] += 1
        if isinstance(order_deltas, list) and order_deltas and submitted_count == 0 and not _is_broker_execution(cycle):
            order_delta_no_submit_cycles += 1
            order_delta_no_submit_reasons[str(reason)] += 1
            if _is_explained_no_submit_reason(str(reason)):
                explained_order_delta_no_submit_cycles += 1
            else:
                unexplained_order_delta_no_submit_cycles += 1

    false_pass_suspect = (
        str(data.get("status")) == "PASS"
        and bool(cycles)
        and score_nonempty_cycles == 0
        and order_delta_count == 0
        and execution_cycles == 0
        and fill_count == 0
    )
    params = data.get("params") or {}
    hot_path_bar_readiness_missing_cycles = (
        len(cycles) - hot_path_bar_readiness_present_cycles
    )
    warnings: list[str] = []
    if (
        str(data.get("status") or "").upper() == "PASS"
        and execution_cycles > 0
        and hot_path_bar_readiness_missing_cycles > 0
    ):
        warnings.append("hot_path_bar_readiness_missing_legacy_report")
    return {
        "path": _repo_relative(path),
        "status": data.get("status"),
        "generated_at": data.get("generated_at"),
        "track_id": params.get("track_id"),
        "policy_hash": params.get("policy_hash"),
        "required_bundle_id": params.get("required_bundle_id"),
        "cycles_requested": params.get("cycles"),
        "cycle_count": len(cycles),
        "cycle_status_counts": dict(cycle_status_counts),
        "quant_mode_counts": dict(quant_mode_counts),
        "score_nonempty_cycles": score_nonempty_cycles,
        "rankable_score_cycles": rankable_score_cycles,
        "order_delta_count": order_delta_count,
        "submitted_order_delta_count": submitted_order_delta_count,
        "submitted_count_source_counts": dict(submitted_count_sources),
        "order_delta_no_submit_cycles": order_delta_no_submit_cycles,
        "explained_order_delta_no_submit_cycles": explained_order_delta_no_submit_cycles,
        "unexplained_order_delta_no_submit_cycles": unexplained_order_delta_no_submit_cycles,
        "order_delta_no_submit_reasons": dict(order_delta_no_submit_reasons),
        "execution_cycles": execution_cycles,
        "fill_count": fill_count,
        "rejection_count": rejection_count,
        "hot_path_bar_readiness_present_cycles": hot_path_bar_readiness_present_cycles,
        "hot_path_bar_readiness_pass_cycles": hot_path_bar_readiness_pass_cycles,
        "hot_path_bar_readiness_missing_cycles": hot_path_bar_readiness_missing_cycles,
        "reason_counts": dict(reason_counts),
        "false_pass_suspect": false_pass_suspect,
        "warnings": warnings,
    }


def build_summary(
    *,
    generated_date: str,
    bundle_id: str,
    report_root: Path,
) -> dict[str, Any]:
    raw_reports = _iter_reports(report_root, generated_date)
    bundle_id = str(bundle_id or "").strip()
    skipped_bundle_mismatch_reports: list[dict[str, str]] = []
    filtered_reports: list[tuple[Path, dict[str, Any]]] = []
    for path, data in raw_reports:
        observed_bundle_id = _report_bundle_id(data)
        if bundle_id and observed_bundle_id != bundle_id:
            skipped_bundle_mismatch_reports.append({
                "path": _repo_relative(path),
                "required_bundle_id": observed_bundle_id,
            })
            continue
        filtered_reports.append((path, data))

    reports = [summarize_report(path, data) for path, data in filtered_reports]
    false_pass_reports = [item["path"] for item in reports if item["false_pass_suspect"]]
    order_delta_no_submit_reason_counts: Counter[str] = Counter()
    reason_counts_total: Counter[str] = Counter()
    for item in reports:
        order_delta_no_submit_reason_counts.update(
            item.get("order_delta_no_submit_reasons") or {}
        )
        reason_counts_total.update(item.get("reason_counts") or {})
    broker_tracks = [
        item
        for item in reports
        if int(item.get("execution_cycles") or 0) > 0
        and str(item.get("status") or "").upper() == "PASS"
        and str(item.get("track_id") or "").strip()
        and not str(item.get("track_id") or "").endswith("_SMOKE")
        and int(item.get("cycle_count") or 0) > 1
    ]
    totals = {
        "report_count": len(reports),
        "raw_report_count": len(raw_reports),
        "skipped_bundle_mismatch_count": len(skipped_bundle_mismatch_reports),
        "cycle_count": sum(int(item.get("cycle_count") or 0) for item in reports),
        "score_nonempty_cycles": sum(int(item.get("score_nonempty_cycles") or 0) for item in reports),
        "rankable_score_cycles": sum(int(item.get("rankable_score_cycles") or 0) for item in reports),
        "order_delta_count": sum(int(item.get("order_delta_count") or 0) for item in reports),
        "submitted_order_delta_count": sum(int(item.get("submitted_order_delta_count") or 0) for item in reports),
        "order_delta_no_submit_cycles": sum(int(item.get("order_delta_no_submit_cycles") or 0) for item in reports),
        "explained_order_delta_no_submit_cycles": sum(int(item.get("explained_order_delta_no_submit_cycles") or 0) for item in reports),
        "unexplained_order_delta_no_submit_cycles": sum(int(item.get("unexplained_order_delta_no_submit_cycles") or 0) for item in reports),
        "order_delta_no_submit_reason_counts": dict(order_delta_no_submit_reason_counts),
        "reason_counts": dict(reason_counts_total),
        "execution_cycles": sum(int(item.get("execution_cycles") or 0) for item in reports),
        "fill_count": sum(int(item.get("fill_count") or 0) for item in reports),
        "rejection_count": sum(int(item.get("rejection_count") or 0) for item in reports),
        "hot_path_bar_readiness_present_cycles": sum(int(item.get("hot_path_bar_readiness_present_cycles") or 0) for item in reports),
        "hot_path_bar_readiness_pass_cycles": sum(int(item.get("hot_path_bar_readiness_pass_cycles") or 0) for item in reports),
        "hot_path_bar_readiness_missing_cycles": sum(int(item.get("hot_path_bar_readiness_missing_cycles") or 0) for item in reports),
    }
    blockers: list[str] = []
    warnings: list[str] = []
    if not reports:
        blockers.append("paper_auto_report_missing")
    if false_pass_reports:
        blockers.append("false_pass_suspect")
    if (
        int(totals["order_delta_count"]) > 0
        and int(totals["submitted_order_delta_count"]) == 0
        and int(totals["execution_cycles"]) == 0
    ):
        if int(totals["unexplained_order_delta_no_submit_cycles"]) > 0:
            blockers.append("unexplained_order_deltas_without_broker_execution")
        else:
            blockers.append("explained_order_deltas_without_broker_execution")
    for warning in sorted({
        warning
        for item in reports
        for warning in list(item.get("warnings") or [])
    }):
        warnings.append(str(warning))
    broker_track_ids = list(dict.fromkeys(str(item.get("track_id")) for item in broker_tracks))
    if len(broker_track_ids) < 2:
        warnings.append("not_ab_comparison")
    status = "PASS" if not blockers else "BLOCKED"
    evidence_scope = "ab_comparison" if len(broker_track_ids) >= 2 else "single_track_runtime"
    remaining_policy_gaps: list[str] = []
    if "runtime_service_policy_filtered_all_orders" in order_delta_no_submit_reason_counts:
        remaining_policy_gaps.append("runtime_policy_filtered_all_orders")
    if (
        "RISK_FAST_TRIGGER" in order_delta_no_submit_reason_counts
        or "fda_veto" in order_delta_no_submit_reason_counts
    ):
        remaining_policy_gaps.append("risk_fast_or_fda_veto_no_submit")
    if int(totals["order_delta_count"]) > 0 and int(totals["submitted_order_delta_count"]) == 0:
        remaining_policy_gaps.append("broker_submit_absent_for_order_deltas")
    if len(broker_track_ids) < 2:
        remaining_policy_gaps.append("not_ab_comparison")

    remaining_quant_gaps: list[str] = []
    if false_pass_reports:
        remaining_quant_gaps.append("zero_or_non_rankable_score_pass_report_present")
    if int(totals["rankable_score_cycles"]) < int(totals["score_nonempty_cycles"]):
        remaining_quant_gaps.append("rankable_score_gap")
    return {
        "schema_version": "1.0.0",
        "status": status,
        "action": "summarize_paper_auto_day",
        "generated_at": datetime.now(_KST).isoformat(),
        "generated_date": generated_date,
        "bundle_id": bundle_id,
        "report_root": _repo_relative(report_root),
        "totals": totals,
        "interpretation": {
            "evidence_scope": evidence_scope,
            "ab_comparison_valid": len(broker_track_ids) >= 2,
            "broker_track_count": len(broker_track_ids),
            "broker_tracks": broker_track_ids,
            "explained_no_submit_reasons": dict(order_delta_no_submit_reason_counts),
            "unexplained_no_submit_count": totals["unexplained_order_delta_no_submit_cycles"],
            "remaining_policy_gaps": remaining_policy_gaps,
            "remaining_quant_gaps": remaining_quant_gaps,
            "safe_statement": (
                "no broker execution evidence; inspect no-submit policy reasons"
                if (
                    "explained_order_deltas_without_broker_execution" in blockers
                    or "unexplained_order_deltas_without_broker_execution" in blockers
                )
                else
                "MAIN 단독 runtime stability evidence"
                if evidence_scope == "single_track_runtime"
                else "multi-track paper comparison evidence"
            ),
        },
        "blockers": blockers,
        "warnings": warnings,
        "false_pass_reports": false_pass_reports,
        "skipped_bundle_mismatch_reports": skipped_bundle_mismatch_reports,
        "reports": reports,
        "safety": {
            "external_api_called": False,
            "env_read": False,
            "registry_mutated": False,
            "live_trading_allowed": False,
        },
    }


def write_summary(output_dir: Path, summary: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    date_key = str(summary["generated_date"])
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"paper_auto_daily_summary_{date_key}_{ts}.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize local paper-auto reports for one date")
    parser.add_argument("--generated-date", required=True, help="YYYYMMDD")
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--report-root", default=str(_DEFAULT_REPORT_ROOT))
    parser.add_argument("--output-dir", default=str(_DEFAULT_OUTPUT_DIR))
    parser.add_argument("--no-write-report", action="store_true")
    args = parser.parse_args(argv)

    summary = build_summary(
        generated_date=str(args.generated_date),
        bundle_id=str(args.bundle_id),
        report_root=Path(str(args.report_root)),
    )
    if not bool(args.no_write_report):
        path = write_summary(Path(str(args.output_dir)), summary)
        summary["report_path"] = str(path)
        summary["report_path_relative"] = _repo_relative(path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

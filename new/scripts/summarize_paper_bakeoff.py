#!/usr/bin/env python
"""Summarize a paper bake-off validation report as JSON, CSV, and Markdown."""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parents[2]


def _repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be object: {path}")
    return data


def _best_report(candidate_report: dict[str, Any]) -> dict[str, Any]:
    reports = candidate_report.get("reports")
    if isinstance(reports, list) and reports:
        dict_reports = [item for item in reports if isinstance(item, dict)]
        if dict_reports:
            return dict_reports[-1]
    return {}


def _cadence(report: dict[str, Any]) -> dict[str, Any]:
    cadence = report.get("cadence")
    return cadence if isinstance(cadence, dict) else {}


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.95)))
    return float(ordered[index])


def _p50(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[len(ordered) // 2])


def _append_unique(values: list[str], raw: Any) -> None:
    if raw is None:
        return
    text = str(raw).strip()
    if text and text not in values:
        values.append(text)


def _candidate_blockers(candidate_report: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    raw_blockers = candidate_report.get("blockers")
    if isinstance(raw_blockers, list):
        for item in raw_blockers:
            _append_unique(blockers, item)
    _append_unique(blockers, candidate_report.get("reason"))

    failures = candidate_report.get("failures")
    if isinstance(failures, list):
        for failure in failures:
            if isinstance(failure, dict):
                _append_unique(blockers, failure.get("reason"))
                nested_blockers = failure.get("blockers")
                if isinstance(nested_blockers, list):
                    for item in nested_blockers:
                        _append_unique(blockers, item)
                if not failure.get("reason") and not nested_blockers:
                    error_type = failure.get("error_type")
                    error = failure.get("error")
                    if error_type and error:
                        _append_unique(blockers, f"{error_type}:{error}")
                    else:
                        _append_unique(blockers, error_type or error)
            else:
                _append_unique(blockers, failure)
    return blockers


def build_summary(validation: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for candidate_report in validation.get("candidate_reports", []):
        if not isinstance(candidate_report, dict):
            continue
        bundle = str(candidate_report.get("bundle_id") or "")
        report = _best_report(candidate_report)
        cadence = _cadence(report)
        start_gaps = [
            float(value)
            for value in cadence.get("start_gaps_sec", [])
            if isinstance(value, (int, float))
        ]
        cycles = cadence.get("cycles") if isinstance(cadence.get("cycles"), list) else []
        warm_cycles = [item for item in cycles if isinstance(item, dict) and int(item.get("cycle_index", 0)) >= 1]
        incremental_cycles = [
            item for item in warm_cycles
            if isinstance(item.get("fetch_policy_counts"), dict)
            and item["fetch_policy_counts"].get("incremental", 0) >= 27
        ]
        failed_tickers = sum(
            len(item.get("failed_tickers") or {})
            for item in cycles
            if isinstance(item, dict)
        )
        row = {
            "bundle": bundle,
            "status": candidate_report.get("status"),
            "completed_cycles": report.get("cycle_count", 0),
            "warm_incremental_rate": (
                round(len(incremental_cycles) / len(warm_cycles), 4)
                if warm_cycles else None
            ),
            "p50_gap": _p50(start_gaps),
            "p95_gap": _p95(start_gaps),
            "failed_tickers": failed_tickers,
            "score_count": report.get("score_nonempty_cycles", 0),
            "rankable_cycles": report.get("score_rankable_cycles", 0),
            "order_candidates": report.get("order_delta_count", 0),
            "fda_veto": (report.get("explained_no_submit_reasons") or {}).get("fda_veto", 0),
            "broker_submits": report.get("execution_cycles", 0),
            "verdict": "PASS" if candidate_report.get("status") == "PASS" else "BLOCKED",
            "blockers": ";".join(_candidate_blockers(candidate_report)),
        }
        rows.append(row)
    return {
        "schema_version": "1.0.0",
        "status": "PASS" if validation.get("status") == "PASS" else "BLOCKED",
        "action": "summarize_paper_bakeoff",
        "generated_at": datetime.now(KST).isoformat(),
        "date": validation.get("date"),
        "rows": rows,
        "safety": {
            "external_api_called": False,
            "env_read": False,
            "registry_mutated": False,
            "live_trading_allowed": False,
        },
    }


def _write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    fieldnames = [
        "bundle",
        "completed_cycles",
        "warm_incremental_rate",
        "p50_gap",
        "p95_gap",
        "failed_tickers",
        "score_count",
        "rankable_cycles",
        "order_candidates",
        "fda_veto",
        "broker_submits",
        "verdict",
        "blockers",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})


def _write_markdown(summary: dict[str, Any], path: Path) -> None:
    headers = [
        "bundle",
        "completed_cycles",
        "warm_incremental_rate",
        "p95_gap",
        "failed_tickers",
        "score_count",
        "order_candidates",
        "broker_submits",
        "verdict",
    ]
    lines = [
        f"# Paper Bake-Off Summary {summary.get('date') or ''}".strip(),
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in summary.get("rows", []):
        lines.append("| " + " | ".join(str(row.get(key, "")) for key in headers) + " |")
    lines.extend([
        "",
        "Safety: shadow-only paper rehearsal. No live trading, no production registry mutation.",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_summary(summary: dict[str, Any], output_dir: str | Path | None = None) -> dict[str, str]:
    date = str(summary.get("date") or "20260604")
    out_dir = Path(output_dir) if output_dir else (
        ROOT / "artifacts" / "reports" / f"paper_bakeoff_{date}"
    )
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "summary.json"
    csv_path = out_dir / "summary.csv"
    md_path = out_dir / "summary.md"
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    _write_csv(summary.get("rows", []), csv_path)
    _write_markdown(summary, md_path)
    return {
        "summary_json": _repo_relative(json_path),
        "summary_csv": _repo_relative(csv_path),
        "summary_md": _repo_relative(md_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize paper bake-off validation")
    parser.add_argument("--validation-report", required=True)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args(argv)

    validation = _load_json(_resolve(args.validation_report))
    summary = build_summary(validation)
    if args.write_report:
        summary["paths"] = write_summary(summary, args.output_dir or None)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

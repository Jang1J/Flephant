#!/usr/bin/env python
"""Community source health artifact.

Reads an existing community_live_risk_smoke report and emits a standalone
source-health report. This script never reads .env; it only inspects local JSON
evidence already produced by a smoke run.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_KST = ZoneInfo("Asia/Seoul")
_DEFAULT_INPUT_DIR = ROOT / "artifacts" / "reports" / "community_live_risk"
_DEFAULT_OUTPUT_DIR = ROOT / "artifacts" / "reports" / "community_source_health"


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _latest_community_report(input_dir: Path) -> Path | None:
    candidates = sorted(input_dir.glob("community_live_risk_smoke_*.json"))
    return candidates[-1] if candidates else None


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be object: {path}")
    return data


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _provider_coverage(
    requested_providers: list[str],
    provider_counts: dict[str, Any],
) -> dict[str, Any]:
    requested = list(
        dict.fromkeys(
            str(provider).strip()
            for provider in requested_providers
            if str(provider).strip()
        )
    )
    observed = {
        provider: _as_int(provider_counts.get(provider, 0))
        for provider in requested
    }
    missing = [provider for provider, count in observed.items() if count <= 0]
    return {
        "requested": requested,
        "observed": observed,
        "missing_requested_providers": missing,
        "status": "WARN" if missing else "PASS",
    }


def _status_from_report(report: dict[str, Any]) -> tuple[str, list[str]]:
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    embedded = (
        report.get("community_source_health")
        if isinstance(report.get("community_source_health"), dict)
        else {}
    )
    raw_count = _as_int(metrics.get("raw_post_count", embedded.get("raw_post_count")))
    valid_count = _as_int(
        metrics.get("valid_event_count", embedded.get("valid_event_count", embedded.get("normalized_event_count")))
    )
    reason_codes = (
        embedded.get("risk_sidecar", {}).get("reason_codes_emitted", [])
        if isinstance(embedded.get("risk_sidecar"), dict)
        else []
    )
    blockers: list[str] = []
    warnings: list[str] = []

    if raw_count <= 0:
        blockers.append("raw_count_zero")
    if valid_count <= 0:
        blockers.append("valid_event_count_zero")
    if not reason_codes:
        warnings.append("risk_reason_code_not_emitted")
    if report.get("internal_fake_naver"):
        warnings.append("internal_fake_naver_smoke_only")
    if report.get("is_mock"):
        blockers.append("community_connector_mock_mode")
    ts_conf = (
        metrics.get("timestamp_confidence_counts")
        or embedded.get("timestamp_confidence_counts")
        or {}
    )
    if isinstance(ts_conf, dict) and _as_int(ts_conf.get("low")) > 0:
        warnings.append("timestamp_confidence_low")
    provider_counts = metrics.get("provider_counts") or embedded.get("provider_counts") or {}
    providers = report.get("providers") or embedded.get("providers") or sorted(provider_counts)
    embedded_coverage = embedded.get("provider_coverage")
    coverage = (
        embedded_coverage
        if isinstance(embedded_coverage, dict)
        else _provider_coverage(list(providers or []), provider_counts)
    )
    missing = coverage.get("missing_requested_providers", [])
    if missing:
        warnings.append("provider_coverage_partial:" + ",".join(map(str, missing)))

    if blockers:
        return "BLOCKED", blockers + warnings
    if warnings:
        return "WARN", warnings
    return "PASS", []


def build_community_source_health(
    *,
    from_report: Path | None,
    output_dir: Path,
    write_report: bool = True,
) -> dict[str, Any]:
    source_path = from_report or _latest_community_report(_DEFAULT_INPUT_DIR)
    if source_path is None:
        report = {
            "status": "BLOCKED",
            "action": "community_source_health",
            "generated_at": datetime.now(_KST).isoformat(),
            "blockers": ["community_live_risk_report_missing"],
            "deploy_quality": False,
        }
        return _write_report(report, output_dir, write_report)

    source_path = source_path if source_path.is_absolute() else ROOT / source_path
    source = _load_json(source_path)
    metrics = source.get("metrics") if isinstance(source.get("metrics"), dict) else {}
    embedded = (
        source.get("community_source_health")
        if isinstance(source.get("community_source_health"), dict)
        else {}
    )
    status, status_notes = _status_from_report(source)

    provider_counts = metrics.get("provider_counts") or embedded.get("provider_counts") or {}
    query_coverage = embedded.get("query_coverage") if isinstance(embedded.get("query_coverage"), dict) else {}
    filter_health = embedded.get("filter_health") if isinstance(embedded.get("filter_health"), dict) else {}
    dedupe = embedded.get("dedupe") if isinstance(embedded.get("dedupe"), dict) else {}
    compliance = embedded.get("compliance") if isinstance(embedded.get("compliance"), dict) else {}
    risk_sidecar = embedded.get("risk_sidecar") if isinstance(embedded.get("risk_sidecar"), dict) else {}

    raw_count = _as_int(metrics.get("raw_post_count", embedded.get("raw_post_count")))
    valid_count = _as_int(
        metrics.get("valid_event_count", embedded.get("valid_event_count", embedded.get("normalized_event_count")))
    )
    tickers_with_posts = metrics.get("tickers_with_posts") or []
    providers = source.get("providers") or sorted(provider_counts)
    provider_coverage = (
        embedded.get("provider_coverage")
        if isinstance(embedded.get("provider_coverage"), dict)
        else _provider_coverage(list(providers or []), provider_counts)
    )
    quota = embedded.get("quota") if isinstance(embedded.get("quota"), dict) else {}

    report = {
        "status": status,
        "action": "community_source_health",
        "generated_at": datetime.now(_KST).isoformat(),
        "source_report_path": str(source_path),
        "source_report_path_relative": _repo_relative(source_path),
        "provider": source.get("provider_mode", embedded.get("community_provider_mode")),
        "providers": providers,
        "provider_coverage": provider_coverage,
        "is_mock": bool(source.get("is_mock")),
        "internal_fake_naver": bool(source.get("internal_fake_naver")),
        "deploy_quality": False,
        "tickers_requested": len(source.get("tickers", []) or []),
        "tickers_with_raw_posts": len(tickers_with_posts),
        "raw_count": raw_count,
        "valid_event_count": valid_count,
        "aligned_count": valid_count,
        "timestamp_quality": {
            "counts": metrics.get("timestamp_quality_counts")
            or embedded.get("timestamp_quality_counts")
            or {},
            "pit_safe_for_historical_c12": False,
        },
        "timestamp_confidence": {
            "counts": metrics.get("timestamp_confidence_counts")
            or embedded.get("timestamp_confidence_counts")
            or {},
            "blog_postdate_confidence": "medium_date_only",
            "cafearticle_confidence": "low",
            "pit_safe_for_historical_c12": False,
        },
        "quality": {
            "spam_filtered": _as_int(filter_health.get("spam_filtered_total")),
            "manipulation_flags": _as_int(filter_health.get("manipulation_flagged_total")),
            "duplicate_rate": _as_float(dedupe.get("duplicate_rate_after_dedupe")),
            "author_hash_count": 0,
            "author_hash_count_note": "not_persisted_in_report_by_raw-content policy",
        },
        "query_coverage": {
            "unique_query_count": _as_int(query_coverage.get("unique_query_count")),
            "queries": query_coverage.get("queries", []),
            "include_ticker_queries": query_coverage.get("include_ticker_queries"),
            "query_suffixes": query_coverage.get("query_suffixes", []),
        },
        "quota": {
            "api_calls": _as_int(quota.get("api_calls")),
            "api_quota_limit": _as_int(quota.get("api_quota_limit", 25000)),
            "api_quota_used_ratio": _as_float(quota.get("api_quota_used_ratio")),
        },
        "compliance": {
            "stores_raw_content": bool(compliance.get("stores_raw_content", False)),
            "stores_derived_signal_only": bool(
                compliance.get("stores_derived_signal_only", True)
            ),
            "terms_review_required_for_durable_archive": bool(
                compliance.get("terms_review_required_for_durable_archive", True)
            ),
            "raw_content_fields_persisted": compliance.get("raw_content_fields_persisted", []),
        },
        "risk_sidecar": {
            "community_live_risk_ready": bool(
                risk_sidecar.get(
                    "community_live_risk_ready",
                    raw_count > 0 and valid_count > 0,
                )
            ),
            "reason_codes_emitted": risk_sidecar.get("reason_codes_emitted", []),
            "recommended_fda_reason_code": risk_sidecar.get("recommended_fda_reason_code"),
        },
        "blockers_or_warnings": status_notes,
        "caveats": [
            "Standalone source-health evidence only; not C12 deploy-quality.",
            "Cafearticle has no official posted_at field, so historical PIT use remains blocked.",
            "Blog postdate is date-only and should not be treated as precise intraday timestamp.",
        ],
    }
    return _write_report(report, output_dir, write_report)


def _write_report(report: dict[str, Any], output_dir: Path, write_report: bool) -> dict[str, Any]:
    if not write_report:
        return report
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"community_source_health_{ts}.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    report["report_path"] = str(path)
    report["report_path_relative"] = _repo_relative(path)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-report", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--no-write-report", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_community_source_health(
        from_report=args.from_report,
        output_dir=args.output_dir,
        write_report=not args.no_write_report,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") in {"PASS", "WARN"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

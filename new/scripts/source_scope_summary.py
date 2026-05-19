#!/usr/bin/env python
"""Summarize news/community source scope for pre-C12 reporting.

Read-only. Does not read .env, does not call external APIs, and does not mutate
model registries or candidate artifacts.
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
_REPORT_DIR = ROOT / "artifacts" / "reports" / "source_scope_summary"


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _latest_json(dir_path: Path, pattern: str) -> Path | None:
    paths = sorted(dir_path.glob(pattern))
    return paths[-1] if paths else None


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload if isinstance(payload, dict) else {}


def _sum_source_stats(per_date: list[Any]) -> dict[str, Any]:
    news_events = 0
    community_events = 0
    score_rows = 0
    non_neutral_dates = 0
    fallback_scope_counts: dict[str, dict[str, int]] = {
        "news": {},
        "community": {},
    }
    for item in per_date:
        if not isinstance(item, dict):
            continue
        if item.get("non_neutral"):
            non_neutral_dates += 1
        score_rows += int(item.get("score_count") or 0)
        stats = item.get("source_stats") if isinstance(item.get("source_stats"), dict) else {}
        news_events += int(stats.get("news_event_count") or 0)
        community_events += int(stats.get("community_event_count") or 0)
        scopes = stats.get("fallback_scope_counts")
        if isinstance(scopes, dict):
            for channel in ("news", "community"):
                channel_counts = scopes.get(channel)
                if not isinstance(channel_counts, dict):
                    continue
                for key, value in channel_counts.items():
                    fallback_scope_counts[channel][str(key)] = (
                        fallback_scope_counts[channel].get(str(key), 0)
                        + int(value or 0)
                    )
    return {
        "news_event_count": news_events,
        "community_event_count": community_events,
        "score_rows": score_rows,
        "non_neutral_dates": non_neutral_dates,
        "fallback_scope_counts": fallback_scope_counts,
    }


def _model_feature_scope(bundle_id: str | None) -> dict[str, Any]:
    if not bundle_id:
        return {}
    bundle_dir = ROOT / "artifacts" / "lgbm_paper_candidate" / bundle_id
    registry = _load_json(bundle_dir / "registry.json")
    active = registry.get("active_version")
    metadata_path = bundle_dir / f"{active}_metadata.json" if active else None
    metadata = _load_json(metadata_path)
    feature_cols = metadata.get("feature_cols") or []
    dual_features = {
        "news_score_t",
        "comm_score_t_1",
        "comm_score_t_2",
        "news_comm_divergence",
        "community_noise_multiplier",
    }
    return {
        "bundle_id": bundle_id,
        "active_version": active,
        "metadata_path": _repo_relative(metadata_path) if metadata_path else None,
        "feature_count": len(feature_cols) if isinstance(feature_cols, list) else 0,
        "feature_cols": feature_cols,
        "uses_dual_source_features": bool(
            isinstance(feature_cols, list) and dual_features.intersection(feature_cols)
        ),
        "uses_exogenous_features": bool(
            isinstance(feature_cols, list)
            and {
                "us_sp500_change",
                "us_nasdaq_change",
                "us_vix",
                "foreign_net_buy",
                "institutional_net_buy",
                "retail_net_buy",
                "interest_rate",
                "usd_krw",
            }.intersection(feature_cols)
        ),
    }


def build_source_scope_summary(
    *,
    bundle_id: str | None,
    output_dir: Path,
    write_report: bool = True,
) -> dict[str, Any]:
    news_report_path = _latest_json(
        ROOT / "artifacts" / "reports" / "build_news_dart_archive",
        "build_news_dart_archive_*.json",
    )
    dual_report_path = _latest_json(
        ROOT / "artifacts" / "reports" / "dual_source_history",
        "materialize_dual_source_history_*.json",
    )
    readiness_report_path = _latest_json(
        ROOT / "artifacts" / "reports" / "data_readiness",
        "data_readiness_*.json",
    )
    news_report = _load_json(news_report_path)
    dual_report = _load_json(dual_report_path)
    readiness_report = _load_json(readiness_report_path)
    source_totals = _sum_source_stats(dual_report.get("per_date") or [])

    coverage = dual_report.get("coverage") or {}
    community_smoke = (
        readiness_report.get("stages", {})
        .get("smoke", {})
        .get("community", {})
    )
    naver_smoke = (
        readiness_report.get("stages", {})
        .get("smoke", {})
        .get("naver", {})
    )
    report = {
        "status": "PASS" if news_report and dual_report else "BLOCKED",
        "action": "source_scope_summary",
        "generated_at": datetime.now(_KST).isoformat(),
        "deploy_quality": False,
        "inputs": {
            "build_news_dart_archive": _repo_relative(news_report_path) if news_report_path else None,
            "materialize_dual_source_history": _repo_relative(dual_report_path) if dual_report_path else None,
            "live_data_readiness": _repo_relative(readiness_report_path) if readiness_report_path else None,
        },
        "news_dart_archive": {
            "status": news_report.get("status"),
            "business_days": news_report.get("date_count"),
            "ticker_count": news_report.get("ticker_count"),
            "total_events": news_report.get("total_events"),
            "zero_event_date_count": news_report.get("zero_event_date_count"),
            "source_apis": ["naver_news_v1", "dart_open_api"],
        },
        "dual_source_history": {
            "status": dual_report.get("status"),
            "business_days": dual_report.get("date_count"),
            "files_written_count": len(dual_report.get("files_written") or []),
            "dual_source_non_neutral_rate": coverage.get("dual_source_non_neutral_date_coverage"),
            "min_dual_source_non_neutral_date_coverage": coverage.get("min_dual_source_non_neutral_date_coverage"),
            "source_totals": source_totals,
            "community_non_neutral_rate": 0.0 if source_totals["community_event_count"] == 0 else None,
        },
        "live_smoke": {
            "naver_news": naver_smoke,
            "community": community_smoke,
        },
        "selected_model": _model_feature_scope(bundle_id),
        "cold_path": {
            "uses_community_risk": True,
            "implementation": "community_live_risk_smoke.py",
            "status": "implemented" if (ROOT / "new" / "scripts" / "community_live_risk_smoke.py").exists() else "missing",
        },
        "caveats": [
            "Historical community_event_count is zero in the current deploy-quality archive.",
            "Current selected 195m candidate does not use Dual-Source features directly.",
            "Community should be described as live Cold Path risk proxy until PIT-safe historical source is available.",
        ],
    }
    if write_report:
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
        path = output_dir / f"source_scope_summary_{ts}.json"
        report["report_path"] = str(path)
        report["report_path_relative"] = _repo_relative(path)
        with path.open("w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
            f.write("\n")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", default=None)
    parser.add_argument("--end-date", default=None, help="Accepted for CLI compatibility; latest reports are summarized.")
    parser.add_argument("--business-days", type=int, default=None, help="Accepted for CLI compatibility; latest reports are summarized.")
    parser.add_argument("--no-write-report", action="store_true")
    parser.add_argument("--output-dir", default=str(_REPORT_DIR))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_source_scope_summary(
        bundle_id=args.bundle_id,
        output_dir=Path(args.output_dir),
        write_report=not bool(args.no_write_report),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""Materialize historical Dual-Source feature artifacts from archived events.

This script never reads .env and never uses live search connectors for old dates.
Historical deploy-quality evidence must come from archived raw news/community
events whose event timestamps are <= the daily snapshot timestamp.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.data.dual_source_runner import _load_active_universe  # noqa: E402
from src.data.dual_source_scorer import DualSourceScorer  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.safe_cast import safe_bool, safe_float, safe_int  # noqa: E402
from src.utils.trading_calendar import (  # noqa: E402
    kospi_trading_dates_between,
    kospi_trading_start_date,
)

_KST = ZoneInfo("Asia/Seoul")
_ARTIFACT_DIR = ROOT / "artifacts" / "dual_source"
_REPORT_DIR = ROOT / "artifacts" / "reports" / "dual_source_history"
_FALLBACK_MIN_DUAL_SOURCE_NON_NEUTRAL_DATE_COVERAGE = 0.8


def _parse_date(date_key: str) -> datetime:
    return datetime.strptime(str(date_key), "%Y%m%d").replace(tzinfo=_KST)


def _snapshot_ts(date_key: str) -> datetime:
    day = _parse_date(date_key).date()
    return datetime.combine(day, time(8, 30), tzinfo=_KST)


def _min_dual_source_non_neutral_date_coverage() -> float:
    cfg = config_load("risk_config.yaml", "phase2_feature_backfill") or {}
    return safe_float(
        cfg.get("min_dual_source_non_neutral_date_coverage"),
        default=_FALLBACK_MIN_DUAL_SOURCE_NON_NEUTRAL_DATE_COVERAGE,
        min_value=0.0,
        max_value=1.0,
    )


def _business_dates(end_date: str, business_days: int) -> list[str]:
    end = _parse_date(end_date).date()
    start = kospi_trading_start_date(end, business_days)
    return kospi_trading_dates_between(start, end)


def _candidate_raw_paths(raw_events_dir: Path, date_key: str) -> list[Path]:
    return [
        raw_events_dir / f"{date_key}.json",
        raw_events_dir / f"dual_source_raw_{date_key}.json",
        raw_events_dir / f"events_{date_key}.json",
        raw_events_dir / f"news_community_{date_key}.json",
    ]


def _read_raw_payload(raw_events_dir: Path, date_key: str) -> tuple[Path | None, dict[str, Any] | None]:
    for path in _candidate_raw_paths(raw_events_dir, date_key):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict):
            raise ValueError(f"raw event payload must be object: {path}")
        return path, payload
    return None, None


def _as_kst(value: Any, *, default: datetime) -> datetime:
    if value in (None, ""):
        return default
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_KST)
    return dt.astimezone(_KST)


def _has_value(value: Any) -> bool:
    return value not in (None, "")


def _required_ts(value: Any, *, field: str, snapshot: datetime) -> datetime:
    if not _has_value(value):
        raise ValueError(f"raw deploy-quality payload missing required timestamp field: {field}")
    return _as_kst(value, default=snapshot)


def _text_from_event(event: dict[str, Any]) -> str:
    parts = [
        str(event.get("title", "") or ""),
        str(event.get("description", "") or ""),
        str(event.get("content", "") or ""),
        str(event.get("text", "") or ""),
    ]
    return " ".join(part for part in parts if part).strip()


def _load_ticker_sector_map() -> dict[str, str]:
    cfg = config_load("sector_config.yaml")
    raw = cfg.get("ticker_to_sector", {}) or {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(ticker).zfill(6): str(sector)
        for ticker, sector in raw.items()
        if str(ticker).strip() and str(sector).strip()
    }


def _fallback_text_limit() -> int | None:
    cfg = config_load("dual_source.yaml")
    community_cfg = cfg.get("community", {}) or {}
    try:
        value = int(community_cfg.get("max_posts_per_ticker", 0) or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _limited_texts(texts: list[str], limit: int | None) -> list[str]:
    if limit is None:
        return list(texts)
    return list(texts[:limit])


def _materialization_text_limits() -> dict[str, int | None]:
    cfg = config_load("dual_source.yaml") or {}
    materialization = cfg.get("materialization", {}) or {}

    def _limit(key: str) -> int | None:
        value = safe_int(
            materialization.get(key),
            default=0,
            min_value=0,
        )
        return value if value > 0 else None

    return {
        "max_news_texts_per_ticker": _limit("max_news_texts_per_ticker"),
        "max_news_texts_per_fallback_scope": _limit("max_news_texts_per_fallback_scope"),
        "max_market_backstop_texts": _limit("max_market_backstop_texts"),
        "max_community_texts_per_ticker": _limit("max_community_texts_per_ticker"),
    }


def _record_text_cap(
    stats: dict[str, dict[str, dict[str, int]]],
    *,
    channel: str,
    scope: str,
    original: int,
    kept: int,
) -> None:
    channel_bucket = stats.setdefault(channel, {})
    bucket = channel_bucket.setdefault(
        scope,
        {
            "original_text_count": 0,
            "kept_text_count": 0,
            "dropped_text_count": 0,
        },
    )
    bucket["original_text_count"] += int(original)
    bucket["kept_text_count"] += int(kept)
    bucket["dropped_text_count"] += max(int(original) - int(kept), 0)


def _limited_texts_with_stats(
    texts: list[str],
    limit: int | None,
    stats: dict[str, dict[str, dict[str, int]]],
    *,
    channel: str,
    scope: str,
) -> list[str]:
    original = len(texts)
    kept_texts = _limited_texts(texts, limit)
    _record_text_cap(
        stats,
        channel=channel,
        scope=scope,
        original=original,
        kept=len(kept_texts),
    )
    return kept_texts


def _bump_scope_count(
    counts: dict[str, dict[str, int]],
    *,
    channel: str,
    scope: str,
) -> None:
    bucket = counts.setdefault(channel, {})
    bucket[scope] = int(bucket.get(scope, 0)) + 1


def _annotate_score_source_notes(
    scores: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> None:
    scopes_by_ticker = {
        str(row.get("ticker", "")).zfill(6): row.get("source_scope") or {}
        for row in rows
    }
    for score in scores:
        ticker = str(score.get("ticker", "")).zfill(6)
        scope = scopes_by_ticker.get(ticker)
        if not isinstance(scope, dict):
            continue
        notes = [str(score.get("source_notes") or "").strip()]
        news_scope = str(scope.get("news") or "").strip()
        comm_scope = str(scope.get("community") or "").strip()
        if news_scope:
            notes.append(f"news_scope={news_scope}")
        if comm_scope:
            notes.append(f"comm_scope={comm_scope}")
        score["source_notes"] = "|".join(note for note in notes if note)


def _apply_market_backstop(
    scores: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    scorer: DualSourceScorer,
    snapshot: datetime,
) -> int:
    if all(_is_non_neutral_score(score) for score in scores):
        return 0
    rows_by_ticker = {
        str(row.get("ticker", "")).zfill(6): row
        for row in rows
    }
    market_texts: list[str] = []
    market_data_ts: str | None = None
    for row in rows:
        raw_texts = row.get("_market_news_texts")
        if isinstance(raw_texts, list) and raw_texts:
            market_texts = [str(text) for text in raw_texts if str(text).strip()]
            market_data_ts = str(row.get("_market_news_data_ts") or snapshot.isoformat())
            break
    if not market_texts:
        return 0

    market_score = scorer.score(
        ticker="000000",
        news_texts=market_texts,
        comm_texts_t1=[],
        comm_texts_t2=[],
        current_volume=0.0,
        historical_volumes=[],
        data_ts=market_data_ts,
        snapshot_ts=snapshot,
    )
    if not _is_non_neutral_score(market_score):
        return 0

    backfilled = 0
    for score in scores:
        if _is_non_neutral_score(score):
            continue
        ticker = str(score.get("ticker", "")).zfill(6)
        if ticker not in rows_by_ticker:
            continue
        score["news_score_t"] = market_score["news_score_t"]
        score["news_comm_divergence"] = market_score["news_comm_divergence"]
        existing = str(score.get("source_notes") or "").strip()
        notes = [existing, "market_backstop"]
        score["source_notes"] = "|".join(note for note in notes if note)
        backfilled += 1
    return backfilled


def _payload_rows_from_explicit_rows(
    payload: dict[str, Any],
    universe: list[dict[str, Any]],
    snapshot: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows_by_ticker: dict[str, dict[str, Any]] = {}
    limits = _materialization_text_limits()
    text_cap_stats: dict[str, dict[str, dict[str, int]]] = {}
    for row in payload.get("rows", []) or []:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker", "")).zfill(6)
        if not ticker or ticker == "000000":
            continue
        data_ts = _required_ts(row.get("data_ts"), field="rows[].data_ts", snapshot=snapshot)
        if data_ts > snapshot:
            raise ValueError(f"PIT violation: ticker={ticker} data_ts={data_ts.isoformat()} > snapshot={snapshot.isoformat()}")
        news_texts = _limited_texts_with_stats(
            [str(text) for text in (row.get("news_texts", []) or []) if str(text).strip()],
            limits["max_news_texts_per_ticker"],
            text_cap_stats,
            channel="news",
            scope="explicit_row",
        )
        comm_texts_t1 = _limited_texts_with_stats(
            [str(text) for text in (row.get("comm_texts_t1", []) or []) if str(text).strip()],
            limits["max_community_texts_per_ticker"],
            text_cap_stats,
            channel="community",
            scope="explicit_row_t1",
        )
        comm_texts_t2 = _limited_texts_with_stats(
            [str(text) for text in (row.get("comm_texts_t2", []) or []) if str(text).strip()],
            limits["max_community_texts_per_ticker"],
            text_cap_stats,
            channel="community",
            scope="explicit_row_t2",
        )
        rows_by_ticker[ticker] = {
            "ticker": ticker,
            "news_texts": news_texts,
            "comm_texts_t1": comm_texts_t1,
            "comm_texts_t2": comm_texts_t2,
            "current_volume": float(row.get("current_volume", 0.0) or 0.0),
            "historical_volumes": list(row.get("historical_volumes", []) or []),
            "data_ts": data_ts.isoformat(),
        }
    out = []
    for item in universe:
        ticker = str(item["ticker"]).zfill(6)
        out.append(rows_by_ticker.get(ticker, {
            "ticker": ticker,
            "news_texts": [],
            "comm_texts_t1": [],
            "comm_texts_t2": [],
            "current_volume": 0.0,
            "historical_volumes": [],
            "data_ts": snapshot.isoformat(),
        }))
    return out, {
        "input_shape": "rows",
        "raw_row_count": len(payload.get("rows", []) or []),
        "text_limits": limits,
        "text_cap_stats": text_cap_stats,
    }


def _payload_rows_from_events(
    payload: dict[str, Any],
    universe: list[dict[str, Any]],
    snapshot: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ticker_to_sector = _load_ticker_sector_map()
    fallback_limit = _fallback_text_limit()
    limits = _materialization_text_limits()
    text_cap_stats: dict[str, dict[str, dict[str, int]]] = {}
    grouped: dict[str, dict[str, Any]] = {}
    event_count = 0
    news_count = 0
    community_count = 0
    latest_ts_by_ticker: dict[str, datetime] = {}
    sector_news_texts: dict[str, list[str]] = {}
    sector_comm_texts: dict[str, list[str]] = {}
    sector_latest_ts: dict[str, datetime] = {}
    market_news_texts: list[str] = []
    market_comm_texts: list[str] = []
    latest_market_ts: datetime | None = None
    for event in payload.get("events", []) or []:
        if not isinstance(event, dict):
            continue
        ticker = str(event.get("ticker", "")).zfill(6)
        if not ticker or ticker == "000000":
            continue
        text = _text_from_event(event)
        if not text:
            continue
        event_ts = _required_ts(
            event.get("event_ts") or event.get("published_at") or event.get("ts"),
            field="events[].event_ts",
            snapshot=snapshot,
        )
        if event_ts > snapshot:
            raise ValueError(f"PIT violation: ticker={ticker} event_ts={event_ts.isoformat()} > snapshot={snapshot.isoformat()}")
        bucket = grouped.setdefault(ticker, {
            "news_texts": [],
            "comm_texts_t1": [],
            "comm_texts_t2": [],
            "current_volume": 0.0,
            "historical_volumes": [],
        })
        source = str(event.get("source") or event.get("kind") or "").lower()
        if "comm" in source or source in {"community", "forum", "social"}:
            bucket["comm_texts_t1"].append(text)
            bucket["current_volume"] = float(bucket["current_volume"]) + 1.0
            market_comm_texts.append(text)
            sector = ticker_to_sector.get(ticker)
            if sector:
                sector_comm_texts.setdefault(sector, []).append(text)
            community_count += 1
        else:
            bucket["news_texts"].append(text)
            market_news_texts.append(text)
            sector = ticker_to_sector.get(ticker)
            if sector:
                sector_news_texts.setdefault(sector, []).append(text)
            news_count += 1
        latest_ts_by_ticker[ticker] = max(latest_ts_by_ticker.get(ticker, event_ts), event_ts)
        sector = ticker_to_sector.get(ticker)
        if sector:
            sector_latest_ts[sector] = max(sector_latest_ts.get(sector, event_ts), event_ts)
        latest_market_ts = max(latest_market_ts or event_ts, event_ts)
        event_count += 1

    out = []
    fallback_scope_counts: dict[str, dict[str, int]] = {"news": {}, "community": {}}
    market_news_texts_for_backstop = _limited_texts_with_stats(
        market_news_texts,
        limits["max_market_backstop_texts"],
        text_cap_stats,
        channel="news",
        scope="market_backstop",
    )
    for item in universe:
        ticker = str(item["ticker"]).zfill(6)
        bucket = grouped.get(ticker, {})
        sector = ticker_to_sector.get(ticker)
        data_ts_candidates = [latest_ts_by_ticker[ticker]] if ticker in latest_ts_by_ticker else []

        news_texts = list(bucket.get("news_texts", []) or [])
        news_scope = "ticker" if news_texts else "none"
        if not news_texts and sector and sector_news_texts.get(sector):
            news_texts = list(sector_news_texts[sector])
            news_scope = "sector_fallback"
            if sector in sector_latest_ts:
                data_ts_candidates.append(sector_latest_ts[sector])
        elif not news_texts and market_news_texts:
            news_texts = list(market_news_texts)
            news_scope = "market_fallback"
            if latest_market_ts is not None:
                data_ts_candidates.append(latest_market_ts)
        news_limit = (
            limits["max_news_texts_per_ticker"]
            if news_scope == "ticker"
            else limits["max_news_texts_per_fallback_scope"]
        )
        news_texts = _limited_texts_with_stats(
            news_texts,
            news_limit,
            text_cap_stats,
            channel="news",
            scope=news_scope,
        )

        comm_texts_t1 = list(bucket.get("comm_texts_t1", []) or [])
        comm_scope = "ticker" if comm_texts_t1 else "none"
        if not comm_texts_t1 and sector and sector_comm_texts.get(sector):
            comm_texts_t1 = list(sector_comm_texts[sector])
            comm_scope = "sector_fallback"
            if sector in sector_latest_ts:
                data_ts_candidates.append(sector_latest_ts[sector])
        elif not comm_texts_t1 and market_comm_texts:
            comm_texts_t1 = list(market_comm_texts)
            comm_scope = "market_fallback"
            if latest_market_ts is not None:
                data_ts_candidates.append(latest_market_ts)
        comm_limit = limits["max_community_texts_per_ticker"] or fallback_limit
        comm_texts_t1 = _limited_texts_with_stats(
            comm_texts_t1,
            comm_limit,
            text_cap_stats,
            channel="community",
            scope=comm_scope,
        )

        _bump_scope_count(fallback_scope_counts, channel="news", scope=news_scope)
        _bump_scope_count(fallback_scope_counts, channel="community", scope=comm_scope)
        out.append({
            "ticker": ticker,
            "news_texts": news_texts,
            "comm_texts_t1": comm_texts_t1,
            "comm_texts_t2": list(bucket.get("comm_texts_t2", []) or []),
            "current_volume": float(bucket.get("current_volume", 0.0) or 0.0),
            "historical_volumes": list(bucket.get("historical_volumes", []) or []),
            "data_ts": (max(data_ts_candidates) if data_ts_candidates else snapshot).isoformat(),
            "source_scope": {
                "news": news_scope,
                "community": comm_scope,
            },
            "_market_news_texts": market_news_texts_for_backstop,
            "_market_news_data_ts": (latest_market_ts or snapshot).isoformat(),
        })
    return out, {
        "input_shape": "events",
        "raw_event_count": event_count,
        "news_event_count": news_count,
        "community_event_count": community_count,
        "fallback_text_limit": fallback_limit,
        "text_limits": limits,
        "text_cap_stats": text_cap_stats,
        "fallback_scope_counts": fallback_scope_counts,
        "market_news_text_count": len(market_news_texts),
        "market_news_text_count_after_cap": len(market_news_texts_for_backstop),
        "market_community_text_count": len(market_comm_texts),
    }


def _build_scorer_inputs(
    payload: dict[str, Any],
    universe: list[dict[str, Any]],
    snapshot: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if isinstance(payload.get("rows"), list):
        return _payload_rows_from_explicit_rows(payload, universe, snapshot)
    if isinstance(payload.get("events"), list):
        return _payload_rows_from_events(payload, universe, snapshot)
    raise ValueError("raw payload must contain either rows[] or events[]")


def _non_deploy_quality_reason(payload: dict[str, Any]) -> str | None:
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        return "raw event payload missing provenance.deploy_quality=true"
    if not safe_bool(provenance.get("deploy_quality"), default=False):
        return str(provenance.get("reason") or "raw event payload is not deploy-quality")
    return None


def _is_non_neutral_score(row: dict[str, Any]) -> bool:
    return (
        abs(float(row.get("news_score_t", 0.0))) > 1e-12
        or abs(float(row.get("comm_score_t_1", 0.0))) > 1e-12
        or abs(float(row.get("comm_score_t_2", 0.0))) > 1e-12
        or abs(float(row.get("news_comm_divergence", 0.0))) > 1e-12
        or abs(float(row.get("community_noise_multiplier", 1.0)) - 1.0) > 1e-12
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def _score_payload(
    *,
    snapshot: datetime,
    raw_path: Path,
    source_stats: dict[str, Any],
    scores: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "batch_date": snapshot.date().isoformat(),
        "snapshot_ts": snapshot.isoformat(),
        "generated_at": datetime.now(_KST).isoformat(),
        "ticker_count": len(scores),
        "source_stats": {
            **source_stats,
            "input_mode": "archived_raw_events",
            "raw_path": str(raw_path),
            "neutral_rehearsal_file": False,
            "external_live_search_api": False,
        },
        "scores": scores,
    }


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def materialize_dual_source_history(
    *,
    end_date: str,
    business_days: int,
    raw_events_dir: Path,
    artifact_dir: Path,
    output_dir: Path,
    skip_existing: bool = False,
) -> dict[str, Any]:
    universe = _load_active_universe()
    dates = _business_dates(end_date, business_days)
    scorer = DualSourceScorer()
    per_date: list[dict[str, Any]] = []
    written: list[str] = []
    blockers: list[str] = []
    non_neutral_dates = 0
    skipped_existing_dates = 0

    artifact_dir.mkdir(parents=True, exist_ok=True)

    for date_key in dates:
        out_path = artifact_dir / f"{date_key}.json"
        if skip_existing and out_path.exists():
            skipped_existing_dates += 1
            per_date.append({
                "date": date_key,
                "status": "SKIP_EXISTING",
                "raw_path": None,
                "scores_written": False,
                "non_neutral": None,
                "artifact_path": str(out_path),
            })
            continue

        snapshot = _snapshot_ts(date_key)
        raw_path, payload = _read_raw_payload(raw_events_dir, date_key)
        if payload is None or raw_path is None:
            per_date.append({
                "date": date_key,
                "status": "MISSING_RAW_EVENTS",
                "raw_path": None,
                "scores_written": False,
                "non_neutral": False,
            })
            continue
        try:
            non_deploy_reason = _non_deploy_quality_reason(payload)
            if non_deploy_reason:
                if "non_deploy_quality_raw_events" not in blockers:
                    blockers.append("non_deploy_quality_raw_events")
                per_date.append({
                    "date": date_key,
                    "status": "NON_DEPLOY_QUALITY_RAW_EVENTS",
                    "raw_path": str(raw_path),
                    "scores_written": False,
                    "non_neutral": False,
                    "reason": non_deploy_reason,
                    "provenance": payload.get("provenance") or {},
                })
                continue
            rows, source_stats = _build_scorer_inputs(payload, universe, snapshot)
            scores = scorer.score_universe(rows, snapshot_ts=snapshot)
            _annotate_score_source_notes(scores, rows)
            market_backstop_rows = _apply_market_backstop(scores, rows, scorer, snapshot)
            if market_backstop_rows:
                source_stats["market_backstop_rows"] = market_backstop_rows
            non_neutral = any(_is_non_neutral_score(row) for row in scores)
            if not non_neutral:
                out_payload = _score_payload(
                    snapshot=snapshot,
                    raw_path=raw_path,
                    source_stats=source_stats,
                    scores=scores,
                )
                _write_json(out_path, out_payload)
                written.append(_repo_relative(out_path))
                per_date.append({
                    "date": date_key,
                    "status": "NEUTRAL_ONLY",
                    "raw_path": str(raw_path),
                    "scores_written": True,
                    "score_count": len(scores),
                    "non_neutral": False,
                    "source_stats": source_stats,
                })
                continue
            out_payload = _score_payload(
                snapshot=snapshot,
                raw_path=raw_path,
                source_stats=source_stats,
                scores=scores,
            )
            _write_json(out_path, out_payload)
            written.append(_repo_relative(out_path))
            non_neutral_dates += 1
            per_date.append({
                "date": date_key,
                "status": "PASS",
                "raw_path": str(raw_path),
                "scores_written": True,
                "score_count": len(scores),
                "non_neutral": True,
                "source_stats": source_stats,
            })
        except Exception as e:
            per_date.append({
                "date": date_key,
                "status": "ERROR",
                "raw_path": str(raw_path),
                "scores_written": False,
                "non_neutral": False,
                "error": str(e),
                "error_type": type(e).__name__,
            })

    min_coverage = _min_dual_source_non_neutral_date_coverage()
    effective_dates = len(dates) - skipped_existing_dates
    coverage = non_neutral_dates / max(effective_dates, 1)
    if effective_dates > 0 and coverage < min_coverage:
        blockers.append("dual_source_non_neutral_coverage_below_threshold")
    if effective_dates > 0 and not written:
        blockers.append("no_dual_source_artifacts_written")
    report = {
        "status": "PASS" if not blockers else "BLOCKED",
        "generated_at": datetime.now(_KST).isoformat(),
        "end_date": end_date,
        "business_days_requested": business_days,
        "date_count": len(dates),
        "skip_existing": bool(skip_existing),
        "skipped_existing_date_count": skipped_existing_dates,
        "raw_events_dir": str(raw_events_dir),
        "artifact_dir": str(artifact_dir),
        "coverage": {
            "dual_source_non_neutral_date_coverage": coverage,
            "min_dual_source_non_neutral_date_coverage": min_coverage,
            "written_date_count": len(written),
        },
        "blockers": blockers,
        "files_written": written,
        "per_date": per_date,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"materialize_dual_source_history_{datetime.now(_KST).strftime('%Y%m%d_%H%M%S')}.json"
    report["report_path"] = str(path)
    report["report_path_relative"] = _repo_relative(path)
    _write_json(path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end-date", required=True, help="YYYYMMDD")
    parser.add_argument("--business-days", type=int, default=80)
    parser.add_argument("--raw-events-dir", required=True, help="Directory containing archived daily raw event JSON files.")
    parser.add_argument("--artifact-dir", default=str(_ARTIFACT_DIR))
    parser.add_argument("--output-dir", default=str(_REPORT_DIR))
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip dates whose dual_source artifact already exists (recommended for incremental updates).",
    )
    args = parser.parse_args(argv)
    report = materialize_dual_source_history(
        end_date=str(args.end_date),
        business_days=int(args.business_days),
        raw_events_dir=Path(str(args.raw_events_dir)),
        artifact_dir=Path(str(args.artifact_dir)),
        output_dir=Path(str(args.output_dir)),
        skip_existing=bool(args.skip_existing),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

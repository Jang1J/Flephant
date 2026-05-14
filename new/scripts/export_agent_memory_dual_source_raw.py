#!/usr/bin/env python
"""Export NewsAgent memory as non-deploy-quality Dual-Source raw events.

This adapter never reads .env and never calls live connectors. It is useful for
paper rehearsal/debugging when only derived agent memory exists, but the output
is explicitly marked ``deploy_quality=false`` so C12/C14 gates cannot treat it
as production evidence.
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

from src.utils.ticker_utils import pad_ticker  # noqa: E402
from src.utils.trading_calendar import (  # noqa: E402
    kospi_trading_dates_between,
    kospi_trading_start_date,
)

_KST = ZoneInfo("Asia/Seoul")
_DEFAULT_AGENT_MEMORY_DIR = ROOT / "artifacts" / "agent_memory"
_DEFAULT_OUTPUT_DIR = ROOT / "artifacts" / "raw" / "dual_source_agent_memory"
_DEFAULT_REPORT_DIR = ROOT / "artifacts" / "reports" / "dual_source_agent_memory"


def _parse_date(date_key: str) -> datetime:
    return datetime.strptime(str(date_key), "%Y%m%d").replace(tzinfo=_KST)


def _snapshot_ts(date_key: str) -> datetime:
    day = _parse_date(date_key).date()
    return datetime.combine(day, time(8, 30), tzinfo=_KST)


def _business_dates(end_date: str, business_days: int) -> list[str]:
    end = _parse_date(end_date).date()
    start = kospi_trading_start_date(end, business_days)
    return kospi_trading_dates_between(start, end)


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


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"{path}:{line_no}: {e}")
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows, errors


def _content_text(content: Any, item: dict[str, Any]) -> str:
    if isinstance(content, dict):
        parts = [
            content.get("title"),
            content.get("narrative"),
            content.get("summary"),
            content.get("description"),
            content.get("text"),
            content.get("stance"),
        ]
    else:
        parts = [content]
    parts.extend([item.get("title"), item.get("narrative"), item.get("text")])
    return " ".join(str(part) for part in parts if part not in (None, "")).strip()


def _event_from_memory(
    *,
    item: dict[str, Any],
    ticker_hint: str,
    date_key: str,
    snapshot: datetime,
) -> dict[str, Any] | None:
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    ticker = pad_ticker(str(item.get("ticker") or content.get("ticker") or ticker_hint))
    event_ts = _as_kst(item.get("ts") or content.get("ts"), default=snapshot)
    if event_ts > snapshot:
        return None
    text = _content_text(item.get("content"), item)
    if not text:
        return None
    source = str(content.get("event_type") or item.get("event_type") or "news").lower()
    if source == "dart":
        source = "news"
    return {
        "ticker": ticker,
        "event_ts": event_ts.isoformat(),
        "source": source,
        "title": str(content.get("title") or item.get("title") or ""),
        "content": text,
        "provenance_source": "agent_memory.news_agent",
        "provenance_date": date_key,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def export_agent_memory_dual_source_raw(
    *,
    end_date: str,
    business_days: int,
    agent_memory_dir: Path,
    output_dir: Path,
    report_dir: Path,
) -> dict[str, Any]:
    dates = _business_dates(end_date, business_days)
    date_set = set(dates)
    news_root = agent_memory_dir / "news_agent"
    per_date: dict[str, dict[str, Any]] = {
        date_key: {
            "date": date_key,
            "memory_files": [],
            "events_exported": 0,
            "future_events_skipped": 0,
            "json_errors": [],
        }
        for date_key in dates
    }
    grouped_events: dict[str, list[dict[str, Any]]] = {date_key: [] for date_key in dates}

    if news_root.exists():
        for path in sorted(news_root.glob("*/*.jsonl")):
            date_key = path.stem
            if date_key not in date_set:
                continue
            ticker_hint = path.parent.name
            snapshot = _snapshot_ts(date_key)
            rows, errors = _read_jsonl(path)
            bucket = per_date[date_key]
            bucket["memory_files"].append(_repo_relative(path))
            bucket["json_errors"].extend(errors)
            before = len(grouped_events[date_key])
            for item in rows:
                event = _event_from_memory(
                    item=item,
                    ticker_hint=ticker_hint,
                    date_key=date_key,
                    snapshot=snapshot,
                )
                if event is None:
                    bucket["future_events_skipped"] += 1
                    continue
                grouped_events[date_key].append(event)
            bucket["events_exported"] += len(grouped_events[date_key]) - before

    files_written: list[str] = []
    for date_key, events in grouped_events.items():
        if not events:
            continue
        snapshot = _snapshot_ts(date_key)
        payload = {
            "schema_version": "agent_memory_dual_source_raw.v1",
            "date": date_key,
            "snapshot_ts": snapshot.isoformat(),
            "provenance": {
                "input_mode": "agent_memory_archive",
                "deploy_quality": False,
                "reason": "derived agent memory is not an original raw news/community archive",
                "source_root": _repo_relative(news_root),
            },
            "events": events,
        }
        out_path = output_dir / f"events_{date_key}.json"
        _write_json(out_path, payload)
        files_written.append(_repo_relative(out_path))

    blockers: list[str] = []
    if not files_written:
        blockers.append("no_agent_memory_events_exported")
    report = {
        "status": "PASS" if files_written else "BLOCKED",
        "generated_at": datetime.now(_KST).isoformat(),
        "end_date": end_date,
        "business_days_requested": business_days,
        "date_count": len(dates),
        "agent_memory_dir": str(agent_memory_dir),
        "output_dir": str(output_dir),
        "deploy_quality": False,
        "blockers": blockers,
        "files_written": files_written,
        "per_date": [per_date[date_key] for date_key in dates],
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"export_agent_memory_dual_source_raw_{datetime.now(_KST).strftime('%Y%m%d_%H%M%S')}.json"
    report["report_path"] = str(path)
    report["report_path_relative"] = _repo_relative(path)
    _write_json(path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end-date", required=True, help="YYYYMMDD")
    parser.add_argument("--business-days", type=int, default=80)
    parser.add_argument("--agent-memory-dir", default=str(_DEFAULT_AGENT_MEMORY_DIR))
    parser.add_argument("--output-dir", default=str(_DEFAULT_OUTPUT_DIR))
    parser.add_argument("--report-dir", default=str(_DEFAULT_REPORT_DIR))
    args = parser.parse_args(argv)
    report = export_agent_memory_dual_source_raw(
        end_date=str(args.end_date),
        business_days=int(args.business_days),
        agent_memory_dir=Path(str(args.agent_memory_dir)),
        output_dir=Path(str(args.output_dir)),
        report_dir=Path(str(args.report_dir)),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

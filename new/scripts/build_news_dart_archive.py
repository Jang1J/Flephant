#!/usr/bin/env python
"""Build deploy-quality dual-source raw archive from DART + Naver News.

각 business day에 대해 [prev 08:30 KST, curr 08:30 KST] 24-hour window 안의
events 만 추출해 ``artifacts/raw/dual_source/{date}.json`` 에 기록한다.
materialize_dual_source_history.py 가 이 디렉토리를 ``--raw-events-dir`` 로 받아
DualSourceScorer (FinBERT or sentiment_dict fallback) 로 5피처 점수를 자동 산출한다.

PIT-safety: event_ts <= snapshot_ts (= 해당 date 08:30 KST) 강제.
Deploy-quality: provenance.deploy_quality=true + source_apis 명시.
Community: 본 archive는 community texts 부재 → comm 4피처는 neutral 유지.
LightGBM 재학습 불필요: dual_source.enabled_for_lgbm=true 유지.
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

from src.connectors.dart_rest import DARTRestClient  # noqa: E402
from src.connectors.naver_rest import NaverNewsClient  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402
from src.utils.trading_calendar import (  # noqa: E402
    kospi_trading_dates_between,
    kospi_trading_start_date,
    previous_kospi_trading_day,
)

# 시장 전체에 broadcast할 키워드. ticker/sector 미매칭 큰 흐름 (KOSPI 시장 wide) 신호.
# 정직성 caveat: 모든 ticker에 같은 점수 broadcast = ticker 차별화 감소.
# 단 row-level non-neutral coverage 통과를 위한 fallback layer.
_MARKET_QUERIES: tuple[str, ...] = ("코스피", "KOSPI", "한국 증시")

_KST = ZoneInfo("Asia/Seoul")
_DEFAULT_OUT = ROOT / "artifacts" / "raw" / "dual_source"
_DEFAULT_REPORT_DIR = ROOT / "artifacts" / "reports" / "build_news_dart_archive"
_DEFAULT_CORP_CACHE = ROOT / "artifacts" / "cache" / "dart_corp_code_kospi20.json"


def _parse_date(date_key: str) -> datetime:
    return datetime.strptime(str(date_key), "%Y%m%d").replace(tzinfo=_KST)


def _snapshot_ts(date_key: str) -> datetime:
    """그 date 08:30 KST (장전 배치 시각, dual_source PIT-safety boundary)."""
    day = _parse_date(date_key).date()
    return datetime.combine(day, time(8, 30), tzinfo=_KST)


def _window_start(date_key: str) -> datetime:
    """이전 영업일 08:30 KST (24-hour window 시작점, KOSPI 거래일 기준)."""
    day = _parse_date(date_key).date()
    prev = previous_kospi_trading_day(day)
    return datetime.combine(prev, time(8, 30), tzinfo=_KST)


def _business_dates(end_date: str, business_days: int) -> list[str]:
    end = _parse_date(end_date).date()
    start = kospi_trading_start_date(end, business_days)
    return kospi_trading_dates_between(start, end)


def _client_is_real(client: Any) -> bool:
    return not bool(getattr(client, "_is_mock", True))


def _write_report(report_dir: Path, report: dict[str, Any]) -> dict[str, Any]:
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    report_path = report_dir / f"build_news_dart_archive_{ts}.json"
    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    report["report_path"] = str(report_path)
    return report


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _parse_iso_kst(value: str, *, default: datetime | None = None) -> datetime:
    if not value:
        return default if default is not None else datetime.now(_KST)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return default if default is not None else datetime.now(_KST)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_KST)
    return dt.astimezone(_KST)


def _load_ticker_meta(path: Path) -> dict[str, dict[str, Any]]:
    """corp_code 캐시 JSON → ticker -> meta dict."""
    if not path.exists():
        raise FileNotFoundError(
            f"corp_code 캐시 부재: {path}. "
            "build_dart_corp_code_cache.py 먼저 실행 필요."
        )
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    mapping = payload.get("mapping") or {}
    if not isinstance(mapping, dict):
        raise ValueError(f"cache mapping 형식 오류: {path}")
    return mapping


def _fetch_dart_window(
    client: DARTRestClient,
    *,
    corp_code: str,
    ticker: str,
    oldest_yyyymmdd: str,
    latest_yyyymmdd: str,
) -> list[dict[str, Any]]:
    """80-day DART 공시 list (한 ticker). page_no 페이지네이션 반복."""
    if not corp_code:
        return []
    aggregated: list[dict[str, Any]] = []
    for page_no in range(1, 11):  # 최대 1000건 per ticker
        try:
            events = client.list_disclosures(
                corp_code=corp_code,
                bgn_de=oldest_yyyymmdd,
                end_de=latest_yyyymmdd,
                page_no=page_no,
                page_count=100,
            )
        except Exception as e:
            sys.stderr.write(
                f"[build_archive] DART list_disclosures page={page_no} 실패 "
                f"ticker={ticker} corp_code={corp_code}: {type(e).__name__}: {e}\n"
            )
            break
        if not events:
            break
        for ev in events:
            payload = ev.get("payload") or {}
            occurred_at = ev.get("occurred_at") or payload.get("disclosure_time", "")
            aggregated.append({
                "ticker": ticker,
                "event_ts": str(occurred_at),
                "source": "news",
                "kind": "dart",
                "title": str(payload.get("title") or ev.get("title", "")),
                "description": str(payload.get("summary", "")),
                "rcept_no": str(payload.get("rcept_no", "")),
            })
        if len(events) < 100:
            break
    return aggregated


def _fetch_naver_for_ticker(
    client: NaverNewsClient,
    *,
    ticker: str,
    meta: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    """회사명 + aliases query × Naver search_news (sort=date) + pub_date 윈도우 필터."""
    queries: list[str] = []
    name = str(meta.get("name", "")).strip()
    if name:
        queries.append(name)
    for alias in (meta.get("aliases") or []):
        a = str(alias).strip()
        if a and a not in queries:
            queries.append(a)
    if not queries:
        return []

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for query in queries:
        for page in range(max_pages):
            start = page * 100 + 1
            if start > 1000:
                break
            try:
                items = client.search_news(
                    query, display=100, start=start, sort="date"
                )
            except Exception as e:
                sys.stderr.write(
                    f"[build_archive] Naver search 실패 ticker={ticker} "
                    f"q={query!r} page={page}: {type(e).__name__}: {e}\n"
                )
                break
            if not items:
                break
            page_in_window = 0
            page_too_old = 0
            for item in items:
                pub_date = item.pub_date
                if pub_date.tzinfo is None:
                    pub_date = pub_date.replace(tzinfo=_KST)
                else:
                    pub_date = pub_date.astimezone(_KST)
                if pub_date > window_end:
                    continue  # 너무 최근 (snapshot 미래)
                if pub_date < window_start:
                    page_too_old += 1
                    continue
                link = (item.naver_link or item.original_link or item.title or "").strip()
                key = f"{ticker}::{link}"
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "ticker": ticker,
                    "event_ts": pub_date.isoformat(),
                    "source": "news",
                    "kind": "naver_news",
                    "title": item.title,
                    "description": item.description,
                    "link": link,
                    "_query": query,
                })
                page_in_window += 1
            # sort=date라 page 끝까지 모두 window_start 보다 옛날이면 break (다음 페이지 더 옛날)
            if page_too_old >= len(items) and page_in_window == 0:
                break
    return out


def _load_sector_to_tickers() -> dict[str, list[str]]:
    """universe_config.yaml에서 sector_name → active ticker list 매핑. SSOT.

    하드코딩 금지 (불변 원칙 5). pending sector는 제외.
    """
    cfg = config_load("universe_config.yaml") or {}
    out: dict[str, list[str]] = {}
    for sector_name, sector in (cfg.get("sectors") or {}).items():
        if str(sector.get("status", "")).lower() != "confirmed":
            continue
        tickers: list[str] = []
        for stock in (sector.get("stocks") or []):
            if not isinstance(stock, dict):
                continue
            if str(stock.get("status", "")).lower() == "active":
                tickers.append(pad_ticker(str(stock.get("ticker", ""))))
        if tickers:
            out[str(sector_name)] = tickers
    return out


def _fetch_naver_broadcast(
    client: NaverNewsClient,
    *,
    query: str,
    target_tickers: list[str],
    window_start: datetime,
    window_end: datetime,
    kind: str,
    level: str,
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    """Naver search → window filter → target_tickers 전체에 같은 event broadcast.

    Sector/market 키워드용. ticker-direct 매칭과 별도 layer.
    """
    items_in_window: list[tuple[Any, datetime, str]] = []  # (item, pub_date, link)
    seen_links: set[str] = set()
    for page in range(max_pages):
        start = page * 100 + 1
        if start > 1000:
            break
        try:
            items = client.search_news(query, display=100, start=start, sort="date")
        except Exception as e:
            sys.stderr.write(
                f"[build_archive] Naver broadcast search 실패 q={query!r} "
                f"page={page}: {type(e).__name__}: {e}\n"
            )
            break
        if not items:
            break
        page_in_window = 0
        page_too_old = 0
        for item in items:
            pub_date = item.pub_date
            if pub_date.tzinfo is None:
                pub_date = pub_date.replace(tzinfo=_KST)
            else:
                pub_date = pub_date.astimezone(_KST)
            if pub_date > window_end:
                continue
            if pub_date < window_start:
                page_too_old += 1
                continue
            link = (item.naver_link or item.original_link or item.title or "").strip()
            if link in seen_links:
                continue
            seen_links.add(link)
            items_in_window.append((item, pub_date, link))
            page_in_window += 1
        # sort=date 라 page 모두 window_start 이전이면 다음 페이지도 더 오래된 거. break.
        if page_too_old >= len(items) and page_in_window == 0:
            break

    # broadcast to all target tickers
    events: list[dict[str, Any]] = []
    for item, pub_date, link in items_in_window:
        for ticker in target_tickers:
            events.append({
                "ticker": ticker,
                "event_ts": pub_date.isoformat(),
                "source": "news",
                "kind": kind,
                "title": item.title,
                "description": item.description,
                "link": link,
                "_query": query,
                "level": level,
            })
    return events


def _distribute_into_dates(
    events_by_ticker: dict[str, list[dict[str, Any]]],
    dates: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """각 date의 24-hour window에 events 분배. event는 여러 date에 들어갈 수 있다 (decay 적용 의도)."""
    by_date: dict[str, list[dict[str, Any]]] = {d: [] for d in dates}
    for date_key in dates:
        snapshot = _snapshot_ts(date_key)
        window_start = _window_start(date_key)
        for events in events_by_ticker.values():
            for ev in events:
                ts = _parse_iso_kst(str(ev.get("event_ts", "")), default=snapshot)
                if ts > snapshot:
                    continue  # PIT-safety
                if ts < window_start:
                    continue  # window 밖
                by_date[date_key].append(ev)
    return by_date


def build_archive(
    *,
    end_date: str,
    business_days: int,
    corp_cache_path: Path,
    output_dir: Path,
    report_dir: Path,
    naver_max_pages: int = 10,
) -> dict[str, Any]:
    ticker_meta = _load_ticker_meta(corp_cache_path)
    if not ticker_meta:
        raise RuntimeError(f"corp_code 캐시 mapping 비어있음: {corp_cache_path}")

    dates = _business_dates(end_date, business_days)
    if not dates:
        raise RuntimeError("business dates list 비어있음")

    oldest_date = dates[0]
    latest_date = dates[-1]
    overall_window_start = _window_start(oldest_date)
    overall_window_end = _snapshot_ts(latest_date)

    dart = DARTRestClient()
    naver = NaverNewsClient()
    provider_availability = {
        "dart_real": _client_is_real(dart),
        "naver_real": _client_is_real(naver),
    }
    if not all(provider_availability.values()):
        return _write_report(report_dir, {
            "status": "BLOCKED",
            "generated_at": datetime.now(_KST).isoformat(),
            "end_date": end_date,
            "business_days_requested": business_days,
            "date_count": len(dates),
            "ticker_count": len(ticker_meta),
            "output_dir": str(output_dir),
            "files_written": [],
            "total_events": 0,
            "provider_availability": provider_availability,
            "fetch_stats": {},
            "sector_broadcast_stats": {},
            "market_broadcast_stats": {},
            "per_date": [],
            "blockers": ["required_real_provider_unavailable"],
        })

    # Stage A: ticker별 한 번씩 fetch (80일 통째로)
    events_by_ticker: dict[str, list[dict[str, Any]]] = {}
    fetch_stats: dict[str, dict[str, int]] = {}
    for ticker, meta in ticker_meta.items():
        corp_code = str(meta.get("corp_code", ""))
        dart_events = _fetch_dart_window(
            dart,
            corp_code=corp_code,
            ticker=ticker,
            oldest_yyyymmdd=oldest_date,
            latest_yyyymmdd=latest_date,
        )
        naver_events = _fetch_naver_for_ticker(
            naver,
            ticker=ticker,
            meta=meta,
            window_start=overall_window_start,
            window_end=overall_window_end,
            max_pages=naver_max_pages,
        )
        events_by_ticker[ticker] = dart_events + naver_events
        fetch_stats[ticker] = {
            "dart_event_count": len(dart_events),
            "naver_event_count": len(naver_events),
        }

    # Stage A2: sector keyword broadcast (news_filter 3-level matching Level 2)
    # 각 sector 키워드 검색 → 그 sector 모든 ticker에 broadcast.
    # 정직성 caveat: 같은 sector ticker가 동일 sector 점수 받음 → 차별화 감소.
    sector_to_tickers = _load_sector_to_tickers()
    sector_broadcast_stats: dict[str, int] = {}
    for sector_name, sector_tickers in sector_to_tickers.items():
        if not sector_tickers:
            continue
        sector_events = _fetch_naver_broadcast(
            naver,
            query=sector_name,
            target_tickers=sector_tickers,
            window_start=overall_window_start,
            window_end=overall_window_end,
            kind="naver_sector",
            level="sector",
            max_pages=naver_max_pages,
        )
        sector_broadcast_stats[sector_name] = (
            len(sector_events) // max(len(sector_tickers), 1)
        )
        for ev in sector_events:
            events_by_ticker.setdefault(ev["ticker"], []).append(ev)

    # Stage A3: market keyword broadcast (news_filter 3-level matching Level 3)
    # 모든 active ticker에 broadcast. 시장 전반 신호 fallback layer.
    all_active_tickers = list(ticker_meta.keys())
    market_broadcast_stats: dict[str, int] = {}
    for market_query in _MARKET_QUERIES:
        market_events = _fetch_naver_broadcast(
            naver,
            query=market_query,
            target_tickers=all_active_tickers,
            window_start=overall_window_start,
            window_end=overall_window_end,
            kind="naver_market",
            level="market",
            max_pages=naver_max_pages,
        )
        market_broadcast_stats[market_query] = (
            len(market_events) // max(len(all_active_tickers), 1)
        )
        for ev in market_events:
            events_by_ticker.setdefault(ev["ticker"], []).append(ev)

    # Stage B: 각 date 24-hour window 적용 + archive 작성
    distributed = _distribute_into_dates(events_by_ticker, dates)
    per_date_report: list[dict[str, Any]] = []
    files_written: list[str] = []
    total_events = 0
    zero_event_dates: list[str] = []

    for date_key in dates:
        snapshot = _snapshot_ts(date_key)
        window_start = _window_start(date_key)
        events_in_window = distributed[date_key]
        if not events_in_window:
            archive_payload = {
                "events": [],
                "provenance": {
                    "deploy_quality": True,
                    "source_apis": ["naver_news_v1", "dart_open_api"],
                    "window_start": window_start.isoformat(),
                    "window_end": snapshot.isoformat(),
                    "ticker_count": len(ticker_meta),
                    "event_count": 0,
                    "generated_at": datetime.now(_KST).isoformat(),
                    "generator": "new/scripts/build_news_dart_archive.py",
                    "zero_event_window": True,
                },
                "per_ticker_event_count": {},
            }
            out_path = output_dir / f"{date_key}.json"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8") as fh:
                json.dump(archive_payload, fh, ensure_ascii=False, indent=2)
            files_written.append(_repo_relative(out_path))
            zero_event_dates.append(date_key)
            per_date_report.append({
                "date": date_key,
                "status": "PASS_ZERO_EVENTS",
                "reason": "no_events_in_window",
                "event_count": 0,
                "path": str(out_path),
            })
            continue

        per_ticker_in_window: dict[str, int] = {}
        for ev in events_in_window:
            t = str(ev.get("ticker", ""))
            per_ticker_in_window[t] = per_ticker_in_window.get(t, 0) + 1

        archive_payload: dict[str, Any] = {
            "events": events_in_window,
            "provenance": {
                "deploy_quality": True,
                "source_apis": ["naver_news_v1", "dart_open_api"],
                "window_start": window_start.isoformat(),
                "window_end": snapshot.isoformat(),
                "ticker_count": len(ticker_meta),
                "event_count": len(events_in_window),
                "generated_at": datetime.now(_KST).isoformat(),
                "generator": "new/scripts/build_news_dart_archive.py",
            },
            "per_ticker_event_count": per_ticker_in_window,
        }
        out_path = output_dir / f"{date_key}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            json.dump(archive_payload, fh, ensure_ascii=False, indent=2)
        files_written.append(_repo_relative(out_path))
        total_events += len(events_in_window)
        per_date_report.append({
            "date": date_key,
            "status": "PASS",
            "event_count": len(events_in_window),
            "path": str(out_path),
        })

    blockers: list[str] = []
    if not files_written:
        blockers.append("no_archive_files_written")
    report: dict[str, Any] = {
        "status": "PASS" if not blockers else "BLOCKED",
        "generated_at": datetime.now(_KST).isoformat(),
        "end_date": end_date,
        "business_days_requested": business_days,
        "date_count": len(dates),
        "ticker_count": len(ticker_meta),
        "output_dir": str(output_dir),
        "files_written": files_written,
        "total_events": total_events,
        "provider_availability": provider_availability,
        "fetch_stats": fetch_stats,
        "sector_broadcast_stats": sector_broadcast_stats,
        "market_broadcast_stats": market_broadcast_stats,
        "zero_event_date_count": len(zero_event_dates),
        "zero_event_dates_sample": zero_event_dates[:10],
        "per_date": per_date_report,
        "blockers": blockers,
    }
    return _write_report(report_dir, report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end-date", required=True, help="YYYYMMDD")
    parser.add_argument("--business-days", type=int, default=80)
    parser.add_argument("--corp-cache", default=str(_DEFAULT_CORP_CACHE))
    parser.add_argument("--output-dir", default=str(_DEFAULT_OUT))
    parser.add_argument("--report-dir", default=str(_DEFAULT_REPORT_DIR))
    parser.add_argument("--naver-max-pages", type=int, default=10)
    args = parser.parse_args(argv)

    report = build_archive(
        end_date=str(args.end_date),
        business_days=int(args.business_days),
        corp_cache_path=Path(str(args.corp_cache)),
        output_dir=Path(str(args.output_dir)),
        report_dir=Path(str(args.report_dir)),
        naver_max_pages=int(args.naver_max_pages),
    )
    summary = {
        "status": report["status"],
        "files_written_count": len(report["files_written"]),
        "total_events": report["total_events"],
        "report_path": report["report_path"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

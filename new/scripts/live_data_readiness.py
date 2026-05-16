"""Live data readiness runner.

This script intentionally does not read ``.env`` files. It only uses process
environment variables through existing connector/AuthManager code.

Stages:
  1. Connector smoke: KIS investor, KRX investor bridge, Community, KOSPI batch
     price snapshot, ECOS macro.
  2. Optional KIS 1m backfill save for the active universe.
  3. Optional LightGBM train only when enough real artifact dates exist.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
NEW_ROOT = REPO_ROOT / "new"
if str(NEW_ROOT) not in sys.path:
    sys.path.insert(0, str(NEW_ROOT))

from src.connectors.community import CommunityCrawler  # noqa: E402
from src.connectors.dart_rest import DARTRestClient  # noqa: E402
from src.connectors.ecos_rest import ECOSRestClient  # noqa: E402
from src.connectors.kis_rest import KISRestClient  # noqa: E402
from src.connectors.krx_rest import KRXRestClient  # noqa: E402
from src.connectors.naver_rest import NaverNewsClient  # noqa: E402
from src.connectors.us_market import USMarketClient  # noqa: E402
from src.data.backfill import Backfill  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.safe_cast import safe_bool, safe_int  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402
from src.utils.trading_calendar import (  # noqa: E402
    kospi_trading_dates_between,
    kospi_trading_start_date,
    previous_kospi_trading_day,
)

_KST = ZoneInfo("Asia/Seoul")
_DATA_ROOT = REPO_ROOT / "artifacts" / "data"
_REPORT_ROOT = REPO_ROOT / "artifacts" / "reports" / "data_readiness"
_UNIVERSE_PATH = NEW_ROOT / "config" / "universe_config.yaml"


def _status(ok: bool, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"status": "PASS" if ok else "FAIL"}
    if detail:
        payload.update(detail)
    return payload


def _mock_blocked(source: str, allow_mock: bool, detail: dict[str, Any]) -> dict[str, Any] | None:
    if allow_mock:
        return None
    return _status(
        False,
        {
            "error_code": "MOCK_SOURCE_NOT_ALLOWED",
            "message": f"{source} is running in mock mode. Pass --allow-mock only for CI/demo.",
            **detail,
        },
    )


def _runtime_metadata() -> dict[str, Any]:
    secret_keys = [
        "KIS_MODE",
        "KIS_APP_KEY",
        "KIS_APP_SECRET",
        "KIS_ACCOUNT_NUMBER",
        "KIS_ACCOUNT_PRODUCT_CODE",
        "DART_API_KEY",
        "KRX_API_KEY",
        "NAVER_CLIENT_ID",
        "NAVER_CLIENT_SECRET",
        "ECOS_API_KEY",
        "COMMUNITY_SCRAPE_ENABLED",
    ]
    versions: dict[str, str | None] = {}
    for module_name in ("numpy", "pandas", "lightgbm"):
        try:
            module = __import__(module_name)
            versions[module_name] = str(getattr(module, "__version__", "unknown"))
        except Exception:
            versions[module_name] = None
    return {
        "python_executable": sys.executable,
        "conda_default_env": os.environ.get("CONDA_DEFAULT_ENV"),
        "kis_mode": os.environ.get("KIS_MODE"),
        "secret_presence": {key: bool(os.environ.get(key)) for key in secret_keys},
        "dependency_versions": versions,
    }


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _previous_business_day(today: date | None = None) -> date:
    return previous_kospi_trading_day(today)


def _business_start_date(end: date, business_days: int) -> date:
    return kospi_trading_start_date(end, business_days)


def _business_dates_between(start: date, end: date) -> list[str]:
    return kospi_trading_dates_between(start, end)


def _date_str(value: date) -> str:
    return value.strftime("%Y%m%d")


def _iso_date_str(yyyymmdd: str) -> str:
    return datetime.strptime(yyyymmdd, "%Y%m%d").strftime("%Y-%m-%d")


def _community_post_date(post: Any) -> str | None:
    """Community raw post timestamp를 YYYYMMDD 문자열로 추출."""
    raw_ts = getattr(post, "timestamp", None)
    if raw_ts is None and isinstance(post, dict):
        raw_ts = post.get("timestamp") or post.get("posted_at")

    if isinstance(raw_ts, datetime):
        ts = raw_ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_KST)
        return ts.astimezone(_KST).strftime("%Y%m%d")

    if raw_ts:
        token = str(raw_ts)[:10].replace("-", "")
        if len(token) == 8 and token.isdigit():
            return token
    return None


def _final_dataset_gate_cfg() -> dict[str, Any]:
    cfg = config_load("risk_config.yaml", "backtest_agent") or {}
    gate_cfg = (
        cfg.get("deploy_decision_gate", {})
        .get("final_dataset_gate", {})
    )
    return gate_cfg if isinstance(gate_cfg, dict) else {}


def _load_active_tickers(max_tickers: int | None) -> list[str]:
    with _UNIVERSE_PATH.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    gate_cfg = _final_dataset_gate_cfg()
    include_pending = safe_bool(
        gate_cfg.get("include_pending_data_tickers"),
        default=False,
    )
    allowed_stock_statuses = {"active"}
    allowed_sector_statuses = {"confirmed"}
    if include_pending:
        allowed_stock_statuses = {
            str(status)
            for status in gate_cfg.get("allowed_stock_statuses", ["active", "pending_data"])
        }
        allowed_sector_statuses = {
            str(status)
            for status in gate_cfg.get(
                "allowed_sector_statuses",
                ["confirmed", "confirmed_pending_data"],
            )
        }
    tickers: list[str] = []
    for sector in (cfg.get("sectors") or {}).values():
        if str(sector.get("status")) not in allowed_sector_statuses:
            continue
        for stock in sector.get("stocks", []) or []:
            if str(stock.get("status")) in allowed_stock_statuses:
                tickers.append(pad_ticker(str(stock["ticker"])))
    if max_tickers is not None:
        tickers = tickers[:max_tickers]
    return tickers


def _rows_for_date(rows: list[dict[str, Any]], yyyymmdd: str) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if str(row.get("date", ""))[:10].replace("-", "") == yyyymmdd
    ]


def _has_fields(payload: dict[str, Any] | None, fields: tuple[str, ...]) -> bool:
    if not isinstance(payload, dict):
        return False
    return all(payload.get(field) is not None for field in fields)


def _artifact_dates(tickers: list[str]) -> list[str]:
    dates: set[str] = set()
    for ticker in tickers:
        ticker_dir = _DATA_ROOT / pad_ticker(ticker)
        if not ticker_dir.exists():
            continue
        for file_path in ticker_dir.iterdir():
            name = file_path.name
            if not name.startswith("bars_1m_"):
                continue
            date_part = name.removeprefix("bars_1m_").split(".", 1)[0]
            if len(date_part) == 8 and date_part.isdigit():
                dates.add(date_part)
    return sorted(dates)


def _readiness_cfg() -> dict[str, Any]:
    return config_load("risk_config.yaml", "live_data_readiness") or {}


def _readiness_min_rows(key: str) -> int:
    cfg = _readiness_cfg()
    if key in cfg:
        return int(cfg[key])
    wf = config_load("risk_config.yaml", "walk_forward") or {}
    return int(wf["trading_minutes_per_day"])


def _saved_file_summary(
    tickers: list[str],
    start: str,
    end: str,
    min_rows_per_day: int | None = None,
) -> dict[str, Any]:
    min_rows = min_rows_per_day or _readiness_min_rows("min_rows_per_day")
    summary: dict[str, Any] = {}
    for ticker in tickers:
        ticker_dir = _DATA_ROOT / pad_ticker(ticker)
        files: list[str] = []
        relative_files: list[str] = []
        row_counts: dict[str, int | None] = {}
        valid_dates: dict[str, bool] = {}
        timestamp_dates_match: dict[str, bool | None] = {}
        ticker_matches: dict[str, bool | None] = {}
        missing_timestamp_counts: dict[str, int | None] = {}
        ticker_mismatch_counts: dict[str, int | None] = {}
        duplicate_ts_counts: dict[str, int | None] = {}
        out_of_hours_counts: dict[str, int | None] = {}
        first_ts_by_date: dict[str, str | None] = {}
        last_ts_by_date: dict[str, str | None] = {}
        session_span_minutes_by_date: dict[str, float | None] = {}
        session_span_ok_by_date: dict[str, bool | None] = {}
        max_gap_minutes_by_date: dict[str, float | None] = {}
        max_gap_ok_by_date: dict[str, bool | None] = {}
        missing_ohlcv_counts: dict[str, int | None] = {}
        non_finite_ohlcv_counts: dict[str, int | None] = {}
        invalid_ohlcv_counts: dict[str, int | None] = {}
        duplicate_date_artifacts: dict[str, list[str]] = {}
        file_mtime_ns: dict[str, int] = {}
        if ticker_dir.exists():
            paths_by_date: dict[str, list[Path]] = {}
            for file_path in sorted(ticker_dir.iterdir()):
                name = file_path.name
                if not name.startswith("bars_1m_"):
                    continue
                date_part = name.removeprefix("bars_1m_").split(".", 1)[0]
                if start <= date_part <= end:
                    paths_by_date.setdefault(date_part, []).append(file_path)
            for date_part, paths in sorted(paths_by_date.items()):
                if len(paths) > 1:
                    duplicate_date_artifacts[date_part] = [
                        _repo_relative(path) for path in paths
                    ]
                file_path = _preferred_bar_artifact(paths)
                files.append(str(file_path))
                relative_files.append(_repo_relative(file_path))
                inspection = _inspect_bar_file(
                    file_path,
                    date_part,
                    pad_ticker(ticker),
                    min_rows_per_day=int(min_rows),
                )
                rows = inspection.get("rows")
                row_counts[date_part] = rows
                timestamp_dates_match[date_part] = inspection.get("timestamp_dates_match")
                ticker_matches[date_part] = inspection.get("ticker_matches")
                missing_timestamp_counts[date_part] = inspection.get("missing_timestamp_count")
                ticker_mismatch_counts[date_part] = inspection.get("ticker_mismatch_count")
                duplicate_ts_counts[date_part] = inspection.get("duplicate_ts_count")
                out_of_hours_counts[date_part] = inspection.get("out_of_hours_count")
                first_ts_by_date[date_part] = inspection.get("first_ts")
                last_ts_by_date[date_part] = inspection.get("last_ts")
                session_span_minutes_by_date[date_part] = inspection.get("session_span_minutes")
                session_span_ok_by_date[date_part] = inspection.get("session_span_ok")
                max_gap_minutes_by_date[date_part] = inspection.get("max_gap_minutes")
                max_gap_ok_by_date[date_part] = inspection.get("max_gap_ok")
                missing_ohlcv_counts[date_part] = inspection.get("missing_ohlcv_count")
                non_finite_ohlcv_counts[date_part] = inspection.get("non_finite_ohlcv_count")
                invalid_ohlcv_counts[date_part] = inspection.get("invalid_ohlcv_count")
                file_mtime_ns[date_part] = int(file_path.stat().st_mtime_ns)
                valid_dates[date_part] = (
                    rows is not None and int(rows) >= int(min_rows)
                    and inspection.get("timestamp_dates_match") is True
                    and inspection.get("ticker_matches") is True
                    and int(inspection.get("duplicate_ts_count") or 0) == 0
                    and int(inspection.get("out_of_hours_count") or 0) == 0
                    and inspection.get("session_span_ok") is True
                    and inspection.get("max_gap_ok") is True
                    and int(inspection.get("missing_ohlcv_count") or 0) == 0
                    and int(inspection.get("non_finite_ohlcv_count") or 0) == 0
                    and int(inspection.get("invalid_ohlcv_count") or 0) == 0
                    and date_part not in duplicate_date_artifacts
                )
        summary[pad_ticker(ticker)] = {
            "files": files,
            "relative_files": relative_files,
            "file_count": len(files),
            "row_counts": row_counts,
            "valid_dates": valid_dates,
            "timestamp_dates_match": timestamp_dates_match,
            "ticker_matches": ticker_matches,
            "missing_timestamp_counts": missing_timestamp_counts,
            "ticker_mismatch_counts": ticker_mismatch_counts,
            "duplicate_ts_counts": duplicate_ts_counts,
            "out_of_hours_counts": out_of_hours_counts,
            "first_ts": first_ts_by_date,
            "last_ts": last_ts_by_date,
            "session_span_minutes": session_span_minutes_by_date,
            "session_span_ok": session_span_ok_by_date,
            "max_gap_minutes": max_gap_minutes_by_date,
            "max_gap_ok": max_gap_ok_by_date,
            "missing_ohlcv_counts": missing_ohlcv_counts,
            "non_finite_ohlcv_counts": non_finite_ohlcv_counts,
            "invalid_ohlcv_counts": invalid_ohlcv_counts,
            "duplicate_date_artifacts": duplicate_date_artifacts,
            "file_mtime_ns": file_mtime_ns,
        }
    return summary


def _artifact_date_quality(
    tickers: list[str],
    start: str,
    end: str,
    min_rows_per_day: int,
) -> dict[str, Any]:
    start_dt = datetime.strptime(start, "%Y%m%d").date()
    end_dt = datetime.strptime(end, "%Y%m%d").date()
    business_dates = _business_dates_between(start_dt, end_dt)
    files = _saved_file_summary(tickers, start, end, min_rows_per_day)
    quality: dict[str, Any] = {}

    for day in business_dates:
        valid_tickers: list[str] = []
        missing_or_short: list[dict[str, Any]] = []
        for ticker in tickers:
            padded = pad_ticker(ticker)
            info = files.get(padded, {})
            row_counts = info.get("row_counts", {})
            valid_dates = info.get("valid_dates", {})
            timestamp_dates_match = info.get("timestamp_dates_match", {})
            ticker_matches = info.get("ticker_matches", {})
            missing_timestamp_counts = info.get("missing_timestamp_counts", {})
            ticker_mismatch_counts = info.get("ticker_mismatch_counts", {})
            duplicate_ts_counts = info.get("duplicate_ts_counts", {})
            out_of_hours_counts = info.get("out_of_hours_counts", {})
            first_ts = info.get("first_ts", {})
            last_ts = info.get("last_ts", {})
            session_span_minutes = info.get("session_span_minutes", {})
            session_span_ok = info.get("session_span_ok", {})
            max_gap_minutes = info.get("max_gap_minutes", {})
            max_gap_ok = info.get("max_gap_ok", {})
            missing_ohlcv_counts = info.get("missing_ohlcv_counts", {})
            non_finite_ohlcv_counts = info.get("non_finite_ohlcv_counts", {})
            invalid_ohlcv_counts = info.get("invalid_ohlcv_counts", {})
            duplicate_date_artifacts = info.get("duplicate_date_artifacts", {})
            rows = row_counts.get(day)
            if valid_dates.get(day) is True:
                valid_tickers.append(padded)
            else:
                missing_or_short.append({
                    "ticker": padded,
                    "rows": rows,
                    "timestamp_dates_match": timestamp_dates_match.get(day),
                    "ticker_matches": ticker_matches.get(day),
                    "missing_timestamp_count": missing_timestamp_counts.get(day),
                    "ticker_mismatch_count": ticker_mismatch_counts.get(day),
                    "duplicate_ts_count": duplicate_ts_counts.get(day),
                    "out_of_hours_count": out_of_hours_counts.get(day),
                    "first_ts": first_ts.get(day),
                    "last_ts": last_ts.get(day),
                    "session_span_minutes": session_span_minutes.get(day),
                    "session_span_ok": session_span_ok.get(day),
                    "max_gap_minutes": max_gap_minutes.get(day),
                    "max_gap_ok": max_gap_ok.get(day),
                    "missing_ohlcv_count": missing_ohlcv_counts.get(day),
                    "non_finite_ohlcv_count": non_finite_ohlcv_counts.get(day),
                    "invalid_ohlcv_count": invalid_ohlcv_counts.get(day),
                    "duplicate_date_artifacts": duplicate_date_artifacts.get(day, []),
                    "valid_date": valid_dates.get(day, False),
                })
        quality[day] = {
            "is_valid": not missing_or_short,
            "valid_ticker_count": len(valid_tickers),
            "expected_ticker_count": len(tickers),
            "missing_or_short_tickers": missing_or_short,
        }
    return quality


def _count_bar_file_rows(file_path: Path) -> int | None:
    """parquet/jsonl row count. 읽기 실패 시 None."""
    try:
        rows = _load_bar_rows(file_path)
        return len(rows) if rows is not None else None
    except Exception:
        return None
    return None


def _preferred_bar_artifact(paths: list[Path]) -> Path:
    """Prefer parquet over jsonl for the same ticker/date, then newest mtime."""
    return sorted(
        paths,
        key=lambda path: (
            0 if path.suffix == ".parquet" else 1,
            -int(path.stat().st_mtime_ns),
            path.name,
        ),
    )[0]


def _load_bar_rows(file_path: Path) -> list[dict[str, Any]] | None:
    if file_path.suffix == ".jsonl":
        rows: list[dict[str, Any]] = []
        with file_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    rows.append(json.loads(line))
        return rows
    if file_path.suffix == ".parquet":
        import pandas as pd  # type: ignore[import]

        return pd.read_parquet(file_path).to_dict("records")
    return None


def _parse_bar_timestamp(row: dict[str, Any]) -> datetime | None:
    for field in ("ts_close", "timestamp", "ts", "datetime"):
        raw = row.get(field)
        if raw is None:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_KST)
        return parsed.astimezone(_KST)
    return None


def _inspect_bar_file(
    file_path: Path,
    yyyymmdd: str,
    ticker: str,
    min_rows_per_day: int | None = None,
) -> dict[str, Any]:
    """Inspect one saved bar artifact for date/ticker/timestamp integrity."""
    empty_inspection = {
        "rows": None,
        "timestamp_dates_match": None,
        "ticker_matches": None,
        "missing_timestamp_count": None,
        "ticker_mismatch_count": None,
        "duplicate_ts_count": None,
        "out_of_hours_count": None,
        "first_ts": None,
        "last_ts": None,
        "session_span_minutes": None,
        "session_span_ok": None,
        "max_gap_minutes": None,
        "max_gap_ok": None,
        "missing_ohlcv_count": None,
        "non_finite_ohlcv_count": None,
        "invalid_ohlcv_count": None,
    }
    expected_date = date(
        int(yyyymmdd[:4]),
        int(yyyymmdd[4:6]),
        int(yyyymmdd[6:]),
    )
    try:
        rows = _load_bar_rows(file_path)
    except Exception:
        return empty_inspection
    if rows is None:
        return empty_inspection

    timestamps = [_parse_bar_timestamp(row) for row in rows]
    present_timestamps = [ts for ts in timestamps if ts is not None]
    timestamp_dates = {ts.date() for ts in present_timestamps}
    missing_timestamp_count = len(rows) - len(present_timestamps)
    timestamp_dates_match = (
        bool(rows)
        and missing_timestamp_count == 0
        and timestamp_dates == {expected_date}
    )

    expected_ticker = pad_ticker(ticker)
    row_tickers = [
        pad_ticker(str(row.get("ticker")))
        if row.get("ticker") is not None
        else None
        for row in rows
    ]
    ticker_mismatch_count = sum(1 for row_ticker in row_tickers if row_ticker != expected_ticker)
    ticker_matches = bool(rows) and ticker_mismatch_count == 0

    iso_timestamps = [ts.isoformat() for ts in present_timestamps]
    duplicate_ts_count = len(iso_timestamps) - len(set(iso_timestamps))
    market_open = time(9, 0)
    market_close = time(15, 30)
    out_of_hours_count = sum(
        1
        for ts in present_timestamps
        if ts.time() < market_open or ts.time() > market_close
    )
    sorted_timestamps = sorted(present_timestamps)
    first_ts = sorted_timestamps[0] if sorted_timestamps else None
    last_ts = sorted_timestamps[-1] if sorted_timestamps else None
    session_span_minutes = (
        (last_ts - first_ts).total_seconds() / 60.0
        if first_ts is not None and last_ts is not None
        else None
    )
    gap_minutes = [
        (right - left).total_seconds() / 60.0
        for left, right in zip(sorted_timestamps, sorted_timestamps[1:])
    ]
    max_gap_minutes = max(gap_minutes) if gap_minutes else (0.0 if sorted_timestamps else None)
    readiness_cfg = _readiness_cfg()
    min_session_span = float(
        readiness_cfg.get(
            "min_session_span_minutes",
            min_rows_per_day or _readiness_min_rows("min_rows_per_day"),
        )
    )
    max_allowed_gap = float(readiness_cfg.get("max_bar_gap_minutes", 5))
    session_span_ok = (
        timestamp_dates_match is True
        and session_span_minutes is not None
        and session_span_minutes >= min_session_span
    )
    max_gap_ok = (
        timestamp_dates_match is True
        and max_gap_minutes is not None
        and max_gap_minutes <= max_allowed_gap
    )
    required_ohlcv = ("open", "high", "low", "close", "volume")
    missing_ohlcv_count = 0
    non_finite_ohlcv_count = 0
    invalid_ohlcv_count = 0
    for row in rows:
        if any(row.get(field) is None for field in required_ohlcv):
            missing_ohlcv_count += 1
            continue
        try:
            open_price = float(row["open"])
            high_price = float(row["high"])
            low_price = float(row["low"])
            close_price = float(row["close"])
            volume = float(row["volume"])
        except (TypeError, ValueError):
            non_finite_ohlcv_count += 1
            continue
        values = (open_price, high_price, low_price, close_price, volume)
        if not all(math.isfinite(value) for value in values):
            non_finite_ohlcv_count += 1
            continue
        if (
            open_price <= 0
            or high_price <= 0
            or low_price <= 0
            or high_price < low_price
            or high_price < max(open_price, close_price)
            or low_price > min(open_price, close_price)
            or close_price <= 0
            or volume < 0
        ):
            invalid_ohlcv_count += 1
    return {
        "rows": len(rows),
        "timestamp_dates_match": timestamp_dates_match,
        "ticker_matches": ticker_matches,
        "missing_timestamp_count": missing_timestamp_count,
        "ticker_mismatch_count": ticker_mismatch_count,
        "duplicate_ts_count": duplicate_ts_count,
        "out_of_hours_count": out_of_hours_count,
        "first_ts": first_ts.isoformat() if first_ts else None,
        "last_ts": last_ts.isoformat() if last_ts else None,
        "session_span_minutes": session_span_minutes,
        "session_span_ok": session_span_ok,
        "max_gap_minutes": max_gap_minutes,
        "max_gap_ok": max_gap_ok,
        "missing_ohlcv_count": missing_ohlcv_count,
        "non_finite_ohlcv_count": non_finite_ohlcv_count,
        "invalid_ohlcv_count": invalid_ohlcv_count,
    }


def _bar_file_date_matches(file_path: Path, yyyymmdd: str) -> bool | None:
    """bars_1m 파일 내부 timestamp가 파일명 날짜와 일치하는지 확인."""
    expected = f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"
    timestamp_fields = ("ts_close", "timestamp", "ts", "datetime")
    try:
        if file_path.suffix == ".jsonl":
            seen_dates: set[str] = set()
            with file_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    for field in timestamp_fields:
                        raw = row.get(field)
                        if raw is not None:
                            seen_dates.add(str(raw)[:10])
                            break
            return bool(seen_dates) and seen_dates == {expected}
        if file_path.suffix == ".parquet":
            import pandas as pd  # type: ignore[import]

            df = pd.read_parquet(file_path)
            for field in timestamp_fields:
                if field in df.columns:
                    dates = set(df[field].astype(str).str.slice(0, 10).dropna().unique())
                    return bool(dates) and dates == {expected}
            return None
    except Exception:
        return None
    return None


def run_smoke(tickers: list[str], as_of_date: str, allow_mock: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}

    kis = KISRestClient()

    try:
        blocked = _mock_blocked("kis_investor_daily", allow_mock, {"source_mode": kis.mode})
        if blocked is not None and kis.mode == "mock":
            result["kis_investor_daily"] = blocked
        else:
            investor_rows = kis.investor_trade_by_stock_daily(tickers[0], as_of_date)
            date_rows = _rows_for_date(investor_rows, as_of_date)
            required_flow = (
                "foreign_net_buy",
                "institutional_net_buy",
                "retail_net_buy",
            )
            valid_date_rows = [
                row for row in date_rows
                if _has_fields(row, required_flow)
            ]
            result["kis_investor_daily"] = _status(
                bool(valid_date_rows),
                {
                    "source_mode": kis.mode,
                    "total_rows": len(investor_rows),
                    "requested_date_rows": len(date_rows),
                    "valid_requested_date_rows": len(valid_date_rows),
                    "required_fields": list(required_flow),
                    "sample": date_rows[:1] or investor_rows[:1],
                },
            )
    except Exception as e:
        result["kis_investor_daily"] = _status(False, {"error": str(e)})

    try:
        krx = KRXRestClient(auth=kis.auth)
        is_mock = kis.mode == "mock" or bool(getattr(krx, "_is_mock", False))
        blocked = _mock_blocked(
            "krx_investor_bridge",
            allow_mock,
            {"provider_mode": kis.mode, "is_mock": is_mock},
        )
        if blocked is not None and is_mock:
            result["krx_investor_bridge"] = blocked
        else:
            events = krx.get_investor_info(tickers[0], as_of_date, as_of_date)
            required_flow = (
                "foreign_net_buy",
                "institutional_net_buy",
                "retail_net_buy",
            )
            valid_events = [
                event for event in events
                if _has_fields(event.get("payload"), required_flow)
            ]
            result["krx_investor_bridge"] = _status(
                bool(valid_events),
                {
                    "provider_mode": kis.mode,
                    "is_mock": getattr(krx, "_is_mock", None),
                    "event_count": len(events),
                    "valid_event_count": len(valid_events),
                    "required_fields": list(required_flow),
                    "sample_payload": events[0].get("payload") if events else None,
                },
            )
    except Exception as e:
        result["krx_investor_bridge"] = _status(False, {"error": str(e)})

    try:
        dart = DARTRestClient()
        blocked = (
            _mock_blocked("dart", allow_mock, {"is_mock": True})
            if getattr(dart, "_is_mock", False)
            else None
        )
        if blocked is not None:
            result["dart"] = blocked
        else:
            events = dart.list_disclosures(
                bgn_de=as_of_date,
                end_de=as_of_date,
                page_count=3,
            )
            result["dart"] = _status(
                bool(events),
                {"event_count": len(events), "is_mock": getattr(dart, "_is_mock", None)},
            )
    except Exception as e:
        result["dart"] = _status(False, {"error": str(e)})

    try:
        naver = NaverNewsClient()
        blocked = (
            _mock_blocked("naver", allow_mock, {"is_mock": True})
            if getattr(naver, "_is_mock", False)
            else None
        )
        if blocked is not None:
            result["naver"] = blocked
        else:
            news = naver.search_news("삼성전자", display=3)
            result["naver"] = _status(
                bool(news),
                {"item_count": len(news), "is_mock": getattr(naver, "_is_mock", None)},
            )
    except Exception as e:
        result["naver"] = _status(False, {"error": str(e)})

    try:
        community = CommunityCrawler()
        blocked = (
            _mock_blocked("community", allow_mock, {"is_mock": True})
            if getattr(community, "_is_mock", False)
            else None
        )
        if blocked is not None:
            result["community"] = blocked
        else:
            posts = community.poll(tickers[: min(3, len(tickers))])
            raw_post_count = len(posts)
            post_dates = [_community_post_date(post) for post in posts]
            as_of_aligned_post_count = sum(1 for post_date in post_dates if post_date == as_of_date)
            unknown_date_count = sum(1 for post_date in post_dates if post_date is None)
            as_of_mismatch_count = raw_post_count - as_of_aligned_post_count - unknown_date_count
            result["community"] = _status(
                raw_post_count > 0,
                {
                    "event_count": 0,
                    "raw_post_count": raw_post_count,
                    "as_of_date": as_of_date,
                    "as_of_aligned_post_count": as_of_aligned_post_count,
                    "as_of_mismatch_count": as_of_mismatch_count,
                    "unknown_post_date_count": unknown_date_count,
                    "normalized_in_smoke": False,
                    "is_mock": getattr(community, "_is_mock", None),
                    "note": (
                        "community connector raw posts are reachable; live readiness "
                        "does not C2-normalize raw posts to avoid historical as_of drift"
                    ),
                },
            )
    except Exception as e:
        result["community"] = _status(False, {"error": str(e)})

    try:
        blocked = _mock_blocked("kospi_batch_snapshot", allow_mock, {"source_mode": kis.mode})
        if blocked is not None and kis.mode == "mock":
            result["kospi_batch_snapshot"] = blocked
        else:
            snapshots = kis.get_price_snapshot(tickers)
            result["kospi_batch_snapshot"] = _status(
                len(snapshots) == len(tickers),
                {
                    "source_mode": kis.mode,
                    "requested": len(tickers),
                    "received": len(snapshots),
                    "sample": snapshots[:2],
                },
            )
    except Exception as e:
        result["kospi_batch_snapshot"] = _status(False, {"error": str(e)})

    try:
        ecos = ECOSRestClient()
        blocked = (
            _mock_blocked("ecos_macro", allow_mock, {"is_mock": True})
            if getattr(ecos, "_is_mock", False)
            else None
        )
        if blocked is not None:
            result["ecos_macro"] = blocked
        else:
            macro = ecos.get_macro_pack(as_of_date)
            result["ecos_macro"] = _status(
                macro.get("interest_rate") is not None and macro.get("usd_krw") is not None,
                {"macro": macro, "is_mock": getattr(ecos, "_is_mock", None)},
            )
    except Exception as e:
        result["ecos_macro"] = _status(False, {"error": str(e)})

    try:
        us_market = USMarketClient()
        indices = us_market.get_indices(as_of=_iso_date_str(as_of_date))
        payload = {
            "us_sp500_change": indices.us_sp500_change,
            "us_nasdaq_change": indices.us_nasdaq_change,
            "us_vix": indices.us_vix,
            "us_soxx_change": indices.us_soxx_change,
            "as_of_date": indices.as_of_date,
            "source": indices.source,
        }
        is_mock = bool(getattr(us_market, "_is_mock", False)) or indices.source == "mock"
        blocked = (
            _mock_blocked("us_overnight", allow_mock, {"is_mock": True, "source": indices.source})
            if is_mock
            else None
        )
        if blocked is not None:
            result["us_overnight"] = blocked
        else:
            complete = (
                bool(indices.as_of_date)
                and indices.source != "mock"
                and indices.us_vix > 0
            )
            result["us_overnight"] = _status(
                complete or allow_mock,
                {"indices": payload, "is_mock": is_mock},
            )
    except Exception as e:
        result["us_overnight"] = _status(False, {"error": str(e)})

    return result


def run_backfill(
    tickers: list[str],
    start_date: str,
    end_date: str,
    *,
    skip_existing: bool = False,
) -> dict[str, Any]:
    try:
        cfg = _readiness_cfg()
        min_rows = _readiness_min_rows("min_rows_per_day")
        require_all = safe_bool(cfg.get("require_all_tickers_for_backfill", True), default=True)
        max_failed_dates = safe_int(
            cfg.get("max_consecutive_backfill_failed_dates", 3),
            default=3,
            min_value=1,
        )
        failed_ticker_ratio_threshold = float(
            cfg.get("backfill_failed_ticker_ratio_threshold", 1.0)
        )
        failed_ticker_ratio_threshold = min(max(failed_ticker_ratio_threshold, 0.0), 1.0)
        backfill = Backfill()
        counts = {pad_ticker(ticker): 0 for ticker in tickers}
        fetch_counts_by_date: dict[str, dict[str, int]] = {}
        skipped_existing_dates: list[str] = []
        consecutive_failed_dates = 0
        circuit_breaker: dict[str, Any] = {"triggered": False}
        start = datetime.strptime(start_date, "%Y%m%d").date()
        end = datetime.strptime(end_date, "%Y%m%d").date()
        business_dates = _business_dates_between(start, end)
        existing_files = _saved_file_summary(tickers, start_date, end_date, min_rows)
        for day in business_dates:
            existing_day_counts = {
                pad_ticker(ticker): int(
                    existing_files
                    .get(pad_ticker(ticker), {})
                    .get("row_counts", {})
                    .get(day, 0) or 0
                )
                for ticker in tickers
            }
            has_all_existing = all(
                bool(
                    existing_files
                    .get(pad_ticker(ticker), {})
                    .get("valid_dates", {})
                    .get(day)
                )
                for ticker in tickers
            )
            if skip_existing and has_all_existing:
                skipped_existing_dates.append(day)
                fetch_counts_by_date[day] = existing_day_counts
                for ticker, count in existing_day_counts.items():
                    counts[ticker] = counts.get(ticker, 0) + count
                continue

            day_counts = backfill.backfill_universe(tickers, day, day)
            fetch_counts_by_date[day] = {
                pad_ticker(ticker): int(day_counts.get(pad_ticker(ticker), 0))
                for ticker in tickers
            }
            for ticker, count in day_counts.items():
                counts[pad_ticker(ticker)] = counts.get(pad_ticker(ticker), 0) + count
            short_fetch_count = sum(
                1
                for ticker in tickers
                if int(fetch_counts_by_date[day].get(pad_ticker(ticker), 0)) < min_rows
            )
            short_fetch_ratio = short_fetch_count / max(len(tickers), 1)
            failed_today = (
                short_fetch_ratio >= failed_ticker_ratio_threshold
                if require_all
                else all(int(count) <= 0 for count in fetch_counts_by_date[day].values())
            )
            consecutive_failed_dates = consecutive_failed_dates + 1 if failed_today else 0
            if consecutive_failed_dates >= max_failed_dates:
                circuit_breaker = {
                    "triggered": True,
                    "date": day,
                    "consecutive_failed_dates": consecutive_failed_dates,
                    "max_consecutive_backfill_failed_dates": max_failed_dates,
                    "min_rows_per_day": min_rows,
                    "short_fetch_count": short_fetch_count,
                    "expected_ticker_count": len(tickers),
                    "short_fetch_ratio": short_fetch_ratio,
                    "failed_ticker_ratio_threshold": failed_ticker_ratio_threshold,
                }
                break
        files = _saved_file_summary(tickers, start_date, end_date, min_rows)
        artifact_missing_or_empty: list[str] = []
        current_fetch_missing_or_short: list[dict[str, Any]] = []
        for ticker in tickers:
            padded = pad_ticker(ticker)
            info = files.get(padded, {})
            valid_dates = info.get("valid_dates", {})
            has_all_valid_dates = all(bool(valid_dates.get(day)) for day in business_dates)
            if require_all and not has_all_valid_dates:
                artifact_missing_or_empty.append(padded)
            for day in business_dates:
                fetched = int(fetch_counts_by_date.get(day, {}).get(padded, 0))
                if require_all and fetched < min_rows:
                    current_fetch_missing_or_short.append(
                        {"ticker": padded, "date": day, "fetched_rows": fetched}
                    )
        ok = not artifact_missing_or_empty and not current_fetch_missing_or_short
        return _status(
            ok,
            {
                "business_dates": business_dates,
                "min_rows_per_day": min_rows,
                "counts": counts,
                "fetch_counts_by_date": fetch_counts_by_date,
                "skipped_existing_dates": skipped_existing_dates,
                "files": files,
                "missing_or_empty_tickers": artifact_missing_or_empty,
                "current_fetch_missing_or_short": current_fetch_missing_or_short,
                "backfill_circuit_breaker": circuit_breaker,
            },
        )
    except Exception as e:
        return _status(False, {"error": str(e)})


def run_train_if_ready(
    tickers: list[str],
    start_date: str,
    end_date: str,
    require_train: bool,
) -> dict[str, Any]:
    wf = config_load("risk_config.yaml", "walk_forward")
    min_dates = int(wf["train_window_days"]) + int(wf["test_window_days"])
    cfg = _readiness_cfg()
    min_rows = _readiness_min_rows("train_min_rows_per_day")
    require_all = safe_bool(cfg.get("require_all_tickers_for_train", True), default=True)
    quality = _artifact_date_quality(tickers, start_date, end_date, min_rows)
    requested_dates = list(quality.keys())
    if require_all:
        dates = sorted(day for day, item in quality.items() if item["is_valid"])
    else:
        dates = [d for d in _artifact_dates(tickers) if start_date <= d <= end_date]
    invalid_requested_dates = [
        day for day in requested_dates if day not in set(dates)
    ]
    if require_all and invalid_requested_dates:
        status = "FAIL" if require_train else "SKIP"
        return {
            "status": status,
            "reason": "invalid_requested_artifact_dates",
            "available_dates": len(dates),
            "required_dates": len(requested_dates),
            "invalid_requested_date_count": len(invalid_requested_dates),
            "invalid_requested_dates_sample": invalid_requested_dates[:10],
            "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None,
            "min_rows_per_day": min_rows,
            "require_all_tickers": require_all,
            "date_quality_sample": {
                day: quality[day]
                for day in invalid_requested_dates[:5]
                if day in quality
            },
        }
    if len(dates) < min_dates:
        status = "FAIL" if require_train else "SKIP"
        return {
            "status": status,
            "reason": "insufficient_real_artifact_dates",
            "available_dates": len(dates),
            "required_dates": min_dates,
            "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None,
            "min_rows_per_day": min_rows,
            "require_all_tickers": require_all,
            "date_quality_sample": {
                day: quality[day]
                for day in sorted(quality)[:5]
                if not quality[day]["is_valid"]
            },
        }

    try:
        from src.models.lgbm_trainer import LGBMTrainer

        version = f"live_{end_date}"
        train_result = LGBMTrainer().train(
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            version=version,
            bundle_id=None,
            is_latest=False,
        )
        synthetic = safe_bool(train_result.get("synthetic_fallback", False), default=False)
        missing_tickers = list(train_result.get("missing_tickers", []))
        return _status(
            not synthetic and not missing_tickers,
            {
                "result": train_result,
                "version": version,
                "synthetic_fallback": synthetic,
                "missing_tickers": missing_tickers,
            },
        )
    except Exception as e:
        return _status(False, {"error": str(e)})


def _write_report(report: dict[str, Any]) -> Path:
    _REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    out_path = _REPORT_ROOT / f"data_readiness_{ts}.json"
    report["report_path"] = str(out_path)
    report["report_path_relative"] = _repo_relative(out_path)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Elephant Lab live data readiness runner")
    default_end = _previous_business_day()
    p.add_argument("--end-date", default=_date_str(default_end), help="YYYYMMDD")
    p.add_argument("--business-days", type=int, default=1)
    p.add_argument("--tickers", default="", help="comma-separated tickers")
    p.add_argument("--max-tickers", type=int, default=None)
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--train", action="store_true")
    p.add_argument("--all", action="store_true")
    p.add_argument("--require-train", action="store_true")
    p.add_argument(
        "--skip-existing-backfill",
        action="store_true",
        help="skip dates whose saved artifacts already satisfy live_data_readiness min rows",
    )
    p.add_argument(
        "--allow-mock",
        action="store_true",
        help="allow mock connector PASS only for CI/demo, never for live readiness",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        end = datetime.strptime(args.end_date, "%Y%m%d").date()
        start = _business_start_date(end, args.business_days)
        start_date = _date_str(start)
        end_date = args.end_date

        if args.tickers.strip():
            tickers = [pad_ticker(t.strip()) for t in args.tickers.split(",") if t.strip()]
        else:
            tickers = _load_active_tickers(args.max_tickers)

        if not tickers:
            raise ValueError("No tickers resolved from universe_config.yaml")

        run_all = bool(args.all)
        do_smoke = run_all or args.smoke or not (args.smoke or args.backfill or args.train)
        do_backfill = run_all or args.backfill
        do_train = run_all or args.train

        report: dict[str, Any] = {
            "status": "RUNNING",
            "generated_at": datetime.now(_KST).isoformat(),
            "start_date": start_date,
            "end_date": end_date,
            "allow_mock": bool(args.allow_mock),
            "runtime": _runtime_metadata(),
            "tickers": tickers,
            "stages": {},
        }

        if do_smoke:
            report["stages"]["smoke"] = run_smoke(
                tickers, end_date, allow_mock=bool(args.allow_mock)
            )
        if do_backfill:
            report["stages"]["backfill"] = run_backfill(
                tickers,
                start_date,
                end_date,
                skip_existing=bool(args.skip_existing_backfill),
            )
        if do_train:
            report["stages"]["train"] = run_train_if_ready(
                tickers, start_date, end_date, bool(args.require_train)
            )

        failures: list[str] = []
        for stage_name, stage_result in report["stages"].items():
            if stage_name == "smoke":
                for name, item in stage_result.items():
                    if item.get("status") == "FAIL":
                        failures.append(f"smoke.{name}")
            elif stage_result.get("status") == "FAIL":
                failures.append(stage_name)

        report["status"] = "FAIL" if failures else "PASS"
        report["failures"] = failures
        _write_report(report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if failures else 0
    except Exception as e:
        report = {
            "status": "ERROR",
            "error_code": "LIVE_DATA_READINESS_ERROR",
            "message": str(e),
            "generated_at": datetime.now(_KST).isoformat(),
            "runtime": _runtime_metadata(),
            "stages": {},
        }
        _write_report(report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

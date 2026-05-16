#!/usr/bin/env python
"""Materialize PIT-safe historical exogenous feature artifacts.

The script writes deploy-quality exogenous artifacts only when all required
providers are real, not mock. If KIS/US/ECOS data is unavailable it writes a
BLOCKED report and does not create neutral deploy evidence.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.connectors.ecos_rest import ECOSRestClient  # noqa: E402
from src.connectors.krx_rest import KRXRestClient  # noqa: E402
from src.connectors.us_market import USMarketClient  # noqa: E402
from src.data.dataset_builder import EXOGENOUS_FEATURES  # noqa: E402
from src.data.exogenous_feature_store import (  # noqa: E402
    DEFAULT_EXOGENOUS_ARTIFACT_DIR,
    is_non_neutral,
    write_exogenous_payload,
)
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.safe_cast import safe_bool, safe_float  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402
from src.utils.trading_calendar import (  # noqa: E402
    kospi_trading_dates_between,
    kospi_trading_start_date,
    previous_kospi_trading_day,
)

_KST = ZoneInfo("Asia/Seoul")
_REPORT_DIR = ROOT / "artifacts" / "reports" / "exogenous_history"
_FALLBACK_MIN_EXOGENOUS_NON_NEUTRAL_DATE_COVERAGE = 0.8


def _parse_date(date_key: str) -> datetime:
    return datetime.strptime(str(date_key), "%Y%m%d").replace(tzinfo=_KST)


def _snapshot_ts(date_key: str) -> datetime:
    day = _parse_date(date_key).date()
    return datetime.combine(day, time(8, 30), tzinfo=_KST)


def _min_exogenous_non_neutral_date_coverage() -> float:
    cfg = config_load("risk_config.yaml", "phase2_feature_backfill") or {}
    return safe_float(
        cfg.get("min_exogenous_non_neutral_date_coverage"),
        default=_FALLBACK_MIN_EXOGENOUS_NON_NEUTRAL_DATE_COVERAGE,
        min_value=0.0,
        max_value=1.0,
    )


def _business_dates(end_date: str, business_days: int) -> list[str]:
    end = _parse_date(end_date).date()
    start = kospi_trading_start_date(end, business_days)
    return kospi_trading_dates_between(start, end)


def _active_tickers() -> list[str]:
    cfg = config_load("universe_config.yaml") or {}
    final_gate = (
        (config_load("risk_config.yaml", "backtest_agent") or {})
        .get("deploy_decision_gate", {})
        .get("final_dataset_gate", {})
    )
    if not isinstance(final_gate, dict):
        final_gate = {}
    include_pending = safe_bool(
        final_gate.get("include_pending_data_tickers"),
        default=False,
    )
    stock_statuses = {"active"}
    sector_statuses = {"confirmed"}
    if include_pending:
        stock_statuses = {
            str(status)
            for status in final_gate.get("allowed_stock_statuses", ["active", "pending_data"])
        }
        sector_statuses = {
            str(status)
            for status in final_gate.get(
                "allowed_sector_statuses",
                ["confirmed", "confirmed_pending_data"],
            )
        }
    tickers: list[str] = []
    for sector in (cfg.get("sectors") or {}).values():
        if str(sector.get("status")) not in sector_statuses:
            continue
        for item in sector.get("stocks", []) or []:
            if str(item.get("status", "")) in stock_statuses:
                tickers.append(pad_ticker(str(item.get("ticker", ""))))
    fallback = (cfg.get("backtest_universe_mode") or {}).get("fallback_tickers", [])
    tickers.extend(pad_ticker(str(t)) for t in fallback)
    return sorted(set(tickers))


def _feature_defaults() -> dict[str, float]:
    cfg = config_load("risk_config.yaml", "exogenous_features") or {}
    defaults = cfg.get("neutral_defaults") or {}
    return {
        feature: float(defaults.get(feature, 0.0))
        for feature in EXOGENOUS_FEATURES
    }


def _provider_availability(
    *,
    us_client: USMarketClient,
    ecos_client: ECOSRestClient,
    krx_client: KRXRestClient,
) -> dict[str, Any]:
    return {
        "us_market_real": not bool(getattr(us_client, "_is_mock", True)),
        "ecos_real": not bool(getattr(ecos_client, "_is_mock", True)),
        "kis_investor_real": bool(krx_client._has_kis_investor_provider()),
    }


def _collect_global_features(
    date_key: str,
    *,
    us_client: USMarketClient,
    ecos_client: ECOSRestClient,
) -> tuple[dict[str, float], dict[str, Any]]:
    expected_us_date = previous_kospi_trading_day(_parse_date(date_key).date())
    expected_us_as_of = expected_us_date.isoformat()
    request_as_of = (expected_us_date + timedelta(days=1)).isoformat()
    us = us_client.get_indices(as_of=request_as_of)
    macro = ecos_client.get_macro_pack(date_key)
    features = {
        "us_sp500_change": float(us.us_sp500_change),
        "us_nasdaq_change": float(us.us_nasdaq_change),
        "us_vix": float(us.us_vix),
        "us_soxx_change": float(us.us_soxx_change),
        "interest_rate": float(macro.get("interest_rate", 0.0)),
        "usd_krw": float(macro.get("usd_krw", 0.0)),
    }
    return features, {
        "us_market_source": us.source,
        "us_market_as_of_date": us.as_of_date,
        "us_market_expected_as_of_date": expected_us_as_of,
        "us_market_request_as_of_date": request_as_of,
        "ecos_macro_pack_date": date_key,
    }


def _nested_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload")
    return payload if isinstance(payload, dict) else {}


def _normalize_date_key(value: Any) -> str | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_KST)
    return parsed.astimezone(_KST).strftime("%Y%m%d")


def _investor_row_date_key(row: dict[str, Any]) -> str | None:
    payload = _nested_payload(row)
    for key in ("date", "basDt", "bas_dt", "trdDd", "trade_date"):
        date_key = _normalize_date_key(row.get(key))
        if date_key:
            return date_key
        date_key = _normalize_date_key(payload.get(key))
        if date_key:
            return date_key
    return _normalize_date_key(row.get("occurred_at") or payload.get("occurred_at"))


def _number_from_row(row: dict[str, Any], keys: tuple[str, ...]) -> float:
    payload = _nested_payload(row)
    for key in keys:
        value = row.get(key)
        if value in (None, ""):
            value = payload.get(key)
        if value in (None, ""):
            continue
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            continue
    return 0.0


def _investor_values(row: dict[str, Any]) -> dict[str, float]:
    return {
        "foreign_net_buy": _number_from_row(row, ("foreign_net_buy", "foreign")),
        "institutional_net_buy": _number_from_row(
            row,
            ("institutional_net_buy", "institutional"),
        ),
        "retail_net_buy": _number_from_row(row, ("retail_net_buy", "retail")),
    }


def _collect_investor_features(
    date_key: str,
    tickers: list[str],
    *,
    krx_client: KRXRestClient,
) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    # The artifact is joined to intraday rows on date_key. To remain PIT-safe for
    # pre-open features, investor flow uses the previous confirmed KRX trading day.
    lookup_date = previous_kospi_trading_day(_parse_date(date_key).date()).strftime("%Y%m%d")
    per_ticker: dict[str, dict[str, float]] = {}
    failures: dict[str, str] = {}
    for ticker in tickers:
        try:
            rows = krx_client.get_investor_info(ticker, lookup_date, lookup_date)
            match = None
            available_dates: list[str] = []
            for row in rows:
                row_date = _investor_row_date_key(row)
                if row_date:
                    available_dates.append(row_date)
                if row_date == lookup_date:
                    match = row
                    break
            if match is None:
                suffix = (
                    f": available_dates={sorted(set(available_dates))[:3]}"
                    if available_dates
                    else ""
                )
                failures[ticker] = f"no_matching_investor_row{suffix}"
                continue
            per_ticker[ticker] = _investor_values(match)
        except Exception as e:
            failures[ticker] = f"{type(e).__name__}: {e}"
    return per_ticker, {
        "investor_lookup_date": lookup_date,
        "investor_ticker_count": len(per_ticker),
        "investor_failures": failures,
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


def materialize_exogenous_history(
    *,
    end_date: str,
    business_days: int,
    artifact_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    tickers = _active_tickers()
    dates = _business_dates(end_date, business_days)
    defaults = _feature_defaults()
    us_client = USMarketClient()
    ecos_client = ECOSRestClient()
    krx_client = KRXRestClient()
    availability = _provider_availability(
        us_client=us_client,
        ecos_client=ecos_client,
        krx_client=krx_client,
    )
    missing_providers = [
        name for name, ok in availability.items() if not ok
    ]

    per_date: list[dict[str, Any]] = []
    written: list[str] = []
    blockers: list[str] = []
    non_neutral_dates = 0

    if missing_providers:
        blockers.append("required_real_provider_unavailable")
        for date_key in dates:
            per_date.append({
                "date": date_key,
                "status": "BLOCKED",
                "reason": "required_real_provider_unavailable",
                "missing_providers": missing_providers,
                "artifact_written": False,
                "non_neutral": False,
            })
    else:
        for date_key in dates:
            snapshot = _snapshot_ts(date_key)
            try:
                global_features, global_stats = _collect_global_features(
                    date_key,
                    us_client=us_client,
                    ecos_client=ecos_client,
                )
                if str(global_stats.get("us_market_source")) != "yfinance":
                    blocker = "us_market_source_not_yfinance"
                    if blocker not in blockers:
                        blockers.append(blocker)
                    per_date.append({
                        "date": date_key,
                        "status": "BLOCKED",
                        "reason": blocker,
                        "artifact_written": False,
                        "non_neutral": False,
                        "source_stats": global_stats,
                    })
                    continue
                actual_us_date = _normalize_date_key(global_stats.get("us_market_as_of_date"))
                expected_us_date = _normalize_date_key(
                    global_stats.get("us_market_expected_as_of_date")
                )
                if actual_us_date != expected_us_date:
                    blocker = (
                        "us_market_as_of_after_expected_close"
                        if actual_us_date and expected_us_date and actual_us_date > expected_us_date
                        else "us_market_as_of_mismatch"
                    )
                    if blocker not in blockers:
                        blockers.append(blocker)
                    per_date.append({
                        "date": date_key,
                        "status": "BLOCKED",
                        "reason": blocker,
                        "artifact_written": False,
                        "non_neutral": False,
                        "source_stats": global_stats,
                    })
                    continue
                per_ticker, investor_stats = _collect_investor_features(
                    date_key,
                    tickers,
                    krx_client=krx_client,
                )
                if len(per_ticker) != len(tickers):
                    per_date.append({
                        "date": date_key,
                        "status": "BLOCKED",
                        "reason": "investor_flow_incomplete",
                        "artifact_written": False,
                        "non_neutral": False,
                        "source_stats": investor_stats,
                    })
                    continue
                non_neutral = (
                    is_non_neutral(global_features, defaults)
                    or any(is_non_neutral(values, defaults) for values in per_ticker.values())
                )
                if not non_neutral:
                    per_date.append({
                        "date": date_key,
                        "status": "NEUTRAL_ONLY",
                        "artifact_written": False,
                        "non_neutral": False,
                    })
                    continue
                payload = {
                    "batch_date": snapshot.date().isoformat(),
                    "snapshot_ts": snapshot.isoformat(),
                    "generated_at": datetime.now(_KST).isoformat(),
                    "ticker_count": len(tickers),
                    "source_stats": {
                        "input_mode": "real",
                        "neutral_rehearsal_file": False,
                        "provider_availability": availability,
                        **global_stats,
                        **investor_stats,
                    },
                    "features": global_features,
                    "per_ticker": per_ticker,
                }
                out_path = write_exogenous_payload(payload, artifact_dir=artifact_dir)
                written.append(_repo_relative(out_path))
                non_neutral_dates += 1
                per_date.append({
                    "date": date_key,
                    "status": "PASS",
                    "artifact_written": True,
                    "path": str(out_path),
                    "non_neutral": True,
                    "source_stats": payload["source_stats"],
                })
            except Exception as e:
                per_date.append({
                    "date": date_key,
                    "status": "ERROR",
                    "artifact_written": False,
                    "non_neutral": False,
                    "error": str(e),
                    "error_type": type(e).__name__,
                })

    min_coverage = _min_exogenous_non_neutral_date_coverage()
    coverage = non_neutral_dates / max(len(dates), 1)
    if coverage < min_coverage:
        blockers.append("exogenous_non_neutral_coverage_below_threshold")
    if not written:
        blockers.append("no_exogenous_artifacts_written")
    report = {
        "status": "PASS" if not blockers else "BLOCKED",
        "generated_at": datetime.now(_KST).isoformat(),
        "end_date": end_date,
        "business_days_requested": business_days,
        "date_count": len(dates),
        "ticker_count": len(tickers),
        "artifact_dir": str(artifact_dir),
        "provider_availability": availability,
        "coverage": {
            "exogenous_non_neutral_date_coverage": coverage,
            "min_exogenous_non_neutral_date_coverage": min_coverage,
            "written_date_count": len(written),
        },
        "blockers": sorted(set(blockers)),
        "files_written": written,
        "per_date": per_date,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"materialize_exogenous_history_{datetime.now(_KST).strftime('%Y%m%d_%H%M%S')}.json"
    report["report_path"] = str(path)
    report["report_path_relative"] = _repo_relative(path)
    _write_json(path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end-date", required=True, help="YYYYMMDD")
    parser.add_argument("--business-days", type=int, default=80)
    parser.add_argument("--artifact-dir", default=str(DEFAULT_EXOGENOUS_ARTIFACT_DIR))
    parser.add_argument("--output-dir", default=str(_REPORT_DIR))
    args = parser.parse_args(argv)
    report = materialize_exogenous_history(
        end_date=str(args.end_date),
        business_days=int(args.business_days),
        artifact_dir=Path(str(args.artifact_dir)),
        output_dir=Path(str(args.output_dir)),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

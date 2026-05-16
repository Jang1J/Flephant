#!/usr/bin/env python
"""Cost-aware label horizon diagnostic.

This script is read-only. It inspects existing 1m bar artifacts and compares
candidate forward-return label horizons after the configured execution cost.
It does not train, deploy, call KIS, or mutate any registry.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.data.dataset_builder import DatasetBuilder  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.safe_cast import safe_bool, safe_float, safe_int  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_DATE_RE = re.compile(r"(20\d{6})")


def _universe_hash(tickers: list[str]) -> str:
    payload = json.dumps(
        sorted({pad_ticker(str(ticker)) for ticker in tickers}),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
        for row in sector.get("stocks", []) or []:
            if str(row.get("status", "")) in stock_statuses:
                tickers.append(pad_ticker(str(row.get("ticker", ""))))
    if tickers:
        return sorted(set(tickers))
    fallback = (cfg.get("backtest_universe_mode") or {}).get("fallback_tickers", [])
    return sorted({pad_ticker(str(t)) for t in fallback})


def _extract_date(path: Path) -> date | None:
    match = _DATE_RE.search(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def _available_dates(artifacts_dir: Path, tickers: list[str]) -> list[date]:
    counts: dict[date, int] = {}
    for ticker in tickers:
        ticker_dir = artifacts_dir / ticker
        if not ticker_dir.exists():
            continue
        seen_for_ticker: set[date] = set()
        for file_path in ticker_dir.iterdir():
            file_date = _extract_date(file_path)
            if file_date is not None:
                seen_for_ticker.add(file_date)
        for file_date in seen_for_ticker:
            counts[file_date] = counts.get(file_date, 0) + 1
    required = len(tickers)
    return sorted(day for day, count in counts.items() if count >= required)


def _default_horizons() -> list[str]:
    label_cfg = config_load("risk_config.yaml", "label") or {}
    pre_cfg = config_load("risk_config.yaml", "preprocessor") or {}
    service_policy_cfg = config_load("risk_config.yaml", "service_policy_replay") or {}
    horizons: list[int] = []
    label_horizon = int(label_cfg.get("horizon_bars", 0) or 0)
    if label_horizon > 0:
        horizons.append(label_horizon)
    for value in pre_cfg.get("multi_scale_windows", []) or []:
        as_int = int(value)
        if as_int > 1:
            horizons.append(as_int)
    min_holding = int(service_policy_cfg.get("min_holding_bars", 0) or 0)
    if min_holding > 1:
        horizons.append(min_holding)
    deduped = [str(v) for v in sorted(set(horizons))]
    return deduped + ["session_close"]


def _cost_bps() -> float:
    cost_cfg = config_load("risk_config.yaml", "execution_cost_model") or {}
    components = cost_cfg.get("components") or {}
    if not isinstance(components, dict) or not {
        "commission_bps",
        "slippage_bps",
    }.issubset(components):
        raise ValueError("execution_cost_model_missing")
    commission_bps = safe_float(components.get("commission_bps"), default=-1.0)
    slippage_bps = safe_float(components.get("slippage_bps"), default=-1.0)
    if commission_bps <= 0.0 or slippage_bps <= 0.0:
        raise ValueError("execution_cost_model_non_positive")
    return commission_bps + slippage_bps


def _diagnostic_thresholds(total_cost_bps: float) -> dict[str, Any]:
    cost_cfg = config_load("risk_config.yaml", "cost_aware_retraining") or {}
    gate_cfg = cost_cfg.get("label_horizon_gate", {}) or {}
    return {
        "min_mean_net_bps": safe_float(
            gate_cfg.get("min_mean_net_bps"),
            default=total_cost_bps,
            min_value=0.0,
        ),
        "min_positive_net_rate": safe_float(
            gate_cfg.get("min_positive_net_rate"),
            default=1.0,
            min_value=0.0,
            max_value=1.0,
        ),
        "allow_warn_for_research_only": safe_bool(
            gate_cfg.get("allow_warn_for_research_only"),
            default=False,
        ),
    }


def _label_generation_settings() -> dict[str, int]:
    """DatasetBuilder label-row policy used by this read-only diagnostic."""
    label_cfg = config_load("risk_config.yaml", "label") or {}
    active_horizon_bars = safe_int(
        label_cfg.get("horizon_bars"),
        default=5,
        min_value=1,
    )
    drop_last_n_bars = safe_int(
        label_cfg.get("drop_last_n_bars"),
        default=active_horizon_bars,
        min_value=0,
    )
    return {
        "active_horizon_bars": active_horizon_bars,
        "drop_last_n_bars": drop_last_n_bars,
    }


def _infer_date_range(
    artifacts_dir: Path,
    tickers: list[str],
    start_date: str | None,
    end_date: str | None,
) -> tuple[str, str, list[str]]:
    warnings: list[str] = []
    if start_date and end_date:
        return start_date, end_date, warnings

    dates = _available_dates(artifacts_dir, tickers)
    if not dates:
        raise RuntimeError(
            f"no common artifact dates found for {len(tickers)} tickers in {artifacts_dir}"
        )

    wf_cfg = config_load("risk_config.yaml", "walk_forward") or {}
    lookback = max(1, int(wf_cfg.get("test_window_days", 1)))
    inferred_end = end_date or dates[-1].strftime("%Y%m%d")
    end_dt = datetime.strptime(inferred_end, "%Y%m%d").date()
    usable = [day for day in dates if day <= end_dt]
    if not usable:
        raise RuntimeError(f"no artifact dates <= end_date {inferred_end}")
    selected = usable[-lookback:]
    inferred_start = start_date or selected[0].strftime("%Y%m%d")
    warnings.append(
        "date_range_inferred_from_common_artifact_dates:"
        f"{inferred_start}~{inferred_end}"
    )
    return inferred_start, inferred_end, warnings


def _load_raw_panel(
    *,
    artifacts_dir: Path,
    tickers: list[str],
    start_date: str,
    end_date: str,
):
    builder = DatasetBuilder(artifacts_dir=artifacts_dir, allow_synthetic_fallback=False)
    frames = []
    missing: list[str] = []
    for ticker in tickers:
        frame = builder._load_ticker_bars(ticker, start_date, end_date)
        if frame is None or frame.empty:
            missing.append(ticker)
            continue
        frames.append(frame)
    if not frames:
        raise RuntimeError("no bar frames loaded for label horizon scan")
    pd = __import__("pandas")
    panel = pd.concat(frames, axis=0, ignore_index=True)
    panel["ticker"] = panel["ticker"].map(lambda value: pad_ticker(str(value)))
    panel["ts_close"] = pd.to_datetime(panel["ts_close"])
    if panel["ts_close"].dt.tz is None:
        panel["ts_close"] = panel["ts_close"].dt.tz_localize(_KST)
    else:
        panel["ts_close"] = panel["ts_close"].dt.tz_convert(_KST)
    panel = panel.sort_values(["ticker", "ts_close"]).reset_index(drop=True)
    return panel, missing


def _quantiles(values: np.ndarray) -> dict[str, float | None]:
    if values.size == 0:
        return {"p25": None, "p50": None, "p75": None, "p90": None}
    return {
        "p25": float(np.percentile(values, 25)),
        "p50": float(np.percentile(values, 50)),
        "p75": float(np.percentile(values, 75)),
        "p90": float(np.percentile(values, 90)),
    }


def _horizon_return_series(
    panel,
    horizon: str,
    *,
    drop_last_n_bars: int = 0,
    active_horizon: str | int | None = None,
):
    df = panel.copy()
    df["_session"] = df["ts_close"].dt.date
    labels = np.full(len(df), np.nan, dtype=float)
    close_values = df["close"].to_numpy(dtype=float)
    grouped_positions = df.groupby(["ticker", "_session"], sort=False).indices
    if horizon == "session_close":
        active_bars = safe_int(active_horizon, default=5, min_value=1)
        drop_count = max(active_bars, drop_last_n_bars)
        for positions in grouped_positions.values():
            positions_arr = np.asarray(positions, dtype=np.int64)
            valid_count = max(0, len(positions_arr) - drop_count)
            if valid_count <= 0:
                continue
            closes = close_values[positions_arr]
            now = closes[:valid_count]
            future = float(closes[-1])
            with np.errstate(divide="ignore", invalid="ignore"):
                labels[positions_arr[:valid_count]] = np.where(
                    now > 1e-8,
                    future / now - 1.0,
                    np.nan,
                )
    else:
        bars = int(horizon)
        drop_count = max(bars, drop_last_n_bars)
        for positions in grouped_positions.values():
            positions_arr = np.asarray(positions, dtype=np.int64)
            valid_count = max(0, len(positions_arr) - drop_count)
            if valid_count <= 0:
                continue
            closes = close_values[positions_arr]
            now = closes[:valid_count]
            future = closes[bars:bars + valid_count]
            with np.errstate(divide="ignore", invalid="ignore"):
                labels[positions_arr[:valid_count]] = np.where(
                    now > 1e-8,
                    future / now - 1.0,
                    np.nan,
                )
    pd = __import__("pandas")
    return pd.Series(labels, index=panel.index, dtype=float)


def _horizon_returns(
    panel,
    horizon: str,
    *,
    drop_last_n_bars: int = 0,
    active_horizon: str | int | None = None,
) -> np.ndarray:
    values = np.asarray(
        _horizon_return_series(
            panel,
            horizon,
            drop_last_n_bars=drop_last_n_bars,
            active_horizon=active_horizon,
        ).dropna(),
        dtype=float,
    )
    return values[np.isfinite(values)]


def _label_topk_stats(
    *,
    panel,
    horizon: str,
    total_cost_bps: float,
    top_k_fraction: float,
    drop_last_n_bars: int = 0,
    active_horizon: str | int | None = None,
) -> dict[str, Any]:
    """Diagnostic top-K of the candidate label itself, not model performance."""
    returns = _horizon_return_series(
        panel,
        horizon,
        drop_last_n_bars=drop_last_n_bars,
        active_horizon=active_horizon,
    )
    joined = panel[["ticker", "ts_close"]].copy()
    joined["_return"] = returns
    joined = joined.replace([np.inf, -np.inf], np.nan).dropna(subset=["_return"])
    if joined.empty:
        return {
            "method": "candidate_label_topk_net_bps",
            "top_k_fraction": top_k_fraction,
            "groups": 0,
            "rows": 0,
            "mean_net_bps": None,
            "positive_net_rate": None,
        }

    selected: list[float] = []
    groups = 0
    for _, group in joined.groupby("ts_close", sort=False):
        if group.empty:
            continue
        k = max(1, int(np.ceil(len(group) * top_k_fraction)))
        top = group.nlargest(k, "_return")
        net_bps = top["_return"].to_numpy(dtype=float) * 10000.0 - total_cost_bps
        finite_net = net_bps[np.isfinite(net_bps)]
        if finite_net.size == 0:
            continue
        selected.extend(float(value) for value in finite_net)
        groups += 1

    if not selected:
        return {
            "method": "candidate_label_topk_net_bps",
            "top_k_fraction": top_k_fraction,
            "groups": 0,
            "rows": 0,
            "mean_net_bps": None,
            "positive_net_rate": None,
        }
    values = np.asarray(selected, dtype=float)
    return {
        "method": "candidate_label_topk_net_bps",
        "top_k_fraction": top_k_fraction,
        "groups": int(groups),
        "rows": int(values.size),
        "mean_net_bps": float(values.mean()),
        "positive_net_rate": float((values > 0.0).mean()),
    }


def _horizon_selection_score(report: dict[str, Any]) -> tuple[float, float, float, float, int]:
    topk = report.get("label_topk")
    if not isinstance(topk, dict):
        topk = {}
    status_rank = 1.0 if report.get("status") == "PASS" else 0.0
    return (
        status_rank,
        safe_float(topk.get("mean_net_bps"), default=-1e18),
        safe_float(topk.get("positive_net_rate"), default=-1.0),
        safe_float(report.get("mean_net_bps"), default=-1e18),
        safe_int(report.get("valid_rows"), default=0, min_value=0),
    )


def _select_best_horizon_report(reports: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(reports, key=_horizon_selection_score, default=None)


def _selection_impact(
    *,
    panel,
    horizon: str,
    active_horizon: str,
    top_k_fraction: float,
    drop_last_n_bars: int = 0,
) -> dict[str, Any]:
    if horizon == active_horizon:
        return {
            "active_horizon": active_horizon,
            "topk_overlap_rate": 1.0,
            "rank_correlation": 1.0,
            "selection_changes": 0,
            "groups_compared": 0,
            "rank_equivalent_to_active_horizon": True,
        }

    pd = __import__("pandas")
    active = _horizon_return_series(
        panel,
        active_horizon,
        drop_last_n_bars=drop_last_n_bars,
        active_horizon=active_horizon,
    )
    candidate = _horizon_return_series(
        panel,
        horizon,
        drop_last_n_bars=drop_last_n_bars,
        active_horizon=active_horizon,
    )
    joined = panel[["ticker", "ts_close"]].copy()
    joined["_active"] = active
    joined["_candidate"] = candidate
    joined = joined.replace([np.inf, -np.inf], np.nan).dropna(
        subset=["_active", "_candidate"]
    )
    if joined.empty:
        return {
            "active_horizon": active_horizon,
            "topk_overlap_rate": None,
            "rank_correlation": None,
            "selection_changes": None,
            "groups_compared": 0,
            "rank_equivalent_to_active_horizon": False,
        }

    overlaps: list[float] = []
    rank_corrs: list[float] = []
    selection_changes = 0
    for _, group in joined.groupby("ts_close", sort=False):
        if len(group) < 2:
            continue
        k = max(1, int(np.ceil(len(group) * top_k_fraction)))
        active_top = set(group.nlargest(k, "_active")["ticker"].astype(str))
        candidate_top = set(group.nlargest(k, "_candidate")["ticker"].astype(str))
        overlaps.append(len(active_top & candidate_top) / max(len(active_top), 1))
        if active_top != candidate_top:
            selection_changes += 1

        active_rank = group["_active"].rank(method="average")
        candidate_rank = group["_candidate"].rank(method="average")
        corr = active_rank.corr(candidate_rank, method="spearman")
        if pd.notna(corr):
            rank_corrs.append(float(corr))

    groups_compared = len(overlaps)
    if groups_compared == 0:
        return {
            "active_horizon": active_horizon,
            "topk_overlap_rate": None,
            "rank_correlation": None,
            "selection_changes": None,
            "groups_compared": 0,
            "rank_equivalent_to_active_horizon": False,
        }
    topk_overlap_rate = float(np.mean(overlaps))
    rank_correlation = float(np.mean(rank_corrs)) if rank_corrs else None
    return {
        "active_horizon": active_horizon,
        "topk_overlap_rate": topk_overlap_rate,
        "rank_correlation": rank_correlation,
        "selection_changes": int(selection_changes),
        "groups_compared": groups_compared,
        "rank_equivalent_to_active_horizon": (
            selection_changes == 0
            and topk_overlap_rate == 1.0
            and (rank_correlation is None or rank_correlation >= 0.999999)
        ),
    }


def _summarize_horizon(
    *,
    panel,
    horizon: str,
    active_horizon: str,
    total_cost_bps: float,
    thresholds: dict[str, float],
    top_k_fraction: float,
    drop_last_n_bars: int = 0,
) -> dict[str, Any]:
    returns = _horizon_returns(
        panel,
        horizon,
        drop_last_n_bars=drop_last_n_bars,
        active_horizon=active_horizon,
    )
    gross_bps = returns * 10000.0
    net_bps = gross_bps - total_cost_bps
    valid_rows = int(net_bps.size)
    if valid_rows == 0:
        return {
            "horizon": horizon,
            "status": "BLOCKED",
            "valid_rows": 0,
            "reason": "no_valid_rows",
            "selection_impact": _selection_impact(
                panel=panel,
                horizon=horizon,
                active_horizon=active_horizon,
                top_k_fraction=top_k_fraction,
                drop_last_n_bars=drop_last_n_bars,
            ),
        }

    mean_gross = float(gross_bps.mean())
    mean_net = float(net_bps.mean())
    positive_net_rate = float((net_bps > 0.0).mean())
    above_cost_buffer_rate = float((net_bps > total_cost_bps).mean())
    pass_mean = mean_net >= thresholds["min_mean_net_bps"]
    pass_hit = positive_net_rate >= thresholds["min_positive_net_rate"]
    status = "PASS" if pass_mean and pass_hit else "WARN"
    return {
        "horizon": horizon,
        "status": status,
        "valid_rows": valid_rows,
        "mean_gross_bps": mean_gross,
        "mean_net_bps": mean_net,
        "positive_net_rate": positive_net_rate,
        "above_cost_buffer_rate": above_cost_buffer_rate,
        "gross_bps_quantiles": _quantiles(gross_bps),
        "net_bps_quantiles": _quantiles(net_bps),
        "pass_mean_net_threshold": bool(pass_mean),
        "pass_positive_net_rate_threshold": bool(pass_hit),
        "label_topk": _label_topk_stats(
            panel=panel,
            horizon=horizon,
            total_cost_bps=total_cost_bps,
            top_k_fraction=top_k_fraction,
            drop_last_n_bars=drop_last_n_bars,
            active_horizon=active_horizon,
        ),
        "selection_impact": _selection_impact(
            panel=panel,
            horizon=horizon,
            active_horizon=active_horizon,
            top_k_fraction=top_k_fraction,
            drop_last_n_bars=drop_last_n_bars,
        ),
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    artifacts_dir = Path(args.artifacts_dir)
    if not artifacts_dir.is_absolute():
        artifacts_dir = ROOT / artifacts_dir
    tickers = (
        [pad_ticker(t.strip()) for t in args.tickers.split(",") if t.strip()]
        if args.tickers
        else _active_tickers()
    )
    if not tickers:
        raise RuntimeError("no active tickers resolved from universe_config.yaml")

    start_date, end_date, warnings = _infer_date_range(
        artifacts_dir,
        tickers,
        args.start_date,
        args.end_date,
    )
    panel, missing_tickers = _load_raw_panel(
        artifacts_dir=artifacts_dir,
        tickers=tickers,
        start_date=start_date,
        end_date=end_date,
    )

    horizons = (
        [item.strip() for item in args.horizons.split(",") if item.strip()]
        if args.horizons
        else _default_horizons()
    )
    total_cost_bps = _cost_bps()
    thresholds = _diagnostic_thresholds(total_cost_bps)
    label_settings = _label_generation_settings()
    active_horizon = str(label_settings["active_horizon_bars"])
    drop_last_n_bars = label_settings["drop_last_n_bars"]
    eval_cfg = config_load("risk_config.yaml", "evaluation") or {}
    top_k_fraction = safe_float(
        eval_cfg.get("top_k_fraction"),
        default=0.25,
        min_value=0.0,
        max_value=1.0,
    )
    horizon_reports = [
        _summarize_horizon(
            panel=panel,
            horizon=horizon,
            active_horizon=active_horizon,
            total_cost_bps=total_cost_bps,
            thresholds=thresholds,
            top_k_fraction=top_k_fraction,
            drop_last_n_bars=drop_last_n_bars,
        )
        for horizon in horizons
    ]
    valid_reports = [r for r in horizon_reports if r.get("valid_rows", 0)]
    best = _select_best_horizon_report(valid_reports)
    deployable = bool(best and best.get("status") == "PASS")
    best_topk = best.get("label_topk") if isinstance(best, dict) else {}
    if not isinstance(best_topk, dict):
        best_topk = {}
    research_trainable = bool(
        best
        and safe_float(best_topk.get("mean_net_bps"), default=-1e18)
        >= thresholds["min_mean_net_bps"]
        and safe_float(best_topk.get("positive_net_rate"), default=-1.0)
        >= thresholds["min_positive_net_rate"]
    )
    return {
        "status": "PASS" if deployable else "WARN",
        "action": "cost_aware_label_horizon_scan",
        "generated_at": datetime.now(_KST).isoformat(),
        "read_only": True,
        "external_kis_api": False,
        "registry_mutated": False,
        "data": {
            "artifacts_dir": str(artifacts_dir),
            "start_date": start_date,
            "end_date": end_date,
            "ticker_count": len(tickers),
            "tickers": tickers,
            "universe_hash": _universe_hash(tickers),
            "loaded_rows": int(len(panel)),
            "missing_tickers": missing_tickers,
        },
        "cost_model": {
            "total_cost_bps": total_cost_bps,
            "thresholds": thresholds,
        },
        "selection_impact_baseline": {
            "active_horizon": active_horizon,
            "top_k_fraction": top_k_fraction,
            "label_generation_parity": {
                "source": "DatasetBuilder._generate_labels",
                "drop_last_n_bars": drop_last_n_bars,
                "session_close_drop_uses_active_horizon": active_horizon,
            },
        },
        "best_horizon": best.get("horizon") if best else None,
        "best_horizon_selection": {
            "method": "status_then_candidate_label_topk_net_bps",
            "score": list(_horizon_selection_score(best)) if best else None,
            "note": "label_topk is label separability diagnostic, not model OOS PnL",
        },
        "deployable_label_recommendation": deployable,
        "research_trainable_label_recommendation": research_trainable,
        "warnings": warnings,
        "horizons": horizon_reports,
    }


def _write_report(report: dict[str, Any], output_dir: str | None) -> Path:
    raw_dir = output_dir or "artifacts/reports/label_horizon_scan"
    out_dir = Path(raw_dir)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"cost_aware_label_horizon_scan_{ts}.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts-dir", default="artifacts/data")
    parser.add_argument("--tickers", default=None)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--horizons", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--no-write-report", action="store_true")
    args = parser.parse_args(argv)

    report = build_report(args)
    if not args.no_write_report:
        path = _write_report(report, args.output_dir)
        report["report_path"] = str(path)
        try:
            report["report_path_relative"] = str(path.relative_to(ROOT))
        except ValueError:
            report["report_path_relative"] = str(path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") in {"PASS", "WARN"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""Read-only rolling-window IC experiment for feature-group selection.

This script is a safer, repo-native replacement for ad-hoc notebooks:

* train windows are trading-date based, not pooled-row based;
* validation IC is averaged by cross-sectional ``ts_close`` groups;
* the default target is the current service-policy label
  ``label_195m_net_ret``;
* the script writes evidence only and never mutates registries or live state.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.data.dataset_builder import DatasetBuilder  # noqa: E402
from src.models.metrics import panel_ic, panel_rank_ic  # noqa: E402
from src.models.ranking_loss import build_lgbm_params, compute_group_sizes  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.safe_cast import safe_bool  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_DEFAULT_BUNDLE_ID = "BUNDLE-20260518-195M0001"


@dataclass(frozen=True)
class FoldWindow:
    mode: str
    train_start: str
    train_end: str
    val_start: str
    val_end: str


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def _normalize_yyyymmdd(value: str) -> str:
    raw = str(value).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    raise ValueError(f"date must be YYYYMMDD or YYYY-MM-DD: {value!r}")


def _active_tickers() -> list[str]:
    cfg = config_load("universe_config.yaml") or {}
    sectors = cfg.get("sectors", {}) if isinstance(cfg, dict) else {}
    tickers: list[str] = []
    for sector in sectors.values():
        if not isinstance(sector, dict):
            continue
        for stock in sector.get("stocks", []) or []:
            if not isinstance(stock, dict):
                continue
            if str(stock.get("status", "")).strip() != "active":
                continue
            ticker = str(stock.get("ticker", "")).zfill(6)
            if ticker.isdigit() and len(ticker) == 6:
                tickers.append(ticker)
    return sorted(set(tickers))


def _default_date_range(bundle_id: str) -> tuple[str, str]:
    path = (
        ROOT
        / "artifacts"
        / "bundles"
        / bundle_id
        / "lgbm"
        / "latest_model_metadata.json"
    )
    if not path.is_file():
        path = (
            ROOT
            / "artifacts"
            / "lgbm_paper_candidate"
            / bundle_id
            / "cost-aware-195m-lr005-leaves31-child50_metadata.json"
        )
    if path.is_file():
        with path.open("r", encoding="utf-8") as fh:
            meta = json.load(fh)
        return (
            _normalize_yyyymmdd(str(meta["train_start"])),
            _normalize_yyyymmdd(str(meta["train_end"])),
        )
    gate = config_load("risk_config.yaml", "backtest_agent").get(
        "deploy_decision_gate", {}
    ).get("final_dataset_gate", {})
    return (
        _normalize_yyyymmdd(str(gate["expected_start_date"])),
        _normalize_yyyymmdd(str(gate["expected_end_date"])),
    )


def _experiment_cfg() -> dict[str, Any]:
    return config_load("risk_config.yaml", "rolling_window_ic_experiment") or {}


def _as_int_list(values: Any) -> list[int]:
    if isinstance(values, str):
        values = [part.strip() for part in values.split(",") if part.strip()]
    return [int(value) for value in values]


def _feature_groups(cfg: dict[str, Any]) -> dict[str, list[str]]:
    pre = config_load("risk_config.yaml", "preprocessor")
    base = list(pre.get("feature_cols", []))
    exog = list(pre.get("exogenous_feature_cols", []))
    allowed_ds = set(pre.get("dual_source_feature_cols", []))
    raw_groups = cfg.get("groups") if isinstance(cfg.get("groups"), dict) else {}
    groups: dict[str, list[str]] = {}
    for name, spec in raw_groups.items():
        if not isinstance(spec, dict):
            continue
        cols: list[str] = []
        if safe_bool(spec.get("include_base", True), default=True):
            cols.extend(base)
        if safe_bool(spec.get("include_exogenous", False), default=False):
            cols.extend(exog)
        for col in spec.get("dual_source_cols", []) or []:
            col = str(col)
            if col not in allowed_ds:
                raise ValueError(f"unknown dual source feature in group {name}: {col}")
            cols.append(col)
        groups[str(name)] = list(dict.fromkeys(cols))
    if not groups:
        groups["OHLCV"] = base
    return groups


def _panel_dates(panel) -> list[str]:
    ts = panel.index.get_level_values("ts_close")
    return sorted({value.strftime("%Y%m%d") for value in ts})


def _build_folds(
    dates: list[str],
    *,
    window_days: int,
    n_folds: int,
    min_validation_days: int,
    embargo_days: int,
) -> list[FoldWindow]:
    remaining = len(dates) - int(window_days) - int(embargo_days)
    if remaining < int(min_validation_days):
        return []
    validation_days = max(int(min_validation_days), remaining // max(int(n_folds), 1))
    folds: list[FoldWindow] = []
    for fold_idx in range(int(n_folds)):
        val_start_idx = int(window_days) + int(embargo_days) + fold_idx * validation_days
        val_end_idx = min(val_start_idx + validation_days, len(dates))
        train_end_idx = val_start_idx - int(embargo_days)
        train_start_idx = train_end_idx - int(window_days)
        if train_start_idx < 0 or val_end_idx <= val_start_idx:
            continue
        folds.append(
            FoldWindow(
                train_start=dates[train_start_idx],
                train_end=dates[train_end_idx - 1],
                val_start=dates[val_start_idx],
                val_end=dates[val_end_idx - 1],
                mode="rolling",
            )
        )
    return folds


def _build_expanding_folds(
    dates: list[str],
    *,
    initial_window_days: int,
    n_folds: int,
    min_validation_days: int,
    embargo_days: int,
) -> list[FoldWindow]:
    remaining = len(dates) - int(initial_window_days) - int(embargo_days)
    if remaining < int(min_validation_days):
        return []
    validation_days = max(int(min_validation_days), remaining // max(int(n_folds), 1))
    folds: list[FoldWindow] = []
    for fold_idx in range(int(n_folds)):
        val_start_idx = (
            int(initial_window_days)
            + int(embargo_days)
            + fold_idx * validation_days
        )
        val_end_idx = min(val_start_idx + validation_days, len(dates))
        train_end_idx = val_start_idx - int(embargo_days)
        if train_end_idx <= 0 or val_end_idx <= val_start_idx:
            continue
        folds.append(
            FoldWindow(
                mode="expanding",
                train_start=dates[0],
                train_end=dates[train_end_idx - 1],
                val_start=dates[val_start_idx],
                val_end=dates[val_end_idx - 1],
            )
        )
    return folds


def _filter_dates(panel, start: str, end: str):
    ts = panel.index.get_level_values("ts_close")
    date_key = ts.strftime("%Y%m%d")
    return panel[(date_key >= start) & (date_key <= end)]


def _sort_for_grouping(panel):
    return (
        panel.reset_index()
        .sort_values(["ts_close", "ticker"])
        .set_index(["ticker", "ts_close"])
    )


def _apply_bar_stride(panel, stride: int):
    stride = int(stride)
    if stride <= 1 or panel.empty:
        return panel
    frame = panel.reset_index().sort_values(["ticker", "ts_close"])
    frame["date_key"] = frame["ts_close"].dt.strftime("%Y%m%d")
    frame["bar_in_day"] = frame.groupby(["ticker", "date_key"]).cumcount()
    frame = frame[frame["bar_in_day"] % stride == 0].drop(
        columns=["date_key", "bar_in_day"]
    )
    return frame.set_index(["ticker", "ts_close"]).sort_index()


def _to_group_dict(
    panel,
    pred: np.ndarray,
    target_col: str,
) -> tuple[dict[Any, np.ndarray], dict[Any, np.ndarray]]:
    frame = panel.reset_index()
    frame["pred"] = np.asarray(pred, dtype=float)
    y_true_by_group: dict[Any, np.ndarray] = {}
    y_pred_by_group: dict[Any, np.ndarray] = {}
    for ts, group in frame.groupby("ts_close"):
        if len(group) < 2:
            continue
        y_true_by_group[ts] = group[target_col].to_numpy(dtype=float)
        y_pred_by_group[ts] = group["pred"].to_numpy(dtype=float)
    return y_true_by_group, y_pred_by_group


def _topk_daily_pnl(
    panel,
    pred: np.ndarray,
    target_col: str,
    top_k_fraction: float,
) -> np.ndarray:
    frame = panel.reset_index()
    frame["pred"] = np.asarray(pred, dtype=float)
    daily: dict[Any, list[float]] = {}
    for ts, group in frame.groupby("ts_close"):
        k = max(1, int(len(group) * float(top_k_fraction)))
        pnl = float(group.nlargest(k, "pred")[target_col].mean())
        daily.setdefault(ts.date(), []).append(pnl)
    return np.asarray([float(np.mean(values)) for values in daily.values()], dtype=float)


def _max_drawdown(daily_pnl: np.ndarray) -> float:
    if daily_pnl.size == 0:
        return 0.0
    equity = np.cumprod(1.0 + daily_pnl)
    peak = np.maximum.accumulate(equity)
    drawdown = equity / np.where(peak == 0.0, 1.0, peak) - 1.0
    return float(np.min(drawdown))


def _train_and_score(
    train_panel,
    val_panel,
    *,
    feature_cols: list[str],
    target_col: str,
    num_boost_round: int,
    num_threads: int,
    top_k_fraction: float,
) -> dict[str, Any]:
    from src.models.ranking_loss import get_lightgbm

    lgb = get_lightgbm()
    params = build_lgbm_params({"num_threads": int(num_threads)})
    train_panel = _sort_for_grouping(train_panel)
    val_panel = _sort_for_grouping(val_panel)
    train_x = train_panel[feature_cols].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    val_x = val_panel[feature_cols].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    train_ds = lgb.Dataset(
        train_x,
        label=train_panel["relevance"].astype(int).to_numpy(),
        group=compute_group_sizes(train_panel),
        free_raw_data=False,
    )
    booster = lgb.train(params, train_ds, num_boost_round=int(num_boost_round))
    pred = booster.predict(val_x)
    y_true, y_pred = _to_group_dict(val_panel, pred, target_col)
    ic_values = panel_ic(y_true, y_pred)
    rank_ic_values = panel_rank_ic(y_true, y_pred)
    daily_pnl = _topk_daily_pnl(val_panel, pred, target_col, top_k_fraction)
    return {
        "ic": float(np.mean(ic_values)) if ic_values.size else 0.0,
        "rank_ic": float(np.mean(rank_ic_values)) if rank_ic_values.size else 0.0,
        "ic_count": int(ic_values.size),
        "daily_pnl_mean": float(np.mean(daily_pnl)) if daily_pnl.size else 0.0,
        "daily_pnl_std": float(np.std(daily_pnl, ddof=1)) if daily_pnl.size > 1 else 0.0,
        "mdd": _max_drawdown(daily_pnl),
        "val_rows": int(len(val_panel)),
        "train_rows": int(len(train_panel)),
    }


def run_experiment(args: argparse.Namespace) -> dict[str, Any]:
    cfg = _experiment_cfg()
    bundle_id = str(args.bundle_id or _DEFAULT_BUNDLE_ID)
    default_start, default_end = _default_date_range(bundle_id)
    start_date = _normalize_yyyymmdd(args.start_date or default_start)
    end_date = _normalize_yyyymmdd(args.end_date or default_end)
    windows = _as_int_list(args.windows or cfg.get("windows", [120, 150, 180]))
    window_modes = [
        str(part).strip().lower()
        for part in str(args.window_modes or "rolling").split(",")
        if str(part).strip()
    ]
    invalid_modes = sorted(set(window_modes) - {"rolling", "expanding"})
    if invalid_modes:
        raise ValueError(f"unknown window_modes: {invalid_modes}")
    n_folds = int(args.n_folds or cfg.get("n_folds", 5))
    min_validation_days = int(cfg.get("min_validation_days", 5))
    embargo_days = int(cfg.get("embargo_days", 1))
    bar_stride = int(args.bar_stride or cfg.get("bar_stride", 15))
    target_col = str(args.target_col or cfg.get("target_col", "label_195m_net_ret"))
    num_boost_round = int(args.num_boost_round or cfg.get("lgbm_num_boost_round", 80))
    num_threads = int(args.num_threads or cfg.get("lgbm_num_threads", 2))
    top_k_fraction = float(config_load("risk_config.yaml", "evaluation")["top_k_fraction"])
    tickers = (
        [str(t).zfill(6) for t in str(args.tickers).split(",") if str(t).strip()]
        if args.tickers
        else _active_tickers()
    )
    if not tickers:
        raise RuntimeError("active ticker universe is empty")

    groups = _feature_groups(cfg)
    all_feature_cols = sorted({col for cols in groups.values() for col in cols})
    builder = DatasetBuilder(
        dual_source_enabled_for_lgbm=True,
        exogenous_enabled_for_lgbm=True,
    )
    panel = builder.build_training_frame(tickers, start_date, end_date)
    if target_col != builder.target_col:
        panel = builder.relabel_panel_for_target(panel, target_col)
    panel = _apply_bar_stride(panel, bar_stride)
    missing_required = sorted(
        set(all_feature_cols + [target_col, "relevance"]) - set(panel.columns)
    )
    if missing_required:
        raise RuntimeError(f"panel missing required columns: {missing_required}")

    dates = _panel_dates(panel)
    results: list[dict[str, Any]] = []
    for window_mode in window_modes:
        for window in windows:
            if window_mode == "rolling":
                folds = _build_folds(
                    dates,
                    window_days=window,
                    n_folds=n_folds,
                    min_validation_days=min_validation_days,
                    embargo_days=embargo_days,
                )
            else:
                folds = _build_expanding_folds(
                    dates,
                    initial_window_days=window,
                    n_folds=n_folds,
                    min_validation_days=min_validation_days,
                    embargo_days=embargo_days,
                )
            for group_name, feature_cols in groups.items():
                fold_metrics: list[dict[str, Any]] = []
                for fold in folds:
                    print(
                        "[rolling_window_ic] "
                        f"mode={window_mode} window={window} group={group_name} "
                        f"train={fold.train_start}~{fold.train_end} "
                        f"val={fold.val_start}~{fold.val_end}",
                        flush=True,
                    )
                    train_panel = _filter_dates(panel, fold.train_start, fold.train_end)
                    val_panel = _filter_dates(panel, fold.val_start, fold.val_end)
                    if train_panel.empty or val_panel.empty:
                        continue
                    metric = _train_and_score(
                        train_panel,
                        val_panel,
                        feature_cols=feature_cols,
                        target_col=target_col,
                        num_boost_round=num_boost_round,
                        num_threads=num_threads,
                        top_k_fraction=top_k_fraction,
                    )
                    metric["fold"] = fold.__dict__
                    fold_metrics.append(metric)
                mean_ic = (
                    float(np.mean([m["ic"] for m in fold_metrics]))
                    if fold_metrics
                    else 0.0
                )
                mean_rank_ic = (
                    float(np.mean([m["rank_ic"] for m in fold_metrics]))
                    if fold_metrics
                    else 0.0
                )
                mean_daily_pnl = (
                    float(np.mean([m["daily_pnl_mean"] for m in fold_metrics]))
                    if fold_metrics
                    else 0.0
                )
                results.append(
                    {
                        "window_mode": window_mode,
                        "window_days": int(window),
                        "group": group_name,
                        "feature_count": len(feature_cols),
                        "feature_cols": feature_cols,
                        "fold_count": len(fold_metrics),
                        "mean_ic": mean_ic,
                        "mean_rank_ic": mean_rank_ic,
                        "mean_daily_pnl": mean_daily_pnl,
                        "fold_metrics": fold_metrics,
                    }
                )

    ranked = sorted(
        results,
        key=lambda item: (
            float(item["mean_rank_ic"]),
            float(item["mean_ic"]),
            float(item["mean_daily_pnl"]),
        ),
        reverse=True,
    )
    report: dict[str, Any] = {
        "status": "PASS" if ranked else "BLOCKED",
        "action": "rolling_window_ic_experiment",
        "generated_at": datetime.now(_KST).isoformat(),
        "read_only": True,
        "registry_mutated": False,
        "live_trading_allowed": False,
        "external_kis_api": False,
        "bundle_id": bundle_id,
        "start_date": start_date,
        "end_date": end_date,
        "target_col": target_col,
        "windows": windows,
        "window_modes": window_modes,
        "n_folds": n_folds,
        "embargo_days": embargo_days,
        "bar_stride": bar_stride,
        "ticker_count": len(tickers),
        "tickers": tickers,
        "panel_rows": int(len(panel)),
        "panel_dates": {"count": len(dates), "first": dates[0], "last": dates[-1]},
        "methodology": {
            "train_window": "trading-date based",
            "validation_metric": "mean timestamp-level cross-sectional IC/RankIC",
            "notebook_bug_avoided": "does not use pooled_row_count = window_days * 390",
            "community_historical_alpha_claim": False,
        },
        "results": results,
        "best": ranked[0] if ranked else None,
        "ranking": [
            {
                "rank": idx + 1,
                "window_mode": item["window_mode"],
                "window_days": item["window_days"],
                "group": item["group"],
                "mean_rank_ic": item["mean_rank_ic"],
                "mean_ic": item["mean_ic"],
                "mean_daily_pnl": item["mean_daily_pnl"],
                "fold_count": item["fold_count"],
            }
            for idx, item in enumerate(ranked)
        ],
        "caveats": [
            "This is read-only research evidence, not a deploy gate by itself.",
            "Dual-Source historical community alpha remains unproven when only news_score_t is used.",
            "bar_stride is aligned to service-policy decision cadence and reduces minute-level autocorrelation.",
        ],
    }
    if args.write_report:
        report_dir = Path(str(cfg.get("report_dir", "artifacts/reports/rolling_window_ic")))
        if not report_dir.is_absolute():
            report_dir = ROOT / report_dir
        ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
        path = report_dir / f"rolling_window_ic_experiment_{ts}.json"
        _write_json(path, report)
        report["report_path"] = str(path)
        report["report_path_relative"] = _repo_relative(path)
        _write_json(path, report)
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    cfg = _experiment_cfg()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", default=_DEFAULT_BUNDLE_ID)
    parser.add_argument("--tickers", default=None)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--target-col", default=None)
    parser.add_argument("--windows", default=None, help="Comma-separated days, e.g. 120,150,180")
    parser.add_argument(
        "--window-modes",
        default="rolling",
        help="Comma-separated modes: rolling,expanding",
    )
    parser.add_argument("--n-folds", type=int, default=None)
    parser.add_argument("--bar-stride", type=int, default=None)
    parser.add_argument("--num-boost-round", type=int, default=None)
    parser.add_argument("--num-threads", type=int, default=None)
    parser.add_argument(
        "--write-report",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.set_defaults(
        windows=",".join(str(v) for v in cfg.get("windows", [120, 150, 180]))
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    report = run_experiment(_parse_args(argv))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

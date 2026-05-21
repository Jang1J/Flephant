#!/usr/bin/env python
"""Research-only feature/window LightGBM candidate grid.

This script trains candidate models for feature/window screening without
touching production registry, live trading, or KIS. It is intentionally a
pre-C12 triage tool: good rows here must still pass bundle staging, C12,
service-policy replay, deploy dry-run, service readiness, and prelive gate.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.data.dataset_builder import DatasetBuilder  # noqa: E402
from src.models.lgbm_trainer import LGBMTrainer  # noqa: E402
from src.models.registry import ModelRegistry  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.safe_cast import safe_bool  # noqa: E402
from src.utils.trading_calendar import kospi_trading_start_date  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_DEFAULT_BUNDLE_ID = "BUNDLE-20260518-195M0001"


@dataclass(frozen=True)
class FeatureGroup:
    name: str
    feature_cols: list[str]
    include_dual_source: bool
    include_exogenous: bool


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


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip()).strip("-").lower()
    return slug or "candidate"


def _as_int_list(values: Any) -> list[int]:
    if isinstance(values, str):
        values = [part.strip() for part in values.split(",") if part.strip()]
    return [int(value) for value in values]


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
    candidates = [
        ROOT / "artifacts" / "bundles" / bundle_id / "lgbm" / "latest_model_metadata.json",
        ROOT
        / "artifacts"
        / "lgbm_paper_candidate"
        / bundle_id
        / "cost-aware-195m-lr005-leaves31-child50_metadata.json",
    ]
    for path in candidates:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return (
                _normalize_yyyymmdd(str(data["train_start"])),
                _normalize_yyyymmdd(str(data["train_end"])),
            )
    gate = config_load("risk_config.yaml", "backtest_agent").get(
        "deploy_decision_gate", {}
    ).get("final_dataset_gate", {})
    return (
        _normalize_yyyymmdd(str(gate["expected_start_date"])),
        _normalize_yyyymmdd(str(gate["expected_end_date"])),
    )


def _start_for_window(end_date: str, window_days: int, default_start: str) -> str:
    if int(window_days) >= 249:
        return default_start
    end_dt = datetime.strptime(end_date, "%Y%m%d").date()
    return kospi_trading_start_date(end_dt, int(window_days)).strftime("%Y%m%d")


def _feature_groups(selected: list[str] | None = None) -> dict[str, FeatureGroup]:
    cfg = config_load("risk_config.yaml", "preprocessor")
    base = list(cfg["feature_cols"])
    exog = list(cfg.get("exogenous_feature_cols", []))
    allowed_ds = set(cfg.get("dual_source_feature_cols", []))
    specs: dict[str, dict[str, Any]] = {
        "OHLCV": {
            "include_base": True,
            "include_exogenous": False,
            "dual_source_cols": [],
        },
        "OHLCV+Exog": {
            "include_base": True,
            "include_exogenous": True,
            "dual_source_cols": [],
        },
        "OHLCV+News_DS": {
            "include_base": True,
            "include_exogenous": False,
            "dual_source_cols": ["news_score_t"],
        },
        "OHLCV+Exog+News_DS": {
            "include_base": True,
            "include_exogenous": True,
            "dual_source_cols": ["news_score_t"],
        },
    }
    if selected:
        wanted = set(selected)
        specs = {name: spec for name, spec in specs.items() if name in wanted}
    groups: dict[str, FeatureGroup] = {}
    for name, spec in specs.items():
        cols: list[str] = []
        if safe_bool(spec.get("include_base", True), default=True):
            cols.extend(base)
        if safe_bool(spec.get("include_exogenous", False), default=False):
            cols.extend(exog)
        ds_cols = [str(col) for col in spec.get("dual_source_cols", []) or []]
        for col in ds_cols:
            if col not in allowed_ds:
                raise ValueError(f"unknown Dual-Source feature: {col}")
            cols.append(col)
        groups[name] = FeatureGroup(
            name=name,
            feature_cols=list(dict.fromkeys(cols)),
            include_dual_source=bool(ds_cols),
            include_exogenous=safe_bool(
                spec.get("include_exogenous", False),
                default=False,
            ),
        )
    return groups


def _metric_value(metrics: dict[str, Any], key: str) -> float:
    try:
        return float(metrics.get(key, 0.0))
    except (TypeError, ValueError):
        return 0.0


def _run_one(
    *,
    window_days: int,
    group: FeatureGroup,
    tickers: list[str],
    start_date: str,
    end_date: str,
    target_col: str,
    registry_root: Path,
    n_estimators: int | None,
    early_stopping_rounds: int | None,
    num_threads: int | None,
) -> dict[str, Any]:
    version = f"w{window_days}-{_safe_slug(group.name)}"
    registry_dir = registry_root / version
    builder = DatasetBuilder(
        dual_source_enabled_for_lgbm=group.include_dual_source,
        exogenous_enabled_for_lgbm=group.include_exogenous,
    )
    registry = ModelRegistry(artifacts_dir=registry_dir)
    lgbm_overrides: dict[str, Any] = {}
    if num_threads is not None:
        lgbm_overrides["num_threads"] = int(num_threads)
    training_overrides: dict[str, int] = {}
    if n_estimators is not None:
        training_overrides["n_estimators"] = int(n_estimators)
    if early_stopping_rounds is not None:
        training_overrides["early_stopping_rounds"] = int(early_stopping_rounds)
    trainer = LGBMTrainer(
        dataset_builder=builder,
        registry=registry,
        lgbm_param_overrides=lgbm_overrides,
        training_control_overrides=training_overrides,
        include_dual_source_features=group.include_dual_source,
        include_exogenous_features=group.include_exogenous,
    )
    trainer.feature_cols = list(group.feature_cols)
    result = trainer.train(
        tickers=tickers,
        start_date=start_date,
        end_date=end_date,
        version=version,
        is_latest=False,
        target_col_override=target_col,
    )
    metrics = dict(result.get("metrics", {}) or {})
    return {
        "status": "PASS",
        "window_days": int(window_days),
        "group": group.name,
        "version": result.get("version", version),
        "train_start": start_date,
        "train_end": end_date,
        "target_col": target_col,
        "feature_count": len(group.feature_cols),
        "feature_cols": list(group.feature_cols),
        "feature_policy": {
            "include_dual_source": group.include_dual_source,
            "include_exogenous": group.include_exogenous,
            "news_ds_only": "news_score_t" in group.feature_cols,
            "community_historical_alpha_claim": False,
        },
        "metrics": metrics,
        "n_folds": result.get("n_folds", 0),
        "n_train_rows": result.get("n_train_rows", 0),
        "n_final_train_rows": result.get("n_final_train_rows", 0),
        "n_val_rows": result.get("n_val_rows", 0),
        "training_controls": {
            "n_estimators": n_estimators,
            "early_stopping_rounds": early_stopping_rounds,
            "num_threads": num_threads,
        },
        "model_path": result.get("model_path", ""),
        "registry_dir": _repo_relative(registry_dir),
    }


def run_experiment(args: argparse.Namespace) -> dict[str, Any]:
    bundle_id = str(args.bundle_id or _DEFAULT_BUNDLE_ID)
    default_start, default_end = _default_date_range(bundle_id)
    end_date = _normalize_yyyymmdd(args.end_date or default_end)
    windows = _as_int_list(args.windows)
    target_col = str(args.target_col or "label_195m_net_ret")
    tickers = (
        [str(t).zfill(6) for t in str(args.tickers).split(",") if str(t).strip()]
        if args.tickers
        else _active_tickers()
    )
    if not tickers:
        raise RuntimeError("active ticker universe is empty")
    group_names = (
        [part.strip() for part in str(args.groups).split(",") if part.strip()]
        if args.groups
        else None
    )
    groups = _feature_groups(group_names)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    registry_root = ROOT / str(args.registry_root or "artifacts/lgbm_research/feature_window_grid") / ts
    registry_root.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for window in windows:
        start_date = _start_for_window(end_date, int(window), default_start)
        for group in groups.values():
            print(
                "[feature_window_grid] "
                f"window={window} group={group.name} "
                f"train={start_date}~{end_date}",
                flush=True,
            )
            try:
                results.append(
                    _run_one(
                        window_days=int(window),
                        group=group,
                        tickers=tickers,
                        start_date=start_date,
                        end_date=end_date,
                        target_col=target_col,
                        registry_root=registry_root,
                        n_estimators=args.n_estimators,
                        early_stopping_rounds=args.early_stopping_rounds,
                        num_threads=args.num_threads,
                    )
                )
            except Exception as e:
                results.append(
                    {
                        "status": "FAIL",
                        "window_days": int(window),
                        "group": group.name,
                        "train_start": start_date,
                        "train_end": end_date,
                        "target_col": target_col,
                        "feature_count": len(group.feature_cols),
                        "feature_cols": list(group.feature_cols),
                        "error": str(e),
                        "error_type": type(e).__name__,
                    }
                )

    ranked = sorted(
        [item for item in results if item.get("status") == "PASS"],
        key=lambda item: (
            _metric_value(item.get("metrics", {}), "rank_ic"),
            _metric_value(item.get("metrics", {}), "ic"),
            _metric_value(item.get("metrics", {}), "sr"),
        ),
        reverse=True,
    )
    report: dict[str, Any] = {
        "status": "PASS" if ranked and len(ranked) == len(results) else "WARN",
        "action": "feature_window_candidate_experiment",
        "generated_at": datetime.now(_KST).isoformat(),
        "read_only": True,
        "registry_mutated": False,
        "production_registry_mutated": False,
        "live_trading_allowed": False,
        "external_kis_api": False,
        "bundle_id_reference": bundle_id,
        "end_date": end_date,
        "windows": windows,
        "target_col": target_col,
        "ticker_count": len(tickers),
        "tickers": tickers,
        "registry_root": _repo_relative(registry_root),
        "training_controls": {
            "n_estimators": args.n_estimators,
            "early_stopping_rounds": args.early_stopping_rounds,
            "num_threads": args.num_threads,
        },
        "methodology": {
            "scope": "research_trainer_proxy",
            "deploy_quality": False,
            "window_definition": (
                "final model trains on the requested full window; "
                "trainer metrics are internal walk-forward diagnostics"
            ),
            "next_required_gate": (
                "stage selected candidate bundle, then run C12, service-policy replay, "
                "deploy dry-run, service readiness, and prelive gate"
            ),
        },
        "results": results,
        "ranking": [
            {
                "rank": idx + 1,
                "window_days": item["window_days"],
                "group": item["group"],
                "rank_ic": _metric_value(item.get("metrics", {}), "rank_ic"),
                "ic": _metric_value(item.get("metrics", {}), "ic"),
                "sr": _metric_value(item.get("metrics", {}), "sr"),
                "mdd": _metric_value(item.get("metrics", {}), "mdd"),
                "version": item.get("version"),
                "registry_dir": item.get("registry_dir"),
            }
            for idx, item in enumerate(ranked)
        ],
        "best": ranked[0] if ranked else None,
        "caveats": [
            "This is trainer-validation proxy evidence, not deploy-quality.",
            "News_DS means news_score_t only; it is not historical community alpha.",
            "Production registry active_version remains untouched.",
        ],
    }
    if args.write_report:
        report_dir = ROOT / str(args.report_dir or "artifacts/reports/feature_window_grid")
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / f"feature_window_candidate_experiment_{ts}.json"
        report["report_path"] = str(path)
        report["report_path_relative"] = _repo_relative(path)
        _write_json(path, report)
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", default=_DEFAULT_BUNDLE_ID)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--windows", default="249,180")
    parser.add_argument("--groups", default="")
    parser.add_argument("--tickers", default=None)
    parser.add_argument("--target-col", default="label_195m_net_ret")
    parser.add_argument("--registry-root", default="artifacts/lgbm_research/feature_window_grid")
    parser.add_argument("--report-dir", default="artifacts/reports/feature_window_grid")
    parser.add_argument("--n-estimators", type=int, default=None)
    parser.add_argument("--early-stopping-rounds", type=int, default=None)
    parser.add_argument("--num-threads", type=int, default=None)
    parser.add_argument(
        "--write-report",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    report = run_experiment(_parse_args(argv))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") in {"PASS", "WARN"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

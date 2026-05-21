#!/usr/bin/env python
"""Research-only LightGBM hyperparameter sweep.

This script intentionally writes only to ``artifacts/lgbm_research`` and
``artifacts/reports``. It does not mutate the production registry, does not
call KIS, and does not mark any candidate deployable. A good row here still
must pass bundle staging, C12, service-policy replay, deploy dry-run, service
readiness, and prelive gate.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
SCRIPT_DIR = Path(__file__).resolve().parent
for candidate in (SRC, SCRIPT_DIR):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from feature_window_candidate_experiment import (  # noqa: E402
    _active_tickers,
    _default_date_range,
    _feature_groups,
    _metric_value,
    _normalize_yyyymmdd,
    _repo_relative,
    _safe_slug,
    _start_for_window,
    _write_json,
)
from src.data.dataset_builder import DatasetBuilder  # noqa: E402
from src.models.lgbm_trainer import LGBMTrainer  # noqa: E402
from src.models.ranking_loss import build_lgbm_params, get_lightgbm  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_DEFAULT_BUNDLE_ID = "BUNDLE-20260518-195M0001"


@dataclass(frozen=True)
class HyperParamCandidate:
    name: str
    lgbm_params: dict[str, Any]
    training_control: dict[str, int]


def _sweep_cfg() -> dict[str, Any]:
    return config_load("risk_config.yaml", "lgbm_hyperparam_sweep") or {}


def _load_grid_from_json(path: Path) -> list[HyperParamCandidate]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("grid JSON must be a list")
    out: list[HyperParamCandidate] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"grid item must be an object: index={idx}")
        name = str(item.get("name") or f"candidate_{idx + 1}")
        lgbm_params = item.get("lgbm_params") or {}
        training_control = item.get("training_control") or {}
        if not isinstance(lgbm_params, dict) or not isinstance(training_control, dict):
            raise ValueError(f"invalid grid item params: index={idx}")
        out.append(
            HyperParamCandidate(
                name=name,
                lgbm_params=dict(lgbm_params),
                training_control={str(k): int(v) for k, v in training_control.items()},
            )
        )
    return out


def _param_grid(preset: str, grid_json: str | None = None) -> list[HyperParamCandidate]:
    if grid_json:
        return _load_grid_from_json(Path(grid_json))
    presets = _sweep_cfg().get("presets", {})
    if not isinstance(presets, dict) or preset not in presets:
        raise ValueError(
            f"unknown preset={preset!r}; choices={sorted(presets) if isinstance(presets, dict) else []}"
        )
    rows = presets[preset]
    if not isinstance(rows, list):
        raise ValueError(f"preset must be a list: {preset}")
    out: list[HyperParamCandidate] = []
    for idx, item in enumerate(rows):
        if not isinstance(item, dict):
            raise ValueError(f"preset item must be an object: preset={preset} index={idx}")
        name = str(item.get("name") or f"{preset}_{idx + 1}")
        lgbm_params = item.get("lgbm_params") or {}
        training_control = item.get("training_control") or {}
        if not isinstance(lgbm_params, dict) or not isinstance(training_control, dict):
            raise ValueError(f"invalid preset item params: preset={preset} index={idx}")
        out.append(
            HyperParamCandidate(
                name=name,
                lgbm_params=dict(lgbm_params),
                training_control={str(k): int(v) for k, v in training_control.items()},
            )
        )
    return out


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _resolve_research_registry_root(raw: str | None) -> Path:
    cfg = _sweep_cfg()
    path = Path(str(raw or cfg.get("registry_root") or "artifacts/lgbm_research/hyperparam_sweep"))
    resolved = path if path.is_absolute() else ROOT / path
    production_root = ROOT / "artifacts" / "lgbm"
    if _is_relative_to(resolved, production_root):
        raise ValueError(
            "lgbm_hyperparam_sweep must not write under artifacts/lgbm; "
            "use artifacts/lgbm_research/..."
        )
    research_root = ROOT / "artifacts" / "lgbm_research"
    if not _is_relative_to(resolved, research_root):
        raise ValueError(
            "lgbm_hyperparam_sweep registry_root must be under artifacts/lgbm_research"
        )
    return resolved


def _validate_target_col(target_col: str) -> str:
    allowed = config_load("risk_config.yaml", "label").get("deploy_target_cols", [])
    allowed_set = {str(item) for item in allowed or []}
    if str(target_col) not in allowed_set:
        raise ValueError(
            f"target_col={target_col!r} is not in label.deploy_target_cols={sorted(allowed_set)}"
        )
    return str(target_col)


def _float_series(fold_metrics: list[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for row in fold_metrics:
        try:
            values.append(float(row.get(key, 0.0)))
        except (TypeError, ValueError):
            values.append(0.0)
    return values


def _fold_stability(fold_metrics: list[dict[str, Any]]) -> dict[str, Any]:
    rank_ics = _float_series(fold_metrics, "rank_ic")
    ics = _float_series(fold_metrics, "ic")

    def mean(values: list[float]) -> float:
        return float(statistics.fmean(values)) if values else 0.0

    def stdev(values: list[float]) -> float:
        return float(statistics.pstdev(values)) if len(values) > 1 else 0.0

    return {
        "fold_count": len(fold_metrics),
        "rank_ic_mean": mean(rank_ics),
        "rank_ic_std": stdev(rank_ics),
        "rank_ic_min": min(rank_ics) if rank_ics else 0.0,
        "rank_ic_max": max(rank_ics) if rank_ics else 0.0,
        "rank_ic_positive_fold_rate": (
            sum(1 for value in rank_ics if value > 0.0) / len(rank_ics)
            if rank_ics
            else 0.0
        ),
        "ic_mean": mean(ics),
        "ic_std": stdev(ics),
        "ic_min": min(ics) if ics else 0.0,
        "ic_max": max(ics) if ics else 0.0,
        "ic_positive_fold_rate": (
            sum(1 for value in ics if value > 0.0) / len(ics) if ics else 0.0
        ),
    }


def _overfit_assessment(
    *,
    metrics: dict[str, Any],
    fold_metrics: list[dict[str, Any]],
    stability: dict[str, Any],
    params: dict[str, Any],
    training_control: dict[str, int],
    final_num_boost_round: int,
) -> dict[str, Any]:
    warnings: list[str] = []
    blockers: list[str] = []
    rank_ic = _metric_value(metrics, "rank_ic")
    ic = _metric_value(metrics, "ic")
    sr = _metric_value(metrics, "sr")
    rank_ic_std = float(stability.get("rank_ic_std", 0.0))
    positive_rate = float(stability.get("rank_ic_positive_fold_rate", 0.0))
    n_estimators = int(training_control.get("n_estimators", 0) or 0)
    num_leaves = int(params.get("num_leaves", 0) or 0)
    min_child_samples = int(params.get("min_child_samples", 0) or 0)

    if len(fold_metrics) < 3:
        blockers.append("too_few_walk_forward_folds")
    if rank_ic <= 0.0 or ic <= 0.0:
        blockers.append("non_positive_ic_or_rank_ic")
    if positive_rate < 0.6:
        blockers.append("rank_ic_positive_fold_rate_below_0_6")
    if rank_ic_std > max(0.08, abs(rank_ic) * 2.0):
        warnings.append("rank_ic_high_fold_dispersion")
    if n_estimators > 0 and final_num_boost_round >= int(n_estimators * 0.95):
        warnings.append("early_stopping_did_not_reduce_rounds")
    if num_leaves >= 63 and min_child_samples < 50:
        warnings.append("high_leaf_low_child_complexity")
    if sr > 8.0:
        warnings.append("trainer_proxy_sr_very_high_verify_with_c12")

    stability_penalty = 0.5 * rank_ic_std
    fold_penalty = max(0.0, 0.8 - positive_rate) * 0.05
    selection_score = float(rank_ic - stability_penalty - fold_penalty)
    status = "FAIL" if blockers else ("WARN" if warnings else "PASS")
    return {
        "status": status,
        "blockers": blockers,
        "warnings": warnings,
        "selection_score": selection_score,
        "selection_formula": "rank_ic - 0.5*rank_ic_std - max(0,0.8-positive_rate)*0.05",
        "deploy_quality": False,
    }


def _active_universe_hash(tickers: list[str]) -> str:
    import hashlib

    payload = ",".join(sorted(str(t).zfill(6) for t in tickers))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_panel_and_folds(
    *,
    group: Any,
    tickers: list[str],
    start_date: str,
    end_date: str,
    target_col: str,
) -> tuple[Any, list[tuple[Any, Any]], dict[str, Any]]:
    builder = DatasetBuilder(
        dual_source_enabled_for_lgbm=group.include_dual_source,
        exogenous_enabled_for_lgbm=group.include_exogenous,
    )
    trainer = LGBMTrainer(
        dataset_builder=builder,
        include_dual_source_features=group.include_dual_source,
        include_exogenous_features=group.include_exogenous,
    )
    trainer.feature_cols = list(group.feature_cols)
    panel = builder.build_training_frame(tickers, start_date, end_date)
    if target_col != trainer.target_col:
        panel = builder.relabel_panel_for_target(panel, target_col)
        if panel.empty:
            raise RuntimeError(f"target_col 적용 후 panel empty: {target_col}")
    data_source = dict(getattr(panel, "attrs", {}) or {})
    trainer._assert_requested_tickers_present(panel, tickers, data_source)
    trainer._assert_feature_manifest(panel)
    folds = list(trainer.splitter.split(panel))
    if not folds:
        raise RuntimeError("walk-forward fold 0개")
    return panel, folds, data_source


def _run_one(
    *,
    candidate: HyperParamCandidate,
    window_days: int,
    group_name: str,
    group: Any,
    panel: Any,
    folds: list[tuple[Any, Any]],
    start_date: str,
    end_date: str,
    target_col: str,
) -> dict[str, Any]:
    version = f"hp-{_safe_slug(candidate.name)}-w{window_days}-{_safe_slug(group_name)}"
    trainer = LGBMTrainer(
        lgbm_param_overrides=dict(candidate.lgbm_params),
        training_control_overrides=dict(candidate.training_control),
        include_dual_source_features=group.include_dual_source,
        include_exogenous_features=group.include_exogenous,
    )
    trainer.feature_cols = list(group.feature_cols)
    lgb = get_lightgbm()
    params = build_lgbm_params(overrides=dict(candidate.lgbm_params))
    training_control = trainer._training_control()
    fold_metrics: list[dict[str, float]] = []
    best_iterations: list[int] = []
    n_cv_last_train_rows = 0
    n_val_rows = 0

    for fold_idx, (train_idx, val_idx) in enumerate(folds):
        train_panel = panel.iloc[train_idx].sort_index(level="ts_close")
        val_panel = panel.iloc[val_idx].sort_index(level="ts_close")
        booster, metrics = trainer._train_fold(
            train_panel,
            val_panel,
            params,
            training_control,
            lgb,
            target_col=target_col,
        )
        fold_metrics.append({"fold": fold_idx, **metrics})
        best_iteration = int(getattr(booster, "best_iteration", 0) or 0)
        if best_iteration > 0:
            best_iterations.append(best_iteration)
        n_cv_last_train_rows = int(len(train_panel))
        n_val_rows = int(len(val_panel))

    metrics = trainer._aggregate_fold_metrics(fold_metrics)
    final_num_boost_round = trainer._final_num_boost_round(
        best_iterations,
        training_control,
    )
    stability = _fold_stability(fold_metrics)
    assessment = _overfit_assessment(
        metrics=metrics,
        fold_metrics=fold_metrics,
        stability=stability,
        params=params,
        training_control=training_control,
        final_num_boost_round=int(final_num_boost_round),
    )
    return {
        "status": "PASS",
        "candidate_name": candidate.name,
        "version": version,
        "window_days": int(window_days),
        "group": group_name,
        "train_start": start_date,
        "train_end": end_date,
        "target_col": target_col,
        "feature_count": len(group.feature_cols),
        "feature_policy": {
            "include_dual_source": group.include_dual_source,
            "include_exogenous": group.include_exogenous,
            "news_ds_only": "news_score_t" in group.feature_cols,
            "community_historical_alpha_claim": False,
        },
        "metrics": metrics,
        "fold_stability": stability,
        "overfit_assessment": assessment,
        "n_folds": len(folds),
        "n_train_rows": int(len(panel)),
        "n_cv_last_train_rows": n_cv_last_train_rows,
        "n_val_rows": n_val_rows,
        "final_num_boost_round": final_num_boost_round,
        "cv_best_iterations": best_iterations,
        "lgbm_params": params,
        "training_control": training_control,
        "model_path": "",
        "registry_dir": "",
    }


def run_sweep(args: argparse.Namespace) -> dict[str, Any]:
    cfg = _sweep_cfg()
    bundle_id = str(args.bundle_id or _DEFAULT_BUNDLE_ID)
    default_start, default_end = _default_date_range(bundle_id)
    end_date = _normalize_yyyymmdd(args.end_date or default_end)
    raw_windows = args.windows
    if not raw_windows:
        raw_windows = ",".join(str(item) for item in cfg.get("default_windows", [249]))
    windows = [int(part.strip()) for part in str(raw_windows).split(",") if part.strip()]
    target_col = _validate_target_col(
        str(args.target_col or cfg.get("target_col") or "label_195m_net_ret")
    )
    tickers = (
        [str(t).strip().zfill(6) for t in str(args.tickers).split(",") if str(t).strip()]
        if args.tickers
        else _active_tickers()
    )
    if not tickers:
        raise RuntimeError("active ticker universe is empty")
    raw_groups = args.groups
    if not raw_groups:
        raw_groups = ",".join(str(item) for item in cfg.get("default_groups", ["OHLCV+Exog+News_DS"]))
    group_names = [part.strip() for part in str(raw_groups).split(",") if part.strip()]
    groups = _feature_groups(group_names or None)
    grid = _param_grid(str(args.preset or cfg.get("default_preset", "compact")), args.grid_json)
    if args.max_combinations is not None:
        grid = grid[: int(args.max_combinations)]
    if not grid:
        raise RuntimeError("hyperparameter grid is empty")

    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    registry_root = _resolve_research_registry_root(args.registry_root) / ts

    results: list[dict[str, Any]] = []
    for window in windows:
        start_date = _start_for_window(end_date, int(window), default_start)
        for group_name, group in groups.items():
            print(
                "[lgbm_hyperparam_sweep] "
                f"build_panel window={window} group={group_name} "
                f"train={start_date}~{end_date}",
                flush=True,
            )
            try:
                panel, folds, data_source = _build_panel_and_folds(
                    group=group,
                    tickers=tickers,
                    start_date=start_date,
                    end_date=end_date,
                    target_col=target_col,
                )
            except Exception as e:
                for candidate in grid:
                    results.append(
                        {
                            "status": "FAIL",
                            "candidate_name": candidate.name,
                            "window_days": int(window),
                            "group": group_name,
                            "train_start": start_date,
                            "train_end": end_date,
                            "target_col": target_col,
                            "error": str(e),
                            "error_type": type(e).__name__,
                            "stage": "build_panel_and_folds",
                            "lgbm_params": candidate.lgbm_params,
                            "training_control": candidate.training_control,
                        }
                    )
                continue
            for candidate in grid:
                print(
                    "[lgbm_hyperparam_sweep] "
                    f"fit window={window} group={group_name} hp={candidate.name} "
                    f"rows={len(panel)} folds={len(folds)}",
                    flush=True,
                )
                try:
                    results.append(
                        _run_one(
                            candidate=candidate,
                            window_days=int(window),
                            group_name=group_name,
                            group=group,
                            panel=panel,
                            folds=folds,
                            start_date=start_date,
                            end_date=end_date,
                            target_col=target_col,
                        )
                    )
                except Exception as e:
                    results.append(
                        {
                            "status": "FAIL",
                            "candidate_name": candidate.name,
                            "window_days": int(window),
                            "group": group_name,
                            "train_start": start_date,
                            "train_end": end_date,
                            "target_col": target_col,
                            "error": str(e),
                            "error_type": type(e).__name__,
                            "lgbm_params": candidate.lgbm_params,
                            "training_control": candidate.training_control,
                            "data_source": data_source,
                        }
                    )

    pass_rows = [row for row in results if row.get("status") == "PASS"]
    ranked = sorted(
        pass_rows,
        key=lambda row: (
            float(row.get("overfit_assessment", {}).get("selection_score", -999.0)),
            _metric_value(row.get("metrics", {}), "rank_ic"),
            _metric_value(row.get("metrics", {}), "ic"),
        ),
        reverse=True,
    )
    safe_ranked = [
        row
        for row in ranked
        if row.get("overfit_assessment", {}).get("status") in {"PASS", "WARN"}
        and not row.get("overfit_assessment", {}).get("blockers")
    ]
    report: dict[str, Any] = {
        "status": "PASS" if pass_rows and len(pass_rows) == len(results) else "WARN",
        "action": "lgbm_hyperparam_sweep",
        "generated_at": datetime.now(_KST).isoformat(),
        "read_only": True,
        "registry_mutated": False,
        "production_registry_mutated": False,
        "live_trading_allowed": False,
        "external_kis_api": False,
        "deploy_quality": False,
        "bundle_id_reference": bundle_id,
        "end_date": end_date,
        "windows": windows,
        "target_col": target_col,
        "ticker_count": len(tickers),
        "universe_hash": _active_universe_hash(tickers),
        "groups": list(groups),
        "preset": str(args.preset or cfg.get("default_preset", "compact")),
        "grid_size": len(grid),
        "total_runs": len(results),
        "registry_root": _repo_relative(registry_root),
        "methodology": {
            "scope": "research_trainer_proxy",
            "selection_metric": "overfit_adjusted_selection_score",
            "overfit_controls": [
                "purged walk-forward folds from LGBMTrainer",
                "early stopping per fold",
                "positive RankIC fold-rate check",
                "RankIC fold dispersion penalty",
                "complexity warnings for wide leaf/low child settings",
            ],
            "next_required_gate": (
                "stage selected candidate bundle, run C12, service-policy replay, "
                "deploy dry-run, service readiness, and prelive gate"
            ),
        },
        "results": results,
        "ranking": [
            {
                "rank": idx + 1,
                "candidate_name": row.get("candidate_name"),
                "window_days": row.get("window_days"),
                "group": row.get("group"),
                "selection_score": row.get("overfit_assessment", {}).get(
                    "selection_score"
                ),
                "overfit_status": row.get("overfit_assessment", {}).get("status"),
                "overfit_warnings": row.get("overfit_assessment", {}).get("warnings"),
                "rank_ic": _metric_value(row.get("metrics", {}), "rank_ic"),
                "ic": _metric_value(row.get("metrics", {}), "ic"),
                "sr": _metric_value(row.get("metrics", {}), "sr"),
                "mdd": _metric_value(row.get("metrics", {}), "mdd"),
                "rank_ic_std": row.get("fold_stability", {}).get("rank_ic_std"),
                "rank_ic_positive_fold_rate": row.get("fold_stability", {}).get(
                    "rank_ic_positive_fold_rate"
                ),
                "final_num_boost_round": row.get("final_num_boost_round"),
                "version": row.get("version"),
                "registry_dir": row.get("registry_dir"),
            }
            for idx, row in enumerate(ranked)
        ],
        "best_research_candidate": safe_ranked[0] if safe_ranked else None,
        "caveats": [
            "This is trainer-validation proxy evidence, not deploy-quality.",
            "Do not mutate production registry from this report.",
            "News_DS means news_score_t only; historical community alpha remains unproven.",
        ],
    }
    if args.write_report:
        report_dir = ROOT / str(
            args.report_dir
            or cfg.get("report_dir")
            or "artifacts/reports/lgbm_hyperparam_sweep"
        )
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / f"lgbm_hyperparam_sweep_{ts}.json"
        report["report_path"] = str(path)
        report["report_path_relative"] = _repo_relative(path)
        _write_json(path, report)
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    cfg = _sweep_cfg()
    presets = cfg.get("presets", {})
    preset_choices = sorted(presets) if isinstance(presets, dict) else ["compact"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", default=_DEFAULT_BUNDLE_ID)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--windows", default=None)
    parser.add_argument("--groups", default=None)
    parser.add_argument("--tickers", default=None)
    parser.add_argument("--target-col", default=None)
    parser.add_argument(
        "--preset",
        default=str(cfg.get("default_preset", "compact")),
        choices=preset_choices,
    )
    parser.add_argument("--grid-json", default=None)
    parser.add_argument("--max-combinations", type=int, default=None)
    parser.add_argument("--registry-root", default=None)
    parser.add_argument("--report-dir", default=None)
    parser.add_argument(
        "--write-report",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    report = run_sweep(_parse_args(argv))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") in {"PASS", "WARN"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

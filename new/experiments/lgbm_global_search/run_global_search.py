#!/usr/bin/env python
"""Research-only bounded global search for LightGBM candidates."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import itertools
import json
import math
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import yaml

KST = ZoneInfo("Asia/Seoul")
SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[3]
SRC = REPO_ROOT / "new"
DEFAULT_CONFIG = SCRIPT_PATH.with_name("search_space_weekend.yaml")
PRODUCTION_REGISTRY = REPO_ROOT / "artifacts" / "lgbm"
PAPER_REGISTRY = REPO_ROOT / "artifacts" / "lgbm_paper"
PAPER_CANDIDATE_ROOT = REPO_ROOT / "artifacts" / "lgbm_paper_candidate"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.data.dataset_builder import DatasetBuilder  # noqa: E402
from src.models.registry import ModelRegistry  # noqa: E402
from src.models.ranking_loss import build_lgbm_params, get_training_control  # noqa: E402
import src.models.lgbm_trainer as trainer_module  # noqa: E402
from src.models.lgbm_trainer import LGBMTrainer  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.safe_cast import safe_bool  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    stage: str
    target_col: str
    feature_policy_id: str
    hyperparam_id: str
    start_date: str
    end_date: str
    feature_policy: dict[str, Any]
    params: dict[str, Any]
    training_control: dict[str, Any]


def _json_dump(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _jsonl_append(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config must be a mapping: {path}")
    return data


def _active_tickers(max_tickers: int) -> list[str]:
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
        for stock in sector.get("stocks", []) or []:
            if str(stock.get("status", "")) in stock_statuses and stock.get("ticker"):
                tickers.append(pad_ticker(str(stock["ticker"])))
    if not tickers:
        fallback = (cfg.get("backtest_universe_mode") or {}).get("fallback_tickers", [])
        tickers = [pad_ticker(str(t)) for t in fallback]
    return sorted(set(tickers))[:max_tickers]


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _candidate_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:10].upper()


def _deadline_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    parsed = datetime.strptime(raw, "%H:%M").time()
    now = datetime.now(KST)
    return datetime.combine(now.date(), parsed, tzinfo=KST)


def _deadline_reached(deadline: datetime | None) -> bool:
    return deadline is not None and datetime.now(KST) >= deadline


def _safe_research_root(path: Path) -> Path:
    resolved = path.resolve()
    allowed_base = (REPO_ROOT / "artifacts" / "lgbm_global_search").resolve()
    forbidden = [
        PRODUCTION_REGISTRY.resolve(),
        PAPER_REGISTRY.resolve(),
        PAPER_CANDIDATE_ROOT.resolve(),
    ]
    if any(resolved == item or item in resolved.parents for item in forbidden):
        raise RuntimeError(f"unsafe research_root: {resolved}")
    if not (resolved == allowed_base or allowed_base in resolved.parents):
        raise RuntimeError(f"research_root must be under {allowed_base}: {resolved}")
    return resolved


def _research_safety_fields(
    *,
    registry_dir: Path | None = None,
    research_registry_mutated: bool | None = None,
) -> dict[str, Any]:
    mutation_scope = "unknown"
    if research_registry_mutated is True:
        mutation_scope = "research_only"
    elif research_registry_mutated is False:
        mutation_scope = "none"
    payload: dict[str, Any] = {
        "research_only": True,
        "external_kis_api": False,
        "registry_mutated": bool(research_registry_mutated)
        if research_registry_mutated is not None
        else None,
        "registry_mutation_scope": mutation_scope,
        "research_registry_mutated": research_registry_mutated,
        "production_registry_mutated": False,
        "paper_registry_mutated": False,
        "live_trading_allowed": False,
        "deploy_quality": False,
        "requires_c12": True,
        "service_policy_replay_pass": False,
    }
    if registry_dir is not None:
        payload["registry_dir"] = str(registry_dir)
    return payload


def _candidate_registry_write_observed(registry_dir: Path, candidate: Candidate) -> bool:
    version = candidate.candidate_id[:180]
    return any(registry_dir.glob(f"{version}*"))


def _feature_cols(feature_policy: dict[str, Any]) -> list[str]:
    pre_cfg = config_load("risk_config.yaml", "preprocessor") or {}
    cols = list(pre_cfg.get("feature_cols", []))
    if bool(feature_policy.get("include_exogenous")):
        for col in pre_cfg.get("exogenous_feature_cols", []) or []:
            if col not in cols:
                cols.append(str(col))
    if bool(feature_policy.get("include_dual_source")):
        for col in feature_policy.get("dual_source_cols", []) or []:
            if col not in cols:
                cols.append(str(col))
    return cols


def _build_candidates(
    *,
    config: dict[str, Any],
    stage: str,
    start_date: str,
    end_date: str,
) -> list[Candidate]:
    targets = [str(t) for t in config.get("targets", [])]
    feature_policies = list(config.get("feature_policies", []) or [])
    hyperparams = list(config.get("hyperparams", []) or [])
    candidates: list[Candidate] = []
    # Balanced order: each hyperparameter family gets tested across all label/feature
    # policies before moving on, so a deadline stop still leaves broad coverage.
    for hp, target_col, feature_policy in itertools.product(hyperparams, targets, feature_policies):
        fp_id = str(feature_policy.get("id"))
        hp_id = str(hp.get("id"))
        payload = {
            "stage": stage,
            "target_col": target_col,
            "feature_policy_id": fp_id,
            "hyperparam_id": hp_id,
            "start_date": start_date,
            "end_date": end_date,
        }
        cid = f"{stage}-{_candidate_hash(payload)}-{target_col}-{fp_id}-{hp_id}"
        cid = cid.replace("/", "_").replace(" ", "_")
        candidates.append(
            Candidate(
                candidate_id=cid,
                stage=stage,
                target_col=target_col,
                feature_policy_id=fp_id,
                hyperparam_id=hp_id,
                start_date=start_date,
                end_date=end_date,
                feature_policy=dict(feature_policy),
                params=dict(hp.get("params") or {}),
                training_control=dict(hp.get("training_control") or {}),
            )
        )
    return candidates


@contextlib.contextmanager
def _patched_lgbm_training(
    params_override: dict[str, Any],
    control_override: dict[str, Any],
) -> Iterator[None]:
    original_params = trainer_module.build_lgbm_params
    original_control = trainer_module.get_training_control

    def patched_params() -> dict[str, Any]:
        return build_lgbm_params(dict(params_override))

    def patched_control() -> dict[str, int]:
        base = get_training_control()
        for key, value in control_override.items():
            if key in base:
                base[key] = int(value)
        return base

    trainer_module.build_lgbm_params = patched_params
    trainer_module.get_training_control = patched_control
    try:
        yield
    finally:
        trainer_module.build_lgbm_params = original_params
        trainer_module.get_training_control = original_control


def _patch_trade_classifier(trainer: LGBMTrainer, skip: bool) -> None:
    if not skip:
        return

    def skipped_classifier(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "enabled": False,
            "status": "SKIPPED_RESEARCH_PROXY",
            "reason": "global_search_skip_trade_classifier",
            "deploy_gate_eligible": False,
        }

    trainer._train_trade_no_trade_classifier = skipped_classifier  # type: ignore[method-assign]


def _score_result(result: dict[str, Any]) -> dict[str, Any]:
    metrics = result.get("metrics") or {}
    fold_metrics = result.get("fold_metrics") or []
    rank_ics: list[float] = []
    ics: list[float] = []
    for row in fold_metrics:
        if not isinstance(row, dict):
            continue
        rank_ic_value = _finite_float(row.get("rank_ic"))
        ic_value = _finite_float(row.get("ic"))
        if rank_ic_value is not None:
            rank_ics.append(rank_ic_value)
        if ic_value is not None:
            ics.append(ic_value)
    rank_ic_std = float("nan")
    rank_ic_positive_rate = 0.0
    if rank_ics:
        import numpy as np

        rank_ic_std = float(np.std(rank_ics, ddof=1)) if len(rank_ics) > 1 else 0.0
        rank_ic_positive_rate = sum(1 for x in rank_ics if x > 0) / len(rank_ics)
    rank_ic = float(metrics.get("rank_ic", 0.0) or 0.0)
    ic = float(metrics.get("ic", 0.0) or 0.0)
    penalty = max(0.0, 0.75 - rank_ic_positive_rate) * 0.02
    selection_score = rank_ic - 0.5 * (rank_ic_std if math.isfinite(rank_ic_std) else 0.0) - penalty
    return {
        "selection_score": selection_score,
        "rank_ic": rank_ic,
        "ic": ic,
        "rank_ic_std": rank_ic_std,
        "rank_ic_positive_rate": rank_ic_positive_rate,
        "fold_count": len(fold_metrics),
        "sr_proxy": float(metrics.get("sr", 0.0) or 0.0),
        "mdd_proxy": float(metrics.get("mdd", 0.0) or 0.0),
        "ic_positive_rate": sum(1 for x in ics if x > 0) / len(ics) if ics else 0.0,
    }


def _run_candidate(
    *,
    candidate: Candidate,
    tickers: list[str],
    research_root: Path,
    run_id: str,
    skip_trade_classifier: bool,
) -> dict[str, Any]:
    started = datetime.now(KST)
    registry_dir = research_root / run_id / "registry"
    registry = ModelRegistry(artifacts_dir=registry_dir)
    builder = DatasetBuilder()
    # DatasetBuilder reads feature-join toggles from risk_config at construction.
    # The research harness intentionally varies those toggles per candidate.
    builder._ds_enabled_for_lgbm = bool(candidate.feature_policy.get("include_dual_source"))
    builder._exog_enabled_for_lgbm = bool(candidate.feature_policy.get("include_exogenous"))
    trainer = LGBMTrainer(registry=registry, dataset_builder=builder)
    trainer.feature_cols = _feature_cols(candidate.feature_policy)
    _patch_trade_classifier(trainer, skip_trade_classifier)
    version = candidate.candidate_id[:180]
    bundle_id = f"{run_id}-{_candidate_hash(asdict(candidate))}"
    with _patched_lgbm_training(candidate.params, candidate.training_control):
        result = trainer.train(
            tickers=tickers,
            start_date=candidate.start_date,
            end_date=candidate.end_date,
            version=version,
            bundle_id=bundle_id,
            target_col_override=candidate.target_col,
        )
    scored = _score_result(result)
    finished = datetime.now(KST)
    return {
        "status": "RESEARCH_PASS",
        "candidate": asdict(candidate),
        "version": result.get("version"),
        "bundle_id": bundle_id,
        "model_path": result.get("model_path"),
        "metrics": result.get("metrics", {}),
        "metric_scope": result.get(
            "metric_scope",
            {
                "scope": "trainer_validation_proxy",
                "deploy_quality": False,
                "reason": "Trainer fold metrics are diagnostic only; C12 real backtest is required before deploy.",
            },
        ),
        "score": scored,
        "n_folds": result.get("n_folds"),
        "n_train_rows": result.get("n_train_rows"),
        "n_val_rows": result.get("n_val_rows"),
        "feature_cols": trainer.feature_cols,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "elapsed_sec": round((finished - started).total_seconds(), 3),
        **_research_safety_fields(
            registry_dir=registry_dir,
            research_registry_mutated=True,
        ),
    }


def _write_summary(run_dir: Path, results: list[dict[str, Any]]) -> dict[str, Any]:
    ranked = sorted(
        results,
        key=lambda row: float((row.get("score") or {}).get("selection_score", -999.0)),
        reverse=True,
    )
    pass_count = sum(1 for r in results if r.get("status") in {"RESEARCH_PASS", "PASS"})
    fail_count = sum(1 for r in results if r.get("status") in {"RESEARCH_FAIL", "FAIL"})
    summary = {
        "status": "RESEARCH_PASS" if pass_count > 0 else "RESEARCH_BLOCKED",
        "generated_at": datetime.now(KST).isoformat(),
        "total_results": len(results),
        "pass_count": pass_count,
        "fail_count": fail_count,
        "top_20": ranked[:20],
        "metric_scope": {
            "scope": "trainer_validation_proxy",
            "deploy_quality": False,
            "reason": "Global-search trainer metrics are research diagnostics; C12 backtest and service-policy replay are required before paper/default promotion.",
        },
        **_research_safety_fields(research_registry_mutated=True),
    }
    _json_dump(run_dir / "summary.json", summary)
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Research-only bounded LGBM global search")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--stage", choices=["proxy", "full"], default="proxy")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--max-runs", type=int, default=0)
    parser.add_argument("--max-tickers", type=int, default=0)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--deadline-kst", default="")
    parser.add_argument("--skip-trade-classifier", action="store_true")
    parser.add_argument("--with-trade-classifier", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = _load_yaml(Path(args.config).expanduser().resolve())
    defaults = dict(config.get("run_defaults") or {})
    run_id = args.run_id or datetime.now(KST).strftime("GS-%Y%m%d-%H%M%S")
    research_root = _safe_research_root(REPO_ROOT / str(defaults.get("research_root", "artifacts/lgbm_global_search")))
    start_date = args.start_date or str(
        defaults.get("start_80d" if args.stage == "proxy" else "start_full")
    )
    end_date = args.end_date or str(defaults.get("end_date"))
    max_tickers = int(args.max_tickers or defaults.get("max_tickers", 30))
    max_runs = int(args.max_runs or defaults.get("max_runs", 0) or 0)
    tickers = _active_tickers(max_tickers)
    candidates = _build_candidates(
        config=config,
        stage=args.stage,
        start_date=start_date,
        end_date=end_date,
    )
    if max_runs > 0:
        candidates = candidates[:max_runs]
    deadline = _deadline_dt(args.deadline_kst)
    skip_trade_classifier = bool(defaults.get("skip_trade_classifier", True))
    if args.skip_trade_classifier:
        skip_trade_classifier = True
    if args.with_trade_classifier:
        skip_trade_classifier = False

    run_dir = research_root / run_id
    plan = {
        "status": "RESEARCH_PLAN",
        "action": "lgbm_global_search_plan",
        "run_id": run_id,
        "stage": args.stage,
        "start_date": start_date,
        "end_date": end_date,
        "ticker_count": len(tickers),
        "tickers": tickers,
        "candidate_count": len(candidates),
        "deadline_kst": args.deadline_kst or None,
        "research_root": str(research_root),
        "skip_trade_classifier": skip_trade_classifier,
        **_research_safety_fields(research_registry_mutated=False),
        "candidates": [asdict(c) for c in candidates],
    }
    _json_dump(run_dir / "plan.json", plan)
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    results: list[dict[str, Any]] = []
    result_jsonl = run_dir / "results.jsonl"
    for index, candidate in enumerate(candidates, start=1):
        if _deadline_reached(deadline):
            break
        print(
            json.dumps(
                {
                    "event": "candidate_start",
                    "index": index,
                    "total": len(candidates),
                    "candidate_id": candidate.candidate_id,
                    "target_col": candidate.target_col,
                    "feature_policy": candidate.feature_policy_id,
                    "hyperparam": candidate.hyperparam_id,
                    "ts": datetime.now(KST).isoformat(),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        try:
            result = _run_candidate(
                candidate=candidate,
                tickers=tickers,
                research_root=research_root,
                run_id=run_id,
                skip_trade_classifier=skip_trade_classifier,
            )
        except Exception as e:
            registry_dir = research_root / run_id / "registry"
            observed_write = _candidate_registry_write_observed(registry_dir, candidate)
            result = {
                "status": "RESEARCH_FAIL",
                "candidate": asdict(candidate),
                "error": str(e),
                "error_type": type(e).__name__,
                "finished_at": datetime.now(KST).isoformat(),
                "metric_scope": {
                    "scope": "trainer_validation_proxy",
                    "deploy_quality": False,
                    "reason": "Failed research candidate; no deploy-quality inference.",
                },
                **_research_safety_fields(
                    registry_dir=registry_dir,
                    research_registry_mutated=observed_write,
                ),
            }
        results.append(result)
        _jsonl_append(result_jsonl, result)
        _json_dump(run_dir / "latest_result.json", result)
        _write_summary(run_dir, results)
        print(
            json.dumps(
                {
                    "event": "candidate_done",
                    "index": index,
                    "status": result.get("status"),
                    "candidate_id": candidate.candidate_id,
                    "score": result.get("score"),
                    "elapsed_sec": result.get("elapsed_sec"),
                    "ts": datetime.now(KST).isoformat(),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    summary = _write_summary(run_dir, results)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if int(summary.get("pass_count", 0) or 0) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

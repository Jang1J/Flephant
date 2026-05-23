#!/usr/bin/env python
"""Generate full-stage one-candidate configs from a proxy global-search summary."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[3]
DEFAULT_BASE_CONFIG = SCRIPT_PATH.with_name("search_space_weekend.yaml")
DEFAULT_RESEARCH_ROOT = REPO_ROOT / "artifacts" / "lgbm_global_search"


def _json_dump(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return data


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be an object: {path}")
    return data


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def _resolve_summary(run_id: str, summary_path: str) -> Path:
    if summary_path:
        path = Path(summary_path)
        return path if path.is_absolute() else REPO_ROOT / path
    return DEFAULT_RESEARCH_ROOT / run_id / "summary.json"


def _resolve_output_dir(raw: str, run_id: str) -> Path:
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else REPO_ROOT / path
    return DEFAULT_RESEARCH_ROOT / run_id / "full_configs"


def _safe_output_dir(path: Path) -> Path:
    resolved = path.resolve()
    allowed = DEFAULT_RESEARCH_ROOT.resolve()
    if not (resolved == allowed or allowed in resolved.parents):
        raise RuntimeError(f"output-dir must be under {allowed}: {resolved}")
    return resolved


def _candidate_config(base_config: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    candidate = row.get("candidate") or {}
    if not isinstance(candidate, dict):
        raise ValueError("candidate row missing candidate object")
    feature_policy = dict(candidate.get("feature_policy") or {})
    params = dict(candidate.get("params") or {})
    training_control = dict(candidate.get("training_control") or {})
    defaults = dict(base_config.get("run_defaults") or {})
    defaults["skip_trade_classifier"] = False
    return {
        "run_defaults": defaults,
        "targets": [str(candidate["target_col"])],
        "feature_policies": [feature_policy],
        "hyperparams": [
            {
                "id": str(candidate["hyperparam_id"]),
                "params": params,
                "training_control": training_control,
            }
        ],
        "source_proxy_candidate": {
            "candidate_id": candidate.get("candidate_id"),
            "version": row.get("version"),
            "bundle_id": row.get("bundle_id"),
            "score": row.get("score"),
            "metric_scope": row.get("metric_scope"),
        },
    }


def _build_command(
    *,
    python_path: str,
    config_path: Path,
    full_run_id: str,
    with_trade_classifier: bool,
) -> str:
    classifier_flag = "--with-trade-classifier" if with_trade_classifier else "--skip-trade-classifier"
    return (
        f"{python_path} new/experiments/lgbm_global_search/run_global_search.py "
        f"--config {config_path} --stage full --run-id {full_run_id} "
        f"--max-runs 1 {classifier_flag}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare full-stage configs from proxy global-search summary")
    parser.add_argument("--proxy-run-id", required=True)
    parser.add_argument("--summary-path", default="")
    parser.add_argument("--base-config", default=str(DEFAULT_BASE_CONFIG))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--full-run-prefix", default="")
    parser.add_argument("--with-trade-classifier", action="store_true")
    args = parser.parse_args(argv)

    summary_path = _resolve_summary(str(args.proxy_run_id), str(args.summary_path))
    base_path = Path(args.base_config).expanduser().resolve()
    output_dir = _safe_output_dir(_resolve_output_dir(str(args.output_dir), str(args.proxy_run_id)))
    summary = _load_json(summary_path)
    base_config = _load_yaml(base_path)

    if summary.get("deploy_quality") is not False:
        raise RuntimeError("source summary must be marked deploy_quality=false")
    if summary.get("production_registry_mutated") is not False:
        raise RuntimeError("source summary reports production registry mutation")

    rows = list(summary.get("top_20") or [])
    selected = rows[: max(0, int(args.top_n))]
    defaults = dict(base_config.get("run_defaults") or {})
    python_path = str(defaults.get("python") or sys.executable)
    full_run_prefix = str(args.full_run_prefix or f"{args.proxy_run_id}-FULL")

    generated: list[dict[str, Any]] = []
    for idx, row in enumerate(selected, start=1):
        config = _candidate_config(base_config, row)
        candidate = row.get("candidate") or {}
        target = str(candidate.get("target_col", "target")).replace("/", "_")
        feature = str(candidate.get("feature_policy_id", "feature")).replace("/", "_")
        hyper = str(candidate.get("hyperparam_id", "hyperparam")).replace("/", "_")
        full_run_id = f"{full_run_prefix}-R{idx:02d}"
        file_name = f"{idx:02d}_{target}_{feature}_{hyper}.yaml"
        config_path = output_dir / file_name
        _write_yaml(config_path, config)
        generated.append(
            {
                "rank": idx,
                "config_path": str(config_path),
                "full_run_id": full_run_id,
                "candidate": candidate,
                "score": row.get("score"),
                "command": _build_command(
                    python_path=python_path,
                    config_path=config_path,
                    full_run_id=full_run_id,
                    with_trade_classifier=bool(args.with_trade_classifier),
                ),
            }
        )

    report = {
        "status": "PASS",
        "action": "prepare_lgbm_global_search_full_configs",
        "proxy_run_id": str(args.proxy_run_id),
        "summary_path": str(summary_path),
        "output_dir": str(output_dir),
        "top_n": len(generated),
        "with_trade_classifier": bool(args.with_trade_classifier),
        "generated": generated,
        "research_only": True,
        "production_registry_mutated": False,
        "paper_registry_mutated": False,
        "live_trading_allowed": False,
        "deploy_quality": False,
        "requires_c12": True,
    }
    _json_dump(output_dir / "full_config_plan.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

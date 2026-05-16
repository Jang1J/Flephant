#!/usr/bin/env python
"""Build a read-only cost-aware retraining preparation plan.

This script does not train a model and does not mutate registry artifacts. It
collects current blocker evidence and writes a plan for the next Mode B
cost-aware retraining experiment.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.safe_cast import safe_bool, safe_int  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_REPORT_DIR = ROOT / "artifacts" / "reports" / "cost_aware_retraining"


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _latest(pattern: str) -> tuple[Path | None, dict[str, Any] | None]:
    paths = [path for path in ROOT.glob(pattern) if path.is_file()]
    if not paths:
        return None, None
    paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for path in paths:
        try:
            return path, _load_json(path)
        except Exception as e:
            _ = e
            continue
    return None, None


def _repo_relative(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _is_newer(candidate: Path | None, reference: Path | None) -> bool:
    if candidate is None or reference is None:
        return False
    try:
        return candidate.stat().st_mtime >= reference.stat().st_mtime
    except OSError:
        return False


def _dataset_date_arg(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        raw = raw[:10]
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        return datetime.strptime(digits[:8], "%Y%m%d").strftime("%Y%m%d")
    except ValueError:
        return None


def _final_dataset_gate_cfg() -> dict[str, Any]:
    gate_cfg = (
        (config_load("risk_config.yaml", "backtest_agent") or {})
        .get("deploy_decision_gate", {})
        .get("final_dataset_gate", {})
    )
    return gate_cfg if isinstance(gate_cfg, dict) else {}


def _final_dataset_window() -> dict[str, str | None]:
    gate_cfg = _final_dataset_gate_cfg()
    return {
        "start_date": _dataset_date_arg(gate_cfg.get("expected_start_date")),
        "end_date": _dataset_date_arg(gate_cfg.get("expected_end_date")),
    }


def _final_dataset_business_days() -> int:
    gate_cfg = _final_dataset_gate_cfg()
    return safe_int(gate_cfg.get("min_business_days"), default=0, min_value=0)


def _final_dataset_min_tickers() -> int:
    gate_cfg = _final_dataset_gate_cfg()
    return safe_int(gate_cfg.get("min_tickers"), default=0, min_value=0)


def _final_training_tickers() -> list[str]:
    cfg = config_load("universe_config.yaml") or {}
    gate_cfg = _final_dataset_gate_cfg()
    include_pending = safe_bool(
        gate_cfg.get("include_pending_data_tickers"),
        default=False,
    )
    stock_statuses = {"active"}
    sector_statuses = {"confirmed"}
    if include_pending:
        stock_statuses = {
            str(status)
            for status in gate_cfg.get("allowed_stock_statuses", ["active", "pending_data"])
        }
        sector_statuses = {
            str(status)
            for status in gate_cfg.get(
                "allowed_sector_statuses",
                ["confirmed", "confirmed_pending_data"],
            )
        }
    tickers: list[str] = []
    for sector in (cfg.get("sectors") or {}).values():
        if str(sector.get("status")) not in sector_statuses:
            continue
        for row in sector.get("stocks", []) or []:
            if str(row.get("status")) in stock_statuses:
                tickers.append(pad_ticker(str(row.get("ticker", ""))))
    if not tickers:
        fallback = (cfg.get("backtest_universe_mode") or {}).get("fallback_tickers", [])
        tickers.extend(pad_ticker(str(ticker)) for ticker in fallback)
    max_tickers = _final_dataset_min_tickers()
    deduped = sorted({ticker for ticker in tickers if ticker != "000000"})
    return deduped[:max_tickers] if max_tickers else deduped


def _label_scan_command(
    *,
    start_date: str | None,
    end_date: str | None,
    tickers: list[str],
) -> str:
    parts = [
        "PYTHONPATH=new python new/scripts/cost_aware_label_horizon_scan.py",
        "--artifacts-dir artifacts/data",
    ]
    if start_date:
        parts.append(f"--start-date {start_date}")
    if end_date:
        parts.append(f"--end-date {end_date}")
    if tickers:
        parts.append(f"--tickers {','.join(tickers)}")
    return " ".join(parts)


def _research_registry_dir(bundle_id: str) -> str:
    safe_bundle = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in bundle_id)
    safe_bundle = safe_bundle or "cost_aware"
    return f"artifacts/lgbm_research/{safe_bundle}"


def _staged_retrain_gate_command(
    *,
    bundle_id: str,
    end_date: str | None,
    tickers: list[str],
    target_col_override: str | None,
) -> str:
    registry_dir = _research_registry_dir(bundle_id)
    business_days = _final_dataset_business_days()
    max_tickers = _final_dataset_min_tickers() or len(tickers)
    parts = [
        "ELEPHANT_MODE=mode_b PYTHONPATH=new python new/scripts/post_backfill_prelive.py",
        f"--bundle-id {bundle_id}",
        f"--registry-dir {registry_dir}",
        "--run-paper-balance",
    ]
    if end_date:
        parts.append(f"--end-date {end_date}")
    if business_days:
        parts.append(f"--business-days {business_days}")
    if max_tickers:
        parts.append(f"--max-tickers {max_tickers}")
    if target_col_override:
        parts.append(f"--target-col-override {target_col_override}")
    return " ".join(parts)


def _horizon_report(label_scan: dict[str, Any] | None, horizon: object) -> dict[str, Any]:
    if not isinstance(label_scan, dict):
        return {}
    target = str(horizon)
    for row in label_scan.get("horizons", []) or []:
        if isinstance(row, dict) and str(row.get("horizon")) == target:
            return row
    return {}


def build_retraining_plan(
    *,
    bundle_id: str,
    write_report: bool = True,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    cfg = config_load("risk_config.yaml", "cost_aware_retraining") or {}
    service_policy_cfg = config_load("risk_config.yaml", "service_policy_replay") or {}
    ppo_cfg = config_load("risk_config.yaml", "nightly_ppo_retrainer") or {}
    label_cfg = config_load("risk_config.yaml", "label") or {}
    phase2_path, phase2 = _latest("artifacts/reports/phase2_feature_backfill/*.json")
    input_path, phase2_input = _latest("artifacts/reports/phase2_input_readiness/*.json")
    service_path, service = _latest(
        f"artifacts/reports/service_policy_replay/service_policy_replay_{bundle_id}_*.json"
    )
    label_scan_path, label_scan = _latest("artifacts/reports/label_horizon_scan/*.json")
    final_window = _final_dataset_window()
    final_tickers = _final_training_tickers()

    blockers: list[str] = []
    phase2_pass = (phase2 or {}).get("status") == "PASS"
    if not phase2_pass:
        blockers.append("phase2_feature_backfill_not_pass")

    phase2_input_blocking = bool(phase2_input and phase2_input.get("status") != "PASS")
    phase2_input_superseded = bool(
        phase2_input_blocking
        and phase2_pass
        and _is_newer(phase2_path, input_path)
    )
    if phase2_input_blocking and not phase2_input_superseded:
        blockers.append("phase2_input_readiness_not_pass")
    if (service or {}).get("status") != "PASS":
        blockers.append("service_policy_replay_not_pass")
    if not label_scan:
        blockers.append("label_horizon_scan_missing")

    objective_cfg = cfg.get("objective", {}) or {}
    active_horizon = label_cfg.get("horizon_bars")
    active_horizon_report = _horizon_report(label_scan, active_horizon)
    best_horizon = (label_scan or {}).get("best_horizon")
    best_horizon_report = _horizon_report(label_scan, best_horizon)
    if best_horizon is None:
        target_col_override = None
    elif str(best_horizon) == "session_close":
        target_col_override = "label_session_close_net_ret"
    else:
        target_col_override = f"label_{best_horizon}m_net_ret"
    plan = {
        "status": "READY" if not blockers else "BLOCKED",
        "generated_at": datetime.now(_KST).isoformat(),
        "bundle_id": bundle_id,
        "read_only": True,
        "registry_mutated": False,
        "training_window": {
            "source": "final_dataset_gate",
            **final_window,
        },
        "training_universe": {
            "source": "final_dataset_gate",
            "ticker_count": len(final_tickers),
            "tickers": final_tickers,
        },
        "research_registry": {
            "registry_dir": _research_registry_dir(bundle_id),
            "production_registry_mutated": False,
            "staging_script": "new/scripts/post_backfill_prelive.py",
            "allow_production_candidate_write": False,
        },
        "active_label": {
            "horizon_bars": active_horizon,
            "target_col": label_cfg.get("target_col"),
            "generation_version": label_cfg.get("generation_version"),
        },
        "candidate_horizons": cfg.get("horizon_candidates", []),
        "objective": {
            "net_of_cost_target": safe_bool(
                objective_cfg.get("net_of_cost_target", True),
                default=True,
            ),
            "trade_no_trade_classifier": safe_bool(
                objective_cfg.get("trade_no_trade_classifier", True),
                default=True,
            ),
            "turnover_penalty": float(ppo_cfg.get("turnover_penalty", 0.0)),
            "min_expected_net_alpha_bps": float(
                service_policy_cfg.get("min_expected_net_alpha_bps", 0.0)
            ),
            "expected_net_alpha_source": str(
                service_policy_cfg.get("expected_net_alpha_source", "rank_score")
            ),
            "min_holding_bars": int(service_policy_cfg.get("min_holding_bars", 0)),
            "rebalance_cooldown_bars": int(service_policy_cfg.get("rebalance_cooldown_bars", 0)),
        },
        "recommended_experiment": {
            "target_horizon": best_horizon,
            "target_col_override": target_col_override,
            "active_horizon_mean_net_bps": active_horizon_report.get("mean_net_bps"),
            "active_horizon_positive_net_rate": active_horizon_report.get("positive_net_rate"),
            "best_horizon_mean_net_bps": best_horizon_report.get("mean_net_bps"),
            "best_horizon_positive_net_rate": best_horizon_report.get("positive_net_rate"),
            "train_target": "net_of_cost_return",
            "selection_gate": "trade_no_trade_or_expected_net_bps",
            "deploy_policy": "manual_review_then_c12_service_policy_replay",
            "do_not_auto_deploy": True,
        },
        "evidence": {
            "phase2_feature_backfill": {
                "status": (phase2 or {}).get("status"),
                "report_path": _repo_relative(phase2_path),
                "coverage": (phase2 or {}).get("coverage", {}),
                "blockers": (phase2 or {}).get("blockers", []),
            },
            "phase2_input_readiness": {
                "status": (phase2_input or {}).get("status"),
                "report_path": _repo_relative(input_path),
                "blockers": (phase2_input or {}).get("blockers", []),
                "blocking": phase2_input_blocking and not phase2_input_superseded,
                "superseded_by_phase2_feature_backfill": phase2_input_superseded,
            },
            "service_policy_replay": {
                "status": (service or {}).get("status"),
                "report_path": _repo_relative(service_path),
                "metrics": (service or {}).get("metrics", {}),
                "blockers": (service or {}).get("gate", {}).get("blockers", []),
            },
            "label_horizon_scan": {
                "status": (label_scan or {}).get("status"),
                "report_path": _repo_relative(label_scan_path),
                "best_horizon": best_horizon,
                "deployable_label_recommendation": (
                    (label_scan or {}).get("deployable_label_recommendation")
                ),
                "active_horizon": active_horizon_report,
                "best_horizon_report": best_horizon_report,
            },
        },
        "blockers": sorted(set(blockers)),
        "next_commands": [
            _label_scan_command(
                start_date=final_window["start_date"],
                end_date=final_window["end_date"],
                tickers=final_tickers,
            ),
            _staged_retrain_gate_command(
                bundle_id=bundle_id,
                end_date=final_window["end_date"],
                tickers=final_tickers,
                target_col_override=target_col_override,
            ),
        ],
    }
    if write_report:
        out_dir = output_dir or _REPORT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"cost_aware_retraining_plan_{bundle_id}_{datetime.now(_KST).strftime('%Y%m%d_%H%M%S')}.json"
        plan["report_path"] = str(path)
        plan["report_path_relative"] = _repo_relative(path)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(plan, fh, ensure_ascii=False, indent=2)
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--output-dir", default=str(_REPORT_DIR))
    parser.add_argument("--no-write-report", action="store_true")
    args = parser.parse_args(argv)
    plan = build_retraining_plan(
        bundle_id=str(args.bundle_id),
        output_dir=Path(str(args.output_dir)),
        write_report=not bool(args.no_write_report),
    )
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0 if plan["status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

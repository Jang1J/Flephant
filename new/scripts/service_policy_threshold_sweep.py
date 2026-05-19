#!/usr/bin/env python
"""Read-only research sweep for service-policy replay knobs.

This script does not change risk_config.yaml, does not call KIS, and does not
mutate any registry. It runs a bounded grid of research-only replay overrides
and writes compact metrics so Mode B can start from a smaller hypothesis set.
"""
from __future__ import annotations

import argparse
import itertools
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

from scripts.service_policy_replay import run_service_policy_replay  # noqa: E402
from src.mode_b.service_policy_replay import ServicePolicyReplayEngine  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_REPORT_DIR = ROOT / "artifacts" / "reports" / "research_threshold_sweep"


def _parse_float_grid(raw: str) -> list[float]:
    values = [float(part.strip()) for part in str(raw).split(",") if part.strip()]
    if not values:
        raise ValueError("float grid must not be empty")
    return values


def _parse_int_grid(raw: str) -> list[int]:
    values = [int(part.strip()) for part in str(raw).split(",") if part.strip()]
    if not values:
        raise ValueError("int grid must not be empty")
    return values


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _result_score(result: dict[str, Any]) -> tuple[float, float, float]:
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    return (
        1.0 if result.get("status") == "PASS" else 0.0,
        float(metrics.get("total_return_bps", -1e18) or -1e18),
        float(metrics.get("sr", -1e18) or -1e18),
    )


def _compact_result(
    *,
    params: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    gate = report.get("gate") if isinstance(report.get("gate"), dict) else {}
    order_stats = (
        report.get("order_stats")
        if isinstance(report.get("order_stats"), dict)
        else {}
    )
    trade_gate = (
        report.get("trade_probability_gate")
        if isinstance(report.get("trade_probability_gate"), dict)
        else {}
    )
    return {
        "status": report.get("status"),
        "params": params,
        "blockers": gate.get("blockers", []),
        "metrics": {
            "total_return_bps": metrics.get("total_return_bps"),
            "sr": metrics.get("sr"),
            "mdd": metrics.get("mdd"),
            "days": metrics.get("days"),
        },
        "order_stats": {
            "total_orders": order_stats.get("total_orders"),
            "buy_orders": order_stats.get("buy_orders"),
            "sell_orders": order_stats.get("sell_orders"),
            "cooldown_skipped_orders": order_stats.get("cooldown_skipped_orders"),
            "max_daily_turnover": order_stats.get("max_daily_turnover"),
        },
        "trade_probability_gate": {
            "enabled": trade_gate.get("enabled"),
            "applied": trade_gate.get("applied"),
            "reason": trade_gate.get("reason"),
            "candidates_rejected": trade_gate.get("candidates_rejected"),
        },
        "valid_rows": report.get("valid_rows"),
    }


def build_sweep(args: argparse.Namespace) -> dict[str, Any]:
    spreads = _parse_float_grid(args.no_trade_score_spreads)
    topk = _parse_float_grid(args.top_k_fractions)
    max_orders = _parse_int_grid(args.max_orders_per_cycle)
    max_qty = _parse_int_grid(getattr(args, "max_order_qty_per_order", "1"))
    strides = _parse_int_grid(args.decision_stride_bars)
    min_holds = _parse_int_grid(args.min_holding_bars)
    cooldowns = _parse_int_grid(args.rebalance_cooldown_bars)

    grid = list(itertools.product(spreads, topk, max_orders, max_qty, strides, min_holds, cooldowns))
    max_runs = int(args.max_runs)
    if max_runs <= 0:
        raise ValueError("max_runs must be positive")
    truncated = len(grid) > max_runs
    grid = grid[:max_runs]

    results: list[dict[str, Any]] = []
    shared_engine = ServicePolicyReplayEngine()
    for spread, top_k_fraction, max_order, order_qty, stride, min_hold, cooldown in grid:
        params = {
            "no_trade_score_spread": spread,
            "top_k_fraction": top_k_fraction,
            "max_orders_per_cycle": max_order,
            "max_order_qty_per_order": order_qty,
            "decision_stride_bars": stride,
            "min_holding_bars": min_hold,
            "rebalance_cooldown_bars": cooldown,
        }
        try:
            replay = run_service_policy_replay(
                str(args.bundle_id),
                start_date=str(args.start_date) if args.start_date else None,
                end_date=str(args.end_date) if args.end_date else None,
                write_report=False,
                engine=shared_engine,
                no_trade_score_spread=spread,
                top_k_fraction=top_k_fraction,
                max_orders_per_cycle=max_order,
                max_order_qty_per_order=order_qty,
                decision_stride_bars=stride,
                min_holding_bars=min_hold,
                rebalance_cooldown_bars=cooldown,
            )
            results.append(_compact_result(params=params, report=replay))
        except Exception as e:
            results.append({
                "status": "ERROR",
                "params": params,
                "error": f"{type(e).__name__}: {e}",
            })

    ranked = sorted(results, key=_result_score, reverse=True)
    best = ranked[0] if ranked else None
    pass_count = sum(1 for row in results if row.get("status") == "PASS")
    return {
        "status": "PASS" if pass_count else "WARN",
        "action": "service_policy_threshold_sweep",
        "generated_at": datetime.now(_KST).isoformat(),
        "read_only": True,
        "external_kis_api": False,
        "registry_mutated": False,
        "bundle_id": str(args.bundle_id),
        "date_range": {
            "start": str(args.start_date) if args.start_date else None,
            "end": str(args.end_date) if args.end_date else None,
        },
        "grid_size": len(results),
        "grid_truncated": truncated,
        "optimization": {
            "shared_replay_engine": True,
            "policy_independent_panel_cache": True,
            "deterministic_tie_break": "ticker_ascending",
        },
        "pass_count": pass_count,
        "best": best,
        "ranked_results": ranked,
    }


def _write_report(report: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle_id = str(report.get("bundle_id", "BUNDLE-UNKNOWN"))
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"{bundle_id}_service_policy_threshold_sweep_{ts}.json"
    report["report_path"] = str(path)
    report["report_path_relative"] = _repo_relative(path)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--no-trade-score-spreads", default="0,0.01,0.03")
    parser.add_argument("--top-k-fractions", default="0.10,0.25")
    parser.add_argument("--max-orders-per-cycle", default="1,3")
    parser.add_argument("--max-order-qty-per-order", default="1")
    parser.add_argument("--decision-stride-bars", default="30")
    parser.add_argument("--min-holding-bars", default="195")
    parser.add_argument("--rebalance-cooldown-bars", default="195")
    parser.add_argument("--max-runs", type=int, default=12)
    parser.add_argument("--output-dir", default=str(_REPORT_DIR))
    parser.add_argument("--no-write-report", action="store_true")
    args = parser.parse_args(argv)

    report = build_sweep(args)
    if not bool(args.no_write_report):
        _write_report(report, Path(str(args.output_dir)))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

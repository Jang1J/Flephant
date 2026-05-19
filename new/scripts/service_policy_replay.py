"""CLI for cost-aware service-policy replay.

Read-only by design:
- does not read .env
- does not call KIS
- does not mutate registry artifacts
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.mode_b.service_policy_replay import ServicePolicyConfig, ServicePolicyReplayEngine

_KST = ZoneInfo("Asia/Seoul")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_REPORT_DIR = _REPO_ROOT / "artifacts" / "reports" / "service_policy_replay"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run KIS paper cash-account service-policy replay for a bundle",
    )
    parser.add_argument("--bundle-id", required=True, help="Candidate bundle id")
    parser.add_argument(
        "--start-date",
        default=None,
        help="Inclusive replay start date in YYYYMMDD. Omit with --end-date for C12 first fold.",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help="Inclusive replay end date in YYYYMMDD. Omit with --start-date for C12 first fold.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(_DEFAULT_REPORT_DIR),
        help="Directory to write the replay JSON report",
    )
    parser.add_argument(
        "--tickers",
        default=None,
        help=(
            "Optional comma-separated universe override. "
            "Omit to use the final deploy universe from SSOT config."
        ),
    )
    parser.add_argument(
        "--no-write-report",
        action="store_true",
        help="Print only; do not persist a report file",
    )
    parser.add_argument(
        "--trade-probability-gate",
        choices=["config", "enable", "disable"],
        default="config",
        help=(
            "Research-only override for cost_aware_retraining.trade_probability_gate.enabled. "
            "Default keeps risk_config.yaml unchanged."
        ),
    )
    parser.add_argument(
        "--min-trade-probability",
        type=float,
        default=None,
        help=(
            "Research-only override for min trade probability. "
            "Requires a value in [0, 1]; does not modify risk_config.yaml."
        ),
    )
    parser.add_argument(
        "--no-trade-score-spread",
        type=float,
        default=None,
        help=(
            "Research-only override for service_policy_replay.no_trade_score_spread. "
            "Requires a non-negative value and does not modify risk_config.yaml."
        ),
    )
    parser.add_argument(
        "--top-k-fraction",
        type=float,
        default=None,
        help=(
            "Research-only override for evaluation.top_k_fraction inside this replay. "
            "Requires a value in (0, 1] and does not modify risk_config.yaml."
        ),
    )
    parser.add_argument(
        "--max-orders-per-cycle",
        type=int,
        default=None,
        help=(
            "Research-only override for paper_auto_trading.max_orders_per_cycle. "
            "Requires a positive integer and does not modify risk_config.yaml."
        ),
    )
    parser.add_argument(
        "--max-order-qty-per-order",
        type=int,
        default=None,
        help=(
            "Research-only override for paper_auto_trading.max_order_qty_per_order. "
            "Requires a positive integer and does not modify risk_config.yaml."
        ),
    )
    parser.add_argument(
        "--decision-stride-bars",
        type=int,
        default=None,
        help=(
            "Research-only override for service_policy_replay.decision_stride_bars. "
            "Requires a positive integer and does not modify risk_config.yaml."
        ),
    )
    parser.add_argument(
        "--min-holding-bars",
        type=int,
        default=None,
        help=(
            "Research-only override for service_policy_replay.min_holding_bars. "
            "Requires a non-negative integer and does not modify risk_config.yaml."
        ),
    )
    parser.add_argument(
        "--rebalance-cooldown-bars",
        type=int,
        default=None,
        help=(
            "Research-only override for service_policy_replay.rebalance_cooldown_bars. "
            "Requires a non-negative integer and does not modify risk_config.yaml."
        ),
    )
    return parser.parse_args(argv)


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)


def _parse_tickers(raw: str | None) -> list[str] | None:
    if raw is None or not str(raw).strip():
        return None
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _policy_with_research_overrides(
    policy: ServicePolicyConfig,
    *,
    trade_probability_gate: str = "config",
    min_trade_probability: float | None = None,
    no_trade_score_spread: float | None = None,
    top_k_fraction: float | None = None,
    max_orders_per_cycle: int | None = None,
    max_order_qty_per_order: int | None = None,
    decision_stride_bars: int | None = None,
    min_holding_bars: int | None = None,
    rebalance_cooldown_bars: int | None = None,
) -> ServicePolicyConfig:
    updates: dict[str, Any] = {}
    if trade_probability_gate == "enable":
        updates["trade_probability_gate_enabled"] = True
    elif trade_probability_gate == "disable":
        updates["trade_probability_gate_enabled"] = False
    elif trade_probability_gate != "config":
        raise ValueError(
            "trade_probability_gate must be one of: config, enable, disable"
        )

    if min_trade_probability is not None:
        prob = float(min_trade_probability)
        if prob < 0.0 or prob > 1.0:
            raise ValueError("min_trade_probability must be in [0, 1]")
        updates["min_trade_probability"] = prob
    if no_trade_score_spread is not None:
        spread = float(no_trade_score_spread)
        if spread < 0.0:
            raise ValueError("no_trade_score_spread must be non-negative")
        updates["no_trade_score_spread"] = spread
    if top_k_fraction is not None:
        fraction = float(top_k_fraction)
        if fraction <= 0.0 or fraction > 1.0:
            raise ValueError("top_k_fraction must be in (0, 1]")
        updates["top_k_fraction"] = fraction
    if max_orders_per_cycle is not None:
        value = int(max_orders_per_cycle)
        if value <= 0:
            raise ValueError("max_orders_per_cycle must be positive")
        updates["max_orders_per_cycle"] = value
    if max_order_qty_per_order is not None:
        value = int(max_order_qty_per_order)
        if value <= 0:
            raise ValueError("max_order_qty_per_order must be positive")
        updates["max_order_qty_per_order"] = value
    if decision_stride_bars is not None:
        value = int(decision_stride_bars)
        if value <= 0:
            raise ValueError("decision_stride_bars must be positive")
        updates["decision_stride_bars"] = value
    if min_holding_bars is not None:
        value = int(min_holding_bars)
        if value < 0:
            raise ValueError("min_holding_bars must be non-negative")
        updates["min_holding_bars"] = value
    if rebalance_cooldown_bars is not None:
        value = int(rebalance_cooldown_bars)
        if value < 0:
            raise ValueError("rebalance_cooldown_bars must be non-negative")
        updates["rebalance_cooldown_bars"] = value

    return replace(policy, **updates) if updates else policy


def _write_report(report: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    bundle_id = str(report.get("bundle_id", "BUNDLE-UNKNOWN"))
    path = output_dir / f"service_policy_replay_{bundle_id}_{ts}.json"
    report["report_path"] = str(path)
    report["report_path_relative"] = _repo_relative(path)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path


def run_service_policy_replay(
    bundle_id: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    output_dir: Path | None = None,
    write_report: bool = True,
    engine: ServicePolicyReplayEngine | None = None,
    tickers: list[str] | None = None,
    trade_probability_gate: str = "config",
    min_trade_probability: float | None = None,
    no_trade_score_spread: float | None = None,
    top_k_fraction: float | None = None,
    max_orders_per_cycle: int | None = None,
    max_order_qty_per_order: int | None = None,
    decision_stride_bars: int | None = None,
    min_holding_bars: int | None = None,
    rebalance_cooldown_bars: int | None = None,
) -> dict[str, Any]:
    policy = _policy_with_research_overrides(
        ServicePolicyConfig.from_config(),
        trade_probability_gate=trade_probability_gate,
        min_trade_probability=min_trade_probability,
        no_trade_score_spread=no_trade_score_spread,
        top_k_fraction=top_k_fraction,
        max_orders_per_cycle=max_orders_per_cycle,
        max_order_qty_per_order=max_order_qty_per_order,
        decision_stride_bars=decision_stride_bars,
        min_holding_bars=min_holding_bars,
        rebalance_cooldown_bars=rebalance_cooldown_bars,
    )
    replay_engine = engine.with_policy(policy) if engine is not None else ServicePolicyReplayEngine(policy=policy)
    report = replay_engine.run(
        bundle_id,
        start_date=start_date,
        end_date=end_date,
        universe=tickers,
    )
    if write_report:
        _write_report(report, output_dir or _DEFAULT_REPORT_DIR)
    return report


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = run_service_policy_replay(
        str(args.bundle_id),
        start_date=args.start_date,
        end_date=args.end_date,
        output_dir=Path(str(args.output_dir)),
        write_report=not bool(args.no_write_report),
        tickers=_parse_tickers(args.tickers),
        trade_probability_gate=str(args.trade_probability_gate),
        min_trade_probability=args.min_trade_probability,
        no_trade_score_spread=args.no_trade_score_spread,
        top_k_fraction=args.top_k_fraction,
        max_orders_per_cycle=args.max_orders_per_cycle,
        max_order_qty_per_order=args.max_order_qty_per_order,
        decision_stride_bars=args.decision_stride_bars,
        min_holding_bars=args.min_holding_bars,
        rebalance_cooldown_bars=args.rebalance_cooldown_bars,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

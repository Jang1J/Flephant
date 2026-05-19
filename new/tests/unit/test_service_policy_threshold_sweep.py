from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path


def _load_script():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "service_policy_threshold_sweep.py"
    )
    spec = importlib.util.spec_from_file_location("service_policy_threshold_sweep", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_threshold_sweep_ranks_pass_without_mutating_external_state(monkeypatch):
    mod = _load_script()
    calls = []

    def fake_replay(bundle_id, **kwargs):
        calls.append((bundle_id, kwargs))
        is_pass = kwargs["top_k_fraction"] == 0.25
        return {
            "status": "PASS" if is_pass else "BLOCKED",
            "metrics": {
                "total_return_bps": 25.0 if is_pass else -5.0,
                "sr": 1.2 if is_pass else -0.4,
                "mdd": -0.01,
                "days": 20,
            },
            "gate": {"blockers": [] if is_pass else ["service_policy_sharpe_below_threshold"]},
            "order_stats": {
                "total_orders": 3 if is_pass else 1,
                "buy_orders": 2 if is_pass else 1,
                "sell_orders": 1 if is_pass else 0,
            },
            "trade_probability_gate": {
                "enabled": False,
                "applied": False,
                "reason": "disabled",
                "candidates_rejected": 0,
            },
            "valid_rows": 100,
        }

    monkeypatch.setattr(mod, "run_service_policy_replay", fake_replay)

    report = mod.build_sweep(
        argparse.Namespace(
            bundle_id="BUNDLE-TEST",
            start_date="20250916",
            end_date="20251020",
            no_trade_score_spreads="0",
            top_k_fractions="0.10,0.25",
            max_orders_per_cycle="1",
            decision_stride_bars="30",
            min_holding_bars="195",
            rebalance_cooldown_bars="195",
            max_runs=2,
        )
    )

    assert report["status"] == "PASS"
    assert report["read_only"] is True
    assert report["external_kis_api"] is False
    assert report["registry_mutated"] is False
    assert report["optimization"]["shared_replay_engine"] is True
    assert report["optimization"]["policy_independent_panel_cache"] is True
    assert report["pass_count"] == 1
    assert report["best"]["params"]["top_k_fraction"] == 0.25
    assert calls[0][1]["write_report"] is False
    assert calls[0][1]["decision_stride_bars"] == 30
    assert calls[0][1]["engine"] is calls[1][1]["engine"]

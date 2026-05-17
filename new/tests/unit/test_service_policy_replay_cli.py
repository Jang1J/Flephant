from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.mode_b.service_policy_replay import ServicePolicyConfig


def _load_cli_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "service_policy_replay.py"
    spec = importlib.util.spec_from_file_location("service_policy_replay_cli", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _policy(**overrides) -> ServicePolicyConfig:
    base = {
        "initial_capital": 1_000_000.0,
        "top_k_fraction": 0.5,
        "max_orders_per_cycle": 1,
        "max_order_qty_per_order": 1,
        "max_names": 10,
        "max_single_name": 1.0,
        "min_cash": 0.0,
        "daily_turnover_cap": 10.0,
        "commission_bps": 5.0,
        "slippage_bps": 10.0,
        "annualization_factor": 252,
        "min_daily_return_std": 1e-8,
        "decision_stride_bars": 1,
        "min_holding_bars": 0,
        "rebalance_cooldown_bars": 0,
        "no_trade_score_spread": 0.0,
        "allow_position_pyramiding": False,
        "turnover_budget_hard_stop": True,
        "min_expected_net_alpha_bps": 15.0,
        "expected_net_alpha_source": "rank_score",
        "min_service_policy_sharpe": 0.0,
        "trade_probability_gate_enabled": False,
        "min_trade_probability": 0.5,
    }
    base.update(overrides)
    return ServicePolicyConfig(**base)


def test_policy_with_research_overrides_enables_trade_probability_gate() -> None:
    mod = _load_cli_module()

    policy = mod._policy_with_research_overrides(
        _policy(),
        trade_probability_gate="enable",
        min_trade_probability=0.35,
    )

    assert policy.trade_probability_gate_enabled is True
    assert policy.min_trade_probability == pytest.approx(0.35)


def test_policy_with_research_overrides_rejects_invalid_probability() -> None:
    mod = _load_cli_module()

    with pytest.raises(ValueError, match="min_trade_probability"):
        mod._policy_with_research_overrides(
            _policy(),
            min_trade_probability=1.1,
        )


def test_run_service_policy_replay_passes_cli_policy_override(monkeypatch) -> None:
    mod = _load_cli_module()
    captured: dict[str, ServicePolicyConfig] = {}

    class _FakeEngine:
        def __init__(self, *, policy: ServicePolicyConfig) -> None:
            captured["policy"] = policy

        def run(self, bundle_id, *, start_date=None, end_date=None, universe=None):
            return {
                "status": "PASS",
                "bundle_id": bundle_id,
                "date_range": {"start": start_date, "end": end_date},
                "universe": universe,
                "policy": {
                    "trade_probability_gate_enabled": (
                        captured["policy"].trade_probability_gate_enabled
                    ),
                    "min_trade_probability": captured["policy"].min_trade_probability,
                },
            }

    monkeypatch.setattr(mod.ServicePolicyConfig, "from_config", lambda: _policy())
    monkeypatch.setattr(mod, "ServicePolicyReplayEngine", _FakeEngine)

    report = mod.run_service_policy_replay(
        "BUNDLE-TEST",
        start_date="20260501",
        end_date="20260502",
        write_report=False,
        tickers=["005930"],
        trade_probability_gate="enable",
        min_trade_probability=0.35,
    )

    assert report["status"] == "PASS"
    assert report["policy"]["trade_probability_gate_enabled"] is True
    assert report["policy"]["min_trade_probability"] == pytest.approx(0.35)

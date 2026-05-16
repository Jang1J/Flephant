from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from src.mode_b.backtest_diagnostics import (
    attach_service_policy_evidence,
    load_service_policy_evidence,
)
from src.mode_b.service_policy_verifier import verify_service_policy_evidence
from src.mode_b.service_policy_replay import (
    ServicePolicyConfig,
    ServicePolicyReplayEngine,
)


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
    }
    base.update(overrides)
    return ServicePolicyConfig(**base)


def _panel(rows: list[tuple[str, str, float, float, float]]) -> pd.DataFrame:
    index = pd.MultiIndex.from_tuples(
        [
            (ticker, pd.Timestamp(ts, tz="Asia/Seoul"))
            for ticker, ts, _, _, _ in rows
        ],
        names=["ticker", "ts_close"],
    )
    return pd.DataFrame(
        {
            "feature_score": [score for _, _, score, _, _ in rows],
            "label_5m_ret": [actual for _, _, _, actual, _ in rows],
            "close": [price for _, _, _, _, price in rows],
        },
        index=index,
    )


def _model(features: list[float]) -> float:
    return float(features[0])


def test_replay_prevents_naked_short_exposure() -> None:
    engine = ServicePolicyReplayEngine(policy=_policy())
    panel = _panel([
        ("005930", "2026-05-01 09:00:00", 2.0, 0.001, 100.0),
        ("000660", "2026-05-01 09:00:00", 1.0, 0.001, 100.0),
        ("005930", "2026-05-01 09:01:00", 1.0, 0.001, 101.0),
        ("000660", "2026-05-01 09:01:00", 3.0, 0.001, 100.0),
    ])

    result = engine._simulate_panel(
        panel=panel,
        model_callable=_model,
        feature_cols=["feature_score"],
        target_col="label_5m_ret",
        policy=_policy(),
    )

    assert result["policy_checks"]["no_naked_short_exposure"] is True
    assert result["order_stats"]["naked_short_attempts"] == 0
    assert all(order["holding_before"] > 0 for order in result["orders"] if order["side"] == "sell")


def test_sell_reduces_existing_holding_only() -> None:
    engine = ServicePolicyReplayEngine(policy=_policy(max_orders_per_cycle=2))
    panel = _panel([
        ("005930", "2026-05-01 09:00:00", 1.0, 0.001, 100.0),
        ("000660", "2026-05-01 09:00:00", 3.0, 0.001, 100.0),
    ])

    result = engine._simulate_panel(
        panel=panel,
        model_callable=_model,
        feature_cols=["feature_score"],
        target_col="label_5m_ret",
        policy=_policy(max_orders_per_cycle=2),
        initial_holdings={"005930": 2},
    )

    sell_orders = [order for order in result["orders"] if order["side"] == "sell"]
    assert sell_orders
    assert sell_orders[0]["ticker"] == "005930"
    assert sell_orders[0]["qty"] == 1
    assert sell_orders[0]["holding_after"] == 1


def test_policy_config_maps_risk_config_values() -> None:
    policy = ServicePolicyConfig.from_config()
    assert policy.max_orders_per_cycle == 3
    assert policy.max_order_qty_per_order == 1
    assert policy.total_cost_bps == 15.0
    assert policy.daily_turnover_cap == 0.30
    assert policy.decision_stride_bars == 30
    assert policy.min_holding_bars == 195
    assert policy.rebalance_cooldown_bars == 195
    assert policy.allow_position_pyramiding is False
    assert policy.turnover_budget_hard_stop is True
    assert policy.min_service_policy_sharpe == 0.0
    assert policy.expected_net_alpha_source == "rank_score"


def test_policy_config_treats_string_false_flags_as_false(monkeypatch) -> None:
    """service policy 운영 플래그가 문자열 false여도 Python truthy로 과장되지 않는다."""
    config_by_section = {
        "backtest": {"initial_capital": 1_000_000.0},
        "evaluation": {
            "top_k_fraction": 0.5,
            "annualization_factor": 252,
            "min_daily_pnl_std": 1e-8,
        },
        "paper_auto_trading": {
            "max_orders_per_cycle": 1,
            "max_order_qty_per_order": 1,
        },
        "position_limits": {
            "max_names": 10,
            "max_single_name": 1.0,
            "min_cash": 0.0,
        },
        "turnover_cap": {"daily_max": 10.0},
        "execution_cost_model": {
            "components": {"commission_bps": 5.0, "slippage_bps": 10.0}
        },
        "label": {"horizon_bars": 5},
        "service_policy_replay": {
            "allow_position_pyramiding": "false",
            "turnover_budget_hard_stop": "false",
        },
    }
    monkeypatch.setattr(
        "src.mode_b.service_policy_replay.config_load",
        lambda _file, section: config_by_section.get(section, {}),
    )

    policy = ServicePolicyConfig.from_config()

    assert policy.allow_position_pyramiding is False
    assert policy.turnover_budget_hard_stop is False


def test_no_trade_score_spread_filters_low_conviction_cycle() -> None:
    engine = ServicePolicyReplayEngine(policy=_policy(no_trade_score_spread=0.1))
    panel = _panel([
        ("005930", "2026-05-01 09:00:00", 1.0, 0.001, 100.0),
    ])

    result = engine._simulate_panel(
        panel=panel,
        model_callable=_model,
        feature_cols=["feature_score"],
        target_col="label_5m_ret",
        policy=_policy(no_trade_score_spread=0.1),
    )

    assert result["orders"] == []
    assert result["order_stats"]["total_orders"] == 0


def test_min_holding_period_blocks_fast_sell() -> None:
    engine = ServicePolicyReplayEngine(policy=_policy(max_orders_per_cycle=2, min_holding_bars=3))
    panel = _panel([
        ("005930", "2026-05-01 09:00:00", 3.0, 0.001, 100.0),
        ("000660", "2026-05-01 09:00:00", 1.0, 0.001, 100.0),
        ("005930", "2026-05-01 09:01:00", 1.0, 0.001, 100.0),
        ("000660", "2026-05-01 09:01:00", 3.0, 0.001, 100.0),
    ])

    result = engine._simulate_panel(
        panel=panel,
        model_callable=_model,
        feature_cols=["feature_score"],
        target_col="label_5m_ret",
        policy=_policy(max_orders_per_cycle=2, min_holding_bars=3),
    )

    assert result["order_stats"]["min_holding_skipped_sells"] > 0
    assert [order["side"] for order in result["orders"]].count("sell") == 0


def test_rebalance_cooldown_blocks_immediate_rebuy() -> None:
    engine = ServicePolicyReplayEngine(policy=_policy(rebalance_cooldown_bars=3))
    panel = _panel([
        ("005930", "2026-05-01 09:00:00", 3.0, 0.001, 100.0),
        ("005930", "2026-05-01 09:01:00", 3.0, 0.001, 100.0),
    ])

    result = engine._simulate_panel(
        panel=panel,
        model_callable=_model,
        feature_cols=["feature_score"],
        target_col="label_5m_ret",
        policy=_policy(rebalance_cooldown_bars=3),
    )

    assert result["order_stats"]["cooldown_skipped_orders"] > 0
    assert result["order_stats"]["buy_orders"] == 1


def test_default_policy_blocks_position_pyramiding() -> None:
    engine = ServicePolicyReplayEngine(policy=_policy())
    panel = _panel([
        ("005930", "2026-05-01 09:00:00", 3.0, 0.001, 100.0),
        ("005930", "2026-05-01 09:01:00", 3.0, 0.001, 101.0),
    ])

    result = engine._simulate_panel(
        panel=panel,
        model_callable=_model,
        feature_cols=["feature_score"],
        target_col="label_5m_ret",
        policy=_policy(),
    )

    assert result["order_stats"]["buy_orders"] == 1
    assert result["order_stats"]["already_held_skipped_buys"] > 0


def test_min_expected_net_alpha_filters_weak_buy_candidate() -> None:
    engine = ServicePolicyReplayEngine(
        policy=_policy(
            min_expected_net_alpha_bps=15.0,
            expected_net_alpha_source="calibrated_net_bps",
        )
    )
    panel = _panel([
        ("005930", "2026-05-01 09:00:00", 14.0, 0.001, 100.0),
        ("000660", "2026-05-01 09:00:00", 10.0, 0.001, 100.0),
    ])

    result = engine._simulate_panel(
        panel=panel,
        model_callable=_model,
        feature_cols=["feature_score"],
        target_col="label_5m_ret",
        policy=_policy(
            min_expected_net_alpha_bps=15.0,
            expected_net_alpha_source="calibrated_net_bps",
        ),
    )

    assert result["orders"] == []
    assert result["order_stats"]["total_orders"] == 0


def test_min_expected_net_alpha_does_not_convert_rank_score_to_bps() -> None:
    engine = ServicePolicyReplayEngine(
        policy=_policy(min_expected_net_alpha_bps=9999.0, expected_net_alpha_source="rank_score")
    )
    panel = _panel([
        ("005930", "2026-05-01 09:00:00", 3.0, 0.001, 100.0),
        ("000660", "2026-05-01 09:00:00", 2.0, 0.001, 100.0),
    ])

    result = engine._simulate_panel(
        panel=panel,
        model_callable=_model,
        feature_cols=["feature_score"],
        target_col="label_5m_ret",
        policy=_policy(min_expected_net_alpha_bps=9999.0, expected_net_alpha_source="rank_score"),
    )

    assert result["order_stats"]["buy_orders"] == 1
    assert result["orders"][0]["ticker"] == "005930"


def test_explicit_replay_window_fails_closed_after_pit_snapshot() -> None:
    from src.mode_b.validation_tools import DataUnavailable

    engine = ServicePolicyReplayEngine(policy=_policy())
    with pytest.raises(DataUnavailable, match="PIT snapshot"):
        engine._resolve_replay_window("29990101", "29990102")


def test_turnover_budget_hard_stop_prevents_budget_breach() -> None:
    engine = ServicePolicyReplayEngine(policy=_policy(initial_capital=1_000.0, daily_turnover_cap=0.05))
    panel = _panel([
        ("005930", "2026-05-01 09:00:00", 3.0, 0.001, 100.0),
    ])

    result = engine._simulate_panel(
        panel=panel,
        model_callable=_model,
        feature_cols=["feature_score"],
        target_col="label_5m_ret",
        policy=_policy(initial_capital=1_000.0, daily_turnover_cap=0.05),
    )

    assert result["orders"] == []
    assert result["order_stats"]["turnover_budget_skipped_orders"] > 0


def test_service_policy_sharpe_threshold_comes_from_policy() -> None:
    blockers = ServicePolicyReplayEngine._gate_blockers(
        metrics={"total_return_bps": 100.0, "sr": 0.5},
        policy=_policy(min_service_policy_sharpe=0.6),
        max_daily_turnover=0.0,
        policy_checks={
            "no_naked_short_exposure": True,
            "order_caps_respected": True,
            "cash_guard_respected": True,
        },
    )

    assert "service_policy_sharpe_below_threshold" in blockers


def test_service_policy_gate_blockers_treat_string_false_as_false() -> None:
    blockers = ServicePolicyReplayEngine._gate_blockers(
        metrics={"total_return_bps": 100.0, "sr": 1.0},
        policy=_policy(),
        max_daily_turnover=0.0,
        policy_checks={
            "no_naked_short_exposure": "false",
            "order_caps_respected": "true",
            "cash_guard_respected": "true",
        },
    )

    assert blockers == ["naked_short_exposure"]


def test_service_policy_evidence_loader_and_attach(tmp_path: Path) -> None:
    report_path = tmp_path / "service_policy_replay_BUNDLE-TEST_20260512_120000.json"
    report_path.write_text(
        json.dumps(
            {
                "status": "BLOCKED",
                "bundle_id": "BUNDLE-TEST",
                "generated_at": "2026-05-12T12:00:00+09:00",
                "gate": {"blockers": ["daily_turnover_cap_violation"]},
                "metrics": {"total_return_bps": -10.0},
                "order_stats": {"total_orders": 3},
                "policy_checks": {"no_naked_short_exposure": True},
                "external_kis_api": False,
                "registry_mutated": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    evidence = load_service_policy_evidence("BUNDLE-TEST", reports_dir=tmp_path)
    assert evidence is not None
    assert evidence["status"] == "BLOCKED"
    assert evidence["bundle_id"] == "BUNDLE-TEST"
    assert evidence["service_policy_generated_at"] == "2026-05-12T12:00:00+09:00"
    assert evidence["service_policy_report_path"] == str(report_path)
    assert len(evidence["service_policy_report_sha256"]) == 64
    merged = attach_service_policy_evidence(
        {"bundle_id": "BUNDLE-TEST", "diagnostic_notes": "c12"},
        reports_dir=tmp_path,
    )
    assert merged["service_policy_replay"]["gate"]["blockers"] == [
        "daily_turnover_cap_violation",
    ]
    assert "service_policy_replay=BLOCKED" in merged["diagnostic_notes"]


def test_service_policy_verifier_handles_string_bools_and_malformed_stats(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "service_policy_replay_BUNDLE-TEST_20260512_120000.json"
    report_path.write_text(
        json.dumps(
            {
                "status": "PASS",
                "bundle_id": "BUNDLE-TEST",
                "date_range": {"start": "20260501", "end": "20260508"},
                "gate": {"status": "PASS", "blockers": []},
                "policy_checks": {
                    "deploy_candidate_by_service_policy": "true",
                    "no_naked_short_exposure": "true",
                    "order_caps_respected": "true",
                    "cash_guard_respected": "true",
                },
                "order_stats": {"naked_short_attempts": "0"},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    evidence = load_service_policy_evidence("BUNDLE-TEST", reports_dir=tmp_path)

    verification = verify_service_policy_evidence(
        evidence,
        bundle_id="BUNDLE-TEST",
        repo_root=tmp_path,
        expected_date_range={"start": "20260501", "end": "20260508"},
    )

    assert verification.status == "PASS"

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["order_stats"]["naked_short_attempts"] = "many"
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    evidence = load_service_policy_evidence("BUNDLE-TEST", reports_dir=tmp_path)

    verification = verify_service_policy_evidence(
        evidence,
        bundle_id="BUNDLE-TEST",
        repo_root=tmp_path,
        expected_date_range={"start": "20260501", "end": "20260508"},
    )

    assert verification.status == "BLOCKED"
    assert "service_policy_naked_short_attempts" in verification.blockers


def test_backtest_diagnostics_treats_string_false_flags_as_false(tmp_path: Path) -> None:
    report_path = tmp_path / "service_policy_replay_BUNDLE-TEST_20260516_120000.json"
    report_path.write_text(
        json.dumps(
            {
                "status": "PASS",
                "bundle_id": "BUNDLE-TEST",
                "gate": {"status": "PASS", "blockers": []},
                "policy_checks": {
                    "deploy_candidate_by_service_policy": True,
                    "no_naked_short_exposure": True,
                    "order_caps_respected": True,
                    "cash_guard_respected": True,
                },
                "order_stats": {"naked_short_attempts": 0},
                "external_kis_api": "false",
                "registry_mutated": "false",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    evidence = load_service_policy_evidence("BUNDLE-TEST", reports_dir=tmp_path)

    assert evidence["verification"]["status"] == "PASS"
    assert evidence["external_kis_api"] is False
    assert evidence["registry_mutated"] is False

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path


def _load_script(name: str):
    script_path = Path(__file__).resolve().parents[2] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cost_aware_retraining_plan_is_blocked_without_evidence(tmp_path, monkeypatch):
    mod = _load_script("cost_aware_retraining_plan")
    monkeypatch.setattr(mod, "ROOT", tmp_path)

    plan = mod.build_retraining_plan(
        bundle_id="BUNDLE-TEST",
        write_report=False,
    )

    assert plan["status"] == "BLOCKED"
    assert "phase2_feature_backfill_not_pass" in plan["blockers"]
    assert "service_policy_replay_not_pass" in plan["predeploy_blockers"]
    assert plan["read_only"] is True
    assert plan["registry_mutated"] is False


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_cost_aware_plan_uses_newer_phase2_backfill_over_stale_input(
    tmp_path,
    monkeypatch,
):
    mod = _load_script("cost_aware_retraining_plan")
    monkeypatch.setattr(mod, "ROOT", tmp_path)

    phase2 = (
        tmp_path
        / "artifacts"
        / "reports"
        / "phase2_feature_backfill"
        / "phase2_feature_backfill_new.json"
    )
    stale_input = (
        tmp_path
        / "artifacts"
        / "reports"
        / "phase2_input_readiness"
        / "phase2_input_readiness_old.json"
    )
    service = (
        tmp_path
        / "artifacts"
        / "reports"
        / "service_policy_replay"
        / "service_policy_replay_BUNDLE-TEST_new.json"
    )
    label_scan = (
        tmp_path
        / "artifacts"
        / "reports"
        / "label_horizon_scan"
        / "cost_aware_label_horizon_scan_new.json"
    )

    _write_json(phase2, {"status": "PASS", "coverage": {"dual_source_file_coverage": 1.0}})
    _write_json(
        stale_input,
        {
            "status": "BLOCKED",
            "blockers": ["dual_source_raw_archive_coverage_below_threshold"],
        },
    )
    _write_json(
        service,
        {
            "status": "PASS",
            "metrics": {"sr": 1.0, "mdd": -0.01},
            "gate": {"blockers": []},
        },
    )
    _write_json(
        label_scan,
        {
            "status": "PASS",
            "best_horizon": "session_close",
            "deployable_label_recommendation": True,
            "data": {
                "start_date": "20250509",
                "end_date": "20260515",
                "ticker_count": 30,
            },
            "horizons": [
                {"horizon": "5", "mean_net_bps": -14.0, "positive_net_rate": 0.25},
                {
                    "horizon": "session_close",
                    "mean_net_bps": 15.5,
                    "positive_net_rate": 0.51,
                },
            ],
        },
    )
    os.utime(stale_input, (1_000_000_000, 1_000_000_000))
    os.utime(phase2, (1_000_000_100, 1_000_000_100))

    plan = mod.build_retraining_plan(bundle_id="BUNDLE-TEST", write_report=False)

    assert plan["status"] == "READY"
    assert "service_policy_replay_not_pass" not in plan["blockers"]
    assert "phase2_input_readiness_not_pass" not in plan["blockers"]
    assert plan["evidence"]["phase2_input_readiness"]["blocking"] is False
    assert (
        plan["evidence"]["phase2_input_readiness"][
            "superseded_by_phase2_feature_backfill"
        ]
        is True
    )
    assert plan["recommended_experiment"]["target_horizon"] == "session_close"
    assert (
        plan["recommended_experiment"]["target_col_override"]
        == "label_session_close_net_ret"
    )
    assert plan["recommended_experiment"]["active_horizon_mean_net_bps"] == -14.0
    assert "20260508" not in plan["next_commands"][0]
    assert plan["research_registry"] == {
        "registry_dir": "artifacts/lgbm_research/BUNDLE-TEST",
        "production_registry_mutated": False,
        "staging_script": "new/scripts/post_backfill_prelive.py",
        "allow_production_candidate_write": False,
    }
    all_commands = "\n".join(plan["next_commands"])
    assert "python -m src.models.lgbm_trainer" not in all_commands
    assert "new/scripts/service_policy_replay.py" not in all_commands
    staged_command = plan["next_commands"][1]
    assert "new/scripts/post_backfill_prelive.py" in staged_command
    assert "--bundle-id BUNDLE-TEST" in staged_command
    assert "--registry-dir artifacts/lgbm_research/BUNDLE-TEST" in staged_command
    assert "--target-col-override label_session_close_net_ret" in staged_command
    assert "--run-paper-balance" in staged_command
    assert "--allow-production-candidate-write" not in staged_command
    assert plan["recommended_experiment"]["do_not_auto_deploy"] is True
    assert (
        plan["recommended_experiment"]["requires_label_ssot_update_before_prelive"]
        is True
    )
    assert "label_ssot_update_required_before_prelive" in plan["predeploy_blockers"]


def test_cost_aware_next_command_uses_final_gate_window_and_universe(monkeypatch, tmp_path):
    mod = _load_script("cost_aware_retraining_plan")
    monkeypatch.setattr(mod, "ROOT", tmp_path)

    def fake_config_load(file: str = "risk_config.yaml", key: str | None = None):
        if file == "universe_config.yaml":
            return {
                "sectors": {
                    "semis": {
                        "status": "confirmed",
                        "stocks": [
                            {"ticker": "005930", "status": "active"},
                        ],
                    },
                    "banks": {
                        "status": "confirmed_pending_data",
                        "stocks": [
                            {"ticker": "105560", "status": "pending_data"},
                        ],
                    },
                    "ignored": {
                        "status": "candidate",
                        "stocks": [
                            {"ticker": "000001", "status": "pending_data"},
                        ],
                    },
                },
            }
        if key == "backtest_agent":
            return {
                "deploy_decision_gate": {
                    "final_dataset_gate": {
                        "expected_start_date": "20250509",
                        "expected_end_date": "20260515",
                        "min_business_days": 249,
                        "min_tickers": 30,
                        "include_pending_data_tickers": True,
                        "allowed_stock_statuses": ["active", "pending_data"],
                        "allowed_sector_statuses": ["confirmed", "confirmed_pending_data"],
                    },
                },
            }
        if key == "cost_aware_retraining":
            return {"horizon_candidates": ["5"], "objective": {}}
        if key == "label":
            return {"horizon_bars": 5, "target_col": "label_5m_ret"}
        return {}

    monkeypatch.setattr(mod, "config_load", fake_config_load)

    plan = mod.build_retraining_plan(bundle_id="BUNDLE-TEST", write_report=False)
    command = plan["next_commands"][0]
    staged_command = plan["next_commands"][1]

    assert plan["training_window"] == {
        "source": "final_dataset_gate",
        "start_date": "20250509",
        "end_date": "20260515",
    }
    assert plan["training_universe"]["tickers"] == ["005930", "105560"]
    assert "--start-date 20250509" in command
    assert "--end-date 20260515" in command
    assert "--tickers 005930,105560" in command
    assert "20260508" not in command
    assert "new/scripts/post_backfill_prelive.py" in staged_command
    assert "--end-date 20260515" in staged_command
    assert "--business-days 249" in staged_command
    assert "--max-tickers 30" in staged_command
    assert "--registry-dir artifacts/lgbm_research/BUNDLE-TEST" in staged_command
    assert "--tickers 005930,105560" not in staged_command
    assert "20260508" not in staged_command


def test_cost_aware_plan_ready_to_train_without_existing_service_policy(
    monkeypatch,
    tmp_path,
):
    mod = _load_script("cost_aware_retraining_plan")
    monkeypatch.setattr(mod, "ROOT", tmp_path)

    _write_json(
        tmp_path
        / "artifacts"
        / "reports"
        / "phase2_feature_backfill"
        / "phase2_feature_backfill.json",
        {"status": "PASS", "coverage": {}},
    )
    _write_json(
        tmp_path
        / "artifacts"
        / "reports"
        / "label_horizon_scan"
        / "cost_aware_label_horizon_scan.json",
        {
            "status": "WARN",
            "best_horizon": "5",
            "deployable_label_recommendation": False,
            "data": {
                "start_date": "20250509",
                "end_date": "20260515",
                "ticker_count": 2,
            },
            "horizons": [
                {"horizon": "5", "mean_net_bps": 1.0, "positive_net_rate": 0.51},
            ],
        },
    )

    def fake_config_load(file: str = "risk_config.yaml", key: str | None = None):
        if file == "universe_config.yaml":
            return {
                "sectors": {
                    "semis": {
                        "status": "confirmed",
                        "stocks": [{"ticker": "005930", "status": "active"}],
                    },
                    "banks": {
                        "status": "confirmed_pending_data",
                        "stocks": [{"ticker": "105560", "status": "pending_data"}],
                    },
                },
            }
        if key == "backtest_agent":
            return {
                "deploy_decision_gate": {
                    "final_dataset_gate": {
                        "expected_start_date": "20250509",
                        "expected_end_date": "20260515",
                        "min_business_days": 249,
                        "min_tickers": 2,
                        "include_pending_data_tickers": True,
                        "allowed_stock_statuses": ["active", "pending_data"],
                        "allowed_sector_statuses": ["confirmed", "confirmed_pending_data"],
                    },
                },
            }
        if key == "cost_aware_retraining":
            return {"horizon_candidates": ["5"], "objective": {}}
        if key == "label":
            return {"horizon_bars": 5, "target_col": "label_5m_ret"}
        return {}

    monkeypatch.setattr(mod, "config_load", fake_config_load)

    plan = mod.build_retraining_plan(bundle_id="BUNDLE-TEST", write_report=False)

    assert plan["status"] == "BLOCKED"
    assert "cost_aware_target_no_rank_change" in plan["blockers"]
    assert "service_policy_replay_not_pass" in plan["predeploy_blockers"]
    assert "cost_aware_target_no_rank_change" in plan["pretraining_blockers"]
    assert "label_horizon_scan_not_pass" in plan["pretraining_blockers"]
    assert "label_horizon_scan_not_deployable" in plan["pretraining_blockers"]
    assert plan["recommended_experiment"]["target_col_candidate"] == "label_5m_net_ret"
    assert plan["recommended_experiment"]["target_col_override"] is None
    assert plan["recommended_experiment"]["rank_changing_target"] is False
    assert (
        plan["recommended_experiment"]["blocked_reason"]
        == "cost_aware_target_no_rank_change"
    )
    assert "--target-col-override" not in plan["next_commands"][1]


def test_cost_aware_plan_allows_research_trainable_warn_label_scan(
    monkeypatch,
    tmp_path,
):
    mod = _load_script("cost_aware_retraining_plan")
    monkeypatch.setattr(mod, "ROOT", tmp_path)

    _write_json(
        tmp_path
        / "artifacts"
        / "reports"
        / "phase2_feature_backfill"
        / "phase2_feature_backfill.json",
        {"status": "PASS", "coverage": {}},
    )
    _write_json(
        tmp_path
        / "artifacts"
        / "reports"
        / "label_horizon_scan"
        / "cost_aware_label_horizon_scan.json",
        {
            "status": "WARN",
            "best_horizon": "30",
            "deployable_label_recommendation": False,
            "research_trainable_label_recommendation": True,
            "data": {
                "start_date": "20250509",
                "end_date": "20260515",
                "ticker_count": 2,
                "missing_tickers": [],
            },
            "horizons": [
                {
                    "horizon": "5",
                    "mean_net_bps": -10.0,
                    "positive_net_rate": 0.45,
                },
                {
                    "horizon": "30",
                    "mean_net_bps": -5.0,
                    "positive_net_rate": 0.48,
                    "selection_impact": {
                        "rank_equivalent_to_active_horizon": False,
                        "selection_changes": 10,
                    },
                    "label_topk": {
                        "mean_net_bps": 40.0,
                        "positive_net_rate": 0.80,
                    },
                },
            ],
        },
    )

    def fake_config_load(file: str = "risk_config.yaml", key: str | None = None):
        if file == "universe_config.yaml":
            return {
                "sectors": {
                    "semis": {
                        "status": "confirmed",
                        "stocks": [
                            {"ticker": "005930", "status": "active"},
                            {"ticker": "105560", "status": "active"},
                        ],
                    },
                },
            }
        if key == "backtest_agent":
            return {
                "deploy_decision_gate": {
                    "final_dataset_gate": {
                        "expected_start_date": "20250509",
                        "expected_end_date": "20260515",
                        "min_tickers": 2,
                    },
                },
            }
        if key == "label":
            return {"horizon_bars": 5, "target_col": "label_5m_ret"}
        return {}

    monkeypatch.setattr(mod, "config_load", fake_config_load)

    plan = mod.build_retraining_plan(bundle_id="BUNDLE-TEST", write_report=False)

    assert plan["status"] == "READY"
    assert "label_horizon_scan_not_pass" not in plan["pretraining_blockers"]
    assert "label_horizon_scan_not_deployable" not in plan["pretraining_blockers"]
    assert "label_horizon_scan_not_deployable_for_prelive" in plan["predeploy_blockers"]
    assert plan["recommended_experiment"]["target_col_override"] == "label_30m_net_ret"


def test_cost_aware_plan_blocks_stale_label_scan_window_and_universe(
    monkeypatch,
    tmp_path,
):
    mod = _load_script("cost_aware_retraining_plan")
    monkeypatch.setattr(mod, "ROOT", tmp_path)

    _write_json(
        tmp_path
        / "artifacts"
        / "reports"
        / "phase2_feature_backfill"
        / "phase2_feature_backfill.json",
        {"status": "PASS", "coverage": {}},
    )
    _write_json(
        tmp_path
        / "artifacts"
        / "reports"
        / "label_horizon_scan"
        / "cost_aware_label_horizon_scan.json",
        {
            "status": "WARN",
            "best_horizon": "5",
            "data": {
                "start_date": "20260101",
                "end_date": "20260515",
                "ticker_count": 1,
            },
            "horizons": [
                {"horizon": "5", "mean_net_bps": 1.0, "positive_net_rate": 0.51},
            ],
        },
    )

    def fake_config_load(file: str = "risk_config.yaml", key: str | None = None):
        if file == "universe_config.yaml":
            return {
                "sectors": {
                    "semis": {
                        "status": "confirmed",
                        "stocks": [
                            {"ticker": "005930", "status": "active"},
                            {"ticker": "105560", "status": "active"},
                        ],
                    },
                },
            }
        if key == "backtest_agent":
            return {
                "deploy_decision_gate": {
                    "final_dataset_gate": {
                        "expected_start_date": "20250509",
                        "expected_end_date": "20260515",
                        "min_tickers": 2,
                    },
                },
            }
        if key == "label":
            return {"horizon_bars": 5, "target_col": "label_5m_ret"}
        return {}

    monkeypatch.setattr(mod, "config_load", fake_config_load)

    plan = mod.build_retraining_plan(bundle_id="BUNDLE-TEST", write_report=False)

    assert plan["status"] == "BLOCKED"
    assert "label_horizon_scan_window_mismatch" in plan["blockers"]
    assert "label_horizon_scan_ticker_mismatch" in plan["blockers"]


def test_cost_aware_plan_blocks_label_scan_missing_tickers(
    monkeypatch,
    tmp_path,
):
    mod = _load_script("cost_aware_retraining_plan")
    monkeypatch.setattr(mod, "ROOT", tmp_path)

    _write_json(
        tmp_path
        / "artifacts"
        / "reports"
        / "phase2_feature_backfill"
        / "phase2_feature_backfill.json",
        {"status": "PASS", "coverage": {}},
    )
    _write_json(
        tmp_path
        / "artifacts"
        / "reports"
        / "label_horizon_scan"
        / "cost_aware_label_horizon_scan.json",
        {
            "status": "WARN",
            "best_horizon": "session_close",
            "data": {
                "start_date": "20250509",
                "end_date": "20260515",
                "ticker_count": 2,
                "missing_tickers": ["105560"],
            },
            "horizons": [
                {"horizon": "5", "mean_net_bps": -1.0, "positive_net_rate": 0.45},
                {"horizon": "session_close", "mean_net_bps": 1.0, "positive_net_rate": 0.55},
            ],
        },
    )

    def fake_config_load(file: str = "risk_config.yaml", key: str | None = None):
        if file == "universe_config.yaml":
            return {
                "sectors": {
                    "semis": {
                        "status": "confirmed",
                        "stocks": [
                            {"ticker": "005930", "status": "active"},
                            {"ticker": "105560", "status": "active"},
                        ],
                    },
                },
            }
        if key == "backtest_agent":
            return {
                "deploy_decision_gate": {
                    "final_dataset_gate": {
                        "expected_start_date": "20250509",
                        "expected_end_date": "20260515",
                        "min_tickers": 2,
                    },
                },
            }
        if key == "label":
            return {"horizon_bars": 5, "target_col": "label_5m_ret"}
        return {}

    monkeypatch.setattr(mod, "config_load", fake_config_load)

    plan = mod.build_retraining_plan(bundle_id="BUNDLE-TEST", write_report=False)

    assert plan["status"] == "BLOCKED"
    assert "label_horizon_scan_missing_tickers" in plan["blockers"]
    assert "label_horizon_scan_ticker_mismatch" not in plan["blockers"]


def test_cost_aware_plan_blocks_same_count_different_label_scan_universe(
    monkeypatch,
    tmp_path,
):
    mod = _load_script("cost_aware_retraining_plan")
    monkeypatch.setattr(mod, "ROOT", tmp_path)

    _write_json(
        tmp_path
        / "artifacts"
        / "reports"
        / "phase2_feature_backfill"
        / "phase2_feature_backfill.json",
        {"status": "PASS", "coverage": {}},
    )
    stale_tickers = ["000660", "005930"]
    _write_json(
        tmp_path
        / "artifacts"
        / "reports"
        / "label_horizon_scan"
        / "cost_aware_label_horizon_scan.json",
        {
            "status": "PASS",
            "best_horizon": "session_close",
            "deployable_label_recommendation": True,
            "data": {
                "start_date": "20250509",
                "end_date": "20260515",
                "ticker_count": 2,
                "tickers": stale_tickers,
                "universe_hash": mod._universe_hash(stale_tickers),
                "missing_tickers": [],
            },
            "horizons": [
                {"horizon": "5", "mean_net_bps": -1.0, "positive_net_rate": 0.45},
                {
                    "horizon": "session_close",
                    "mean_net_bps": 1.0,
                    "positive_net_rate": 0.55,
                },
            ],
        },
    )

    def fake_config_load(file: str = "risk_config.yaml", key: str | None = None):
        if file == "universe_config.yaml":
            return {
                "sectors": {
                    "semis": {
                        "status": "confirmed",
                        "stocks": [
                            {"ticker": "005930", "status": "active"},
                            {"ticker": "105560", "status": "active"},
                        ],
                    },
                },
            }
        if key == "backtest_agent":
            return {
                "deploy_decision_gate": {
                    "final_dataset_gate": {
                        "expected_start_date": "20250509",
                        "expected_end_date": "20260515",
                        "min_tickers": 2,
                    },
                },
            }
        if key == "label":
            return {"horizon_bars": 5, "target_col": "label_5m_ret"}
        return {}

    monkeypatch.setattr(mod, "config_load", fake_config_load)

    plan = mod.build_retraining_plan(bundle_id="BUNDLE-TEST", write_report=False)

    assert plan["status"] == "BLOCKED"
    assert "label_horizon_scan_universe_mismatch" in plan["blockers"]
    assert "label_horizon_scan_ticker_mismatch" not in plan["blockers"]


def test_cost_aware_plan_blocks_warn_label_scan_even_if_window_matches(
    monkeypatch,
    tmp_path,
):
    mod = _load_script("cost_aware_retraining_plan")
    monkeypatch.setattr(mod, "ROOT", tmp_path)

    _write_json(
        tmp_path
        / "artifacts"
        / "reports"
        / "phase2_feature_backfill"
        / "phase2_feature_backfill.json",
        {"status": "PASS", "coverage": {}},
    )
    _write_json(
        tmp_path
        / "artifacts"
        / "reports"
        / "label_horizon_scan"
        / "cost_aware_label_horizon_scan.json",
        {
            "status": "WARN",
            "best_horizon": "session_close",
            "deployable_label_recommendation": False,
            "data": {
                "start_date": "20250509",
                "end_date": "20260515",
                "ticker_count": 2,
                "missing_tickers": [],
            },
            "horizons": [
                {"horizon": "5", "mean_net_bps": -1.0, "positive_net_rate": 0.45},
                {
                    "horizon": "session_close",
                    "mean_net_bps": 1.0,
                    "positive_net_rate": 0.55,
                },
            ],
        },
    )

    def fake_config_load(file: str = "risk_config.yaml", key: str | None = None):
        if file == "universe_config.yaml":
            return {
                "sectors": {
                    "semis": {
                        "status": "confirmed",
                        "stocks": [
                            {"ticker": "005930", "status": "active"},
                            {"ticker": "105560", "status": "active"},
                        ],
                    },
                },
            }
        if key == "backtest_agent":
            return {
                "deploy_decision_gate": {
                    "final_dataset_gate": {
                        "expected_start_date": "20250509",
                        "expected_end_date": "20260515",
                        "min_tickers": 2,
                    },
                },
            }
        if key == "label":
            return {"horizon_bars": 5, "target_col": "label_5m_ret"}
        return {}

    monkeypatch.setattr(mod, "config_load", fake_config_load)

    plan = mod.build_retraining_plan(bundle_id="BUNDLE-TEST", write_report=False)

    assert plan["status"] == "BLOCKED"
    assert "label_horizon_scan_not_pass" in plan["blockers"]
    assert "label_horizon_scan_not_deployable" in plan["blockers"]


def test_cost_aware_objective_string_false(monkeypatch, tmp_path):
    """objective 설정 문자열 false를 Python truthiness로 True 처리하지 않는다."""
    mod = _load_script("cost_aware_retraining_plan")
    monkeypatch.setattr(mod, "ROOT", tmp_path)

    def fake_config_load(file: str = "risk_config.yaml", key: str | None = None):
        if key == "cost_aware_retraining":
            return {
                "objective": {
                    "net_of_cost_target": "false",
                    "trade_no_trade_classifier": "false",
                },
                "horizon_candidates": ["5"],
            }
        if key == "label":
            return {"horizon_bars": 5, "target_col": "label_5m_ret"}
        if key == "ppo_allocator":
            return {}
        if key == "service_policy_replay":
            return {}
        return {}

    monkeypatch.setattr(mod, "config_load", fake_config_load)

    plan = mod.build_retraining_plan(bundle_id="BUNDLE-TEST", write_report=False)

    assert plan["objective"]["net_of_cost_target"] is False
    assert plan["objective"]["trade_no_trade_classifier"] is False
    assert plan["objective"]["expected_net_alpha_source"] == "rank_score"

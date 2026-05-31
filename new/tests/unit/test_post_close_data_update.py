"""post_close_data_update orchestrator tests."""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _load_script_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "post_close_data_update.py"
    spec = importlib.util.spec_from_file_location("post_close_data_update", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fake_config():
    return {
        "pit_safety": {"snapshot_hour": 18, "snapshot_minute": 0},
        "backtest_agent": {
            "deploy_decision_gate": {
                "final_dataset_gate": {
                    "expected_start_date": "20260514",
                    "expected_end_date": "20260515",
                    "min_business_days": 249,
                    "min_tickers": 30,
                }
            }
        },
        "post_close_data_update": {
            "stage_timeout_sec": 60,
            "stop_on_stage_failure": True,
            "skip_existing_backfill": True,
            "training_window_policy": "expanding_from_final_start",
            "raw_events_dir": "artifacts/raw/dual_source",
            "target_col_override": "label_195m_net_ret",
            "registry_dirs": {
                "live_data_readiness_train": "artifacts/lgbm_research/post_close/{target_end_date}/live_data_readiness",
                "post_backfill_prelive": "artifacts/lgbm_research/post_close/{target_end_date}/post_backfill_prelive",
            },
            "final_dataset_promotion": {
                "enabled": True,
                "update_risk_config": True,
                "require_mode_b": True,
                "require_post_backfill_prelive_pass": True,
                "backup_dir": "artifacts/reports/post_close_data_update/config_backups",
            },
            "stages": {
                "live_data_readiness": True,
                "news_dart_archive": True,
                "dual_source_materialize": True,
                "exogenous_materialize": True,
                "phase2_feature_backfill": True,
                "post_backfill_prelive": True,
            },
        },
    }


def test_latest_pit_safe_trading_day_before_snapshot_uses_previous_day(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setattr(mod, "config_load", lambda *args, **kwargs: _fake_config())
    kst = ZoneInfo("Asia/Seoul")

    assert (
        mod.latest_pit_safe_trading_day(datetime(2026, 5, 19, 12, 52, tzinfo=kst)).strftime(
            "%Y%m%d"
        )
        == "20260518"
    )


def test_latest_pit_safe_trading_day_after_snapshot_uses_today(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setattr(mod, "config_load", lambda *args, **kwargs: _fake_config())
    kst = ZoneInfo("Asia/Seoul")

    assert (
        mod.latest_pit_safe_trading_day(datetime(2026, 5, 19, 18, 1, tzinfo=kst)).strftime(
            "%Y%m%d"
        )
        == "20260519"
    )


def test_dry_run_reports_full_stage_plan(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setattr(mod, "config_load", lambda *args, **kwargs: _fake_config())

    report = mod.run_update(
        end_date="20260518",
        business_days=249,
        max_tickers=30,
        dry_run=True,
        run_prelive=True,
        bundle_id="BUNDLE-TEST",
    )

    assert report["status"] == "DRY_RUN"
    assert report["target_end_date"] == "20260518"
    assert report["current_final_dataset_end_date"] == "20260515"
    assert report["training_window_policy"] == "expanding_from_final_start"
    assert report["final_dataset_start_anchor"] == "20260514"
    assert report["requested_business_days"] == 249
    assert report["business_days"] == 249
    assert set(report["commands"]) == {
        "live_data_readiness",
        "news_dart_archive",
        "dual_source_materialize",
        "exogenous_materialize",
        "phase2_feature_backfill",
        "post_backfill_prelive",
    }
    assert "--skip-existing-backfill" in report["commands"]["live_data_readiness"]
    live_cmd = report["commands"]["live_data_readiness"]
    assert "--train-registry-dir" in live_cmd
    assert "--dns-preflight" in live_cmd
    assert "artifacts/lgbm_research/post_close/20260518/live_data_readiness" in live_cmd
    prelive_cmd = report["commands"]["post_backfill_prelive"]
    assert "--registry-dir" in prelive_cmd
    assert "artifacts/lgbm_research/post_close/20260518/post_backfill_prelive" in prelive_cmd


def test_stage_retryable_only_for_transient_failures():
    mod = _load_script_module()

    assert mod._stage_retryable({"failure_class": "transient_network_dns"}) is True
    assert mod._stage_retryable({"failure_classes": ["transient_network_dns"]}) is True
    assert mod._stage_retryable({"retryable": True}) is True
    assert mod._stage_retryable({"failure_class": "post_backfill_evidence_contract_failed"}) is False


def test_retry_rechecks_mode_b_window_before_second_attempt(monkeypatch):
    mod = _load_script_module()
    cfg = _fake_config()
    cfg["post_close_data_update"]["stages"] = {
        "live_data_readiness": True,
        "news_dart_archive": False,
        "dual_source_materialize": False,
        "exogenous_materialize": False,
        "phase2_feature_backfill": False,
        "post_backfill_prelive": False,
    }
    cfg["post_close_data_update"]["retry"] = {
        "enabled": True,
        "max_attempts": 2,
        "backoff_sec": 0,
    }
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    monkeypatch.setattr(mod, "config_load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(mod.time_module, "sleep", lambda _seconds: None)
    windows = iter([
        {"mode_ok": True, "window_ok": True},
        {"mode_ok": True, "window_ok": True},
        {"mode_ok": True, "window_ok": False},
    ])
    monkeypatch.setattr(mod, "_mode_b_window_state", lambda _now=None: next(windows))
    calls = {"run_stage": 0}

    def fake_run_stage(stage, timeout, retry_after_sec):
        del retry_after_sec
        calls["run_stage"] += 1
        if calls["run_stage"] > 1:
            raise AssertionError("retry must not start after Mode B window closes")
        return {
            "status": "BLOCKED",
            "command": stage.command,
            "returncode": 1,
            "failure_class": "transient_network_dns",
            "retryable": True,
        }

    monkeypatch.setattr(mod, "_run_stage", fake_run_stage)

    report = mod.run_update(
        end_date="20260518",
        business_days=249,
        max_tickers=30,
        dry_run=False,
        run_prelive=False,
        bundle_id="BUNDLE-TEST",
        now=datetime(2026, 5, 18, 18, 30, tzinfo=ZoneInfo("Asia/Seoul")),
    )

    assert calls["run_stage"] == 1
    assert report["status"] == "BLOCKED"
    assert report["blockers"] == ["live_data_readiness"]
    attempts = report["stage_attempts"]["live_data_readiness"]
    assert attempts[-1]["reason"] == "mode_b_window_closed_before_retry"


def test_expanding_policy_extends_business_days_from_final_dataset_start(monkeypatch):
    mod = _load_script_module()
    cfg = _fake_config()
    cfg["backtest_agent"]["deploy_decision_gate"]["final_dataset_gate"][
        "expected_start_date"
    ] = "20260511"
    cfg["backtest_agent"]["deploy_decision_gate"]["final_dataset_gate"][
        "expected_end_date"
    ] = "20260510"
    cfg["backtest_agent"]["deploy_decision_gate"]["final_dataset_gate"][
        "min_business_days"
    ] = 3
    monkeypatch.setattr(mod, "config_load", lambda *args, **kwargs: cfg)

    report = mod.run_update(
        end_date="20260515",
        business_days=3,
        max_tickers=30,
        dry_run=True,
        run_prelive=False,
        bundle_id=None,
    )

    assert report["status"] == "DRY_RUN"
    assert report["requested_business_days"] == 3
    assert report["business_days"] == 5
    assert report["target_start_date"] == "20260511"


def test_noop_when_final_dataset_already_covers_target(monkeypatch):
    mod = _load_script_module()
    cfg = _fake_config()
    cfg["backtest_agent"]["deploy_decision_gate"]["final_dataset_gate"][
        "expected_end_date"
    ] = "20260518"
    monkeypatch.setattr(mod, "config_load", lambda *args, **kwargs: cfg)

    report = mod.run_update(
        end_date="20260518",
        business_days=249,
        max_tickers=30,
        dry_run=False,
        run_prelive=False,
        bundle_id=None,
    )

    assert report["status"] == "SKIP"
    assert report["reason"] == "final_dataset_already_at_or_after_target"


def test_real_run_blocks_outside_mode_b_before_subprocess(monkeypatch):
    mod = _load_script_module()
    monkeypatch.delenv("ELEPHANT_MODE", raising=False)
    monkeypatch.setattr(mod, "config_load", lambda *args, **kwargs: _fake_config())

    report = mod.run_update(
        end_date="20260518",
        business_days=249,
        max_tickers=30,
        dry_run=False,
        run_prelive=False,
        bundle_id=None,
    )

    assert report["status"] == "BLOCKED"
    assert report["blockers"] == ["mode_b_window_not_open"]


def test_rewrite_final_dataset_gate_updates_only_expected_fields():
    mod = _load_script_module()
    text = """backtest_agent:
  deploy_decision_gate:
    final_dataset_gate:
      required: true
      expected_start_date: "20250509"
      expected_end_date: "20260515"
      min_business_days: 249
      min_tickers: 30
    on_pass:
      action: "deploy"
"""

    updated, replaced = mod._rewrite_final_dataset_gate_text(
        text,
        target_end_date="20260520",
        business_days=252,
    )

    assert replaced == {"expected_end_date": True, "min_business_days": True}
    assert 'expected_end_date: "20260520"' in updated
    assert "min_business_days: 252" in updated
    assert 'expected_start_date: "20250509"' in updated
    assert 'action: "deploy"' in updated


def test_successful_mode_b_run_promotes_final_dataset_gate(monkeypatch, tmp_path):
    mod = _load_script_module()
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    monkeypatch.setattr(mod, "config_load", lambda *args, **kwargs: _fake_config())
    risk_config = tmp_path / "risk_config.yaml"
    risk_config.write_text(
        """backtest_agent:
  deploy_decision_gate:
    final_dataset_gate:
      required: true
      expected_start_date: "20260514"
      expected_end_date: "20260515"
      min_business_days: 249
      min_tickers: 30
    on_pass:
      action: "deploy"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "_RISK_CONFIG_PATH", risk_config)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "config_reload", lambda *args, **kwargs: None)
    prelive_report = tmp_path / "artifacts" / "reports" / "prelive_pipeline" / "post_backfill_prelive_test.json"
    prelive_report.parent.mkdir(parents=True)
    prelive_report.write_text(
        json.dumps(
            {
                "status": "PASS",
                "end_date": "20260518",
                "business_days": 249,
                "bundle_id": "BUNDLE-TEST",
                "training_ticker_count": 30,
                "stages": {
                    "02_feature_coverage": {"status": "PASS"},
                    "02_lgbm_bundle": {
                        "status": "PASS",
                        "result": {
                            "candidate_bundle_staged": True,
                            "synthetic_fallback": False,
                        },
                    },
                    "03_service_policy_replay": {
                        "status": "PASS",
                        "result": {
                            "date_range": {
                                "start": "20250917",
                                "end": "20251021",
                            },
                        },
                    },
                    "04_backtest": {
                        "status": "PASS",
                        "result": {
                            "verdict": "pass",
                            "date_range": {
                                "start": "20250514T18:00:00+09:00",
                                "end": "20260518T18:00:00+09:00",
                            },
                            "service_policy_expected_date_range": {
                                "start": "20250917",
                                "end": "20251021",
                            },
                            "regression_risk": {"flagged": False},
                            "minute_bar_leakage_check": {"verdict": "pass"},
                            "service_policy_replay": {
                                "status": "PASS",
                                "date_range": {
                                    "start": "20250917",
                                    "end": "20251021",
                                },
                            },
                            "candidate_model_metadata": {
                                "train_end": "20260518",
                            },
                        },
                    },
                    "05_paper_balance_reconciliation": {"status": "PASS"},
                    "07_prelive_gate_after": {
                        "status": "PASS",
                        "end_date": "20260518",
                        "business_days": 249,
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fake_stage(stage, timeout, retry_after_sec):
        del retry_after_sec
        result = {"status": "PASS", "command": stage.command, "returncode": 0}
        if stage.name == "post_backfill_prelive":
            result["report_path"] = str(prelive_report)
        return result

    monkeypatch.setattr(mod, "_run_stage", fake_stage)

    report = mod.run_update(
        end_date="20260518",
        business_days=249,
        max_tickers=30,
        dry_run=False,
        run_prelive=True,
        bundle_id="BUNDLE-TEST",
        now=datetime(2026, 5, 18, 18, 30, tzinfo=ZoneInfo("Asia/Seoul")),
    )

    assert report["status"] == "PASS"
    assert report["ssot_promotion"]["status"] == "PASS"
    assert report["ssot_promotion"]["registry_mutated"] is False
    assert report["ssot_promotion"]["live_trading_allowed"] is False
    saved = risk_config.read_text(encoding="utf-8")
    assert 'expected_end_date: "20260518"' in saved
    assert "min_business_days: 249" in saved
    assert (tmp_path / "artifacts" / "reports" / "post_close_data_update" / "config_backups").exists()


def test_post_backfill_validator_blocks_nested_service_policy_range_mismatch(tmp_path):
    mod = _load_script_module()
    report_path = tmp_path / "post_backfill_prelive_mismatch.json"
    report_path.write_text(
        json.dumps(
            {
                "status": "PASS",
                "end_date": "20260518",
                "business_days": 249,
                "bundle_id": "BUNDLE-TEST",
                "training_ticker_count": 30,
                "stages": {
                    "02_feature_coverage": {"status": "PASS"},
                    "02_lgbm_bundle": {
                        "status": "PASS",
                        "result": {
                            "candidate_bundle_staged": True,
                            "synthetic_fallback": False,
                        },
                    },
                    "03_service_policy_replay": {
                        "status": "PASS",
                        "result": {
                            "date_range": {
                                "start": "20250101",
                                "end": "20250131",
                            },
                        },
                    },
                    "04_backtest": {
                        "status": "PASS",
                        "result": {
                            "verdict": "pass",
                            "service_policy_expected_date_range": {
                                "start": "20250917",
                                "end": "20251021",
                            },
                            "regression_risk": {"flagged": False},
                            "minute_bar_leakage_check": {"verdict": "pass"},
                            "service_policy_replay": {
                                "status": "PASS",
                                "date_range": {
                                    "start": "20250917",
                                    "end": "20251021",
                                },
                            },
                            "candidate_model_metadata": {
                                "train_end": "20260518",
                            },
                        },
                    },
                    "05_paper_balance_reconciliation": {"status": "PASS"},
                    "07_prelive_gate_after": {
                        "status": "PASS",
                        "end_date": "20260518",
                        "business_days": 249,
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = mod._validate_post_backfill_evidence(
        {"status": "PASS", "report_path": str(report_path)},
        target_end_date="20260518",
        business_days=249,
        bundle_id="BUNDLE-TEST",
        max_tickers=30,
    )

    assert result["status"] == "BLOCKED"
    assert "service_policy_replay_date_range_mismatch" in result["blockers"]


def test_successful_data_run_without_prelive_blocks_ssot_promotion(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    monkeypatch.setattr(mod, "config_load", lambda *args, **kwargs: _fake_config())
    monkeypatch.setattr(
        mod,
        "_run_stage",
        lambda stage, timeout, retry_after_sec: {
            "status": "PASS",
            "command": stage.command,
            "returncode": 0,
        },
    )

    report = mod.run_update(
        end_date="20260518",
        business_days=249,
        max_tickers=30,
        dry_run=False,
        run_prelive=False,
        bundle_id="BUNDLE-TEST",
        now=datetime(2026, 5, 18, 18, 30, tzinfo=ZoneInfo("Asia/Seoul")),
    )

    assert report["status"] == "BLOCKED"
    assert report["blockers"] == ["ssot_final_dataset_promotion"]
    assert report["ssot_promotion"]["reason"] == "post_backfill_prelive_not_requested"

"""post_backfill_prelive orchestrator tests."""
from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path


def _load_script_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "post_backfill_prelive.py"
    spec = importlib.util.spec_from_file_location("post_backfill_prelive", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gate(status: str = "PASS") -> dict:
    stages = {
        "01_code_ssot": {"status": "PASS"},
        "02_real_data_readiness": {"status": "PASS"},
        "03_80_business_day_data": {"status": status},
        "09_ops_risk": {"status": "PASS"},
    }
    return {
        "status": "PASS" if status == "PASS" else "BLOCKED",
        "stages": stages,
        "blockers": [] if status == "PASS" else ["03_80_business_day_data"],
    }


def _deployable_backtest(bundle_id: str) -> dict:
    return {
        "status": "PASS",
        "verdict": "pass",
        "bundle_id": bundle_id,
        "regression_risk": {"flagged": False},
        "minute_bar_leakage_check": {"verdict": "pass"},
        "feature_quality": {
            "dual_source_rows": 100,
            "dual_source_non_neutral_rows": 90,
            "exogenous_rows": 100,
            "exogenous_non_neutral_rows": 90,
        },
        "service_policy_replay": {
            "status": "PASS",
            "bundle_id": bundle_id,
            "service_policy_report_path": f"artifacts/reports/service_policy_replay/service_policy_replay_{bundle_id}.json",
            "service_policy_report_sha256": "a" * 64,
            "gate": {"status": "PASS"},
            "policy_checks": {
                "deploy_candidate_by_service_policy": True,
                "no_naked_short_exposure": True,
                "order_caps_respected": True,
                "cash_guard_respected": True,
            },
            "order_stats": {"naked_short_attempts": 0},
        },
    }


def _service_policy_pass(bundle_id: str, write_report: bool = True) -> dict:
    return {
        "status": "PASS",
        "gate": {"status": "PASS"},
        "metrics": {"sr": 1.0},
        "date_range": {"start": "20260414", "end": "20260503"},
        "report_path": f"artifacts/reports/service_policy_replay/service_policy_replay_{bundle_id}.json",
    }


def _feature_coverage(status: str = "PASS") -> dict:
    blockers = [] if status == "PASS" else ["dual_source_non_neutral_coverage_below_threshold"]
    return {
        "status": status,
        "date_range": {"start": "20250509", "end": "20260515"},
        "artifact_date_coverage": {
            "expected_date_count": 249,
            "selected_date_count": 249,
            "missing_date_count": 0,
        },
        "coverage": {
            "dual_source_file_coverage": 1.0,
            "dual_source_non_neutral_date_coverage": 1.0 if status == "PASS" else 80 / 249,
            "exogenous_file_coverage": 1.0,
            "exogenous_non_neutral_date_coverage": 1.0,
        },
        "blockers": blockers,
        "report_path": "artifacts/reports/phase2_feature_backfill/test.json",
        "report_path_relative": "artifacts/reports/phase2_feature_backfill/test.json",
    }


def test_business_start_date_uses_krx_calendar():
    mod = _load_script_module()

    assert mod._business_start_date(date(2026, 5, 8), 80).strftime("%Y%m%d") == "20260109"


def test_parse_args_defaults_follow_final_dataset_gate(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setattr(
        mod.prelive_gate,
        "_final_dataset_gate_cfg",
        lambda: {
            "expected_end_date": "20260515",
            "min_business_days": 249,
            "min_tickers": 30,
        },
    )

    args = mod._parse_args([])

    assert args.end_date == "20260515"
    assert args.business_days == 249
    assert args.max_tickers == 30


def test_pipeline_stops_before_training_when_80d_gate_blocked(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    monkeypatch.setattr(mod.prelive_gate, "build_report", lambda **kwargs: _gate("BLOCKED"))

    report = mod.run_pipeline(
        end_date="20260508",
        business_days=80,
        max_tickers=20,
        bundle_id="BUNDLE-TEST",
        run_paper_balance=False,
        system_positions_json=None,
        submit_probe=False,
        ticker="005930",
        side="buy",
        qty=1,
        price=None,
        confirm_phrase=None,
        order_type="00",
        target_col_override=None,
        registry_dir=None,
    )

    assert report["status"] == "BLOCKED"
    assert report["blockers"] == ["01_prelive_gate_before"]
    assert report["stages"]["02_lgbm_bundle"]["status"] == "SKIP"


def test_pipeline_runs_ordered_happy_path(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    gate_calls = []
    retrainer_init_kwargs = {}
    retrain_kwargs = {}

    def fake_build_report(**kwargs):
        gate_calls.append(kwargs)
        return _gate("PASS")

    monkeypatch.setattr(mod.prelive_gate, "build_report", fake_build_report)
    monkeypatch.setattr(
        mod.prelive_gate,
        "_active_tickers",
        lambda max_tickers, include_pending_data=None: ["005930", "105560"],
    )

    class FakeLGBM:
        def retrain(self, **kwargs):
            retrain_kwargs.update(kwargs)
            return {
                "candidate_bundle_staged": True,
                "bundle_id": kwargs["bundle_id"],
                "synthetic_fallback": False,
            }

    class FakePaper:
        def run_balance_reconciliation(self, system_positions=None, write_report=True):
            return {"status": "PASS", "stages": {"balance": {"status": "PASS"}}}

        def submit_probe_order(self, **kwargs):
            return {"status": "PASS", "stages": {"execution": {"status": "PASS"}}}

    def fake_retrainer_factory(**kwargs):
        retrainer_init_kwargs.update(kwargs)
        return FakeLGBM()

    monkeypatch.setattr(mod, "NightlyLGBMRetrainer", fake_retrainer_factory)
    monkeypatch.setattr(mod, "run_phase2_feature_backfill", lambda **kwargs: _feature_coverage("PASS"))
    monkeypatch.setattr(mod, "run_service_policy_replay", _service_policy_pass)
    monkeypatch.setattr(
        mod,
        "run_backtest",
        lambda bundle_id, write_report=True: _deployable_backtest(bundle_id),
    )
    monkeypatch.setattr(
        mod.prelive_gate,
        "_is_deployable_backtest_report",
        lambda payload, bundle_id: True,
    )
    monkeypatch.setattr(mod, "PaperTradingRunner", lambda: FakePaper())

    report = mod.run_pipeline(
        end_date="20260508",
        business_days=80,
        max_tickers=20,
        bundle_id="BUNDLE-TEST",
        run_paper_balance=True,
        system_positions_json=None,
        submit_probe=True,
        ticker="005930",
        side="buy",
        qty=1,
        price=1.0,
        confirm_phrase="PAPER_ORDER_OK",
        order_type="00",
        target_col_override="label_session_close_net_ret",
        registry_dir="artifacts/lgbm_research/BUNDLE-TEST",
    )

    assert report["status"] == "PASS"
    assert report["stages"]["02_feature_coverage"]["status"] == "PASS"
    assert report["stages"]["02_lgbm_bundle"]["status"] == "PASS"
    assert report["stages"]["03_service_policy_replay"]["status"] == "PASS"
    assert report["stages"]["04_backtest"]["status"] == "PASS"
    assert report["stages"]["05_paper_balance_reconciliation"]["status"] == "PASS"
    assert report["stages"]["06_paper_probe_order"]["status"] == "PASS"
    assert gate_calls[0].get("bundle_id") is None
    assert gate_calls[-1]["bundle_id"] == "BUNDLE-TEST"
    assert report["training_tickers"] == ["005930", "105560"]
    assert retrainer_init_kwargs["registry_dir"] == "artifacts/lgbm_research/BUNDLE-TEST"
    assert retrainer_init_kwargs["allow_production_candidate_write"] is False
    assert retrain_kwargs["tickers"] == ["005930", "105560"]
    assert retrain_kwargs["target_col_override"] == "label_session_close_net_ret"
    assert report["registry_dir"] == "artifacts/lgbm_research/BUNDLE-TEST"


def test_pipeline_blocks_when_final_training_universe_empty(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    monkeypatch.setattr(mod.prelive_gate, "build_report", lambda **kwargs: _gate("PASS"))
    monkeypatch.setattr(
        mod.prelive_gate,
        "_active_tickers",
        lambda max_tickers, include_pending_data=None: [],
    )

    report = mod.run_pipeline(
        end_date="20260508",
        business_days=80,
        max_tickers=30,
        bundle_id="BUNDLE-TEST",
        run_paper_balance=False,
        system_positions_json=None,
        submit_probe=False,
        ticker="005930",
        side="buy",
        qty=1,
        price=1.0,
        confirm_phrase=None,
        order_type="00",
    )

    assert report["status"] == "BLOCKED"
    assert report["blockers"] == ["02_lgbm_bundle"]
    assert report["stages"]["02_lgbm_bundle"]["status"] == "BLOCKED"
    assert report["training_ticker_count"] == 0


def test_pipeline_blocks_before_training_when_feature_coverage_blocked(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    monkeypatch.setattr(mod.prelive_gate, "build_report", lambda **kwargs: _gate("PASS"))
    monkeypatch.setattr(
        mod.prelive_gate,
        "_active_tickers",
        lambda max_tickers, include_pending_data=None: ["005930", "105560"],
    )
    monkeypatch.setattr(
        mod,
        "run_phase2_feature_backfill",
        lambda **kwargs: _feature_coverage("BLOCKED"),
    )

    class FakeLGBM:
        def retrain(self, **kwargs):
            _ = kwargs
            raise AssertionError("training must not start when final feature coverage blocks")

    monkeypatch.setattr(mod, "NightlyLGBMRetrainer", lambda **kwargs: FakeLGBM())

    report = mod.run_pipeline(
        end_date="20260515",
        business_days=249,
        max_tickers=30,
        bundle_id="BUNDLE-TEST",
        run_paper_balance=False,
        system_positions_json=None,
        submit_probe=False,
        ticker="005930",
        side="buy",
        qty=1,
        price=1.0,
        confirm_phrase=None,
        order_type="00",
    )

    assert report["status"] == "BLOCKED"
    assert report["blockers"] == ["02_feature_coverage"]
    assert report["stages"]["02_feature_coverage"]["status"] == "BLOCKED"
    assert report["stages"]["02_lgbm_bundle"]["status"] == "SKIP"
    assert "dual_source_non_neutral_coverage_below_threshold" in (
        report["stages"]["02_lgbm_bundle"]["upstream_blockers"]
    )


def test_pipeline_blocks_backtest_pass_with_leakage_fail(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    monkeypatch.setattr(mod.prelive_gate, "build_report", lambda **kwargs: _gate("PASS"))

    class FakeLGBM:
        def retrain(self, **kwargs):
            return {
                "candidate_bundle_staged": True,
                "bundle_id": kwargs["bundle_id"],
                "synthetic_fallback": False,
            }

    monkeypatch.setattr(mod, "NightlyLGBMRetrainer", lambda **kwargs: FakeLGBM())
    monkeypatch.setattr(mod, "run_phase2_feature_backfill", lambda **kwargs: _feature_coverage("PASS"))
    monkeypatch.setattr(mod, "run_service_policy_replay", _service_policy_pass)
    monkeypatch.setattr(mod, "run_backtest", lambda bundle_id, write_report=True: {
        "status": "PASS",
        "verdict": "pass",
        "bundle_id": bundle_id,
        "regression_risk": {"flagged": False},
        "minute_bar_leakage_check": {"verdict": "fail"},
    })

    report = mod.run_pipeline(
        end_date="20260508",
        business_days=80,
        max_tickers=20,
        bundle_id="BUNDLE-TEST",
        run_paper_balance=False,
        system_positions_json=None,
        submit_probe=False,
        ticker="005930",
        side="buy",
        qty=1,
        price=1.0,
        confirm_phrase=None,
        order_type="00",
    )

    assert report["status"] == "BLOCKED"
    assert report["blockers"] == ["04_backtest"]
    assert report["stages"]["04_backtest"]["deployable"] is False


def test_pipeline_stops_when_service_policy_replay_blocks(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    monkeypatch.setattr(mod.prelive_gate, "build_report", lambda **kwargs: _gate("PASS"))

    class FakeLGBM:
        def retrain(self, **kwargs):
            return {
                "candidate_bundle_staged": True,
                "bundle_id": kwargs["bundle_id"],
                "synthetic_fallback": False,
            }

    monkeypatch.setattr(mod, "NightlyLGBMRetrainer", lambda **kwargs: FakeLGBM())
    monkeypatch.setattr(mod, "run_phase2_feature_backfill", lambda **kwargs: _feature_coverage("PASS"))
    monkeypatch.setattr(
        mod,
        "run_service_policy_replay",
        lambda bundle_id, write_report=True: {
            "status": "BLOCKED",
            "gate": {"status": "BLOCKED", "blockers": ["service_policy_sharpe_below_threshold"]},
        },
    )

    report = mod.run_pipeline(
        end_date="20260508",
        business_days=80,
        max_tickers=20,
        bundle_id="BUNDLE-TEST",
        run_paper_balance=False,
        system_positions_json=None,
        submit_probe=False,
        ticker="005930",
        side="buy",
        qty=1,
        price=1.0,
        confirm_phrase=None,
        order_type="00",
    )

    assert report["status"] == "BLOCKED"
    assert report["blockers"] == ["03_service_policy_replay"]
    assert "04_backtest" not in report["stages"]


def test_pipeline_blocks_backtest_pass_with_regression_flag(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    monkeypatch.setattr(mod.prelive_gate, "build_report", lambda **kwargs: _gate("PASS"))

    class FakeLGBM:
        def retrain(self, **kwargs):
            return {
                "candidate_bundle_staged": True,
                "bundle_id": kwargs["bundle_id"],
                "synthetic_fallback": False,
            }

    monkeypatch.setattr(mod, "NightlyLGBMRetrainer", lambda **kwargs: FakeLGBM())
    monkeypatch.setattr(mod, "run_phase2_feature_backfill", lambda **kwargs: _feature_coverage("PASS"))
    monkeypatch.setattr(mod, "run_service_policy_replay", _service_policy_pass)
    monkeypatch.setattr(mod, "run_backtest", lambda bundle_id, write_report=True: {
        "status": "PASS",
        "verdict": "pass",
        "bundle_id": bundle_id,
        "regression_risk": {"flagged": True},
        "minute_bar_leakage_check": {"verdict": "pass"},
    })

    report = mod.run_pipeline(
        end_date="20260508",
        business_days=80,
        max_tickers=20,
        bundle_id="BUNDLE-TEST",
        run_paper_balance=False,
        system_positions_json=None,
        submit_probe=False,
        ticker="005930",
        side="buy",
        qty=1,
        price=1.0,
        confirm_phrase=None,
        order_type="00",
    )

    assert report["status"] == "BLOCKED"
    assert report["blockers"] == ["04_backtest"]
    assert report["stages"]["04_backtest"]["deployable"] is False


def test_pipeline_blocks_before_readiness_without_mode_b(monkeypatch):
    mod = _load_script_module()
    monkeypatch.delenv("ELEPHANT_MODE", raising=False)

    report = mod.run_pipeline(
        end_date="20260508",
        business_days=80,
        max_tickers=20,
        bundle_id="BUNDLE-TEST",
        run_paper_balance=False,
        system_positions_json=None,
        submit_probe=False,
        ticker="005930",
        side="buy",
        qty=1,
        price=None,
        confirm_phrase=None,
        order_type="00",
    )

    assert report["status"] == "BLOCKED"
    assert report["blockers"] == ["00_mode_b_guard"]
    assert "01_prelive_gate_before" not in report["stages"]


def test_write_report_sets_path(monkeypatch, tmp_path):
    mod = _load_script_module()
    monkeypatch.setattr(mod, "_REPORT_ROOT", tmp_path)
    report = {"status": "BLOCKED", "stages": {}}

    path = mod._write_report(report)
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert saved["report_path"] == str(path)
    assert saved["report_path_relative"].endswith(path.name)

from __future__ import annotations

import json
import os
from pathlib import Path

from src.ops import service_readiness_status


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _final_dataset_metadata() -> dict:
    tickers = [
        "005930", "000660", "042700", "403870", "058470",
        "329180", "042660", "010140", "009540", "267250",
        "006400", "051910", "373220", "096770", "247540",
        "012450", "047810", "079550", "298040", "272210",
        "105560", "055550", "086790", "024110", "000810",
        "005380", "000270", "012330", "011210", "086280",
    ]
    return {
        "train_start": "2025-05-09",
        "train_end": "2026-05-15",
        "data_source": "artifact_bars",
        "synthetic_fallback": False,
        "requested_tickers": tickers,
        "loaded_tickers": tickers,
        "missing_tickers": [],
        "n_tickers": len(tickers),
    }


def test_service_status_is_read_only_and_blocks_without_broker(tmp_path: Path) -> None:
    bundle_id = "BUNDLE-TEST"
    _write_json(
        tmp_path / "artifacts/lgbm/registry.json",
        {"active_version": None, "versions": []},
    )
    _write_json(
        tmp_path / "artifacts/lgbm_paper/registry.json",
        {"active_version": "paper-v1", "versions": [{"version": "paper-v1"}]},
    )
    _write_json(
        tmp_path / f"artifacts/reports/backtest/backtest_{bundle_id}_20260513.json",
        {
            "bundle_id": bundle_id,
            "verdict": "warn",
            "regression_risk": {"flagged": True, "severity": "high"},
            "minute_bar_leakage_check": {"verdict": "pass"},
            "feature_quality": {},
        },
    )

    payload = service_readiness_status.build_service_status(bundle_id=bundle_id, root=tmp_path)

    assert payload["status"] == "PARTIAL"
    assert payload["read_only"] is True
    assert payload["external_api_called"] is False
    assert payload["registry_mutated"] is False
    assert payload["deploy_quality"] == "BLOCKED"
    assert payload["live_trading_allowed"] is False
    assert payload["be_contract"]["safe_to_enable_order_actions"] is False
    assert payload["production_registry"]["active_version"] is None


def test_service_status_passes_with_external_broker_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bundle_id = "BUNDLE-TEST"
    monkeypatch.setattr(
        service_readiness_status,
        "_service_policy_gate_pass",
        lambda backtest, bundle_id, **kwargs: True,
    )
    _write_json(
        tmp_path / "artifacts/lgbm/registry.json",
        {"active_version": None, "versions": []},
    )
    _write_json(
        tmp_path / "artifacts/lgbm_paper/registry.json",
        {"active_version": "paper-v1", "versions": [{"version": "paper-v1"}]},
    )
    _write_json(
        tmp_path / f"artifacts/reports/backtest/backtest_{bundle_id}_20260514.json",
        {
            "bundle_id": bundle_id,
            "verdict": "pass",
            "regression_risk": {"flagged": False, "severity": "low"},
            "minute_bar_leakage_check": {"verdict": "pass"},
            "feature_quality": {
                "dual_source_rows": 10,
                "dual_source_non_neutral_rows": 10,
                "exogenous_rows": 10,
                "exogenous_non_neutral_rows": 10,
            },
            "service_policy_replay": {"status": "PASS"},
            "candidate_model_metadata": _final_dataset_metadata(),
        },
    )
    _write_json(
        tmp_path
        / "artifacts/reports/paper_auto_trading/paper_auto_service_rehearsal_20260514.json",
        {
            "status": "PASS",
            "bundle_id": bundle_id,
            "external_kis_api": True,
            "evidence_level": "external_kis_virtual",
            "stage_statuses": {
                "preflight": "PASS",
                "paper_auto_cycle": "PASS",
                "balance_reconciliation": "PASS",
                "probe_order": "PASS",
                "order_history_requery": "PASS",
            },
            "stages": {
                "paper_auto_cycle": {
                    "status": "PASS",
                    "stages": {
                        "active_model_guard": {"bundle_id": bundle_id},
                        "cycles": {
                            "items": [{
                                "status": "PASS",
                                "order_history_verification": {
                                    "status": "PASS",
                                    "queries": [{"matched_order_count": 1}],
                                },
                            }],
                        },
                    },
                },
            },
        },
    )

    payload = service_readiness_status.build_service_status(
        bundle_id=bundle_id,
        root=tmp_path,
    )

    assert payload["status"] == "PASS"
    assert payload["deploy_quality"] == "PASS"
    assert payload["broker_evidence"] == "PASS"
    assert payload["kis_broker_evidence"]["evidence_level"] == "external_kis_virtual"
    assert payload["kis_broker_evidence"]["bundle_match"] is True
    assert payload["kis_broker_evidence"]["paper_auto_cycle_history_matched"] is True


def test_service_status_blocks_failed_auto_cycle_even_with_preflight_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bundle_id = "BUNDLE-TEST"
    monkeypatch.setattr(
        service_readiness_status,
        "_service_policy_gate_pass",
        lambda backtest, bundle_id, **kwargs: True,
    )
    _write_json(
        tmp_path / "artifacts/lgbm/registry.json",
        {"active_version": None, "versions": []},
    )
    _write_json(
        tmp_path / "artifacts/lgbm_paper/registry.json",
        {"active_version": "paper-v1", "versions": [{"version": "paper-v1"}]},
    )
    _write_json(
        tmp_path / f"artifacts/reports/backtest/backtest_{bundle_id}_20260514.json",
        {
            "bundle_id": bundle_id,
            "verdict": "pass",
            "regression_risk": {"flagged": False, "severity": "low"},
            "minute_bar_leakage_check": {"verdict": "pass"},
            "feature_quality": {
                "dual_source_rows": 10,
                "dual_source_non_neutral_rows": 10,
                "exogenous_rows": 10,
                "exogenous_non_neutral_rows": 10,
            },
            "service_policy_replay": {"status": "PASS"},
            "candidate_model_metadata": _final_dataset_metadata(),
        },
    )
    _write_json(
        tmp_path
        / "artifacts/reports/paper_auto_trading/paper_auto_service_rehearsal_20260514.json",
        {
            "status": "FAIL",
            "bundle_id": bundle_id,
            "external_kis_api": True,
            "evidence_level": "external_kis_virtual",
            "stage_statuses": {
                "preflight": "PASS",
                "paper_auto_cycle": "FAIL",
                "balance_reconciliation": "PASS",
                "probe_order": "PASS",
                "order_history_requery": "PASS",
            },
            "stages": {
                "paper_auto_cycle": {
                    "status": "FAIL",
                    "stages": {
                        "active_model_guard": {"bundle_id": bundle_id},
                        "cycles": {
                            "items": [{
                                "status": "FAIL",
                                "order_history_verification": {
                                    "status": "SKIP",
                                    "queries": [],
                                },
                            }],
                        },
                    },
                },
            },
        },
    )

    payload = service_readiness_status.build_service_status(
        bundle_id=bundle_id,
        root=tmp_path,
    )

    assert payload["status"] == "PARTIAL"
    assert payload["broker_evidence"] == "BLOCKED"
    assert payload["kis_broker_evidence"]["paper_auto_cycle_history_matched"] is False


def test_service_status_summarizes_latest_paper_smoke_market_closed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bundle_id = "BUNDLE-TEST"
    monkeypatch.setattr(
        service_readiness_status,
        "_service_policy_gate_pass",
        lambda backtest, bundle_id, **kwargs: True,
    )
    _write_json(
        tmp_path / "artifacts/lgbm/registry.json",
        {"active_version": None, "versions": []},
    )
    _write_json(
        tmp_path / "artifacts/lgbm_paper/registry.json",
        {"active_version": "paper-v1", "versions": [{"version": "paper-v1"}]},
    )
    _write_json(
        tmp_path / f"artifacts/reports/backtest/backtest_{bundle_id}_20260514.json",
        {
            "bundle_id": bundle_id,
            "verdict": "pass",
            "regression_risk": {"flagged": False, "severity": "low"},
            "minute_bar_leakage_check": {"verdict": "pass"},
            "feature_quality": {
                "dual_source_rows": 10,
                "dual_source_non_neutral_rows": 10,
                "exogenous_rows": 10,
                "exogenous_non_neutral_rows": 10,
            },
            "service_policy_replay": {"status": "PASS"},
            "candidate_model_metadata": _final_dataset_metadata(),
        },
    )
    _write_json(
        tmp_path
        / "artifacts/reports/paper_trading/paper_trading_balance_reconciliation_20260514.json",
        {
            "status": "PASS",
            "runtime": {"kis_mode": "virtual"},
            "stages": {
                "balance": {
                    "status": "PASS",
                    "_mode": "virtual",
                    "balance": {"cash": 500000000.0},
                },
            },
        },
    )
    _write_json(
        tmp_path
        / "artifacts/reports/paper_trading/paper_trading_submit_probe_order_20260514.json",
        {
            "status": "FAIL",
            "runtime": {"kis_mode": "virtual"},
            "stages": {
                "execution": {
                    "status": "FAIL",
                    "result": {
                        "execution_report": {
                            "rejections": [{
                                "error": (
                                    "[kis_rest] KIS API 오류 "
                                    "msg_cd=40580000 msg=모의투자 장종료 입니다."
                                ),
                            }],
                        },
                    },
                },
                "order_history": {
                    "status": "PASS",
                    "matched_order_count": 0,
                    "_mode": "virtual",
                },
            },
        },
    )

    payload = service_readiness_status.build_service_status(
        bundle_id=bundle_id,
        root=tmp_path,
    )

    assert payload["status"] == "PARTIAL"
    assert payload["deploy_quality"] == "PASS"
    assert payload["broker_evidence"] == "BLOCKED"
    evidence = payload["kis_broker_evidence"]
    assert evidence["external_kis_api"] is True
    assert evidence["evidence_level"] == "external_kis_virtual_paper_trading"
    assert evidence["stage_statuses"]["balance_reconciliation"] == "PASS"
    assert evidence["stage_statuses"]["probe_order"] == "BLOCKED"
    assert (
        evidence["paper_trading_evidence"]["probe_order"]["blocker"]["error_code"]
        == "BROKER_MARKET_CLOSED"
    )


def test_service_status_prefers_deployable_evidence_over_newer_failed_experiment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bundle_id = "BUNDLE-TEST"
    monkeypatch.setattr(
        service_readiness_status,
        "_service_policy_gate_pass",
        lambda backtest, bundle_id, **kwargs: True,
    )
    _write_json(
        tmp_path / "artifacts/lgbm/registry.json",
        {"active_version": None, "versions": []},
    )
    _write_json(
        tmp_path / "artifacts/lgbm_paper/registry.json",
        {"active_version": "paper-v1", "versions": [{"version": "paper-v1"}]},
    )

    pass_backtest = (
        tmp_path
        / f"artifacts/reports/backtest/backtest_{bundle_id}_20260514_202334.json"
    )
    fail_backtest = (
        tmp_path
        / f"artifacts/reports/backtest/backtest_{bundle_id}_20260516_035526.json"
    )
    _write_json(
        pass_backtest,
        {
            "bundle_id": bundle_id,
            "verdict": "pass",
            "regression_risk": {"flagged": "false", "severity": "low"},
            "minute_bar_leakage_check": {"verdict": "pass"},
            "feature_quality": {
                "dual_source_rows": 10,
                "dual_source_non_neutral_rows": 10,
                "exogenous_rows": 10,
                "exogenous_non_neutral_rows": 10,
            },
            "service_policy_replay": {"status": "PASS"},
            "metrics": {"sr": 8.3},
            "candidate_model_metadata": _final_dataset_metadata(),
        },
    )
    _write_json(
        fail_backtest,
        {
            "bundle_id": bundle_id,
            "verdict": "fail",
            "regression_risk": {"flagged": True, "severity": "high"},
            "minute_bar_leakage_check": {"verdict": "fail"},
            "feature_quality": {},
            "service_policy_replay": {"status": "PASS"},
            "metrics": {"sr": 0.0},
        },
    )
    os.utime(pass_backtest, (1000, 1000))
    os.utime(fail_backtest, (2000, 2000))

    pass_replay = (
        tmp_path
        / f"artifacts/reports/service_policy_replay/service_policy_replay_{bundle_id}_20260514.json"
    )
    fail_replay = (
        tmp_path
        / f"artifacts/reports/service_policy_replay/service_policy_replay_{bundle_id}_20260516.json"
    )
    _write_json(pass_replay, {"status": "PASS", "blockers": []})
    _write_json(fail_replay, {"status": "BLOCKED", "blockers": ["experiment_failed"]})
    os.utime(pass_replay, (1000, 1000))
    os.utime(fail_replay, (2000, 2000))

    _write_json(
        tmp_path
        / "artifacts/reports/paper_auto_trading/paper_auto_service_rehearsal_20260515.json",
        {
            "status": "PASS",
            "bundle_id": bundle_id,
            "external_kis_api": True,
            "evidence_level": "external_kis_virtual",
            "stage_statuses": {
                "preflight": "PASS",
                "paper_auto_cycle": "PASS",
                "balance_reconciliation": "PASS",
                "probe_order": "PASS",
                "order_history_requery": "PASS",
            },
            "stages": {
                "paper_auto_cycle": {
                    "status": "PASS",
                    "stages": {
                        "active_model_guard": {"bundle_id": bundle_id},
                        "cycles": {
                            "items": [{
                                "status": "PASS",
                                "order_history_verification": {
                                    "status": "PASS",
                                    "queries": [{"matched_order_count": 1}],
                                },
                            }],
                        },
                    },
                },
            },
        },
    )

    payload = service_readiness_status.build_service_status(
        bundle_id=bundle_id,
        root=tmp_path,
    )

    assert payload["status"] == "PASS"
    assert payload["deploy_quality"] == "PASS"
    assert payload["c12_backtest"]["selection"] == "latest_deployable"
    assert payload["c12_backtest"]["ignored_newer_non_deployable_reports"] == 1
    assert payload["c12_backtest"]["report_path"].endswith("20260514_202334.json")
    assert payload["service_policy_replay"]["status"] == "PASS"
    assert payload["service_policy_replay"]["report_path"].endswith("20260514.json")
    assert payload["c12_backtest"]["regression_risk_flagged"] == "false"


def test_broker_evidence_treats_string_false_external_flag_as_false(
    tmp_path: Path,
) -> None:
    bundle_id = "BUNDLE-TEST"
    _write_json(
        tmp_path
        / "artifacts/reports/paper_auto_trading/paper_auto_service_rehearsal_20260515.json",
        {
            "status": "PASS",
            "bundle_id": bundle_id,
            "external_kis_api": "false",
            "evidence_level": "external_kis_virtual",
            "stage_statuses": {
                "paper_auto_cycle": "PASS",
                "balance_reconciliation": "PASS",
                "probe_order": "PASS",
                "order_history_requery": "PASS",
            },
            "stages": {
                "paper_auto_cycle": {
                    "status": "PASS",
                    "stages": {
                        "active_model_guard": {"bundle_id": bundle_id},
                        "cycles": {
                            "items": [{
                                "status": "PASS",
                                "order_history_verification": {
                                    "status": "PASS",
                                    "queries": [{"matched_order_count": 1}],
                                },
                            }],
                        },
                    },
                },
            },
        },
    )

    evidence = service_readiness_status._broker_evidence_state(tmp_path, bundle_id)

    assert evidence["status"] == "BLOCKED"
    assert evidence["external_kis_api"] is False


def test_service_status_blocks_paper_trading_only_even_if_history_pass(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bundle_id = "BUNDLE-TEST"
    monkeypatch.setattr(
        service_readiness_status,
        "_service_policy_gate_pass",
        lambda backtest, bundle_id, **kwargs: True,
    )
    _write_json(
        tmp_path / "artifacts/lgbm/registry.json",
        {"active_version": None, "versions": []},
    )
    _write_json(
        tmp_path / "artifacts/lgbm_paper/registry.json",
        {"active_version": "paper-v1", "versions": [{"version": "paper-v1"}]},
    )
    _write_json(
        tmp_path / f"artifacts/reports/backtest/backtest_{bundle_id}_20260514.json",
        {
            "bundle_id": bundle_id,
            "verdict": "pass",
            "regression_risk": {"flagged": False, "severity": "low"},
            "minute_bar_leakage_check": {"verdict": "pass"},
            "feature_quality": {
                "dual_source_rows": 10,
                "dual_source_non_neutral_rows": 10,
                "exogenous_rows": 10,
                "exogenous_non_neutral_rows": 10,
            },
            "service_policy_replay": {"status": "PASS"},
            "candidate_model_metadata": _final_dataset_metadata(),
        },
    )
    _write_json(
        tmp_path
        / "artifacts/reports/paper_trading/paper_trading_balance_reconciliation_20260514.json",
        {
            "status": "PASS",
            "runtime": {"kis_mode": "virtual"},
            "stages": {"balance": {"status": "PASS", "_mode": "virtual"}},
        },
    )
    _write_json(
        tmp_path
        / "artifacts/reports/paper_trading/paper_trading_submit_probe_order_20260514.json",
        {
            "status": "PASS",
            "runtime": {"kis_mode": "virtual"},
            "stages": {
                "execution": {"status": "PASS"},
                "order_history": {
                    "status": "PASS",
                    "matched_order_count": 1,
                    "_mode": "virtual",
                },
            },
        },
    )

    payload = service_readiness_status.build_service_status(
        bundle_id=bundle_id,
        root=tmp_path,
    )

    assert payload["status"] == "PARTIAL"
    assert payload["deploy_quality"] == "PASS"
    assert payload["broker_evidence"] == "BLOCKED"
    assert payload["kis_broker_evidence"]["blocker"] == "paper_auto_bundle_evidence_missing"
    assert payload["kis_broker_evidence"]["paper_trading_evidence"]["order_history"]["status"] == "PASS"


def test_broker_evidence_prefers_external_pass_over_newer_internal_fake(
    tmp_path: Path,
) -> None:
    bundle_id = "BUNDLE-TEST"
    external_path = (
        tmp_path
        / "artifacts/reports/paper_auto_trading/paper_auto_service_rehearsal_20260515_135618.json"
    )
    internal_path = (
        tmp_path
        / "artifacts/reports/paper_auto_trading/paper_auto_service_rehearsal_20260516_065843.json"
    )
    external_payload = {
        "status": "PASS",
        "bundle_id": bundle_id,
        "external_kis_api": True,
        "evidence_level": "external_kis_virtual",
        "stage_statuses": {
            "paper_auto_cycle": "PASS",
            "balance_reconciliation": "PASS",
            "probe_order": "PASS",
            "order_history_requery": "PASS",
        },
        "stages": {
            "paper_auto_cycle": {
                "status": "PASS",
                "stages": {
                    "active_model_guard": {"bundle_id": bundle_id},
                    "cycles": {
                        "items": [{
                            "status": "PASS",
                            "order_history_verification": {
                                "status": "PASS",
                                "queries": [{"matched_order_count": 1}],
                            },
                        }],
                    },
                },
            },
        },
    }
    internal_payload = {
        **external_payload,
        "external_kis_api": False,
        "evidence_level": "internal_fake_kis_real_hot_runner",
        "stage_statuses": {
            **external_payload["stage_statuses"],
            "preflight": "BLOCKED",
        },
    }
    _write_json(external_path, external_payload)
    _write_json(internal_path, internal_payload)
    os.utime(external_path, (1000, 1000))
    os.utime(internal_path, (2000, 2000))

    evidence = service_readiness_status._broker_evidence_state(tmp_path, bundle_id)

    assert evidence["status"] == "PASS"
    assert evidence["external_kis_api"] is True
    assert evidence["evidence_level"] == "external_kis_virtual"
    assert evidence["report_path"].endswith("20260515_135618.json")


def test_probe_blocker_classifies_non_business_day_as_market_closed() -> None:
    blocker = service_readiness_status._probe_order_blocker({
        "status": "FAIL",
        "stages": {
            "execution": {
                "result": {
                    "execution_report": {
                        "rejections": [{
                            "error": (
                                "msg_cd=40100000 msg=모의투자 영업일이 아닙니다."
                            )
                        }]
                    }
                }
            }
        },
    })

    assert blocker["error_code"] == "BROKER_MARKET_CLOSED"

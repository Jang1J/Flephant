from __future__ import annotations

import json
from pathlib import Path

from src.ops import service_readiness_status


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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
        lambda backtest, bundle_id: True,
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
        lambda backtest, bundle_id: True,
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
        lambda backtest, bundle_id: True,
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

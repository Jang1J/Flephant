from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import paper_auto_preflight  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")


def _evidence() -> dict:
    return {
        "broker_env_fingerprint": "fp-test",
        "account_hash": "acct-test",
    }


def _now_iso() -> str:
    return datetime.now(_KST).isoformat()


def _runtime() -> dict:
    return {"kis_mode": "virtual", "live_enabled": False}


def _probe_execution(order_id: str = "OD-1") -> dict:
    return {
        "status": "PASS",
        "result": {
            "execution_report": {
                "fills": [{"broker_order_id": order_id}],
            },
        },
    }


def test_paper_auto_preflight_passes_when_all_narrow_gates_pass(monkeypatch) -> None:
    monkeypatch.setattr(
        paper_auto_preflight.print_env_readiness,
        "build_report",
        lambda: {"status": "PASS"},
    )
    monkeypatch.setattr(
        paper_auto_preflight.model_registry_readiness,
        "build_report",
        lambda **_: {"status": "WARN", "warnings": ["candidate_not_marked_deploy_quality"]},
    )
    monkeypatch.setattr(
        paper_auto_preflight,
        "_ops_risk",
        lambda: {"status": "PASS"},
    )
    monkeypatch.setattr(
        paper_auto_preflight,
        "_paper_evidence",
        lambda: {"status": "PASS"},
    )

    report = paper_auto_preflight.build_report(registry_dir="artifacts/lgbm_paper")

    assert report["status"] == "PASS"
    assert report["stage_statuses"]["active_registry"] == "WARN"


def test_ops_risk_treats_string_false_live_enabled_as_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        paper_auto_preflight,
        "config_load",
        lambda file_name, section: {"live_enabled": "false"} if section == "execution" else {},
    )

    report = paper_auto_preflight._ops_risk()

    assert report["status"] == "PASS"
    assert report["risk_config_live_enabled"] is False


def test_paper_auto_preflight_blocks_without_paper_evidence(monkeypatch) -> None:
    monkeypatch.setattr(
        paper_auto_preflight.print_env_readiness,
        "build_report",
        lambda: {"status": "PASS"},
    )
    monkeypatch.setattr(
        paper_auto_preflight.model_registry_readiness,
        "build_report",
        lambda **_: {"status": "WARN"},
    )
    monkeypatch.setattr(paper_auto_preflight, "_ops_risk", lambda: {"status": "PASS"})
    monkeypatch.setattr(paper_auto_preflight, "_paper_evidence", lambda: {"status": "BLOCKED"})

    report = paper_auto_preflight.build_report()

    assert report["status"] == "BLOCKED"
    assert report["blockers"] == ["paper_evidence"]


def test_paper_auto_preflight_accepts_no_write_report_flag(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        paper_auto_preflight,
        "build_report",
        lambda **_: {"status": "PASS"},
    )

    rc = paper_auto_preflight.main(["--no-write-report"])

    assert rc == 0
    assert '"status": "PASS"' in capsys.readouterr().out


def test_paper_evidence_blocks_probe_without_matched_order(monkeypatch) -> None:
    def _fake_latest(pattern: str):
        if "balance_reconciliation" in pattern:
            return {
                "status": "PASS",
                "_path": "balance.json",
                "generated_at": _now_iso(),
                "evidence": _evidence(),
                "stages": {
                    "balance": {"status": "PASS"},
                    "reconciliation": {"status": "PASS"},
                },
            }
        if "submit_probe_order" in pattern:
            return {
                "action": "submit_probe_order",
                "status": "PASS",
                "_path": "probe.json",
                "generated_at": _now_iso(),
                "runtime": _runtime(),
                "evidence": _evidence(),
                "stages": {
                    "execution": _probe_execution(),
                    "order_history": {
                        "status": "PASS",
                        "matched_order_count": 0,
                    },
                },
            }
        if "order_history" in pattern:
            return None
        return None

    monkeypatch.setattr(paper_auto_preflight, "_latest_json", _fake_latest)

    evidence = paper_auto_preflight._paper_evidence()

    assert evidence["status"] == "BLOCKED"
    assert evidence["order_history"]["status"] == "BLOCKED"
    assert evidence["order_history"]["matched_order_count"] == 0


def test_paper_evidence_accepts_matched_broker_order(monkeypatch) -> None:
    def _fake_latest(pattern: str):
        if "balance_reconciliation" in pattern:
            return {
                "status": "PASS",
                "_path": "balance.json",
                "generated_at": _now_iso(),
                "evidence": _evidence(),
                "stages": {
                    "balance": {"status": "PASS"},
                    "reconciliation": {"status": "PASS"},
                },
            }
        if "submit_probe_order" in pattern:
            return {
                "action": "submit_probe_order",
                "status": "PASS",
                "_path": "probe.json",
                "generated_at": _now_iso(),
                "runtime": _runtime(),
                "evidence": _evidence(),
                "stages": {
                    "execution": _probe_execution(),
                    "order_history": {
                        "status": "PASS",
                        "matched_order_count": 1,
                    },
                },
            }
        if "order_history" in pattern:
            return None
        return None

    monkeypatch.setattr(paper_auto_preflight, "_latest_json", _fake_latest)

    evidence = paper_auto_preflight._paper_evidence()

    assert evidence["status"] == "PASS"
    assert evidence["order_history"]["matched_order_count"] == 1


def test_paper_evidence_reports_market_closed_probe_blocker(monkeypatch) -> None:
    def _fake_latest(pattern: str):
        if "balance_reconciliation" in pattern:
            return {
                "status": "PASS",
                "_path": "balance.json",
                "generated_at": _now_iso(),
                "evidence": _evidence(),
                "stages": {
                    "balance": {"status": "PASS"},
                    "reconciliation": {"status": "PASS"},
                },
            }
        if "submit_probe_order" in pattern:
            return {
                "action": "submit_probe_order",
                "status": "FAIL",
                "_path": "probe.json",
                "generated_at": _now_iso(),
                "runtime": _runtime(),
                "evidence": _evidence(),
                "stages": {
                    "execution": {
                        "result": {
                            "execution_report": {
                                "rejections": [{
                                    "error": (
                                        "[kis_rest] KIS API 오류 "
                                        "msg_cd=40580000 msg=모의투자 장종료 입니다."
                                    )
                                }],
                            },
                        },
                    },
                    "order_history": {
                        "status": "PASS",
                        "matched_order_count": 0,
                    },
                },
            }
        if "order_history" in pattern:
            return None
        return None

    monkeypatch.setattr(paper_auto_preflight, "_latest_json", _fake_latest)

    evidence = paper_auto_preflight._paper_evidence()

    assert evidence["status"] == "BLOCKED"
    assert evidence["probe_order"]["blocker"]["error_code"] == "BROKER_MARKET_CLOSED"


def test_paper_evidence_reports_non_business_day_probe_blocker(monkeypatch) -> None:
    def _fake_latest(pattern: str):
        if "balance_reconciliation" in pattern:
            return {
                "status": "PASS",
                "_path": "balance.json",
                "generated_at": _now_iso(),
                "evidence": _evidence(),
                "stages": {
                    "balance": {"status": "PASS"},
                    "reconciliation": {"status": "PASS"},
                },
            }
        if "submit_probe_order" in pattern:
            return {
                "action": "submit_probe_order",
                "status": "FAIL",
                "_path": "probe.json",
                "generated_at": _now_iso(),
                "runtime": _runtime(),
                "evidence": _evidence(),
                "stages": {
                    "execution": {
                        "result": {
                            "execution_report": {
                                "rejections": [{
                                    "error": (
                                        "[kis_rest] KIS API 오류 "
                                        "msg_cd=40100000 msg=모의투자 영업일이 아닙니다."
                                    )
                                }],
                            },
                        },
                    },
                    "order_history": {
                        "status": "PASS",
                        "matched_order_count": 0,
                    },
                },
            }
        if "order_history" in pattern:
            return None
        return None

    monkeypatch.setattr(paper_auto_preflight, "_latest_json", _fake_latest)

    evidence = paper_auto_preflight._paper_evidence()

    assert evidence["status"] == "BLOCKED"
    assert evidence["probe_order"]["blocker"]["error_code"] == "BROKER_MARKET_CLOSED"


def test_paper_evidence_blocks_mismatched_broker_fingerprint(monkeypatch) -> None:
    def _fake_latest(pattern: str):
        if "balance_reconciliation" in pattern:
            return {
                "status": "PASS",
                "_path": "balance.json",
                "generated_at": _now_iso(),
                "evidence": {"broker_env_fingerprint": "fp-a"},
                "stages": {
                    "balance": {"status": "PASS"},
                    "reconciliation": {"status": "PASS"},
                },
            }
        if "submit_probe_order" in pattern:
            return {
                "action": "submit_probe_order",
                "status": "PASS",
                "_path": "probe.json",
                "generated_at": _now_iso(),
                "runtime": _runtime(),
                "evidence": {"broker_env_fingerprint": "fp-b"},
                "stages": {
                    "execution": _probe_execution(),
                    "order_history": {
                        "status": "PASS",
                        "matched_order_count": 1,
                    },
                },
            }
        if "order_history" in pattern:
            return None
        return None

    monkeypatch.setattr(paper_auto_preflight, "_latest_json", _fake_latest)

    evidence = paper_auto_preflight._paper_evidence()

    assert evidence["status"] == "BLOCKED"
    assert evidence["evidence_guard"]["reason"] == "broker_env_fingerprint_mismatch"

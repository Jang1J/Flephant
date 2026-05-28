from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.ops.paper_order_path_evidence import (
    find_fresh_paper_order_path_evidence,
    paper_auto_order_path_evidence_from_report,
)

_KST = ZoneInfo("Asia/Seoul")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _paper_auto_report(*, bundle_id: str = "BUNDLE-TEST", matched: int = 1) -> dict:
    return {
        "status": "PASS",
        "action": "paper_auto_trade",
        "generated_at": datetime.now(_KST).isoformat(),
        "runtime": {
            "kis_mode": "virtual",
            "execution_mode": "paper",
            "live_enabled": False,
            "broker_submit_enabled": True,
            "shadow_only": False,
        },
        "params": {"required_bundle_id": bundle_id},
        "evidence": {"broker_env_fingerprint": "fp-test"},
        "stages": {
            "cycles": {
                "status": "PASS",
                "items": [{
                    "status": "PASS",
                    "broker_order_submitted": True,
                    "hot_path_bar_readiness": {"status": "PASS"},
                    "execution": {
                        "execution_report": {
                            "status": "submitted",
                            "fills": [{"broker_order_id": "OD-1", "ticker": "005930"}],
                        },
                    },
                    "order_history_verification": {
                        "status": "PASS",
                        "queries": [{
                            "query": {"order_id": "OD-1"},
                            "status": "PASS" if matched > 0 else "FAIL",
                            "matched_order_count": matched,
                            "matched_orders": [{"broker_order_id": "OD-1"}] if matched > 0 else [],
                        }],
                    },
                }],
            },
        },
    }


def test_order_path_accepts_paper_auto_order_history_without_probe(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "artifacts/reports/paper_auto_trading/MAIN/paper_auto_trade_20260529.json",
        _paper_auto_report(),
    )

    result = find_fresh_paper_order_path_evidence(root=tmp_path, bundle_id="BUNDLE-TEST")

    assert result["status"] == "PASS"
    assert result["evidence_type"] == "paper_auto_order"
    assert result["broker_order_id_count"] == 1
    assert result["matched_order_count"] == 1


def test_order_path_blocks_no_submit_report(tmp_path: Path) -> None:
    report = _paper_auto_report()
    item = report["stages"]["cycles"]["items"][0]
    item["broker_order_submitted"] = False
    item["execution"] = {"execution_report": {"status": "SKIP", "fills": []}}
    item["order_history_verification"] = {"status": "SKIP", "queries": []}
    _write_json(
        tmp_path / "artifacts/reports/paper_auto_trading/MAIN/paper_auto_trade_20260529.json",
        report,
    )

    result = find_fresh_paper_order_path_evidence(root=tmp_path, bundle_id="BUNDLE-TEST")

    assert result["status"] == "BLOCKED"
    assert result["broker_order_id_count"] == 0


def test_order_path_blocks_matched_zero(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "artifacts/reports/paper_auto_trading/MAIN/paper_auto_trade_20260529.json",
        _paper_auto_report(matched=0),
    )

    result = find_fresh_paper_order_path_evidence(root=tmp_path, bundle_id="BUNDLE-TEST")

    assert result["status"] == "BLOCKED"
    assert result["inspected_reports"][0]["failures"]


def test_order_path_blocks_matched_count_without_verified_order_id(tmp_path: Path) -> None:
    report = _paper_auto_report()
    query = report["stages"]["cycles"]["items"][0]["order_history_verification"]["queries"][0]
    query["query"] = {"ticker": "005930"}
    query["matched_orders"] = []

    result = paper_auto_order_path_evidence_from_report(
        report,
        report_path=tmp_path / "artifacts/reports/paper_auto_trading/MAIN/paper_auto_trade_20260529.json",
        root=tmp_path,
        bundle_id="BUNDLE-TEST",
    )

    assert result["status"] == "BLOCKED"
    assert result["unmatched_order_count"] == 1
    assert any(failure["reason"] == "submitted_order_unmatched" for failure in result["failures"])


def test_order_path_blocks_stale_report(tmp_path: Path) -> None:
    report = _paper_auto_report()
    report["generated_at"] = (datetime.now(_KST) - timedelta(days=10)).isoformat()
    _write_json(
        tmp_path / "artifacts/reports/paper_auto_trading/MAIN/paper_auto_trade_20260529.json",
        report,
    )

    result = find_fresh_paper_order_path_evidence(root=tmp_path, bundle_id="BUNDLE-TEST")

    assert result["status"] == "BLOCKED"
    assert result["inspected_reports"][0]["failures"]


def test_order_path_blocks_shadow_report(tmp_path: Path) -> None:
    report = _paper_auto_report()
    report["runtime"]["broker_submit_enabled"] = False
    report["runtime"]["shadow_only"] = True
    _write_json(
        tmp_path / "artifacts/reports/paper_auto_trading/MAIN/paper_auto_trade_20260529.json",
        report,
    )

    result = find_fresh_paper_order_path_evidence(root=tmp_path, bundle_id="BUNDLE-TEST")

    assert result["status"] == "BLOCKED"

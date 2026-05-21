from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import paper_auto_service_rehearsal  # noqa: E402


def test_internal_fake_kis_rehearsal_runs_cycle_even_if_external_preflight_blocked(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        paper_auto_service_rehearsal.paper_auto_preflight,
        "build_report",
        lambda registry_dir=None: {"status": "BLOCKED", "blockers": ["external_kis"]},
    )
    args = argparse.Namespace(
        internal_fake_kis=True,
        tickers="005930",
        cycles=1,
        interval_sec=0.0,
        registry_dir=None,
        confirm_phrase="PAPER_AUTO_OK",
        no_write_report=True,
    )

    report = paper_auto_service_rehearsal.build_report(args)

    assert report["status"] == "PASS"
    assert report["evidence_level"] == "internal_fake_kis"
    cycle = report["stages"]["paper_auto_cycle"]["stages"]["cycles"]["items"][0]
    assert cycle["order_history_verification"]["status"] == "PASS"


def test_external_rehearsal_skips_cycle_when_preflight_blocked(monkeypatch) -> None:
    monkeypatch.setattr(
        paper_auto_service_rehearsal.paper_auto_preflight,
        "build_report",
        lambda registry_dir=None: {"status": "BLOCKED", "blockers": ["paper_evidence"]},
    )
    args = argparse.Namespace(
        internal_fake_kis=False,
        bundle_id="BUNDLE-TEST",
        tickers="005930",
        cycles=1,
        interval_sec=0.0,
        registry_dir=None,
        confirm_phrase="PAPER_AUTO_OK",
        no_write_report=True,
    )

    report = paper_auto_service_rehearsal.build_report(args)

    assert report["status"] == "BLOCKED"
    assert report["bundle_id"] == "BUNDLE-TEST"
    assert report["model_bundle_id"] is None
    assert report["stages"]["paper_auto_cycle"]["status"] == "SKIP"


def test_external_rehearsal_flattens_broker_evidence_statuses(monkeypatch) -> None:
    monkeypatch.setattr(
        paper_auto_service_rehearsal.paper_auto_preflight,
        "build_report",
        lambda registry_dir=None: {
            "status": "PASS",
            "stages": {
                "paper_evidence": {
                    "balance_reconciliation": {"status": "PASS"},
                    "probe_order": {"status": "PASS"},
                    "order_history": {"status": "PASS", "matched_order_count": 1},
                },
            },
        },
    )

    class _FakeTrader:
        def run(self, **kwargs):
            return {"status": "PASS", "stages": {"cycles": {"items": []}}}

    monkeypatch.setattr(
        paper_auto_service_rehearsal,
        "PaperAutoTrader",
        lambda **kwargs: _FakeTrader(),
    )
    args = argparse.Namespace(
        internal_fake_kis=False,
        tickers="005930",
        cycles=1,
        interval_sec=0.0,
        registry_dir=None,
        confirm_phrase="PAPER_AUTO_OK",
        no_write_report=True,
    )

    report = paper_auto_service_rehearsal.build_report(args)

    assert report["status"] == "PASS"
    assert report["external_kis_api"] is True
    assert report["stage_statuses"]["balance_reconciliation"] == "PASS"
    assert report["stage_statuses"]["probe_order"] == "PASS"
    assert report["stage_statuses"]["order_history_requery"] == "PASS"


def test_external_rehearsal_converts_trader_exception_to_fail_closed_stage(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        paper_auto_service_rehearsal.paper_auto_preflight,
        "build_report",
        lambda registry_dir=None: {
            "status": "PASS",
            "stages": {
                "paper_evidence": {
                    "balance_reconciliation": {"status": "PASS"},
                    "probe_order": {"status": "PASS"},
                    "order_history": {"status": "PASS", "matched_order_count": 1},
                },
            },
        },
    )

    class _FailingTrader:
        def run(self, **kwargs):
            raise ConnectionError("dns failed")

    monkeypatch.setattr(
        paper_auto_service_rehearsal,
        "PaperAutoTrader",
        lambda **kwargs: _FailingTrader(),
    )
    args = argparse.Namespace(
        internal_fake_kis=False,
        tickers="005930",
        cycles=1,
        interval_sec=0.0,
        registry_dir=None,
        confirm_phrase="PAPER_AUTO_OK",
        no_write_report=True,
    )

    report = paper_auto_service_rehearsal.build_report(args)

    assert report["status"] == "FAIL"
    assert report["stage_statuses"]["paper_auto_cycle"] == "FAIL"
    cycle = report["stages"]["paper_auto_cycle"]
    assert cycle["reason"] == "paper_auto_cycle_exception"
    assert cycle["exception_type"] == "ConnectionError"
    assert cycle["fail_closed"] is True


def test_internal_fake_hot_runner_uses_safe_price_fallback() -> None:
    """Demo helper의 malformed price가 rehearsal 전체를 중단시키지 않는다."""
    runner = paper_auto_service_rehearsal._FakeHotRunner()

    result = runner.run_once(tickers=["005930"], latest_prices={"005930": "bad-price"})

    order = result["final_decision"]["order_deltas"][0]
    assert order["ticker"] == "005930"
    assert order["price"] == 70000.0

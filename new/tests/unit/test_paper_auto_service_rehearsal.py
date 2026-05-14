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
        tickers="005930",
        cycles=1,
        interval_sec=0.0,
        registry_dir=None,
        confirm_phrase="PAPER_AUTO_OK",
        no_write_report=True,
    )

    report = paper_auto_service_rehearsal.build_report(args)

    assert report["status"] == "BLOCKED"
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

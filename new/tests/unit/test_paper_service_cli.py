from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import paper_service_rehearsal  # noqa: E402
import paper_trading_smoke  # noqa: E402
import collect_kis_paper_evidence  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")


def _probe_pass_report() -> dict:
    return {
        "action": "submit_probe_order",
        "status": "PASS",
        "generated_at": datetime.now(_KST).isoformat(),
        "runtime": {"kis_mode": "virtual", "live_enabled": False},
        "evidence": {"broker_env_fingerprint": "fp-test"},
        "stages": {
            "execution": {
                "status": "PASS",
                "result": {
                    "execution_report": {
                        "fills": [{"broker_order_id": "OD-1"}],
                    },
                },
            },
            "order_history": {
                "status": "PASS",
                "matched_order_count": 1,
                "_mode": "virtual",
            },
        },
    }


def test_paper_trading_smoke_can_assume_empty_system_positions() -> None:
    assert paper_trading_smoke._load_system_positions(  # noqa: SLF001
        None,
        assume_empty=True,
    ) == []


def test_collect_kis_paper_evidence_loads_system_positions_json(tmp_path: Path) -> None:
    positions_path = tmp_path / "positions.json"
    positions_path.write_text(
        '{"positions": [{"ticker": "005930", "qty": 74}]}',
        encoding="utf-8",
    )

    assert collect_kis_paper_evidence._load_system_positions(  # noqa: SLF001
        str(positions_path),
    ) == [{"ticker": "005930", "qty": 74}]


def test_collect_kis_paper_evidence_rejects_ambiguous_system_positions(tmp_path: Path) -> None:
    positions_path = tmp_path / "positions.json"
    positions_path.write_text("[]", encoding="utf-8")

    try:
        collect_kis_paper_evidence._load_system_positions(  # noqa: SLF001
            str(positions_path),
            assume_empty=True,
        )
    except ValueError as exc:
        assert "mutually exclusive" in str(exc)
    else:
        raise AssertionError("expected mutually exclusive ValueError")


def test_collect_kis_paper_evidence_derives_registry_dir_from_bundle(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeRunner:
        def run_balance_reconciliation(self, system_positions=None, write_report=True):
            return {"status": "PASS"}

        def submit_probe_order(
            self,
            ticker,
            side,
            qty,
            price,
            order_type,
            confirm_phrase,
            write_report=True,
        ):
            return _probe_pass_report()

    def fake_service_rehearsal(args):
        calls["registry_dir"] = args.registry_dir
        return {"status": "PASS"}

    monkeypatch.setattr(collect_kis_paper_evidence, "PaperTradingRunner", FakeRunner)
    monkeypatch.setattr(
        collect_kis_paper_evidence.paper_auto_service_rehearsal,
        "build_report",
        fake_service_rehearsal,
    )

    report = collect_kis_paper_evidence.collect(
        argparse.Namespace(
            system_positions_json=None,
            assume_empty_system_positions=True,
            price=70000.0,
            auto_price=False,
            order_type="00",
            ticker="005930",
            side="buy",
            qty=1,
            probe_confirm_phrase="PAPER_ORDER_OK",
            auto_confirm_phrase="PAPER_AUTO_OK",
            tickers="005930",
            cycles=1,
            interval_sec=0.0,
            registry_dir="",
            cold_risk_report="",
            no_write_report=True,
            use_real_hot_runner=False,
            bundle_id="BUNDLE-TEST",
        )
    )

    assert report["status"] == "PASS"
    assert report["registry_dir"] == "artifacts/lgbm_paper_candidate/BUNDLE-TEST"
    assert calls["registry_dir"] == "artifacts/lgbm_paper_candidate/BUNDLE-TEST"


def test_collect_kis_paper_evidence_forwards_cold_risk_report(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeRunner:
        def run_balance_reconciliation(self, system_positions=None, write_report=True):
            return {"status": "PASS"}

        def submit_probe_order(
            self,
            ticker,
            side,
            qty,
            price,
            order_type,
            confirm_phrase,
            write_report=True,
        ):
            return _probe_pass_report()

    def fake_service_rehearsal(args):
        calls["cold_risk_report"] = args.cold_risk_report
        calls["tickers"] = args.tickers
        calls["bundle_id"] = args.bundle_id
        return {"status": "PASS"}

    monkeypatch.setattr(collect_kis_paper_evidence, "PaperTradingRunner", FakeRunner)
    monkeypatch.setattr(
        collect_kis_paper_evidence.paper_auto_service_rehearsal,
        "build_report",
        fake_service_rehearsal,
    )

    report = collect_kis_paper_evidence.collect(
        argparse.Namespace(
            system_positions_json=None,
            assume_empty_system_positions=False,
            price=70000.0,
            auto_price=False,
            order_type="00",
            ticker="005930",
            side="buy",
            qty=1,
            probe_confirm_phrase="PAPER_ORDER_OK",
            auto_confirm_phrase="PAPER_AUTO_OK",
            tickers="005930,000660",
            cycles=1,
            interval_sec=0.0,
            registry_dir="artifacts/lgbm_paper_candidate/BUNDLE-TEST",
            cold_risk_report="artifacts/reports/community_live_risk/example.json",
            no_write_report=True,
            use_real_hot_runner=False,
            bundle_id="BUNDLE-TEST",
        )
    )

    assert report["status"] == "PASS"
    assert calls["cold_risk_report"] == "artifacts/reports/community_live_risk/example.json"
    assert calls["tickers"] == "005930,000660"
    assert calls["bundle_id"] == "BUNDLE-TEST"


def test_collect_kis_paper_evidence_skips_probe_when_order_path_is_fresh(
    monkeypatch,
) -> None:
    calls: dict[str, int] = {"probe": 0, "service": 0}

    class FakeRunner:
        def run_balance_reconciliation(self, system_positions=None, write_report=True):
            return {"status": "PASS"}

        def submit_probe_order(
            self,
            ticker,
            side,
            qty,
            price,
            order_type,
            confirm_phrase,
            write_report=True,
        ):
            calls["probe"] += 1
            raise AssertionError("probe should be skipped when order-path evidence is fresh")

    monkeypatch.setattr(collect_kis_paper_evidence, "PaperTradingRunner", FakeRunner)
    monkeypatch.setattr(
        collect_kis_paper_evidence,
        "find_fresh_paper_order_path_evidence",
        lambda **kwargs: {
            "status": "PASS",
            "evidence_type": "paper_auto_order",
            "report_path": "artifacts/reports/paper_auto_trading/MAIN/report.json",
            "matched_order_count": 1,
        },
    )
    monkeypatch.setattr(
        collect_kis_paper_evidence.paper_auto_service_rehearsal,
        "build_report",
        lambda args: calls.__setitem__("service", calls["service"] + 1)
        or {"status": "PASS"},
    )

    report = collect_kis_paper_evidence.collect(
        argparse.Namespace(
            system_positions_json=None,
            assume_empty_system_positions=False,
            price=70000.0,
            auto_price=False,
            order_type="00",
            ticker="005930",
            side="buy",
            qty=1,
            probe_confirm_phrase="PAPER_ORDER_OK",
            probe_mode="if-no-fresh-order-evidence",
            auto_confirm_phrase="PAPER_AUTO_OK",
            tickers="005930",
            cycles=1,
            interval_sec=0.0,
            registry_dir="artifacts/lgbm_paper_candidate/BUNDLE-TEST",
            cold_risk_report="",
            no_write_report=True,
            use_real_hot_runner=False,
            bundle_id="BUNDLE-TEST",
        )
    )

    assert report["status"] == "PASS"
    assert calls == {"probe": 0, "service": 1}
    assert report["probe_policy"]["probe_submitted"] is False
    assert report["probe_policy"]["skip_reason"] == "fresh_paper_order_path_evidence_found"
    assert report["stage_statuses"]["probe_order"] == "SKIP"
    assert report["stage_statuses"]["paper_order_path"] == "PASS"


def test_collect_kis_paper_evidence_probe_mode_never_blocks_without_order_path(
    monkeypatch,
) -> None:
    calls: dict[str, int] = {"probe": 0, "service": 0}

    class FakeRunner:
        def run_balance_reconciliation(self, system_positions=None, write_report=True):
            return {"status": "PASS"}

        def submit_probe_order(
            self,
            ticker,
            side,
            qty,
            price,
            order_type,
            confirm_phrase,
            write_report=True,
        ):
            calls["probe"] += 1
            raise AssertionError("probe-mode never must not submit a probe")

    monkeypatch.setattr(collect_kis_paper_evidence, "PaperTradingRunner", FakeRunner)
    monkeypatch.setattr(
        collect_kis_paper_evidence,
        "find_fresh_paper_order_path_evidence",
        lambda **kwargs: {
            "status": "BLOCKED",
            "reason": "paper_order_path_evidence_missing",
            "matched_order_count": 0,
        },
    )
    monkeypatch.setattr(
        collect_kis_paper_evidence.paper_auto_service_rehearsal,
        "build_report",
        lambda args: calls.__setitem__("service", calls["service"] + 1)
        or {"status": "PASS"},
    )

    report = collect_kis_paper_evidence.collect(
        argparse.Namespace(
            system_positions_json=None,
            assume_empty_system_positions=False,
            price=70000.0,
            auto_price=False,
            order_type="00",
            ticker="005930",
            side="buy",
            qty=1,
            probe_confirm_phrase="PAPER_ORDER_OK",
            probe_mode="never",
            auto_confirm_phrase="PAPER_AUTO_OK",
            tickers="005930",
            cycles=1,
            interval_sec=0.0,
            registry_dir="artifacts/lgbm_paper_candidate/BUNDLE-TEST",
            cold_risk_report="",
            no_write_report=True,
            use_real_hot_runner=False,
            bundle_id="BUNDLE-TEST",
        )
    )

    assert report["status"] == "BLOCKED"
    assert calls == {"probe": 0, "service": 0}
    assert report["probe_policy"]["probe_submitted"] is False
    assert (
        report["probe_policy"]["skip_reason"]
        == "probe_mode_never_order_path_evidence_missing"
    )
    assert report["stage_statuses"]["probe_order"] == "SKIP"
    assert report["stage_statuses"]["paper_order_path"] == "BLOCKED"
    assert report["stage_statuses"]["paper_auto_service_rehearsal"] == "SKIP"
    assert "paper_order_path" in report["blockers"]


def test_collect_kis_paper_evidence_converts_service_exception_to_blocked(
    monkeypatch,
) -> None:
    class FakeRunner:
        def run_balance_reconciliation(self, system_positions=None, write_report=True):
            return {"status": "PASS"}

        def submit_probe_order(
            self,
            ticker,
            side,
            qty,
            price,
            order_type,
            confirm_phrase,
            write_report=True,
        ):
            return _probe_pass_report()

    def fail_service_rehearsal(args):
        raise ConnectionError("dns failed")

    monkeypatch.setattr(collect_kis_paper_evidence, "PaperTradingRunner", FakeRunner)
    monkeypatch.setattr(
        collect_kis_paper_evidence.paper_auto_service_rehearsal,
        "build_report",
        fail_service_rehearsal,
    )

    report = collect_kis_paper_evidence.collect(
        argparse.Namespace(
            system_positions_json=None,
            assume_empty_system_positions=False,
            price=70000.0,
            auto_price=False,
            order_type="00",
            ticker="005930",
            side="buy",
            qty=1,
            probe_confirm_phrase="PAPER_ORDER_OK",
            auto_confirm_phrase="PAPER_AUTO_OK",
            tickers="005930",
            cycles=1,
            interval_sec=0.0,
            registry_dir="artifacts/lgbm_paper_candidate/BUNDLE-TEST",
            cold_risk_report="artifacts/reports/community_live_risk/example.json",
            no_write_report=True,
            use_real_hot_runner=False,
            bundle_id="BUNDLE-TEST",
        )
    )

    assert report["status"] == "BLOCKED"
    assert report["stage_statuses"]["paper_auto_service_rehearsal"] == "BLOCKED"
    service = report["stages"]["paper_auto_service_rehearsal"]
    assert service["blockers"] == ["paper_auto_service_rehearsal_exception"]
    assert service["stages"]["paper_auto_cycle"]["exception_type"] == "ConnectionError"
    assert service["stages"]["paper_auto_cycle"]["fail_closed"] is True


def test_paper_service_rehearsal_auto_price_and_empty_reconciliation(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeRunner:
        def run_balance_reconciliation(self, system_positions=None, write_report=True):
            calls["system_positions"] = system_positions
            calls["balance_write_report"] = write_report
            return {"status": "PASS"}

        def submit_probe_order(
            self,
            ticker,
            side,
            qty,
            price,
            order_type,
            confirm_phrase,
            write_report=True,
        ):
            calls["probe_price"] = price
            calls["probe_write_report"] = write_report
            return {
                "status": "PASS",
                "stages": {
                    "execution": {
                        "result": {
                            "execution_report": {
                                "fills": [{"broker_order_id": "OD-1"}],
                            },
                        },
                    },
                },
            }

        def run_order_history(
            self,
            ticker,
            side,
            order_id,
            execution_filter,
            write_report=True,
        ):
            calls["order_id"] = order_id
            return {"status": "PASS"}

    monkeypatch.setattr(paper_service_rehearsal, "PaperTradingRunner", FakeRunner)
    monkeypatch.setattr(
        paper_service_rehearsal.print_env_readiness,
        "build_report",
        lambda: {"status": "PASS"},
    )
    monkeypatch.setattr(paper_service_rehearsal, "_auto_price", lambda ticker: 71000.0)

    report = paper_service_rehearsal.build_report(
        argparse.Namespace(
            include_probe=True,
            system_positions_json=None,
            assume_empty_system_positions=True,
            ticker="005930",
            side="buy",
            qty=1,
            price=None,
            auto_price=True,
            order_type="00",
            execution_filter="all",
            confirm_phrase="PAPER_ORDER_OK",
        )
    )

    assert report["status"] == "PASS"
    assert report["params"]["price"] == 71000.0
    assert report["params"]["price_source"] == "kis_current_price"
    assert report["params"]["system_positions_source"] == "assume_empty"
    assert calls["system_positions"] == []
    assert calls["probe_price"] == 71000.0
    assert calls["order_id"] == "OD-1"

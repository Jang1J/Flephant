"""run_backtest job CLI tests."""
from __future__ import annotations

import json

from src.jobs.run_backtest import main, run_backtest


class FakeBacktestAgent:
    def run(self, bundle_id: str) -> dict:
        return {
            "status": "PASS",
            "verdict": "pass",
            "bundle_id": bundle_id,
            "metrics": {"sr": 1.0},
        }


class FailingBacktestAgent:
    def run(self, bundle_id: str) -> dict:
        raise RuntimeError("mode guard blocked")


def test_run_backtest_writes_report(tmp_path):
    report = run_backtest(
        "BUNDLE-TEST",
        output_dir=tmp_path,
        write_report=True,
        agent=FakeBacktestAgent(),
    )

    saved_files = list(tmp_path.glob("backtest_BUNDLE-TEST_*.json"))
    assert len(saved_files) == 1
    saved = json.loads(saved_files[0].read_text(encoding="utf-8"))
    assert report["verdict"] == "pass"
    assert saved["report_path"] == str(saved_files[0])
    assert saved["bundle_id"] == "BUNDLE-TEST"


def test_run_backtest_fail_closed_on_agent_error(tmp_path):
    report = run_backtest(
        "BUNDLE-TEST",
        output_dir=tmp_path,
        write_report=False,
        agent=FailingBacktestAgent(),
    )

    assert "status" not in report
    assert report["verdict"] == "fail"
    assert report["error_type"] == "RuntimeError"
    assert report["backtest_id"].startswith("BT-")
    assert set(report["metrics"]) == {"ic", "icir", "rank_ic", "arr", "ir", "mdd", "sr"}
    assert report["regime_breakdown"] == []
    assert report["ablation"] == []
    assert report["regression_risk"]["flagged"] is True
    assert report["regression_risk"]["severity"] == "high"
    assert report["llm_reasoning_ref"] == ""
    assert report["failure_case_cards"] == []
    assert report["regression_cases"] == []
    assert report["minute_bar_leakage_check"]["verdict"] == "fail"


def test_main_returns_zero_only_for_pass(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    monkeypatch.setattr(
        "src.jobs.run_backtest.BacktestAgent",
        lambda: FailingBacktestAgent(),
    )
    rc = main([
        "--bundle-id",
        "BUNDLE-NO-SUCH",
        "--output-dir",
        str(tmp_path),
    ])

    captured = capsys.readouterr()
    assert rc == 1
    assert "BUNDLE-NO-SUCH" in captured.out


def test_main_blocks_without_mode_b(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ELEPHANT_MODE", raising=False)

    rc = main([
        "--bundle-id",
        "BUNDLE-TEST",
        "--output-dir",
        str(tmp_path),
        "--no-write-report",
    ])

    captured = capsys.readouterr()
    assert rc == 2
    assert "ELEPHANT_MODE=mode_b" in captured.err

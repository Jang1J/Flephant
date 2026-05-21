"""Post-close job store tests."""
from __future__ import annotations

from pathlib import Path

from src.ops.post_close_job_store import PostCloseJobStore


def test_job_store_records_and_reads_latest_run(tmp_path: Path):
    store = PostCloseJobStore(tmp_path / "jobs.sqlite3")
    try:
        run_id = store.create_run(
            target_end_date="20260520",
            bundle_id="BUNDLE-20260520-POSTCLOSE",
            dry_run=False,
            run_prelive=True,
            decision={"reason": "due"},
        )
        store.finish_run(
            run_id,
            status="PASS",
            report_path="artifacts/reports/post_close_data_update/test.json",
            blockers=[],
            update_report={"status": "PASS", "target_end_date": "20260520"},
            registry_mutated=False,
            live_trading_allowed=False,
        )

        latest = store.latest_run("20260520")

        assert latest is not None
        assert latest["run_id"] == run_id
        assert latest["status"] == "PASS"
        assert latest["blockers"] == []
        assert latest["registry_mutated"] is False
        assert latest["live_trading_allowed"] is False
        assert store.has_passed_target("20260520") is True
    finally:
        store.close()


def test_job_store_keeps_latest_attempt_per_target(tmp_path: Path):
    store = PostCloseJobStore(tmp_path / "jobs.sqlite3")
    try:
        first = store.create_run(
            target_end_date="20260520",
            bundle_id="BUNDLE-A",
            dry_run=False,
            run_prelive=True,
            decision={"reason": "due"},
        )
        store.finish_run(
            first,
            status="BLOCKED",
            report_path=None,
            blockers=["live_data_readiness"],
            update_report={"status": "BLOCKED"},
            registry_mutated=False,
            live_trading_allowed=False,
        )
        second = store.create_run(
            target_end_date="20260520",
            bundle_id="BUNDLE-B",
            dry_run=False,
            run_prelive=True,
            decision={"reason": "retry"},
        )

        latest = store.latest_run("20260520")

        assert latest is not None
        assert latest["run_id"] == second
        assert latest["status"] == "RUNNING"
    finally:
        store.close()

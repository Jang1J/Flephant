"""Post-close paper-auto daily summary tests."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_script(name: str):
    script_path = Path(__file__).resolve().parents[2] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_report(
    path: Path,
    *,
    track_id: str,
    cycles: int,
    bundle_id: str = "BUNDLE-20260521-POSTCLOSE",
) -> None:
    cycle = {
        "status": "PASS",
        "hot_result": {
            "quant_output": {
                "mode": "active",
                "scores": {"005930": 0.2, "000660": 0.1},
            },
        },
        "final_decision": {
            "reason_code": "NORMAL_APPROVE",
            "order_deltas": [{"ticker": "005930", "qty_delta": 1}],
        },
        "execution": {
            "execution_report": {
                "status": "submitted",
                "fills": [{"ticker": "005930", "filled_qty": 1}],
                "rejections": [],
            },
        },
    }
    payload = {
        "status": "PASS",
        "generated_at": "2026-05-26T10:00:00+09:00",
        "params": {
            "track_id": track_id,
            "policy_hash": f"{track_id}-HASH",
            "required_bundle_id": bundle_id,
            "cycles": cycles,
        },
        "cycles": [cycle for _ in range(cycles)],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def test_daily_summary_excludes_smoke_and_empty_track_from_ab(tmp_path):
    mod = _load_script("summarize_paper_auto_day")
    _write_report(
        tmp_path / "MAIN_BASELINE" / "paper_auto_trade_20260526_100000.json",
        track_id="MAIN_BASELINE",
        cycles=3,
    )
    _write_report(
        tmp_path / "paper_auto_trade_20260526_092238.json",
        track_id="",
        cycles=1,
    )
    _write_report(
        tmp_path / "ACTIVE_SMALL_SMOKE" / "paper_auto_trade_20260526_104044.json",
        track_id="ACTIVE_SMALL_SMOKE",
        cycles=1,
    )

    summary = mod.build_summary(
        generated_date="20260526",
        bundle_id="BUNDLE-20260521-POSTCLOSE",
        report_root=tmp_path,
    )

    assert summary["status"] == "PASS"
    assert summary["interpretation"]["evidence_scope"] == "single_track_runtime"
    assert summary["interpretation"]["ab_comparison_valid"] is False
    assert summary["interpretation"]["broker_tracks"] == ["MAIN_BASELINE"]
    assert "not_ab_comparison" in summary["warnings"]
    assert "hot_path_bar_readiness_missing_legacy_report" in summary["warnings"]
    assert summary["totals"]["submitted_order_delta_count"] == 5
    assert summary["totals"]["hot_path_bar_readiness_missing_cycles"] == 5
    main_report = next(
        item for item in summary["reports"] if item["track_id"] == "MAIN_BASELINE"
    )
    assert main_report["submitted_order_delta_count"] == 3
    assert main_report["submitted_count_source_counts"] == {
        "legacy_execution_report_fills_rejections": 3
    }


def test_daily_summary_filters_reports_by_bundle_id(tmp_path):
    mod = _load_script("summarize_paper_auto_day")
    _write_report(
        tmp_path / "MAIN_BASELINE" / "paper_auto_trade_20260526_100000.json",
        track_id="MAIN_BASELINE",
        cycles=3,
    )
    _write_report(
        tmp_path / "OTHER_BUNDLE" / "paper_auto_trade_20260526_100100.json",
        track_id="OTHER_BUNDLE",
        cycles=3,
        bundle_id="BUNDLE-OTHER",
    )

    summary = mod.build_summary(
        generated_date="20260526",
        bundle_id="BUNDLE-20260521-POSTCLOSE",
        report_root=tmp_path,
    )

    assert summary["status"] == "PASS"
    assert summary["totals"]["raw_report_count"] == 2
    assert summary["totals"]["report_count"] == 1
    assert summary["totals"]["skipped_bundle_mismatch_count"] == 1
    assert summary["skipped_bundle_mismatch_reports"] == [{
        "path": str(
            tmp_path
            / "OTHER_BUNDLE"
            / "paper_auto_trade_20260526_100100.json"
        ),
        "required_bundle_id": "BUNDLE-OTHER",
    }]
    assert summary["interpretation"]["broker_tracks"] == ["MAIN_BASELINE"]

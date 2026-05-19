"""Cold Path risk E2E smoke script tests."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_script():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "cold_path_risk_e2e_smoke.py"
    spec = importlib.util.spec_from_file_location("cold_path_risk_e2e_smoke", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def _community_report() -> dict:
    return {
        "status": "PASS",
        "generated_at": "2026-05-18T17:03:06+09:00",
        "is_mock": False,
        "internal_fake_naver": False,
        "provider_coverage": {
            "requested": ["cafearticle", "blog"],
            "observed": {"cafearticle": 50, "blog": 0},
            "missing_requested_providers": ["blog"],
            "status": "WARN",
        },
        "metrics": {
            "raw_post_count": 50,
            "valid_event_count": 50,
            "message_pool_publish_count": 50,
            "risk_warning_count": 50,
        },
        "community_scores": {
            "005930": {
                "comm_score": -0.2,
                "post_count": 10,
                "timestamp": "2026-05-18T17:03:06+09:00",
            }
        },
    }


def _datalab_report() -> dict:
    return {
        "status": "PASS",
        "is_mock": False,
        "internal_fake_naver": False,
        "ratio_is_relative": True,
        "metrics": {
            "attention_ratio_rows": 4,
            "latest_ratio_by_ticker": {"005930": 15.0},
        },
        "rows": [
            {"ticker": "005930", "period": "2026-05-15", "ratio": 10.0},
            {"ticker": "005930", "period": "2026-05-16", "ratio": 12.0},
            {"ticker": "005930", "period": "2026-05-17", "ratio": 70.0},
            {"ticker": "005930", "period": "2026-05-18", "ratio": 15.0},
        ],
    }


def _source_scope_report() -> dict:
    return {
        "status": "PASS",
        "dual_source_history": {
            "status": "PASS",
            "community_event_count": 0,
            "community_non_neutral_rate": 0.0,
        },
        "cold_path": {
            "community_live_risk_sidecar": True,
        },
        "caveats": [
            "Historical community_event_count is zero in the current deploy-quality archive."
        ],
    }


def test_cold_path_risk_e2e_runs_full_agent_chain(tmp_path):
    script = _load_script()
    community_path = _write_json(tmp_path / "community.json", _community_report())
    datalab_path = _write_json(tmp_path / "datalab.json", _datalab_report())
    source_scope_path = _write_json(tmp_path / "source_scope.json", _source_scope_report())

    report = script.run_cold_path_risk_e2e_smoke(
        ticker="005930",
        community_report_path=str(community_path),
        datalab_report_path=str(datalab_path),
        source_scope_path=str(source_scope_path),
        output_dir=tmp_path,
        write_report=False,
    )

    assert report["status"] == "PASS"
    assert report["checks"]["community_real_ready"] is True
    assert report["checks"]["datalab_real_ready"] is True
    assert report["checks"]["risk_fast_stress_triggered"] is True
    assert report["checks"]["uncertainty_signal_published"] is True
    assert report["checks"]["risk_slow_executed"] is True
    assert report["checks"]["debate_conflict_detected"] is True
    assert report["checks"]["fda_vetoed_by_risk"] is True
    assert report["checks"]["fda_reason_dual_source"] is True
    assert report["checks"]["external_api_called"] is False
    assert report["agent_results"]["fda"]["final_decision"]["reason_code"] == "NEWS_COMMUNITY_DIVERGENCE"
    assert report["agent_results"]["message_pool_counts"]["final_decision"] == 1
    assert "news_comm_divergence_strong" in report["agent_results"]["risk_fast_stress"]["triggered_rules"]


def test_cold_path_risk_e2e_blocks_without_datalab_rows(tmp_path):
    script = _load_script()
    community_path = _write_json(tmp_path / "community.json", _community_report())
    datalab = _datalab_report()
    datalab["metrics"]["attention_ratio_rows"] = 0
    datalab["rows"] = []
    datalab_path = _write_json(tmp_path / "datalab.json", datalab)
    source_scope_path = _write_json(tmp_path / "source_scope.json", _source_scope_report())

    report = script.run_cold_path_risk_e2e_smoke(
        ticker="005930",
        community_report_path=str(community_path),
        datalab_report_path=str(datalab_path),
        source_scope_path=str(source_scope_path),
        output_dir=tmp_path,
        write_report=False,
    )

    assert report["status"] == "BLOCKED"
    assert "datalab_attention_not_ready" in report["blockers"]
    assert report["checks"]["datalab_real_ready"] is False

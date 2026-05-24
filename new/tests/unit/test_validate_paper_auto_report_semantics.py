from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_validator_module():
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "validate_paper_auto_report_semantics.py"
    )
    spec = importlib.util.spec_from_file_location(
        "validate_paper_auto_report_semantics",
        script_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_paper_auto_semantics_blocks_pass_with_zero_scores(tmp_path, monkeypatch) -> None:
    validator = _load_validator_module()
    repo_root = tmp_path
    report_dir = repo_root / "artifacts" / "reports" / "paper_auto_trading"
    report_dir.mkdir(parents=True)
    (report_dir / "paper_auto_trade_20260522_144359.json").write_text(
        """
{
  "status": "PASS",
  "generated_at": "2026-05-22T13:18:13+09:00",
  "params": {"required_bundle_id": "BUNDLE-TEST"},
  "stages": {
    "cycles": {
      "status": "PASS",
      "items": [
        {
          "status": "PASS",
          "order_guard": {"status": "SKIP", "reason": "no_order_deltas"},
          "execution": null,
          "hot_result": {
            "quant_output": {"mode": "warmup", "scores": {}},
            "final_decision": {"order_deltas": []}
          }
        }
      ]
    }
  }
}
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(validator, "REPO_ROOT", repo_root)

    report = validator.build_report(bundle_id="BUNDLE-TEST")

    assert report["status"] == "BLOCKED"
    assert report["failure_count"] == 1
    blockers = report["failures"][0]["blockers"]
    assert "pass_with_zero_quant_scores" in blockers
    assert "pass_with_only_no_order_delta_cycles" in blockers


def test_paper_auto_semantics_passes_report_with_rankable_execution(
    tmp_path,
    monkeypatch,
) -> None:
    validator = _load_validator_module()
    repo_root = tmp_path
    report_dir = repo_root / "artifacts" / "reports" / "paper_auto_trading"
    report_dir.mkdir(parents=True)
    (report_dir / "paper_auto_trade_20260522_090203.json").write_text(
        """
{
  "status": "PASS",
  "generated_at": "2026-05-22T09:02:03+09:00",
  "params": {"required_bundle_id": "BUNDLE-TEST"},
  "stages": {
    "cycles": {
      "status": "PASS",
      "items": [
        {
          "status": "PASS",
          "order_guard": {"status": "PASS"},
          "execution": {
            "execution_report": {
              "fills": [{"ticker": "005930"}],
              "rejections": []
            }
          },
          "hot_result": {
            "quant_output": {
              "mode": "active",
              "scores": {"005930": 0.1, "000660": 0.2}
            },
            "final_decision": {
              "order_deltas": [{"ticker": "005930", "side": "buy", "qty": 1}]
            }
          }
        }
      ]
    }
  }
}
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(validator, "REPO_ROOT", repo_root)

    report = validator.build_report(bundle_id="BUNDLE-TEST")

    assert report["status"] == "PASS"
    assert report["failure_count"] == 0
    assert report["reports"][0]["score_rankable_cycles"] == 1
    assert report["reports"][0]["execution_cycles"] == 1


def test_paper_auto_semantics_allows_rankable_no_rebalance_pass(
    tmp_path,
    monkeypatch,
) -> None:
    validator = _load_validator_module()
    repo_root = tmp_path
    report_dir = repo_root / "artifacts" / "reports" / "paper_auto_trading"
    report_dir.mkdir(parents=True)
    (report_dir / "paper_auto_trade_20260526_101500.json").write_text(
        """
{
  "status": "PASS",
  "generated_at": "2026-05-26T10:15:00+09:00",
  "params": {"required_bundle_id": "BUNDLE-TEST"},
  "stages": {
    "cycles": {
      "status": "PASS",
      "items": [
        {
          "status": "PASS",
          "order_guard": {"status": "SKIP", "reason": "no_order_deltas"},
          "execution": null,
          "hot_result": {
            "quant_output": {
              "mode": "active",
              "scores": {"005930": 0.1, "000660": 0.2}
            },
            "final_decision": {"order_deltas": []}
          }
        }
      ]
    }
  }
}
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(validator, "REPO_ROOT", repo_root)

    report = validator.build_report(bundle_id="BUNDLE-TEST")

    assert report["status"] == "PASS"
    assert report["failure_count"] == 0
    assert report["reports"][0]["blockers"] == []
    assert report["reports"][0]["warnings"] == [
        "pass_with_only_no_order_delta_cycles_evidence_limited"
    ]

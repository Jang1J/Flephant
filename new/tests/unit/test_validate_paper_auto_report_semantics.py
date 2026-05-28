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
    report_dir.mkdir(parents=True, exist_ok=True)
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
    report_dir.mkdir(parents=True, exist_ok=True)
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
              "status": "submitted",
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
    assert report["reports"][0]["warnings"] == [
        "hot_path_bar_readiness_missing_legacy_report"
    ]
    assert report["reports"][0]["hot_path_bar_readiness_missing_cycles"] == 1


def test_paper_auto_semantics_does_not_count_rejected_execution_as_broker_cycle(
    tmp_path,
    monkeypatch,
) -> None:
    validator = _load_validator_module()
    repo_root = tmp_path
    report_dir = repo_root / "artifacts" / "reports" / "paper_auto_trading"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "paper_auto_trade_20260526_101200.json").write_text(
        """
{
  "status": "PASS",
  "generated_at": "2026-05-26T10:12:00+09:00",
  "params": {"required_bundle_id": "BUNDLE-TEST"},
  "stages": {
    "cycles": {
      "items": [
        {
          "status": "PASS",
          "execution": {
            "execution_report": {
              "status": "rejected",
              "fills": [],
              "rejections": [{"ticker": "005930"}]
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

    assert report["status"] == "BLOCKED"
    assert report["reports"][0]["execution_cycles"] == 0
    assert report["reports"][0]["rejection_count"] == 1
    assert "pass_with_order_deltas_without_broker_execution" in report["failures"][0]["blockers"]


def test_paper_auto_semantics_allows_rankable_no_rebalance_pass(
    tmp_path,
    monkeypatch,
) -> None:
    validator = _load_validator_module()
    repo_root = tmp_path
    report_dir = repo_root / "artifacts" / "reports" / "paper_auto_trading"
    report_dir.mkdir(parents=True, exist_ok=True)
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


def test_paper_auto_semantics_does_not_count_shadow_as_broker_execution(
    tmp_path,
    monkeypatch,
) -> None:
    validator = _load_validator_module()
    repo_root = tmp_path
    report_dir = repo_root / "artifacts" / "reports" / "paper_auto_trading"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "paper_auto_trade_20260526_101600.json").write_text(
        """
{
  "status": "PASS",
  "generated_at": "2026-05-26T10:16:00+09:00",
  "params": {"required_bundle_id": "BUNDLE-TEST"},
  "stages": {
    "cycles": {
      "status": "PASS",
      "items": [
        {
          "status": "PASS",
          "order_guard": {"status": "PASS"},
          "execution": {
            "status": "NOT_SUBMITTED_SHADOW",
            "execution_report": {
              "status": "NOT_SUBMITTED_SHADOW",
              "fills": [],
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
    assert report["reports"][0]["execution_cycles"] == 0
    assert report["reports"][0]["order_delta_count"] == 1
    assert report["reports"][0]["shadow_order_delta_cycles"] == 1
    assert report["reports"][0]["warnings"] == [
        "shadow_only_order_deltas_not_broker_execution"
    ]


def test_paper_auto_semantics_allows_explained_fda_veto_no_submit(
    tmp_path,
    monkeypatch,
) -> None:
    validator = _load_validator_module()
    repo_root = tmp_path
    report_dir = repo_root / "artifacts" / "reports" / "paper_auto_trading"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "paper_auto_trade_20260528_153000.json").write_text(
        """
{
  "status": "PASS",
  "generated_at": "2026-05-28T15:30:00+09:00",
  "params": {"required_bundle_id": "BUNDLE-TEST"},
  "stages": {
    "cycles": {
      "status": "PASS",
      "items": [
        {
          "status": "SKIP",
          "order_guard": {
            "status": "SKIP",
            "safe_skip": true,
            "reason": "fda_veto"
          },
          "execution": null,
          "hot_result": {
            "quant_output": {
              "mode": "active",
              "scores": {"005930": 0.1, "000660": 0.2}
            },
            "final_decision": {
              "approved": false,
              "reason_code": "RISK_FAST_TRIGGER",
              "order_deltas": [
                {"ticker": "005930", "side": "sell", "qty": 1, "reason": "risk_reduce"}
              ]
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

    assert report["status"] == "BLOCKED"
    assert report["failure_count"] == 1
    summary = report["reports"][0]
    assert summary["explained_no_submit_order_delta_cycles"] == 1
    assert summary["unexplained_order_delta_no_broker_cycles"] == 0
    assert summary["explained_no_submit_reasons"] == {"fda_veto": 1}
    assert summary["blockers"] == [
        "pass_with_order_deltas_no_broker_execution_explained_by_guard"
    ]


def test_paper_auto_semantics_filters_by_generated_date(
    tmp_path,
    monkeypatch,
) -> None:
    validator = _load_validator_module()
    repo_root = tmp_path
    report_dir = repo_root / "artifacts" / "reports" / "paper_auto_trading"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "paper_auto_trade_20260522_144359.json").write_text(
        """
{
  "status": "PASS",
  "generated_at": "2026-05-22T13:18:13+09:00",
  "params": {"required_bundle_id": "BUNDLE-TEST"},
  "stages": {
    "cycles": {
      "items": [
        {
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
    (report_dir / "paper_auto_trade_20260526_101500.json").write_text(
        """
{
  "status": "PASS",
  "generated_at": "2026-05-26T10:15:00+09:00",
  "params": {"required_bundle_id": "BUNDLE-TEST"},
  "stages": {
    "cycles": {
      "items": [
        {
          "order_guard": {"status": "PASS"},
          "execution": {"execution_report": {"status": "submitted", "fills": [{"ticker": "005930"}]}},
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

    report = validator.build_report(
        bundle_id="BUNDLE-TEST",
        generated_date="20260526",
    )

    assert report["status"] == "PASS"
    assert report["generated_date"] == "20260526"
    assert report["report_count"] == 1
    assert report["reports"][0]["path"].endswith("paper_auto_trade_20260526_101500.json")


def test_paper_auto_semantics_recursively_includes_track_reports(
    tmp_path,
    monkeypatch,
) -> None:
    validator = _load_validator_module()
    repo_root = tmp_path
    report_dir = repo_root / "artifacts" / "reports" / "paper_auto_trading"
    nested_dir = report_dir / "MAIN_BASELINE"
    nested_dir.mkdir(parents=True, exist_ok=True)
    (nested_dir / "paper_auto_trade_20260526_115108.json").write_text(
        """
{
  "status": "PASS",
  "generated_at": "2026-05-26T11:51:08+09:00",
  "params": {"required_bundle_id": "BUNDLE-TEST"},
  "stages": {
    "cycles": {
      "items": [
        {
          "status": "PASS",
          "order_guard": {"status": "PASS"},
          "execution": {"execution_report": {"status": "submitted", "fills": [{"ticker": "005930"}]}},
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

    report = validator.build_report(
        bundle_id="BUNDLE-TEST",
        generated_date="20260526",
    )

    assert report["status"] == "PASS"
    assert report["scan_mode"] == "recursive"
    assert report["report_count"] == 1
    assert report["reports"][0]["path"].endswith(
        "MAIN_BASELINE/paper_auto_trade_20260526_115108.json"
    )


def test_paper_auto_semantics_blocks_when_generated_date_has_no_reports(
    tmp_path,
    monkeypatch,
) -> None:
    validator = _load_validator_module()
    repo_root = tmp_path
    (repo_root / "artifacts" / "reports" / "paper_auto_trading").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(validator, "REPO_ROOT", repo_root)

    report = validator.build_report(
        bundle_id="BUNDLE-TEST",
        generated_date="20260526",
    )

    assert report["status"] == "BLOCKED"
    assert report["report_count"] == 0
    assert report["failures"] == [{
        "reason": "paper_auto_report_missing",
        "bundle_id": "BUNDLE-TEST",
        "report_dir": "artifacts/reports/paper_auto_trading",
        "pattern": "paper_auto_trade_*.json",
        "generated_date": "20260526",
    }]


def test_paper_auto_semantics_cli_accepts_no_write_report(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    validator = _load_validator_module()
    repo_root = tmp_path
    (repo_root / "artifacts" / "reports" / "paper_auto_trading").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(validator, "REPO_ROOT", repo_root)

    rc = validator.main([
        "--bundle-id",
        "BUNDLE-TEST",
        "--generated-date",
        "20260526",
        "--no-write-report",
    ])

    assert rc == 1
    assert "paper_auto_report_missing" in capsys.readouterr().out

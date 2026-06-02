from __future__ import annotations

import importlib.util
import json
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


def _strict_cadence_cycle(
    *,
    cycle_index: int,
    started_at: str,
    fetch_policy: str,
    fetch_n: int,
    failed_tickers: dict[str, str] | None = None,
) -> dict:
    tickers = {
        f"{idx:06d}": {
            "fetch_policy": fetch_policy,
            "fetch_n": fetch_n,
            "reason": "ok",
        }
        for idx in range(1, 31)
    }
    return {
        "status": "PASS",
        "cycle_index": cycle_index,
        "started_at": started_at,
        "order_guard": {"status": "SKIP", "reason": "no_order_deltas"},
        "execution": None,
        "hot_path_bar_readiness": {
            "status": "PASS",
            "required_bars": 60,
            "rows_by_ticker": {ticker: 60 for ticker in tickers},
            "missing_bars_by_ticker": {},
            "latest_bar_ts_by_ticker": {},
            "future_rows": [],
            "invalid_rows": [],
            "stale_rows": [],
            "contiguity_gaps": [],
            "bar_warmup_topup": {
                "minute_bar_window_cache": {
                    "status": "PASS",
                    "reason": "ok",
                    "tickers": tickers,
                    "failed_tickers": failed_tickers or {},
                }
            },
        },
        "hot_result": {
            "quant_output": {
                "mode": "active",
                "scores": {"000001": 0.1, "000002": 0.2},
            },
            "final_decision": {"order_deltas": []},
        },
    }


def test_paper_auto_semantics_strict_cadence_passes_clean_incremental_report(
    tmp_path,
    monkeypatch,
) -> None:
    validator = _load_validator_module()
    repo_root = tmp_path
    report_dir = repo_root / "artifacts" / "reports" / "paper_auto_trading" / "DCD"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "paper_auto_trade_20260602_090300.json"
    report_path.write_text(
        json.dumps(
            {
                "status": "PASS",
                "generated_at": "2026-06-02T09:03:00+09:00",
                "runtime": {
                    "kis_mode": "virtual",
                    "live_enabled": False,
                    "broker_submit_enabled": False,
                    "shadow_only": True,
                },
                "params": {"required_bundle_id": "BUNDLE-TEST"},
                "stages": {
                    "cycles": {
                        "items": [
                            _strict_cadence_cycle(
                                cycle_index=0,
                                started_at="2026-06-02T09:00:05+09:00",
                                fetch_policy="cold",
                                fetch_n=61,
                            ),
                            _strict_cadence_cycle(
                                cycle_index=1,
                                started_at="2026-06-02T09:01:04+09:00",
                                fetch_policy="incremental",
                                fetch_n=6,
                            ),
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(validator, "REPO_ROOT", repo_root)

    report = validator.build_report(
        bundle_id="BUNDLE-TEST",
        generated_date="20260602",
        strict_cadence=True,
        require_shadow_only=True,
    )

    assert report["status"] == "PASS"
    assert report["reports"][0]["cadence"]["runtime_checks"]["shadow_only"] is True
    assert report["reports"][0]["cadence"]["start_gaps_sec"] == [59.0]
    assert report["reports"][0]["cadence"]["blockers"] == []


def test_paper_auto_semantics_strict_cadence_allows_shadow_fda_veto_skips(
    tmp_path,
    monkeypatch,
) -> None:
    validator = _load_validator_module()
    repo_root = tmp_path
    report_dir = repo_root / "artifacts" / "reports" / "paper_auto_trading" / "DCD"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "paper_auto_trade_20260602_091500.json"
    cycle0 = _strict_cadence_cycle(
        cycle_index=0,
        started_at="2026-06-02T09:15:05+09:00",
        fetch_policy="cold",
        fetch_n=61,
    )
    cycle1 = _strict_cadence_cycle(
        cycle_index=1,
        started_at="2026-06-02T09:16:05+09:00",
        fetch_policy="incremental",
        fetch_n=6,
    )
    for cycle in (cycle0, cycle1):
        cycle["status"] = "SKIP"
        cycle["order_guard"] = {
            "status": "SKIP",
            "safe_skip": True,
            "reason": "fda_veto",
        }
        cycle["hot_result"]["final_decision"]["approved"] = False
        cycle["hot_result"]["final_decision"]["reason_code"] = "RISK_FAST_TRIGGER"
        cycle["hot_result"]["final_decision"]["order_deltas"] = [
            {"ticker": "005930", "side": "buy", "qty": 1}
        ]
    report_path.write_text(
        json.dumps(
            {
                "status": "PASS",
                "generated_at": "2026-06-02T09:17:00+09:00",
                "runtime": {
                    "kis_mode": "virtual",
                    "live_enabled": False,
                    "broker_submit_enabled": False,
                    "shadow_only": True,
                },
                "params": {"required_bundle_id": "BUNDLE-TEST"},
                "stages": {"cycles": {"items": [cycle0, cycle1]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(validator, "REPO_ROOT", repo_root)

    report = validator.build_report(
        bundle_id="BUNDLE-TEST",
        generated_date="20260602",
        strict_cadence=True,
        require_shadow_only=True,
    )

    assert report["status"] == "PASS"
    summary = report["reports"][0]
    assert summary["explained_no_submit_reasons"] == {"fda_veto": 2}
    assert summary["cadence"]["blockers"] == []
    assert summary["warnings"] == ["shadow_only_order_deltas_explained_by_guard"]


def test_paper_auto_semantics_strict_cadence_blocks_cache_timeout_and_slow_gap(
    tmp_path,
    monkeypatch,
) -> None:
    validator = _load_validator_module()
    repo_root = tmp_path
    report_dir = repo_root / "artifacts" / "reports" / "paper_auto_trading"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "paper_auto_trade_20260602_090500.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "generated_at": "2026-06-02T09:05:00+09:00",
                "runtime": {
                    "kis_mode": "virtual",
                    "live_enabled": False,
                    "broker_submit_enabled": False,
                    "shadow_only": True,
                },
                "params": {"required_bundle_id": "BUNDLE-TEST"},
                "stages": {
                    "cycles": {
                        "items": [
                            _strict_cadence_cycle(
                                cycle_index=0,
                                started_at="2026-06-02T09:00:05+09:00",
                                fetch_policy="cold",
                                fetch_n=61,
                            ),
                            _strict_cadence_cycle(
                                cycle_index=1,
                                started_at="2026-06-02T09:01:40+09:00",
                                fetch_policy="incremental",
                                fetch_n=6,
                                failed_tickers={"000001": "fetch_timeout"},
                            ),
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(validator, "REPO_ROOT", repo_root)

    report = validator.build_report(
        bundle_id="BUNDLE-TEST",
        generated_date="20260602",
        strict_cadence=True,
        require_shadow_only=True,
    )

    blockers = report["failures"][0]["blockers"]
    assert report["status"] == "BLOCKED"
    assert "cadence_cycle_1_cache_failed_tickers" in blockers
    assert "cadence_cycle_1_fetch_timeout" in blockers
    assert "cadence_cycle_start_gap_exceeds_limit" in blockers


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

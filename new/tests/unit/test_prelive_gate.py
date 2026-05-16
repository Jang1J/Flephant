"""prelive_gate runner tests."""
from __future__ import annotations

import importlib.util
import json
import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def _load_script_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "prelive_gate.py"
    spec = importlib.util.spec_from_file_location("prelive_gate", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _required_label_generation_version(gate) -> str:
    cfg = gate._load_yaml(gate.NEW_ROOT / "config" / "risk_config.yaml")
    return str(cfg["label"]["generation_version"])


def _required_label_session_scope(gate) -> str:
    cfg = gate._load_yaml(gate.NEW_ROOT / "config" / "risk_config.yaml")
    return str(cfg["label"]["session_scope"])


def _required_target_col(gate) -> str:
    cfg = gate._load_yaml(gate.NEW_ROOT / "config" / "risk_config.yaml")
    return str(cfg["label"]["target_col"])


def _write_staged_lgbm_bundle(
    root: Path,
    gate,
    bundle_id: str,
    *,
    metadata: dict | None = None,
    model_bytes: bytes = b"staged-model",
) -> dict:
    bundle_dir = root / "artifacts" / "bundles" / bundle_id / "lgbm"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "latest_model.pkl").write_bytes(model_bytes)
    payload = {
        "version": "staged",
        "status": "candidate",
        "bundle_id": bundle_id,
        "model_path": f"artifacts/bundles/{bundle_id}/lgbm/latest_model.pkl",
        "created_at": "2026-05-12T09:00:00+09:00",
        "synthetic_fallback": False,
        "data_source": "artifact_bars",
        "n_train_rows": 1000,
        "label_generation_version": _required_label_generation_version(gate),
        "label_session_scope": _required_label_session_scope(gate),
        "target_col": _required_target_col(gate),
    }
    if metadata:
        payload.update(metadata)
    (bundle_dir / "latest_model_metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def _service_policy_evidence(
    root: Path,
    *,
    bundle_id: str = "BUNDLE-TEST",
    status: str = "PASS",
    date_range: dict | None = None,
) -> dict:
    checks = {
        "deploy_candidate_by_service_policy": status == "PASS",
        "no_naked_short_exposure": True,
        "order_caps_respected": True,
        "cash_guard_respected": True,
    }
    report = {
        "status": status,
        "bundle_id": bundle_id,
        "gate": {"status": status},
        "policy_checks": checks,
        "order_stats": {"naked_short_attempts": 0},
    }
    if date_range is not None:
        report["date_range"] = date_range
    path = root / "artifacts" / "reports" / "service_policy_replay" / f"service_policy_replay_{bundle_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        **report,
        "service_policy_report_path": str(path.relative_to(root)),
        "service_policy_report_sha256": digest,
    }


def _final_dataset_metadata() -> dict:
    tickers = [
        "005930", "000660", "042700", "403870", "058470",
        "329180", "042660", "010140", "009540", "267250",
        "006400", "051910", "373220", "096770", "247540",
        "012450", "047810", "079550", "298040", "272210",
        "105560", "055550", "086790", "024110", "000810",
        "005380", "000270", "012330", "011210", "086280",
    ]
    return {
        "train_start": "2025-05-09",
        "train_end": "2026-05-15",
        "data_source": "artifact_bars",
        "synthetic_fallback": False,
        "requested_tickers": tickers,
        "loaded_tickers": tickers,
        "missing_tickers": [],
        "n_tickers": len(tickers),
    }


def _write_parquet_day(
    root: Path,
    ticker: str,
    yyyymmdd: str,
    *,
    rows: int = 301,
    row_ticker: str | None = None,
    ts_yyyymmdd: str | None = None,
    gap_after: int | None = None,
    gap_minutes: int = 0,
) -> None:
    import pandas as pd

    data_dir = root / ticker
    data_dir.mkdir(parents=True, exist_ok=True)
    ts_day = ts_yyyymmdd or yyyymmdd
    start = datetime(
        int(ts_day[:4]),
        int(ts_day[4:6]),
        int(ts_day[6:]),
        9,
        0,
        tzinfo=ZoneInfo("Asia/Seoul"),
    )
    records = []
    for i in range(rows):
        offset = i
        if gap_after is not None and i > gap_after:
            offset += gap_minutes
        records.append({
            "ticker": row_ticker or ticker,
            "ts_close": (start + timedelta(minutes=offset)).isoformat(),
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1.0,
        })
    pd.DataFrame.from_records(records).to_parquet(
        data_dir / f"bars_1m_{yyyymmdd}.parquet",
        index=False,
    )


def test_configured_train_min_rows_requires_config_value():
    gate = _load_script_module()

    value, blocker = gate._configured_train_min_rows({"live_data_readiness": {}})

    assert value is None
    assert blocker["status"] == "BLOCKED"
    assert "train_min_rows_per_day" in blocker["message"]


def test_business_start_date_counts_krx_trading_days():
    gate = _load_script_module()

    assert gate._business_start_date(date(2026, 5, 8), 1).strftime("%Y%m%d") == "20260508"
    assert gate._business_start_date(date(2026, 5, 11), 2).strftime("%Y%m%d") == "20260508"
    assert gate._business_start_date(date(2026, 5, 11), 3).strftime("%Y%m%d") == "20260507"
    assert gate._business_start_date(date(2026, 5, 8), 80).strftime("%Y%m%d") == "20260109"
    assert gate._business_dates_between(date(2026, 2, 13), date(2026, 2, 19)) == [
        "20260213",
        "20260219",
    ]


def test_80_day_artifact_gate_rejects_internal_date_mismatch(monkeypatch, tmp_path):
    gate = _load_script_module()
    monkeypatch.setattr(gate, "_DATA_ROOT", tmp_path)
    _write_parquet_day(tmp_path, "005930", "20260508", ts_yyyymmdd="20260507")

    result = gate._check_80_day_artifacts(
        tickers=["005930"],
        end_yyyymmdd="20260508",
        business_days=1,
        min_rows_per_day=300,
    )

    assert result["status"] == "BLOCKED"
    first = result["sample_missing_or_short"]["20260508"][0]
    assert first["rows"] == 301
    assert first["timestamp_dates_match"] is False
    assert first["valid_artifact"] is False


def test_80_day_artifact_gate_rejects_ticker_mismatch(monkeypatch, tmp_path):
    gate = _load_script_module()
    monkeypatch.setattr(gate, "_DATA_ROOT", tmp_path)
    _write_parquet_day(tmp_path, "005930", "20260508", row_ticker="000660")

    result = gate._check_80_day_artifacts(
        tickers=["005930"],
        end_yyyymmdd="20260508",
        business_days=1,
        min_rows_per_day=300,
    )

    assert result["status"] == "BLOCKED"
    first = result["sample_missing_or_short"]["20260508"][0]
    assert first["ticker_matches"] is False


def test_80_day_artifact_gate_rejects_morning_only_session_span(monkeypatch, tmp_path):
    gate = _load_script_module()
    monkeypatch.setattr(gate, "_DATA_ROOT", tmp_path)
    _write_parquet_day(tmp_path, "005930", "20260508", rows=300)

    result = gate._check_80_day_artifacts(
        tickers=["005930"],
        end_yyyymmdd="20260508",
        business_days=1,
        min_rows_per_day=300,
    )

    assert result["status"] == "BLOCKED"
    first = result["sample_missing_or_short"]["20260508"][0]
    assert first["session_span_ok"] is False
    assert first["session_span_minutes"] == 299.0


def test_80_day_artifact_gate_rejects_large_intraday_gap(monkeypatch, tmp_path):
    gate = _load_script_module()
    monkeypatch.setattr(gate, "_DATA_ROOT", tmp_path)
    _write_parquet_day(
        tmp_path,
        "005930",
        "20260508",
        rows=301,
        gap_after=150,
        gap_minutes=20,
    )

    result = gate._check_80_day_artifacts(
        tickers=["005930"],
        end_yyyymmdd="20260508",
        business_days=1,
        min_rows_per_day=300,
    )

    assert result["status"] == "BLOCKED"
    first = result["sample_missing_or_short"]["20260508"][0]
    assert first["max_gap_ok"] is False
    assert first["max_gap_minutes"] == 21.0


def test_latest_matching_report_skips_non_matching_and_bad_json(tmp_path):
    gate = _load_script_module()
    (tmp_path / "prelive_gate_bad.json").write_text("{", encoding="utf-8")
    old = tmp_path / "prelive_gate_20260511_000000.json"
    old.write_text(json.dumps({"status": "FAIL"}, ensure_ascii=False), encoding="utf-8")
    new = tmp_path / "prelive_gate_20260511_000001.json"
    new.write_text(json.dumps({"status": "PASS"}, ensure_ascii=False), encoding="utf-8")

    path, data = gate._latest_matching_report(
        tmp_path,
        "prelive_gate_",
        lambda payload: payload.get("status") == "PASS",
    )

    assert path == new
    assert data == {"status": "PASS"}


def test_real_readiness_treats_allow_mock_string_false_as_real(monkeypatch, tmp_path):
    gate = _load_script_module()
    report_dir = tmp_path / "data_readiness"
    report_dir.mkdir(parents=True)
    report = report_dir / "data_readiness_20260516_000000.json"
    report.write_text(
        json.dumps(
            {
                "status": "PASS",
                "end_date": "20260515",
                "allow_mock": "false",
                "runtime": {"secret_presence": {"KIS_MODE": True}},
                "stages": {
                    "smoke": {"naver": {"status": "PASS"}},
                    "backfill": {"status": "PASS", "counts": {"005930": 30400}},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "_REPORT_ROOT", tmp_path)

    result = gate._check_real_readiness("20260515")

    assert result["status"] == "PASS"
    assert result["backfill_min_rows"] == 30400


def test_deployable_backtest_treats_regression_string_false_as_false(monkeypatch):
    gate = _load_script_module()
    monkeypatch.setattr(gate, "_service_policy_gate_pass", lambda *_args, **_kwargs: True)

    assert gate._is_deployable_backtest_report(
        {
            "bundle_id": "BUNDLE-TEST",
            "verdict": "pass",
            "regression_risk": {"flagged": "false"},
            "minute_bar_leakage_check": {"verdict": "pass"},
            "feature_quality": {
                "dual_source_rows": 10,
                "dual_source_non_neutral_rows": 10,
                "exogenous_rows": 10,
                "exogenous_non_neutral_rows": 10,
            },
            "candidate_model_metadata": _final_dataset_metadata(),
        },
        "BUNDLE-TEST",
    )


def test_probe_order_blocked_without_report(monkeypatch, tmp_path):
    gate = _load_script_module()
    monkeypatch.setattr(gate, "_REPORT_ROOT", tmp_path)

    result = gate._check_probe_order()

    assert result["status"] == "BLOCKED"
    assert "No paper probe order report" in result["message"]


def test_probe_order_blocks_without_order_history_match(monkeypatch, tmp_path):
    gate = _load_script_module()
    paper_dir = tmp_path / "paper_trading"
    paper_dir.mkdir(parents=True)
    report_path = paper_dir / "paper_trading_submit_probe_order_20260512_010000.json"
    report_path.write_text(
        json.dumps(
            {
                "action": "submit_probe_order",
                "status": "PASS",
                "stages": {
                    "execution": {"status": "PASS"},
                    "order_history": {
                        "status": "PASS",
                        "matched_order_count": 0,
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "_REPORT_ROOT", tmp_path)

    result = gate._check_probe_order()

    assert result["status"] == "BLOCKED"
    assert result["matched_order_count"] == 0


def test_probe_order_passes_with_order_history_match(monkeypatch, tmp_path):
    gate = _load_script_module()
    paper_dir = tmp_path / "paper_trading"
    paper_dir.mkdir(parents=True)
    report_path = paper_dir / "paper_trading_submit_probe_order_20260512_020000.json"
    report_path.write_text(
        json.dumps(
            {
                "action": "submit_probe_order",
                "status": "PASS",
                "stages": {
                    "execution": {"status": "PASS"},
                    "order_history": {
                        "status": "PASS",
                        "matched_order_count": 1,
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "_REPORT_ROOT", tmp_path)

    result = gate._check_probe_order()

    assert result["status"] == "PASS"
    assert result["matched_order_count"] == 1


def test_lgbm_real_train_prefers_candidate_bundle(monkeypatch, tmp_path):
    gate = _load_script_module()
    label_version = _required_label_generation_version(gate)
    label_scope = _required_label_session_scope(gate)
    target_col = _required_target_col(gate)
    repo_root = tmp_path
    lgbm_dir = repo_root / "artifacts" / "lgbm"
    lgbm_dir.mkdir(parents=True)
    (lgbm_dir / "v2.pkl").write_bytes(b"candidate")
    (lgbm_dir / "registry.json").write_text(
        json.dumps(
            {
                "active_version": "v1",
                "versions": [
                    {
                        "version": "v1",
                        "status": "active",
                        "bundle_id": None,
                        "model_path": "artifacts/lgbm/v1.pkl",
                        "created_at": "2026-05-10T00:00:00+09:00",
                        "synthetic_fallback": False,
                    },
                    {
                        "version": "v2",
                        "status": "candidate",
                        "bundle_id": "BUNDLE-TEST",
                        "model_path": "artifacts/lgbm/v2.pkl",
                        "created_at": "2026-05-11T00:00:00+09:00",
                        "synthetic_fallback": False,
                        "data_source": "artifact_bars",
                        "n_train_rows": 1000,
                        "label_generation_version": label_version,
                        "label_session_scope": label_scope,
                        "target_col": target_col,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "REPO_ROOT", repo_root)

    result = gate._check_lgbm_real_train()

    assert result["status"] == "PASS"
    assert result["version"] == "v2"
    assert result["candidate_bundle_id"] == "BUNDLE-TEST"
    assert result["registry_status"] == "candidate"


def test_lgbm_real_train_treats_synthetic_fallback_string_false_as_real(
    monkeypatch, tmp_path
):
    gate = _load_script_module()
    label_version = _required_label_generation_version(gate)
    label_scope = _required_label_session_scope(gate)
    target_col = _required_target_col(gate)
    repo_root = tmp_path
    lgbm_dir = repo_root / "artifacts" / "lgbm"
    lgbm_dir.mkdir(parents=True)
    (lgbm_dir / "candidate.pkl").write_bytes(b"candidate")
    (lgbm_dir / "registry.json").write_text(
        json.dumps(
            {
                "active_version": None,
                "versions": [
                    {
                        "version": "candidate",
                        "status": "candidate",
                        "bundle_id": "BUNDLE-TEST",
                        "model_path": "artifacts/lgbm/candidate.pkl",
                        "created_at": "2026-05-11T00:00:00+09:00",
                        "synthetic_fallback": "false",
                        "data_source": "artifact_bars",
                        "n_train_rows": 1000,
                        "label_generation_version": label_version,
                        "label_session_scope": label_scope,
                        "target_col": target_col,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "REPO_ROOT", repo_root)

    result = gate._check_lgbm_real_train()

    assert result["status"] == "PASS"
    assert result["synthetic_fallback"] is False


def test_lgbm_real_train_prefers_latest_candidate_even_without_bundle(monkeypatch, tmp_path):
    gate = _load_script_module()
    label_version = _required_label_generation_version(gate)
    label_scope = _required_label_session_scope(gate)
    target_col = _required_target_col(gate)
    repo_root = tmp_path
    lgbm_dir = repo_root / "artifacts" / "lgbm"
    lgbm_dir.mkdir(parents=True)
    (lgbm_dir / "v2.pkl").write_bytes(b"synthetic")
    (lgbm_dir / "live_20260508.pkl").write_bytes(b"real")
    (lgbm_dir / "registry.json").write_text(
        json.dumps(
            {
                "active_version": None,
                "versions": [
                    {
                        "version": "v2",
                        "status": "candidate",
                        "bundle_id": "BUNDLE-OLD",
                        "model_path": "artifacts/lgbm/v2.pkl",
                        "created_at": "2026-05-09T10:16:09+00:00",
                        "synthetic_fallback": True,
                        "data_source": "synthetic_fallback",
                        "n_train_rows": 93360,
                    },
                    {
                        "version": "live_20260508",
                        "status": "candidate",
                        "bundle_id": None,
                        "model_path": "artifacts/lgbm/live_20260508.pkl",
                        "created_at": "2026-05-11T11:21:29+00:00",
                        "synthetic_fallback": False,
                        "data_source": "artifact_bars",
                        "n_train_rows": 454639,
                        "label_generation_version": label_version,
                        "label_session_scope": label_scope,
                        "target_col": target_col,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "REPO_ROOT", repo_root)

    result = gate._check_lgbm_real_train()

    assert result["status"] == "PASS"
    assert result["version"] == "live_20260508"
    assert result["candidate_bundle_id"] is None
    assert result["synthetic_fallback"] is False
    assert result["data_source"] == "artifact_bars"


def test_lgbm_real_train_uses_requested_bundle_metadata(monkeypatch, tmp_path):
    gate = _load_script_module()
    repo_root = tmp_path
    lgbm_dir = repo_root / "artifacts" / "lgbm"
    lgbm_dir.mkdir(parents=True)
    (lgbm_dir / "newer.pkl").write_bytes(b"newer")
    _write_staged_lgbm_bundle(
        repo_root,
        gate,
        "BUNDLE-REQUESTED",
        metadata={"version": "requested"},
    )
    (lgbm_dir / "registry.json").write_text(
        json.dumps(
            {
                "active_version": None,
                "versions": [
                    {
                        "version": "newer",
                        "status": "candidate",
                        "bundle_id": "BUNDLE-NEWER",
                        "model_path": "artifacts/lgbm/newer.pkl",
                        "created_at": "2026-05-12T10:00:00+09:00",
                        "synthetic_fallback": False,
                        "data_source": "artifact_bars",
                        "n_train_rows": 1000,
                        "label_generation_version": _required_label_generation_version(gate),
                        "label_session_scope": _required_label_session_scope(gate),
                        "target_col": _required_target_col(gate),
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "REPO_ROOT", repo_root)

    result = gate._check_lgbm_real_train(bundle_id="BUNDLE-REQUESTED")

    assert result["status"] == "PASS"
    assert result["version"] == "requested"
    assert result["requested_bundle_id"] == "BUNDLE-REQUESTED"
    assert result["candidate_bundle_id"] == "BUNDLE-REQUESTED"
    assert result["bundle_id_matches_request"] is True
    assert result["metadata_source"] == "staged_bundle"
    assert result["model_path"] == "artifacts/bundles/BUNDLE-REQUESTED/lgbm/latest_model.pkl"


def test_lgbm_real_train_blocks_unknown_requested_bundle(monkeypatch, tmp_path):
    gate = _load_script_module()
    repo_root = tmp_path
    lgbm_dir = repo_root / "artifacts" / "lgbm"
    lgbm_dir.mkdir(parents=True)
    (lgbm_dir / "registry.json").write_text(
        json.dumps({"active_version": None, "versions": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "REPO_ROOT", repo_root)

    result = gate._check_lgbm_real_train(bundle_id="BUNDLE-MISSING")

    assert result["status"] == "BLOCKED"
    assert result["requested_bundle_id"] == "BUNDLE-MISSING"
    assert "requested bundle id" in result["message"]


def test_lgbm_real_train_blocks_staged_bundle_id_mismatch(monkeypatch, tmp_path):
    gate = _load_script_module()
    repo_root = tmp_path
    _write_staged_lgbm_bundle(
        repo_root,
        gate,
        "BUNDLE-REQUESTED",
        metadata={"bundle_id": "BUNDLE-OTHER"},
    )
    monkeypatch.setattr(gate, "REPO_ROOT", repo_root)

    result = gate._check_lgbm_real_train(bundle_id="BUNDLE-REQUESTED")

    assert result["status"] == "BLOCKED"
    assert result["metadata_source"] == "staged_bundle"
    assert result["bundle_id_matches_request"] is False


def test_lgbm_real_train_blocks_target_col_override_candidate(monkeypatch, tmp_path):
    gate = _load_script_module()
    repo_root = tmp_path
    _write_staged_lgbm_bundle(
        repo_root,
        gate,
        "BUNDLE-REQUESTED",
        metadata={"target_col": "label_session_close_net_ret"},
    )
    monkeypatch.setattr(gate, "REPO_ROOT", repo_root)

    result = gate._check_lgbm_real_train(bundle_id="BUNDLE-REQUESTED")

    assert result["status"] == "BLOCKED"
    assert result["target_col"] == "label_session_close_net_ret"
    assert result["required_target_col"] == _required_target_col(gate)


def test_lgbm_real_train_sorts_created_at_by_instant(monkeypatch, tmp_path):
    gate = _load_script_module()
    label_version = _required_label_generation_version(gate)
    label_scope = _required_label_session_scope(gate)
    target_col = _required_target_col(gate)
    repo_root = tmp_path
    lgbm_dir = repo_root / "artifacts" / "lgbm"
    lgbm_dir.mkdir(parents=True)
    (lgbm_dir / "kst.pkl").write_bytes(b"older")
    (lgbm_dir / "utc.pkl").write_bytes(b"newer")
    (lgbm_dir / "registry.json").write_text(
        json.dumps(
            {
                "active_version": None,
                "versions": [
                    {
                        "version": "kst_time",
                        "status": "candidate",
                        "bundle_id": "BUNDLE-KST",
                        "model_path": "artifacts/lgbm/kst.pkl",
                        "created_at": "2026-05-12T09:30:00+09:00",
                        "synthetic_fallback": False,
                        "data_source": "artifact_bars",
                        "label_generation_version": label_version,
                        "label_session_scope": label_scope,
                        "target_col": target_col,
                    },
                    {
                        "version": "utc_time",
                        "status": "candidate",
                        "bundle_id": "BUNDLE-UTC",
                        "model_path": "artifacts/lgbm/utc.pkl",
                        "created_at": "2026-05-12T01:00:00+00:00",
                        "synthetic_fallback": False,
                        "data_source": "artifact_bars",
                        "label_generation_version": label_version,
                        "label_session_scope": label_scope,
                        "target_col": target_col,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "REPO_ROOT", repo_root)

    result = gate._check_lgbm_real_train()

    assert result["status"] == "PASS"
    assert result["version"] == "utc_time"
    assert result["candidate_bundle_id"] == "BUNDLE-UTC"


def test_lgbm_real_train_blocks_missing_label_generation_version(monkeypatch, tmp_path):
    gate = _load_script_module()
    repo_root = tmp_path
    lgbm_dir = repo_root / "artifacts" / "lgbm"
    lgbm_dir.mkdir(parents=True)
    (lgbm_dir / "live_20260508.pkl").write_bytes(b"real")
    (lgbm_dir / "registry.json").write_text(
        json.dumps(
            {
                "active_version": None,
                "versions": [
                    {
                        "version": "live_20260508",
                        "status": "candidate",
                        "bundle_id": "BUNDLE-STALE",
                        "model_path": "artifacts/lgbm/live_20260508.pkl",
                        "created_at": "2026-05-11T11:21:29+00:00",
                        "synthetic_fallback": False,
                        "data_source": "artifact_bars",
                        "n_train_rows": 454639,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "REPO_ROOT", repo_root)

    result = gate._check_lgbm_real_train()

    assert result["status"] == "BLOCKED"
    assert result["label_generation_version"] is None
    assert result["required_label_generation_version"] == _required_label_generation_version(gate)


def test_lgbm_real_train_blocks_missing_real_data_source(monkeypatch, tmp_path):
    gate = _load_script_module()
    label_version = _required_label_generation_version(gate)
    label_scope = _required_label_session_scope(gate)
    target_col = _required_target_col(gate)
    repo_root = tmp_path
    lgbm_dir = repo_root / "artifacts" / "lgbm"
    lgbm_dir.mkdir(parents=True)
    (lgbm_dir / "candidate.pkl").write_bytes(b"real")
    (lgbm_dir / "registry.json").write_text(
        json.dumps(
            {
                "active_version": None,
                "versions": [
                    {
                        "version": "candidate",
                        "status": "candidate",
                        "bundle_id": "BUNDLE-NO-SOURCE",
                        "model_path": "artifacts/lgbm/candidate.pkl",
                        "created_at": "2026-05-12T10:00:00+09:00",
                        "synthetic_fallback": False,
                        "n_train_rows": 1000,
                        "label_generation_version": label_version,
                        "label_session_scope": label_scope,
                        "target_col": target_col,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "REPO_ROOT", repo_root)

    result = gate._check_lgbm_real_train()

    assert result["status"] == "BLOCKED"
    assert result["real_data_source"] is False


def test_lgbm_real_train_blocks_missing_label_session_scope(monkeypatch, tmp_path):
    gate = _load_script_module()
    label_version = _required_label_generation_version(gate)
    repo_root = tmp_path
    lgbm_dir = repo_root / "artifacts" / "lgbm"
    lgbm_dir.mkdir(parents=True)
    (lgbm_dir / "candidate.pkl").write_bytes(b"real")
    (lgbm_dir / "registry.json").write_text(
        json.dumps(
            {
                "active_version": None,
                "versions": [
                    {
                        "version": "candidate",
                        "status": "candidate",
                        "bundle_id": "BUNDLE-NO-SCOPE",
                        "model_path": "artifacts/lgbm/candidate.pkl",
                        "created_at": "2026-05-12T10:00:00+09:00",
                        "synthetic_fallback": False,
                        "data_source": "artifact_bars",
                        "n_train_rows": 1000,
                        "label_generation_version": label_version,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "REPO_ROOT", repo_root)

    result = gate._check_lgbm_real_train()

    assert result["status"] == "BLOCKED"
    assert result["label_session_scope"] is None
    assert result["required_label_session_scope"] == _required_label_session_scope(gate)


def test_backtest_gate_passes_when_matching_report_exists(monkeypatch, tmp_path):
    gate = _load_script_module()
    report_dir = tmp_path / "backtest"
    report_dir.mkdir(parents=True)
    report_path = report_dir / "backtest_BUNDLE-TEST_20260511_010000.json"
    service_policy = _service_policy_evidence(tmp_path)
    report_path.write_text(
        json.dumps(
            {
                "bundle_id": "BUNDLE-TEST",
                "backtest_id": "BT-TEST",
                "generated_at": "2026-05-11T01:00:00+09:00",
                "verdict": "pass",
                "metrics": {"sr": 1.2},
                "regression_risk": {"flagged": False, "severity": "low", "evidence": []},
                "minute_bar_leakage_check": {"verdict": "pass"},
                "feature_quality": {
                    "dual_source_rows": 100,
                    "dual_source_non_neutral_rows": 90,
                    "exogenous_rows": 100,
                    "exogenous_non_neutral_rows": 90,
                },
                "service_policy_replay": service_policy,
                "candidate_model_metadata": _final_dataset_metadata(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "_REPORT_ROOT", tmp_path)

    result = gate._check_backtest_gate({
        "status": "PASS",
        "candidate_bundle_id": "BUNDLE-TEST",
    })

    assert result["status"] == "PASS"
    assert result["bundle_id"] == "BUNDLE-TEST"
    assert result["report_path"].endswith(report_path.name)
    assert result["verdict"] == "pass"


def test_backtest_gate_blocks_service_policy_date_range_mismatch(monkeypatch, tmp_path):
    gate = _load_script_module()
    report_dir = tmp_path / "backtest"
    report_dir.mkdir(parents=True)
    report_path = report_dir / "backtest_BUNDLE-TEST_20260511_011000.json"
    service_policy = _service_policy_evidence(
        tmp_path,
        date_range={"start": "20260401", "end": "20260420"},
    )
    report_path.write_text(
        json.dumps(
            {
                "bundle_id": "BUNDLE-TEST",
                "verdict": "pass",
                "date_range": {"start": "20260415", "end": "20260504"},
                "regression_risk": {"flagged": False},
                "minute_bar_leakage_check": {"verdict": "pass"},
                "feature_quality": {
                    "dual_source_rows": 100,
                    "dual_source_non_neutral_rows": 90,
                    "exogenous_rows": 100,
                    "exogenous_non_neutral_rows": 90,
                },
                "service_policy_replay": service_policy,
                "candidate_model_metadata": _final_dataset_metadata(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "_REPORT_ROOT", tmp_path)

    result = gate._check_backtest_gate({
        "status": "PASS",
        "bundle_id": "BUNDLE-TEST",
    })

    assert result["status"] == "BLOCKED"
    assert result["latest_report_path"].endswith(report_path.name)
    assert result["latest_feature_quality_gate_pass"] is True
    assert result["latest_service_policy_gate_pass"] is False
    assert result["latest_service_policy_report_sha256"] == service_policy[
        "service_policy_report_sha256"
    ]


def test_backtest_gate_uses_service_policy_expected_date_range(monkeypatch, tmp_path):
    gate = _load_script_module()
    report_dir = tmp_path / "backtest"
    report_dir.mkdir(parents=True)
    report_path = report_dir / "backtest_BUNDLE-TEST_20260511_011500.json"
    service_range = {"start": "20260401", "end": "20260420"}
    service_policy = _service_policy_evidence(tmp_path, date_range=service_range)
    report_path.write_text(
        json.dumps(
            {
                "bundle_id": "BUNDLE-TEST",
                "verdict": "pass",
                "date_range": {"start": "20260415", "end": "20260504"},
                "service_policy_expected_date_range": service_range,
                "regression_risk": {"flagged": False},
                "minute_bar_leakage_check": {"verdict": "pass"},
                "feature_quality": {
                    "dual_source_rows": 100,
                    "dual_source_non_neutral_rows": 90,
                    "exogenous_rows": 100,
                    "exogenous_non_neutral_rows": 90,
                },
                "service_policy_replay": service_policy,
                "candidate_model_metadata": _final_dataset_metadata(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "_REPORT_ROOT", tmp_path)

    result = gate._check_backtest_gate({
        "status": "PASS",
        "bundle_id": "BUNDLE-TEST",
    })

    assert result["status"] == "PASS"
    assert result["report_path"].endswith(report_path.name)


def test_backtest_gate_blocks_pass_report_with_zero_feature_quality(monkeypatch, tmp_path):
    gate = _load_script_module()
    report_dir = tmp_path / "backtest"
    report_dir.mkdir(parents=True)
    report_path = report_dir / "backtest_BUNDLE-TEST_20260511_020000.json"
    service_policy = _service_policy_evidence(tmp_path)
    report_path.write_text(
        json.dumps(
            {
                "bundle_id": "BUNDLE-TEST",
                "status": "PASS",
                "verdict": "pass",
                "regression_risk": {"flagged": False},
                "minute_bar_leakage_check": {"verdict": "pass"},
                "feature_quality": {
                    "dual_source_rows": 100,
                    "dual_source_non_neutral_rows": 0,
                    "exogenous_rows": 100,
                    "exogenous_non_neutral_rows": 0,
                },
                "service_policy_replay": service_policy,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "_REPORT_ROOT", tmp_path)

    result = gate._check_backtest_gate({
        "status": "PASS",
        "bundle_id": "BUNDLE-TEST",
    })

    assert result["status"] == "BLOCKED"
    assert result["latest_report_path"].endswith(report_path.name)
    assert result["latest_feature_quality_gate_pass"] is False
    assert result["latest_service_policy_gate_pass"] is True


def test_backtest_gate_blocks_pass_report_with_blocked_service_policy(monkeypatch, tmp_path):
    gate = _load_script_module()
    report_dir = tmp_path / "backtest"
    report_dir.mkdir(parents=True)
    report_path = report_dir / "backtest_BUNDLE-TEST_20260511_030000.json"
    service_policy = _service_policy_evidence(tmp_path, status="BLOCKED")
    report_path.write_text(
        json.dumps(
            {
                "bundle_id": "BUNDLE-TEST",
                "status": "PASS",
                "verdict": "pass",
                "regression_risk": {"flagged": False},
                "minute_bar_leakage_check": {"verdict": "pass"},
                "feature_quality": {
                    "dual_source_rows": 100,
                    "dual_source_non_neutral_rows": 90,
                    "exogenous_rows": 100,
                    "exogenous_non_neutral_rows": 90,
                },
                "service_policy_replay": service_policy,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "_REPORT_ROOT", tmp_path)

    result = gate._check_backtest_gate({
        "status": "PASS",
        "bundle_id": "BUNDLE-TEST",
    })

    assert result["status"] == "BLOCKED"
    assert result["latest_report_path"].endswith(report_path.name)
    assert result["latest_feature_quality_gate_pass"] is True
    assert result["latest_service_policy_gate_pass"] is False


def test_backtest_gate_blocks_without_candidate_bundle_id():
    gate = _load_script_module()

    result = gate._check_backtest_gate({
        "status": "PASS",
        "version": "v2",
        "registry_status": "active",
    })

    assert result["status"] == "BLOCKED"
    assert "candidate bundle id" in result["message"]


def test_backtest_gate_blocks_pass_report_with_leakage_fail(monkeypatch, tmp_path):
    gate = _load_script_module()
    report_dir = tmp_path / "backtest"
    report_dir.mkdir(parents=True)
    report_path = report_dir / "backtest_BUNDLE-TEST_20260511_010000.json"
    report_path.write_text(
        json.dumps(
            {
                "bundle_id": "BUNDLE-TEST",
                "status": "PASS",
                "verdict": "pass",
                "regression_risk": {"flagged": False},
                "minute_bar_leakage_check": {"verdict": "fail"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "_REPORT_ROOT", tmp_path)

    result = gate._check_backtest_gate({
        "status": "PASS",
        "bundle_id": "BUNDLE-TEST",
    })

    assert result["status"] == "BLOCKED"
    assert result["latest_verdict"] == "pass"
    assert result["latest_leakage_verdict"] == "fail"
    assert result["latest_report_path"].endswith(report_path.name)


def test_backtest_gate_blocks_pass_report_with_regression_flag(monkeypatch, tmp_path):
    gate = _load_script_module()
    report_dir = tmp_path / "backtest"
    report_dir.mkdir(parents=True)
    report_path = report_dir / "backtest_BUNDLE-TEST_20260511_010000.json"
    report_path.write_text(
        json.dumps(
            {
                "bundle_id": "BUNDLE-TEST",
                "status": "PASS",
                "verdict": "pass",
                "regression_risk": {"flagged": True},
                "minute_bar_leakage_check": {"verdict": "pass"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "_REPORT_ROOT", tmp_path)

    result = gate._check_backtest_gate({
        "status": "PASS",
        "bundle_id": "BUNDLE-TEST",
    })

    assert result["status"] == "BLOCKED"
    assert result["latest_verdict"] == "pass"
    assert result["latest_regression_flagged"] is True
    assert result["latest_report_path"].endswith(report_path.name)


def test_backtest_gate_reports_latest_failed_candidate(monkeypatch, tmp_path):
    gate = _load_script_module()
    report_dir = tmp_path / "backtest"
    report_dir.mkdir(parents=True)
    report_path = report_dir / "backtest_BUNDLE-TEST_20260511_010000.json"
    report_path.write_text(
        json.dumps(
            {
                "bundle_id": "BUNDLE-TEST",
                "status": "FAIL",
                "verdict": "fail",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "_REPORT_ROOT", tmp_path)

    result = gate._check_backtest_gate({
        "status": "PASS",
        "bundle_id": "BUNDLE-TEST",
    })

    assert result["status"] == "BLOCKED"
    assert result["latest_verdict"] == "fail"
    assert result["latest_report_path"].endswith(report_path.name)


def test_ops_risk_fails_if_live_enabled(monkeypatch, tmp_path):
    gate = _load_script_module()
    cfg_dir = tmp_path / "new" / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "risk_config.yaml").write_text(
        "\n".join([
            "execution:",
            "  mode: live",
            "  live_enabled: true",
            "paper_trading:",
            "  confirm_order_phrase: PAPER_ORDER_OK",
            "  require_virtual_mode: true",
            "  max_probe_order_qty: 1",
            "  allow_market_order: false",
            "paper_auto_trading:",
            "  confirm_start_phrase: PAPER_AUTO_OK",
            "  require_virtual_mode: true",
            "  require_prelive_pass: true",
            "  require_active_model: true",
            "  max_order_qty_per_order: 1",
            "  allow_market_order: false",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "NEW_ROOT", tmp_path / "new")

    result = gate._check_ops_risk()

    assert result["status"] == "FAIL"
    assert result["checks"]["execution_live_disabled"] is False
    assert result["checks"]["execution_not_live"] is False


def test_write_report_persists_report_path(monkeypatch, tmp_path):
    gate = _load_script_module()
    monkeypatch.setattr(gate, "_GATE_REPORT_ROOT", tmp_path)
    report = {"status": "BLOCKED", "stages": {}}

    path = gate.write_report(report)
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert saved["report_path"] == str(path)
    assert saved["report_path_relative"].endswith(path.name)
    assert report["report_path"] == str(path)


def test_build_report_passes_requested_bundle_to_lgbm_and_backtest(monkeypatch):
    gate = _load_script_module()
    calls: dict[str, object] = {}

    monkeypatch.setattr(gate, "_active_tickers", lambda max_tickers: ["005930"])
    monkeypatch.setattr(gate, "_configured_train_min_rows", lambda risk_cfg: (1, None))
    monkeypatch.setattr(gate, "_load_yaml", lambda path: {})
    monkeypatch.setattr(gate, "_check_code_ssot", lambda: {"status": "PASS"})
    monkeypatch.setattr(gate, "_check_real_readiness", lambda end_date: {"status": "PASS"})
    monkeypatch.setattr(
        gate,
        "_check_80_day_artifacts",
        lambda **kwargs: {"status": "PASS"},
    )

    def fake_lgbm(bundle_id=None):
        calls["lgbm_bundle_id"] = bundle_id
        return {"status": "PASS", "candidate_bundle_id": bundle_id}

    def fake_backtest(stage):
        calls["backtest_stage"] = stage
        return {"status": "PASS", "bundle_id": stage["candidate_bundle_id"]}

    monkeypatch.setattr(gate, "_check_lgbm_real_train", fake_lgbm)
    monkeypatch.setattr(gate, "_check_backtest_gate", fake_backtest)
    monkeypatch.setattr(gate, "_check_paper_balance", lambda: {"status": "PASS"})
    monkeypatch.setattr(gate, "_check_paper_reconciliation", lambda: {"status": "PASS"})
    monkeypatch.setattr(gate, "_check_probe_order", lambda: {"status": "PASS"})
    monkeypatch.setattr(gate, "_check_ops_risk", lambda: {"status": "PASS"})

    report = gate.build_report(
        end_date="20260508",
        business_days=80,
        max_tickers=20,
        bundle_id="BUNDLE-REQUESTED",
    )

    assert report["status"] == "PASS"
    assert report["bundle_id"] == "BUNDLE-REQUESTED"
    assert calls["lgbm_bundle_id"] == "BUNDLE-REQUESTED"
    assert calls["backtest_stage"] == {
        "status": "PASS",
        "candidate_bundle_id": "BUNDLE-REQUESTED",
    }


def test_next_commands_keep_deploy_candidate_in_dry_run() -> None:
    gate = _load_script_module()

    commands = gate._next_commands(
        end_date="20260515",
        business_days=80,
        max_tickers=30,
    )

    assert "--dry-run" in commands["deploy_candidate_after_backtest_pass"]

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_script():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "serving_feature_readiness.py"
    spec = importlib.util.spec_from_file_location("serving_feature_readiness", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_metadata(repo_root: Path, bundle_id: str, feature_cols: list[str]) -> Path:
    path = repo_root / "artifacts" / "bundles" / bundle_id / "lgbm" / "latest_model_metadata.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"feature_cols": feature_cols}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _patch_root_and_config(monkeypatch, module, repo_root: Path) -> None:
    monkeypatch.setattr(module, "ROOT", repo_root)
    monkeypatch.setattr(
        module,
        "config_load",
        lambda *args, **kwargs: {
            "dual_source_feature_cols": [
                "news_score_t",
                "comm_score_t_1",
                "comm_score_t_2",
                "news_comm_divergence",
                "community_noise_multiplier",
            ],
        },
    )


def test_serving_feature_readiness_passes_when_model_does_not_require_dual_source(
    tmp_path,
    monkeypatch,
) -> None:
    script = _load_script()
    _patch_root_and_config(monkeypatch, script, tmp_path)
    _write_metadata(tmp_path, "BUNDLE-TEST", ["open", "high", "low", "close", "volume"])

    report = script.build_report(
        bundle_id="BUNDLE-TEST",
        tickers=["5930"],
        asof="2026-05-26T08:30:00+09:00",
    )

    assert report["status"] == "PASS"
    assert report["required"] is False
    assert report["reason"] == "no_required_dual_source_features"
    assert report["safety"]["external_api_called"] is False
    assert report["safety"]["env_read"] is False


def test_serving_feature_readiness_blocks_missing_required_artifact(
    tmp_path,
    monkeypatch,
) -> None:
    script = _load_script()
    _patch_root_and_config(monkeypatch, script, tmp_path)
    _write_metadata(tmp_path, "BUNDLE-TEST", ["close", "news_score_t"])

    report = script.build_report(
        bundle_id="BUNDLE-TEST",
        tickers=["005930"],
        asof="2026-05-26T08:30:00+09:00",
        artifact_dir=tmp_path / "artifacts" / "dual_source",
    )

    assert report["status"] == "FAIL"
    assert report["reason"] == "required_feature_artifact_missing"
    assert report["missing_artifacts"] == ["artifacts/dual_source/20260526.json"]


def test_serving_feature_readiness_passes_current_day_required_news_score(
    tmp_path,
    monkeypatch,
) -> None:
    script = _load_script()
    _patch_root_and_config(monkeypatch, script, tmp_path)
    _write_metadata(tmp_path, "BUNDLE-TEST", ["close", "news_score_t"])
    artifact_dir = tmp_path / "artifacts" / "dual_source"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "20260526.json").write_text(
        json.dumps({"scores": []}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    def fake_load_scores(date_key: str, *, artifact_dir: Path | None = None) -> list[dict]:
        calls.append({"date_key": date_key, "artifact_dir": artifact_dir})
        return [{
            "ticker": "005930",
            "news_score_t": 0.42,
            "generated_at": "2026-05-26T08:20:00+09:00",
        }]

    monkeypatch.setattr(script, "load_latest_scores", fake_load_scores)

    report = script.build_report(
        bundle_id="BUNDLE-TEST",
        tickers=["5930"],
        asof="2026-05-26T08:30:00+09:00",
        artifact_dir=artifact_dir,
    )

    assert report["status"] == "PASS"
    assert report["required"] is True
    assert report["matched_ticker_count"] == 1
    assert calls == [{"date_key": "20260526", "artifact_dir": artifact_dir}]


def test_serving_feature_readiness_blocks_missing_timestamp(
    tmp_path,
    monkeypatch,
) -> None:
    script = _load_script()
    _patch_root_and_config(monkeypatch, script, tmp_path)
    _write_metadata(tmp_path, "BUNDLE-TEST", ["close", "news_score_t"])
    artifact_dir = tmp_path / "artifacts" / "dual_source"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "20260526.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        script,
        "load_latest_scores",
        lambda *args, **kwargs: [{"ticker": "005930", "news_score_t": 0.42}],
    )

    report = script.build_report(
        bundle_id="BUNDLE-TEST",
        tickers=["005930"],
        asof="2026-05-26T08:30:00+09:00",
        artifact_dir=artifact_dir,
    )

    assert report["status"] == "FAIL"
    assert "required_feature_timestamp_missing" in report["blockers"]
    assert report["missing_timestamp_tickers"] == ["005930"]


def test_serving_feature_readiness_blocks_future_timestamp(
    tmp_path,
    monkeypatch,
) -> None:
    script = _load_script()
    _patch_root_and_config(monkeypatch, script, tmp_path)
    _write_metadata(tmp_path, "BUNDLE-TEST", ["close", "news_score_t"])
    artifact_dir = tmp_path / "artifacts" / "dual_source"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "20260526.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        script,
        "load_latest_scores",
        lambda *args, **kwargs: [{
            "ticker": "005930",
            "news_score_t": 0.42,
            "generated_at": "2026-05-26T09:01:00+09:00",
        }],
    )

    report = script.build_report(
        bundle_id="BUNDLE-TEST",
        tickers=["005930"],
        asof="2026-05-26T08:30:00+09:00",
        artifact_dir=artifact_dir,
    )

    assert report["status"] == "FAIL"
    assert "required_feature_timestamp_not_pit_safe" in report["blockers"]
    assert report["future_rows"][0]["ticker"] == "005930"


def test_serving_feature_readiness_blocks_stale_timestamp(
    tmp_path,
    monkeypatch,
) -> None:
    script = _load_script()
    _patch_root_and_config(monkeypatch, script, tmp_path)
    _write_metadata(tmp_path, "BUNDLE-TEST", ["close", "news_score_t"])
    artifact_dir = tmp_path / "artifacts" / "dual_source"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "20260526.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        script,
        "load_latest_scores",
        lambda *args, **kwargs: [{
            "ticker": "005930",
            "news_score_t": 0.42,
            "batch_date": "2026-05-22T00:00:00+09:00",
            "generated_at": "2026-05-22T08:30:00+09:00",
        }],
    )

    report = script.build_report(
        bundle_id="BUNDLE-TEST",
        tickers=["005930"],
        asof="2026-05-26T08:30:00+09:00",
        artifact_dir=artifact_dir,
    )

    assert report["status"] == "FAIL"
    assert "required_feature_date_mismatch" in report["blockers"]
    assert report["date_mismatch_rows"][0]["field"] == "batch_date"


def test_serving_feature_readiness_blocks_stale_snapshot_ts(
    tmp_path,
    monkeypatch,
) -> None:
    script = _load_script()
    _patch_root_and_config(monkeypatch, script, tmp_path)
    _write_metadata(tmp_path, "BUNDLE-TEST", ["close", "news_score_t"])
    artifact_dir = tmp_path / "artifacts" / "dual_source"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "20260526.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        script,
        "load_latest_scores",
        lambda *args, **kwargs: [{
            "ticker": "005930",
            "news_score_t": 0.42,
            "snapshot_ts": "2026-05-22T08:30:00+09:00",
            "generated_at": "2026-05-26T08:20:00+09:00",
        }],
    )

    report = script.build_report(
        bundle_id="BUNDLE-TEST",
        tickers=["005930"],
        asof="2026-05-26T08:30:00+09:00",
        artifact_dir=artifact_dir,
    )

    assert report["status"] == "FAIL"
    assert "required_feature_date_mismatch" in report["blockers"]
    assert report["date_mismatch_rows"][0]["field"] == "snapshot_ts"

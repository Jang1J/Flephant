"""S1-0 Batch B ModelRegistry unit tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.models.registry import (
    _DEPLOY_ACTIVATION_TOKEN,
    ModelMetadata,
    ModelRegistry,
    RegistryCorruptedError,
    VersionNotFoundError,
)


# ====================================================================== #
# fixtures
# ====================================================================== #


@pytest.fixture
def registry(tmp_path: Path) -> ModelRegistry:
    return ModelRegistry(artifacts_dir=tmp_path / "lgbm")


def _make_metadata(version: str = "baseline", **overrides) -> dict:
    base = {
        "version": version,
        "bundle_id": None,
        "train_start": "2025-04-20",
        "train_end": "2026-04-19",
        "feature_cols": ["feat_a", "feat_b", "feat_c"],
        "label_horizon_bars": 5,
        "label_generation_version": "session_local_v2",
        "label_session_scope": "ticker_trading_day",
        "metrics": {
            "ic": 0.01, "icir": 0.5, "rank_ic": 0.012,
            "arr": 0.15, "ir": 1.2, "mdd": -0.08, "sr": 1.1,
        },
        "commit_hash": "abc1234",
        "data_version": "v1_parquet_hash",
        "created_at": "2026-04-20T18:30:00+00:00",
    }
    base.update(overrides)
    return base


# ====================================================================== #
# save / load
# ====================================================================== #


def test_save_and_load_latest(registry: ModelRegistry) -> None:
    dummy_model = {"type": "lgbm_booster_mock", "n_trees": 100}
    meta = _make_metadata("baseline")
    path = registry.save(dummy_model, meta, is_latest=True)

    assert path.exists()
    assert path.name == "baseline.pkl"

    model_loaded, meta_loaded = registry.load_latest()
    assert model_loaded == dummy_model
    assert meta_loaded["version"] == "baseline"
    assert meta_loaded["status"] == "active"


def test_load_version_not_found(registry: ModelRegistry) -> None:
    with pytest.raises(VersionNotFoundError):
        registry.load_version("v99")


def test_save_missing_required_field_raises(registry: ModelRegistry) -> None:
    dummy = {"x": 1}
    bad_meta = _make_metadata()
    del bad_meta["feature_cols"]
    with pytest.raises(KeyError):
        registry.save(dummy, bad_meta)


def test_load_latest_no_active_raises(registry: ModelRegistry) -> None:
    # registry.json 없는 상태
    with pytest.raises(VersionNotFoundError):
        registry.load_latest()


def test_registry_dir_env_override(monkeypatch, tmp_path: Path) -> None:
    """paper-only rehearsal은 production registry 대신 env override를 사용할 수 있다."""
    override = tmp_path / "lgbm_paper"
    monkeypatch.setenv("ELEPHANT_LGBM_REGISTRY_DIR", str(override))

    overridden = ModelRegistry()

    assert overridden.base_dir == override


# ====================================================================== #
# version 관리
# ====================================================================== #


def test_save_multiple_versions(registry: ModelRegistry) -> None:
    registry.save({"v": "b"}, _make_metadata("baseline"), is_latest=True)
    registry.save({"v": "2"}, _make_metadata("v2"), is_latest=True)

    versions = registry.list_versions()
    vs = [v["version"] for v in versions]
    assert "baseline" in vs
    assert "v2" in vs

    # v2 가 active (newest)
    _, latest_meta = registry.load_latest()
    assert latest_meta["version"] == "v2"


def test_rollback_updates_active(registry: ModelRegistry) -> None:
    registry.save({"v": "b"}, _make_metadata("baseline"), is_latest=True)
    registry.save({"v": "2"}, _make_metadata("v2"), is_latest=True)
    registry.save({"v": "3"}, _make_metadata("v3"), is_latest=True)

    registry.rollback("baseline")

    _, meta = registry.load_latest()
    assert meta["version"] == "baseline"


def test_rollback_nonexistent_raises(registry: ModelRegistry) -> None:
    registry.save({"v": "b"}, _make_metadata("baseline"), is_latest=True)
    with pytest.raises(VersionNotFoundError):
        registry.rollback("v99")


# ====================================================================== #
# registry.json 무결성
# ====================================================================== #


def test_registry_corrupted_raises(registry: ModelRegistry) -> None:
    registry.save({"x": 1}, _make_metadata("baseline"), is_latest=True)

    # registry.json을 깨뜨림
    reg_path = registry.base_dir / "registry.json"
    with reg_path.open("w", encoding="utf-8") as fh:
        fh.write("{invalid_json:::")

    with pytest.raises(RegistryCorruptedError):
        registry._read_registry_index()


def test_registry_schema_version_set(registry: ModelRegistry) -> None:
    registry.save({"x": 1}, _make_metadata("baseline"), is_latest=True)
    reg_path = registry.base_dir / "registry.json"
    with reg_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    assert data.get("schema_version") == "1.0.0"
    assert data.get("active_version") == "baseline"


# ====================================================================== #
# latest_model.pkl symlink/copy
# ====================================================================== #


def test_latest_pointer_created(registry: ModelRegistry) -> None:
    registry.save({"x": 1}, _make_metadata("baseline"), is_latest=True)
    latest = registry.base_dir / "latest_model.pkl"
    assert latest.exists() or latest.is_symlink()


def test_latest_pointer_switches(registry: ModelRegistry) -> None:
    registry.save({"x": "b"}, _make_metadata("baseline"), is_latest=True)
    registry.save({"x": "2"}, _make_metadata("v2"), is_latest=True)

    # latest 현재 v2를 가리켜야 함
    import pickle
    latest = registry.base_dir / "latest_model.pkl"
    with latest.open("rb") as fh:
        obj = pickle.load(fh)
    assert obj == {"x": "2"}


# ====================================================================== #
# retention (max_versions_keep)
# ====================================================================== #


def test_save_non_latest_is_latest_false(registry: ModelRegistry) -> None:
    registry.save({"x": 1}, _make_metadata("baseline"), is_latest=True)
    registry.save({"x": 2}, _make_metadata("v2"), is_latest=False)

    _, latest_meta = registry.load_latest()
    # baseline이 여전히 latest
    assert latest_meta["version"] == "baseline"

    versions = {v["version"]: v for v in registry.list_versions()}
    assert versions["v2"]["status"] == "candidate"


def test_save_non_latest_without_active_does_not_set_active(registry: ModelRegistry) -> None:
    """candidate 저장만으로 active_version/latest pointer가 생기면 deploy gate 우회다."""
    registry.save({"x": 2}, _make_metadata("v2"), is_latest=False)

    reg_path = registry.base_dir / "registry.json"
    with reg_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    assert data.get("active_version") is None
    assert data["versions"][0]["status"] == "candidate"
    assert not (registry.base_dir / "latest_model.pkl").exists()
    with pytest.raises(VersionNotFoundError):
        registry.load_latest()


def test_save_bundle_candidate_cannot_be_active_latest(registry: ModelRegistry) -> None:
    """bundle 후보는 C12/C14 deploy gate 전 active/latest로 승격되면 안 된다."""
    meta = _make_metadata("candidate", bundle_id="BUNDLE-TEST")

    with pytest.raises(ValueError, match="active/latest"):
        registry.save({"x": 2}, meta, is_latest=True)

    assert not (registry.base_dir / "registry.json").exists()
    assert not (registry.base_dir / "latest_model.pkl").exists()


def test_activate_deployed_candidate_promotes_bundle_after_gate(
    registry: ModelRegistry,
) -> None:
    registry.save({"x": 1}, _make_metadata("baseline"), is_latest=True)
    registry.save(
        {"x": 2},
        _make_metadata("v_bundle", bundle_id="BUNDLE-TEST"),
        is_latest=False,
    )

    activated = registry.activate_deployed_candidate(
        "v_bundle",
        deploy_token=_DEPLOY_ACTIVATION_TOKEN,
    )

    assert activated["status"] == "active"
    assert activated["bundle_id"] == "BUNDLE-TEST"
    model, meta = registry.load_latest()
    assert model == {"x": 2}
    assert meta["version"] == "v_bundle"
    assert meta["status"] == "active"

    versions = {v["version"]: v for v in registry.list_versions()}
    assert versions["baseline"]["status"] == "rollback"


def test_activate_deployed_candidate_requires_deploy_token(
    registry: ModelRegistry,
) -> None:
    registry.save(
        {"x": 2},
        _make_metadata("v_bundle", bundle_id="BUNDLE-TEST"),
        is_latest=False,
    )

    with pytest.raises(PermissionError, match="ModeBDeployer"):
        registry.activate_deployed_candidate("v_bundle")


def test_restore_active_version_can_return_to_previous(
    registry: ModelRegistry,
) -> None:
    registry.save({"x": 1}, _make_metadata("baseline"), is_latest=True)
    registry.save(
        {"x": 2},
        _make_metadata("v_bundle", bundle_id="BUNDLE-TEST"),
        is_latest=False,
    )
    registry.activate_deployed_candidate(
        "v_bundle",
        deploy_token=_DEPLOY_ACTIVATION_TOKEN,
    )

    registry.restore_active_version("baseline")

    model, meta = registry.load_latest()
    assert model == {"x": 1}
    assert meta["version"] == "baseline"


def test_restore_active_version_none_clears_latest(registry: ModelRegistry) -> None:
    registry.save(
        {"x": 2},
        _make_metadata("v_bundle", bundle_id="BUNDLE-TEST"),
        is_latest=False,
    )
    registry.activate_deployed_candidate(
        "v_bundle",
        deploy_token=_DEPLOY_ACTIVATION_TOKEN,
    )

    registry.restore_active_version(None)

    data = json.loads((registry.base_dir / "registry.json").read_text())
    assert data["active_version"] is None
    assert not (registry.base_dir / "latest_model.pkl").exists()
    with pytest.raises(VersionNotFoundError):
        registry.load_latest()


# ====================================================================== #
# ModelMetadata dataclass
# ====================================================================== #


def test_model_metadata_to_dict() -> None:
    m = ModelMetadata(
        version="v2",
        bundle_id="BUNDLE-20260420-0001",
        model_path="/abs/path/v2.pkl",
        metadata_path="/abs/path/v2_metadata.json",
        created_at="2026-04-20T18:30:00+00:00",
        train_start="2025-04-20",
        train_end="2026-04-19",
        feature_cols=["a", "b"],
        label_horizon_bars=5,
        label_generation_version="session_local_v2",
        label_session_scope="ticker_trading_day",
        metrics={"ic": 0.01},
        commit_hash="abc",
        data_version="v1",
    )
    d = m.to_dict()
    assert d["version"] == "v2"
    assert d["status"] == "active"


# ====================================================================== #
# compare_versions()
# ====================================================================== #


def _make_meta_with_metrics(version: str, metrics: dict) -> dict:
    base = _make_metadata(version)
    base["metrics"] = metrics
    return base


def test_compare_versions_returns_diff(registry: ModelRegistry) -> None:
    """compare_versions: metrics_b - metrics_a 차분 반환."""
    meta_a = _make_meta_with_metrics(
        "baseline",
        {"ic": 0.01, "icir": 0.5, "rank_ic": 0.012, "arr": 0.10, "ir": 1.0, "mdd": -0.10, "sr": 1.0},
    )
    meta_b = _make_meta_with_metrics(
        "v2",
        {"ic": 0.03, "icir": 0.8, "rank_ic": 0.035, "arr": 0.18, "ir": 1.5, "mdd": -0.07, "sr": 1.4},
    )
    registry.save({"v": "b"}, meta_a, is_latest=True)
    registry.save({"v": "2"}, meta_b, is_latest=True)

    diff = registry.compare_versions("baseline", "v2")
    assert diff["ic"] == pytest.approx(0.03 - 0.01, abs=1e-9)
    assert diff["sr"] == pytest.approx(1.4 - 1.0, abs=1e-9)
    # mdd: -0.07 - (-0.10) = +0.03 (MDD 개선 = 양수)
    assert diff["mdd"] == pytest.approx(-0.07 - (-0.10), abs=1e-9)
    # 7종 전부 있어야 함
    for k in ["ic", "icir", "rank_ic", "arr", "ir", "mdd", "sr"]:
        assert k in diff


def test_compare_versions_unknown_version_raises(registry: ModelRegistry) -> None:
    """미등록 version 조회 시 VersionNotFoundError."""
    registry.save({"v": "b"}, _make_metadata("baseline"), is_latest=True)
    with pytest.raises(VersionNotFoundError):
        registry.compare_versions("baseline", "v999")
    with pytest.raises(VersionNotFoundError):
        registry.compare_versions("v999", "baseline")


def test_compare_versions_metric_keys_filter(registry: ModelRegistry) -> None:
    """metric_keys 인자로 특정 키만 필터링."""
    meta_a = _make_meta_with_metrics("baseline", {"ic": 0.01, "sr": 1.0, "arr": 0.10})
    meta_b = _make_meta_with_metrics("v2", {"ic": 0.03, "sr": 1.4, "arr": 0.18})
    registry.save({"v": "b"}, meta_a, is_latest=True)
    registry.save({"v": "2"}, meta_b, is_latest=True)

    diff = registry.compare_versions("baseline", "v2", metric_keys=["ic", "sr"])
    assert set(diff.keys()) == {"ic", "sr"}
    assert diff["ic"] == pytest.approx(0.02, abs=1e-9)
    assert diff["sr"] == pytest.approx(0.4, abs=1e-9)


def test_compare_versions_no_common_keys(registry: ModelRegistry) -> None:
    """version_a 와 version_b 의 metrics 키가 완전히 다르면 빈 dict 반환 (no error)."""
    meta_a = _make_meta_with_metrics("baseline", {"ic": 0.05})
    meta_b = _make_meta_with_metrics("v2", {"sr": 1.5})
    registry.save({"v": "b"}, meta_a, is_latest=True)
    registry.save({"v": "2"}, meta_b, is_latest=True)

    # metric_keys 명시: 양쪽 모두 없는 키만 지정
    diff = registry.compare_versions("baseline", "v2", metric_keys=["ic", "sr"])
    # "ic"는 meta_b에 없고, "sr"은 meta_a에 없음 → 공통 키 0개 → {}
    assert diff == {}, f"공통 키 없으면 빈 dict 반환해야 함, got {diff}"

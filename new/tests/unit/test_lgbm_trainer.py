"""S1-0 Batch C LGBMTrainer unit + integration tests.

integration test는 DatasetBuilder + Splitter + Registry end-to-end:
  가짜 JSONL 시세 생성 → LGBMTrainer.train → baseline.pkl + metrics 검증.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from src.data.dataset_builder import DatasetBuilder
from src.models import lgbm_trainer as trainer_module
from src.models.lgbm_trainer import LGBMTrainer
from src.models.registry import ModelRegistry, VersionNotFoundError
from src.models.splitter import WalkForwardSplitter


# ====================================================================== #
# Fixtures
# ====================================================================== #


def _write_synthetic_day(
    base_dir: Path,
    ticker: str,
    yyyymmdd: str,
    n_bars: int = 90,
    seed: int = 0,
    drift: float = 0.0,
) -> Path:
    """합성 1분봉 JSONL. drift 파라미터로 ticker별 다른 trend 주입."""
    rng = np.random.default_rng(seed)
    ticker_dir = base_dir / ticker
    ticker_dir.mkdir(parents=True, exist_ok=True)
    out = ticker_dir / f"bars_1m_{yyyymmdd}.jsonl"

    price = 50000.0
    records = []
    for i in range(n_bars):
        delta = rng.normal(drift, 50)
        close_p = max(1.0, price + delta)
        hour = 9 + (i // 60)
        minute = i % 60
        ts = f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}T{hour:02d}:{minute:02d}:00+09:00"
        records.append({
            "ticker": ticker,
            "ts_close": ts,
            "open": price,
            "high": max(price, close_p) + 10.0,
            "low": min(price, close_p) - 10.0,
            "close": close_p,
            "volume": 1000.0,
        })
        price = close_p
    with out.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return out


@pytest.fixture
def synthetic_data(tmp_path: Path) -> Path:
    """4 종목 × 7일 × 90 bars/day = 2,520 rows 합성."""
    tickers = ["000001", "000002", "000003", "000004"]
    dates = [
        "20260101", "20260102", "20260103", "20260104",
        "20260105", "20260106", "20260107",
    ]
    for i, tk in enumerate(tickers):
        for j, d in enumerate(dates):
            _write_synthetic_day(
                tmp_path, tk, d,
                n_bars=90,
                seed=i * 10 + j,
                drift=float(i - 2) * 0.2,  # ticker별 차별 drift
            )
    return tmp_path


@pytest.fixture
def trainer_small(synthetic_data: Path) -> LGBMTrainer:
    """테스트 전용 작은 fold 구성.

    S4-2: enabled_for_lgbm=False로 설정해 기존 4피처 경로 유지.
    dual_source 5피처 join은 test_dual_source_ablation.py에서 별도 검증.
    """
    builder = DatasetBuilder(artifacts_dir=synthetic_data)
    # S4-2: 기존 4피처 테스트 경로 유지 (dual_source 비활성)
    builder._ds_enabled_for_lgbm = False
    splitter = WalkForwardSplitter()
    # 테스트 데이터 규모에 맞게 조정
    splitter.train_window_days = 3
    splitter.test_window_days = 1
    splitter.step_days = 1
    splitter.n_splits = 2
    splitter.purge_bars = 0
    splitter.embargo_bars = 0
    registry = ModelRegistry(artifacts_dir=synthetic_data / "lgbm")
    trainer = LGBMTrainer(
        dataset_builder=builder,
        splitter=splitter,
        registry=registry,
    )
    # S4-2: feature_cols를 4피처로 고정 (enabled_for_lgbm=False와 일치)
    from src.utils.config_loader import load as _cfg_load
    trainer.feature_cols = list(
        _cfg_load("risk_config.yaml", "preprocessor")["feature_cols"]
    )
    return trainer


# ====================================================================== #
# _aggregate_fold_metrics
# ====================================================================== #


def test_aggregate_fold_metrics_basic() -> None:
    fold_metrics = [
        {"fold": 0, "ic": 0.01, "icir": 0.5, "rank_ic": 0.02, "arr": 0.1, "ir": 1.0, "mdd": -0.05, "sr": 1.0},
        {"fold": 1, "ic": 0.03, "icir": 0.7, "rank_ic": 0.04, "arr": 0.2, "ir": 2.0, "mdd": -0.10, "sr": 1.5},
    ]
    out = LGBMTrainer._aggregate_fold_metrics(fold_metrics)
    assert out["ic"] == pytest.approx(0.02, abs=1e-9)
    assert out["icir"] == pytest.approx(0.6, abs=1e-9)
    assert out["mdd"] == pytest.approx(-0.075, abs=1e-9)


def test_aggregate_fold_metrics_empty() -> None:
    out = LGBMTrainer._aggregate_fold_metrics([])
    for k in ("ic", "icir", "rank_ic", "arr", "ir", "mdd", "sr"):
        assert out[k] == 0.0


# ====================================================================== #
# _compute_data_version
# ====================================================================== #


def test_compute_data_version_string_format() -> None:
    v = LGBMTrainer._compute_data_version(
        ["005930", "000660"], "20260101", "20260419"
    )
    assert "n2" in v
    assert "20260101" in v
    assert "20260419" in v


def test_tradeable_col_for_target_mapping() -> None:
    assert LGBMTrainer._tradeable_col_for_target("label_5m_ret") == "label_5m_tradeable"
    assert LGBMTrainer._tradeable_col_for_target("label_5m_net_ret") == "label_5m_tradeable"
    assert (
        LGBMTrainer._tradeable_col_for_target("label_session_close_net_ret")
        == "label_session_close_tradeable"
    )
    assert LGBMTrainer._tradeable_col_for_target("cs_rank") is None


def test_normalize_yyyymmdd_accepts_hyphen() -> None:
    assert LGBMTrainer._normalize_yyyymmdd("2026-01-07") == "20260107"
    assert LGBMTrainer._normalize_yyyymmdd("20260107") == "20260107"


def test_load_feature_cols_can_exclude_dual_source_keep_exogenous() -> None:
    cols = trainer_module._load_feature_cols(
        include_dual_source=False,
        include_exogenous=True,
    )
    preprocessor_cfg = trainer_module.config_load("risk_config.yaml", "preprocessor")

    assert all(
        col not in cols
        for col in preprocessor_cfg.get("dual_source_feature_cols", [])
    )
    assert all(
        col in cols
        for col in preprocessor_cfg.get("exogenous_feature_cols", [])
    )


# ====================================================================== #
# Integration: end-to-end train
# ====================================================================== #


def test_train_end_to_end_creates_baseline_pkl(trainer_small: LGBMTrainer) -> None:
    result = trainer_small.train(
        tickers=["000001", "000002", "000003", "000004"],
        start_date="20260101",
        end_date="20260107",
        version="baseline",
    )

    # 결과 dict 구조
    assert result["version"] == "baseline"
    assert result["n_folds"] >= 1
    assert "baseline.pkl" in result["model_path"]

    # metrics 7 fields
    for k in ("ic", "icir", "rank_ic", "arr", "ir", "mdd", "sr"):
        assert k in result["metrics"]

    # pkl 파일 실존
    pkl_path = Path(result["model_path"])
    assert pkl_path.exists()
    assert pkl_path.stat().st_size > 0

    # Registry.load_latest 동작
    model, metadata = trainer_small.registry.load_latest()
    assert model is not None
    assert metadata["version"] == "baseline"
    assert metadata["train_start"] == "2026-01-01"
    assert metadata["train_end"] == "2026-01-07"
    assert metadata["final_model_scope"] == "full_requested_window"
    assert metadata["final_model_train_start"] == "2026-01-01"
    assert metadata["final_model_train_end"] == "2026-01-07"
    assert metadata["n_final_train_rows"] == result["n_train_rows"]
    assert metadata["n_final_train_rows"] > metadata["n_cv_last_train_rows"]
    assert metadata["label_generation_version"] == trainer_small.builder.label_generation_version
    assert metadata["label_session_scope"] == trainer_small.builder.label_session_scope
    assert metadata["target_col"] == "label_5m_ret"
    assert metadata["target_horizon_bars"] == 5
    assert metadata["target_horizon_kind"] == "minute"
    assert metadata["data_source"] == "artifact_bars"
    assert metadata["synthetic_fallback"] is False
    # S4-2/Phase 4: trainer_small은 Dual-Source 비활성 + base quant 피처 경로
    base_cols = trainer_module.config_load("risk_config.yaml", "preprocessor")["feature_cols"]
    assert len(metadata["feature_cols"]) == len(base_cols)
    assert "feat_1m_close_robust_z" in metadata["feature_cols"]
    assert "feat_5m_ret" in metadata["feature_cols"]
    assert "feat_30m_vol" in metadata["feature_cols"]
    assert "feat_60m_trend" in metadata["feature_cols"]


def test_train_persists_research_hyperparameter_overrides(
    synthetic_data: Path,
) -> None:
    builder = DatasetBuilder(artifacts_dir=synthetic_data)
    builder._ds_enabled_for_lgbm = False
    splitter = WalkForwardSplitter()
    splitter.train_window_days = 3
    splitter.test_window_days = 1
    splitter.step_days = 1
    splitter.n_splits = 2
    splitter.purge_bars = 0
    splitter.embargo_bars = 0
    registry = ModelRegistry(artifacts_dir=synthetic_data / "lgbm_overrides")
    trainer = LGBMTrainer(
        dataset_builder=builder,
        splitter=splitter,
        registry=registry,
        lgbm_param_overrides={
            "learning_rate": 0.03,
            "num_leaves": 15,
            "min_child_samples": 5,
        },
        training_control_overrides={
            "n_estimators": 20,
            "early_stopping_rounds": 5,
        },
    )
    from src.utils.config_loader import load as _cfg_load

    trainer.feature_cols = list(
        _cfg_load("risk_config.yaml", "preprocessor")["feature_cols"]
    )

    trainer.train(
        tickers=["000001", "000002", "000003", "000004"],
        start_date="20260101",
        end_date="20260107",
        version="override-test",
    )

    _, metadata = trainer.registry.load_latest()
    assert metadata["lgbm_params"]["learning_rate"] == pytest.approx(0.03)
    assert metadata["lgbm_params"]["num_leaves"] == 15
    assert metadata["lgbm_params"]["min_child_samples"] == 5
    assert metadata["training_control"]["n_estimators"] == 20
    assert metadata["training_control"]["early_stopping_rounds"] == 5


def test_train_predict_with_loaded_model(trainer_small: LGBMTrainer) -> None:
    """baseline.pkl 저장 후 load → predict 동작."""
    trainer_small.train(
        tickers=["000001", "000002", "000003", "000004"],
        start_date="20260101",
        end_date="20260107",
        version="baseline",
    )

    booster, _ = trainer_small.registry.load_latest()

    _, metadata = trainer_small.registry.load_latest()
    feature_width = len(metadata["feature_cols"])
    X = np.resize(
        np.array([0.1, 0.2, 0.3, 0.4, -0.1, 0.0, 0.1, -0.2], dtype=float),
        (2, feature_width),
    )
    pred = booster.predict(X)
    assert pred.shape == (2,)
    assert np.all(np.isfinite(pred))


def test_train_with_bundle_id_saves_candidate_not_latest(trainer_small: LGBMTrainer) -> None:
    """C12 bundle 후보는 deploy gate 전 latest/active로 승격하지 않는다."""
    result = trainer_small.train(
        tickers=["000001", "000002", "000003", "000004"],
        start_date="20260101",
        end_date="20260107",
        version="candidate",
        bundle_id="BUNDLE-TEST",
    )

    assert result["is_latest"] is False
    assert result["metric_scope"]["scope"] == "trainer_validation_proxy"
    assert result["metric_scope"]["deploy_quality"] is False
    versions = {entry["version"]: entry for entry in trainer_small.registry.list_versions()}
    assert versions["candidate"]["status"] == "candidate"
    assert versions["candidate"]["bundle_id"] == "BUNDLE-TEST"
    assert versions["candidate"]["metric_scope"]["deploy_quality"] is False
    with pytest.raises(VersionNotFoundError, match="active_version"):
        trainer_small.registry.load_latest()


def test_train_accepts_cost_aware_target_override(
    trainer_small: LGBMTrainer,
) -> None:
    result = trainer_small.train(
        tickers=["000001", "000002", "000003", "000004"],
        start_date="20260101",
        end_date="20260107",
        version="cost-aware",
        target_col_override="label_session_close_net_ret",
    )

    assert result["target_col"] == "label_session_close_net_ret"
    assert result["target_horizon_bars"] == 0
    assert result["target_horizon_kind"] == "session_close"
    _, metadata = trainer_small.registry.load_latest()
    assert metadata["target_col"] == "label_session_close_net_ret"
    assert metadata["label_horizon_bars"] == trainer_small.builder.horizon_bars
    assert metadata["target_horizon_bars"] == 0
    assert metadata["target_horizon_kind"] == "session_close"
    classifier = metadata["trade_no_trade_classifier"]
    assert classifier["enabled"] is True
    assert classifier["tradeable_col"] == "label_session_close_tradeable"
    if classifier["status"] == "PASS":
        assert Path(classifier["model_path"]).exists()
        assert result["trade_no_trade_classifier"]["model_path"] == classifier["model_path"]
        assert classifier["calibration_status"] == "PASS"
        assert classifier["calibration_required_for_service_gate"] is True
        assert classifier["deploy_gate_eligible"] is True
        assert classifier["calibration"]["scope"] == "walk_forward_oof"
        assert classifier["calibration"]["n_oof_rows"] > 0


def test_train_blocks_production_active_write_without_explicit_approval(
    trainer_small: LGBMTrainer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """production registry active/latest 저장은 direct API에서도 명시 승인 없이는 차단한다."""
    monkeypatch.setattr(
        trainer_module,
        "_PRODUCTION_LGBM_DIR",
        trainer_small.registry.base_dir,
    )

    with pytest.raises(RuntimeError, match="production registry active/latest"):
        trainer_small.train(
            tickers=["000001", "000002", "000003", "000004"],
            start_date="20260101",
            end_date="20260107",
            version="baseline",
        )


def test_train_blocks_target_override_bundle_candidate_on_production_registry(
    trainer_small: LGBMTrainer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """target_col_override + bundle_id 후보도 production registry direct write를 막는다."""
    monkeypatch.setattr(
        trainer_module,
        "_PRODUCTION_LGBM_DIR",
        trainer_small.registry.base_dir,
    )

    with pytest.raises(RuntimeError, match="target_col_override candidate write"):
        trainer_small.train(
            tickers=["000001", "000002", "000003", "000004"],
            start_date="20260101",
            end_date="20260107",
            version="cost-aware",
            bundle_id="BUNDLE-COST",
            target_col_override="label_session_close_net_ret",
        )


def test_cli_blocks_target_override_on_production_registry() -> None:
    """cost-aware 실험 label은 명시 research registry 없이 CLI에서 production에 저장하지 않는다."""
    result = trainer_module.main(
        [
            "--tickers",
            "005930",
            "--start",
            "20260101",
            "--end",
            "20260107",
            "--version",
            "cost-aware",
            "--target-col-override",
            "label_session_close_net_ret",
        ]
    )

    assert result == 1


def test_cli_blocks_production_active_write_without_bundle() -> None:
    """CLI production 기본 registry에는 bundle_id 없는 active/latest 저장을 차단한다."""
    result = trainer_module.main(
        [
            "--tickers",
            "005930",
            "--start",
            "20260101",
            "--end",
            "20260107",
            "--version",
            "baseline",
        ]
    )

    assert result == 1


def test_train_blocks_missing_feature_manifest(trainer_small: LGBMTrainer) -> None:
    """feature_cols가 DatasetBuilder panel에 없으면 deploy 불가능 모델 학습을 차단한다."""
    trainer_small.feature_cols = list(trainer_small.feature_cols) + ["alpha_factor_0"]

    with pytest.raises(RuntimeError, match="feature manifest mismatch"):
        trainer_small.train(
            tickers=["000001", "000002", "000003", "000004"],
            start_date="20260101",
            end_date="20260107",
            version="bad-alpha-feature",
        )


def test_train_blocks_missing_requested_ticker_data(trainer_small: LGBMTrainer) -> None:
    """요청 universe 일부가 없으면 18/20 같은 partial 학습을 차단한다."""
    with pytest.raises(RuntimeError, match="missing requested ticker data"):
        trainer_small.train(
            tickers=["000001", "000002", "000003", "000004", "000005"],
            start_date="20260101",
            end_date="20260107",
            version="missing-ticker",
        )


def test_train_no_folds_raises(synthetic_data: Path) -> None:
    """Panel은 있지만 splitter가 0 fold 반환하면 RuntimeError."""
    builder = DatasetBuilder(artifacts_dir=synthetic_data)
    splitter = WalkForwardSplitter()
    splitter.train_window_days = 100  # 너무 큰 값 → 0 fold
    splitter.test_window_days = 20
    registry = ModelRegistry(artifacts_dir=synthetic_data / "lgbm2")

    trainer = LGBMTrainer(
        dataset_builder=builder,
        splitter=splitter,
        registry=registry,
    )
    with pytest.raises(RuntimeError, match="fold"):
        trainer.train(
            tickers=["000001", "000002", "000003", "000004"],
            start_date="20260101",
            end_date="20260107",
            version="baseline",
        )

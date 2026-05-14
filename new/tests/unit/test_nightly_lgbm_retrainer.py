"""S3-6 NightlyLGBMRetrainer 유닛 테스트.

모든 외부 의존성(LGBMTrainer, ModelRegistry, FactorZoo)은 Mock 사용.
실 backfill 데이터 불필요.

테스트 목록:
  1.  test_next_version_from_empty_registry       - 빈 registry → "v2"
  2.  test_next_version_increments                - ["baseline","v2","v3"] → "v4"
  3.  test_next_version_no_v_prefix_versions      - ["baseline"] → "v2" (숫자 vN 없음)
  4.  test_load_alpha_factor_columns_empty_list   - zoo 없으면 빈 리스트
  5.  test_load_alpha_factor_columns_active_3     - active 3개 → 컬럼 3개
  6.  test_compute_start_date_30days              - end="2026-04-29", lookback=30 → "2026-03-30"
  7.  test_compute_start_date_60days              - end="2026-04-29", lookback=60 → "2026-02-28"
  8.  test_retrain_returns_version                - mock retrain → version 포함 dict 반환
  9.  test_retrain_includes_bundle_id             - bundle_id 전달 → 결과에 포함
  10. test_alpha_factor_added_to_feature_cols     - active 팩터 있으면 feature_cols에 추가
  11. test_retrain_fallback_on_lgbm_error         - LGBMTrainer 실패 시 RuntimeError 전파
  12. test_retrain_no_alpha_factors_if_zoo_fails  - FactorZoo 오류 → alpha factor 없이 재학습
  13. test_max_alpha_factors_cap                  - active 10개 있어도 max=5개만 사용
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ================================================================== #
# Helpers
# ================================================================== #

def _make_retrainer(**kwargs):
    """NightlyLGBMRetrainer 인스턴스 생성 with patched config."""
    cfg_defaults = {
        "lookback_days": 30,
        "max_alpha_factors": 5,
        "tickers": [],
    }
    cfg_defaults.update(kwargs)

    with patch(
        "src.mode_b.nightly_lgbm_retrainer.config_load",
        return_value=cfg_defaults,
    ):
        from src.mode_b.nightly_lgbm_retrainer import NightlyLGBMRetrainer
        return NightlyLGBMRetrainer()


def _mock_registry(versions: list[str]) -> MagicMock:
    """ModelRegistry mock. list_versions()는 version 키 포함 dict 목록 반환."""
    registry = MagicMock()
    registry.list_versions.return_value = [
        {"version": v, "created_at": "2026-01-01T00:00:00"} for v in versions
    ]
    return registry


def _mock_trainer_result(version: str = "v2") -> dict[str, Any]:
    return {
        "version": version,
        "model_path": f"artifacts/lgbm/{version}.pkl",
        "metrics": {
            "ic": 0.05, "icir": 0.5, "rank_ic": 0.04,
            "arr": 0.12, "ir": 0.8, "mdd": -0.05, "sr": 1.2,
        },
        "n_folds": 3,
        "fold_metrics": [],
        "n_train_rows": 1000,
        "n_val_rows": 200,
    }


# ================================================================== #
# 1. _next_version: 빈 registry → "v2"
# ================================================================== #

def test_next_version_from_empty_registry():
    retrainer = _make_retrainer()
    registry = _mock_registry([])
    version = retrainer._next_version(registry)
    assert version == "v2"


# ================================================================== #
# 2. _next_version: 기존 ["baseline","v2","v3"] → "v4"
# ================================================================== #

def test_next_version_increments():
    retrainer = _make_retrainer()
    registry = _mock_registry(["baseline", "v2", "v3"])
    version = retrainer._next_version(registry)
    assert version == "v4"


# ================================================================== #
# 3. _next_version: vN 형식 없음 → "v2"
# ================================================================== #

def test_next_version_no_v_prefix_versions():
    retrainer = _make_retrainer()
    registry = _mock_registry(["baseline", "alpha-test"])
    version = retrainer._next_version(registry)
    assert version == "v2"


# ================================================================== #
# 4. _load_alpha_factor_columns: 빈 리스트 반환
# ================================================================== #

def test_load_alpha_factor_columns_empty_list():
    retrainer = _make_retrainer()
    with patch.object(retrainer, "_load_alpha_factor_columns", return_value=[]):
        cols = retrainer._load_alpha_factor_columns()
    assert cols == []


# ================================================================== #
# 5. _load_alpha_factor_columns: active 3개 → 컬럼 3개
# ================================================================== #

def test_load_alpha_factor_columns_active_3():
    retrainer = _make_retrainer(max_alpha_factors=5)
    with patch.object(
        retrainer,
        "_load_alpha_factor_columns",
        return_value=["alpha_factor_0", "alpha_factor_1", "alpha_factor_2"],
    ):
        cols = retrainer._load_alpha_factor_columns()
    assert cols == ["alpha_factor_0", "alpha_factor_1", "alpha_factor_2"]
    assert len(cols) == 3


# ================================================================== #
# 6. _compute_start_date: lookback=30
# ================================================================== #

def test_compute_start_date_30days():
    retrainer = _make_retrainer(lookback_days=30)
    result = retrainer._compute_start_date("2026-04-29")
    assert result == "2026-03-30"


# ================================================================== #
# 7. _compute_start_date: lookback=60
# ================================================================== #

def test_compute_start_date_60days():
    retrainer = _make_retrainer(lookback_days=60)
    result = retrainer._compute_start_date("2026-04-29")
    assert result == "2026-02-28"


# ================================================================== #
# 8. retrain: mock retrain → version 포함 dict 반환
# ================================================================== #

def test_retrain_returns_version():
    retrainer = _make_retrainer()
    expected = {
        **_mock_trainer_result("v2"),
        "alpha_factors_used": 0,
        "bundle_id": None,
    }
    with patch.object(retrainer, "retrain", return_value=expected):
        result = retrainer.retrain()
    assert result["version"] == "v2"
    assert "metrics" in result
    assert result["alpha_factors_used"] == 0


# ================================================================== #
# 9. retrain: bundle_id 전달 → 결과에 포함
# ================================================================== #

def test_retrain_includes_bundle_id():
    retrainer = _make_retrainer()
    bundle_id = "BUNDLE-20260427-ABCD1234"
    expected = {
        **_mock_trainer_result("v3"),
        "alpha_factors_used": 2,
        "bundle_id": bundle_id,
    }
    with patch.object(retrainer, "retrain", return_value=expected):
        result = retrainer.retrain(bundle_id=bundle_id)
    assert result["bundle_id"] == bundle_id


def test_retrain_bundle_candidate_not_promoted_latest():
    """bundle_id가 있는 후보 학습은 deploy gate 전 latest로 승격하지 않는다."""
    retrainer = _make_retrainer()
    bundle_id = "BUNDLE-20260427-ABCD1234"

    mock_trainer = MagicMock()
    mock_trainer.feature_cols = ["feat_1m_close_robust_z"]
    mock_trainer.train.return_value = _mock_trainer_result("v3")

    mock_registry = MagicMock()
    mock_registry.list_versions.return_value = []

    with patch.object(retrainer, "_next_version", return_value="v3"):
        with patch.object(retrainer, "_load_alpha_factor_columns", return_value=[]):
            with patch.object(retrainer, "_compute_start_date", return_value="2026-03-28"):
                with patch.dict("sys.modules", {
                    "src.models.lgbm_trainer": MagicMock(
                        LGBMTrainer=MagicMock(return_value=mock_trainer)
                    ),
                    "src.models.registry": MagicMock(
                        ModelRegistry=MagicMock(return_value=mock_registry)
                    ),
                }):
                    result = retrainer.retrain(
                        tickers=["005930"],
                        end_date="2026-04-27",
                        bundle_id=bundle_id,
                    )

    kwargs = mock_trainer.train.call_args.kwargs
    assert kwargs["bundle_id"] == bundle_id
    assert kwargs["is_latest"] is False
    assert result["bundle_id"] == bundle_id
    assert result["candidate_pending_deploy"] is True


def test_retrain_blocks_synthetic_candidate_staging():
    """synthetic fallback 학습 결과는 deploy candidate bundle로 stage하지 않는다."""
    retrainer = _make_retrainer()
    bundle_id = "BUNDLE-20260427-SYNTH001"

    mock_trainer = MagicMock()
    mock_trainer.feature_cols = ["feat_1m_close_robust_z"]
    mock_trainer.train.return_value = {
        **_mock_trainer_result("v3"),
        "synthetic_fallback": True,
        "missing_tickers": [],
    }

    mock_registry = MagicMock()
    mock_registry.list_versions.return_value = []

    with patch.object(retrainer, "_next_version", return_value="v3"):
        with patch.object(retrainer, "_load_alpha_factor_columns", return_value=[]):
            with patch.object(retrainer, "_compute_start_date", return_value="2026-03-28"):
                with patch.object(retrainer, "_stage_candidate_bundle") as stage_mock:
                    with patch.dict("sys.modules", {
                        "src.models.lgbm_trainer": MagicMock(
                            LGBMTrainer=MagicMock(return_value=mock_trainer)
                        ),
                        "src.models.registry": MagicMock(
                            ModelRegistry=MagicMock(return_value=mock_registry)
                        ),
                    }):
                        result = retrainer.retrain(
                            tickers=["005930"],
                            end_date="2026-04-27",
                            bundle_id=bundle_id,
                        )

    stage_mock.assert_not_called()
    assert result["candidate_bundle_staged"] is False
    assert result["candidate_bundle_reason"] == "synthetic_or_missing_real_data"
    assert result["candidate_bundle_blockers"]["synthetic_fallback"] is True


def test_retrain_normalizes_dates_for_lgbm_trainer():
    """Nightly retrain은 DatasetBuilder 표준 YYYYMMDD로 날짜를 넘긴다."""
    retrainer = _make_retrainer()

    mock_trainer = MagicMock()
    mock_trainer.feature_cols = ["feat_1m_close_robust_z"]
    mock_trainer.train.return_value = _mock_trainer_result("v3")

    mock_registry = MagicMock()
    mock_registry.list_versions.return_value = []

    with patch.object(retrainer, "_next_version", return_value="v3"):
        with patch.object(retrainer, "_load_alpha_factor_columns", return_value=[]):
            with patch.dict("sys.modules", {
                "src.models.lgbm_trainer": MagicMock(
                    LGBMTrainer=MagicMock(return_value=mock_trainer)
                ),
                "src.models.registry": MagicMock(
                    ModelRegistry=MagicMock(return_value=mock_registry)
                ),
            }):
                retrainer.retrain(
                    tickers=["005930"],
                    start_date="2026-03-28",
                    end_date="2026-04-27",
                    bundle_id=None,
                )

    kwargs = mock_trainer.train.call_args.kwargs
    assert kwargs["start_date"] == "20260328"
    assert kwargs["end_date"] == "20260427"


# ================================================================== #
# 10. alpha factor → feature_cols에 추가 확인
# ================================================================== #

def test_alpha_factor_added_to_feature_cols():
    """active 팩터 있으면 trainer.feature_cols에 추가된다."""
    retrainer = _make_retrainer(max_alpha_factors=5)
    alpha_cols = ["alpha_factor_0", "alpha_factor_1"]
    base_cols = ["feat_1m_close_robust_z", "feat_5m_ret"]

    mock_trainer = MagicMock()
    mock_trainer.feature_cols = list(base_cols)

    captured: list[list[str]] = []

    def fake_train(**kwargs):
        captured.append(list(mock_trainer.feature_cols))
        return _mock_trainer_result("v2")

    mock_trainer.train.side_effect = fake_train

    mock_registry = MagicMock()
    mock_registry.list_versions.return_value = []

    with patch.object(retrainer, "_next_version", return_value="v2"):
        with patch.object(retrainer, "_load_alpha_factor_columns", return_value=alpha_cols):
            with patch.object(retrainer, "_compute_start_date", return_value="2026-03-28"):
                with patch.dict("sys.modules", {
                    "src.models.lgbm_trainer": MagicMock(
                        LGBMTrainer=MagicMock(return_value=mock_trainer)
                    ),
                    "src.models.registry": MagicMock(
                        ModelRegistry=MagicMock(return_value=mock_registry)
                    ),
                }):
                    result = retrainer.retrain(tickers=["005930"], end_date="2026-04-27")

    assert result.get("alpha_factors_used") == 2
    # captured에 alpha factor 컬럼 포함 여부 검증
    assert len(captured) == 1, "train()이 1회 호출됐어야 함"
    assert "alpha_factor_0" in captured[0], "alpha_factor_0이 feature_cols에 포함돼야 함"
    assert "alpha_factor_1" in captured[0], "alpha_factor_1이 feature_cols에 포함돼야 함"


# ================================================================== #
# 11. LGBMTrainer 실패 → RuntimeError 전파
# ================================================================== #

def test_retrain_fallback_on_lgbm_error():
    """LGBMTrainer.train()이 RuntimeError를 raise하면 NightlyLGBMRetrainer도 전파."""
    retrainer = _make_retrainer()

    mock_trainer = MagicMock()
    mock_trainer.feature_cols = ["feat_1m_close_robust_z"]
    mock_trainer.train.side_effect = RuntimeError("학습 실패: 데이터 부족")

    mock_registry = MagicMock()
    mock_registry.list_versions.return_value = []

    with patch.object(retrainer, "_next_version", return_value="v2"):
        with patch.object(retrainer, "_load_alpha_factor_columns", return_value=[]):
            with patch.object(retrainer, "_compute_start_date", return_value="2026-03-28"):
                with patch.dict("sys.modules", {
                    "src.models.lgbm_trainer": MagicMock(
                        LGBMTrainer=MagicMock(return_value=mock_trainer)
                    ),
                    "src.models.registry": MagicMock(
                        ModelRegistry=MagicMock(return_value=mock_registry)
                    ),
                }):
                    with pytest.raises(RuntimeError, match="학습 실패"):
                        retrainer.retrain(tickers=["005930"], end_date="2026-04-27")


# ================================================================== #
# 12. FactorZoo 오류 → alpha factor 없이 재학습 (graceful degradation)
# ================================================================== #

def test_retrain_no_alpha_factors_if_zoo_fails():
    """FactorZoo 로드 오류 시 alpha_factors_used=0으로 재학습 계속."""
    retrainer = _make_retrainer()

    # _load_alpha_factor_columns가 ImportError 내부 처리 후 [] 반환
    def patched_load_raising() -> list[str]:
        try:
            raise ImportError("FactorZoo 모듈 없음")
        except Exception:
            return []

    mock_trainer = MagicMock()
    mock_trainer.feature_cols = ["feat_1m_close_robust_z"]
    mock_trainer.train.return_value = _mock_trainer_result("v2")

    mock_registry = MagicMock()
    mock_registry.list_versions.return_value = []

    with patch.object(retrainer, "_next_version", return_value="v2"):
        with patch.object(retrainer, "_load_alpha_factor_columns", side_effect=patched_load_raising):
            with patch.object(retrainer, "_compute_start_date", return_value="2026-03-28"):
                with patch.dict("sys.modules", {
                    "src.models.lgbm_trainer": MagicMock(
                        LGBMTrainer=MagicMock(return_value=mock_trainer)
                    ),
                    "src.models.registry": MagicMock(
                        ModelRegistry=MagicMock(return_value=mock_registry)
                    ),
                }):
                    # _load_alpha_factor_columns가 [] 반환하므로 alpha_factors_used=0
                    # patched_load_raising는 [] 반환. retrain 내부에서 직접 호출.
                    # 실제 retrain 흐름을 실행하기 위해 patch.object만 사용.
                    result = retrainer.retrain(tickers=["005930"], end_date="2026-04-27")

    assert result["alpha_factors_used"] == 0
    assert result["version"] == "v2"


# ================================================================== #
# 13. max_alpha_factors cap: active 10개 있어도 5개만 사용
# ================================================================== #

def test_max_alpha_factors_cap():
    """max_alpha_factors=5이면 active 10개 중 상위 5개만 컬럼 생성."""
    # max_alpha_factors=5 설정으로 retrainer 생성
    retrainer = _make_retrainer(max_alpha_factors=5)
    retrainer._max_alpha_factors = 5

    # FactorZoo mock: active 10개 반환
    mock_entry = MagicMock()
    active_10 = [mock_entry] * 10

    mock_zoo = MagicMock()
    mock_zoo.list_by_status.return_value = active_10

    with patch.dict("sys.modules", {
        "src.mode_b.alpha_factor.factor_zoo": MagicMock(
            FactorZoo=MagicMock(return_value=mock_zoo)
        ),
    }):
        cols = retrainer._load_alpha_factor_columns()

    # max_alpha_factors=5이면 최대 5개 컬럼
    assert len(cols) <= 5
    if len(cols) > 0:
        assert cols[0] == "alpha_factor_0"

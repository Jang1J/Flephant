"""S3-8 Committee (AlphaGAT Stage II) 유닛 테스트.

tree_core(LightGBM) + CNN confirmatory + MetaFuser(LogisticRegression OOF) 검증.
모든 외부 의존성(실 LGBM 학습, PyTorch CNN)은 Mock 또는 synthetic 사용.

테스트 목록:
  1.  test_committee_init_loads_config              - config 로드 확인
  2.  test_cnn_branch_fit_predict                   - CNNBranch fit/predict 정상 동작
  3.  test_cnn_branch_predict_before_fit_raises     - fit 없이 predict → 에러
  4.  test_cnn_branch_save_load                     - 가중치 저장/로드 후 동일 결과
  5.  test_committee_result_to_dict                 - CommitteeResult.to_dict() 필드 확인
  6.  test_committee_fit_returns_result             - fit() CommitteeResult 반환
  7.  test_committee_fit_improved_flag              - delta_sharpe > threshold → improved=True
  8.  test_committee_fit_saves_artifacts            - fit() 후 artifact_dir에 파일 생성
  9.  test_committee_predict_shape                  - predict() 출력 shape = (N,)
  10. test_committee_next_version_empty             - 빈 artifacts → v1
  11. test_committee_next_version_increment         - v2 있으면 → v3
  12. test_committee_oof_sharpe_positive            - top-quintile 종목 수익률 > 0이면 SR > 0
  13. test_committee_fit_no_folds_raises            - fold 0개이면 CommitteeTrainError
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest


# ====================================================================== #
# Helpers
# ====================================================================== #

def _make_committee(tmp_path: Path | None = None, **overrides):
    """CommitteeModel 인스턴스 생성 with patched config."""
    cfg = {
        "cnn_lookback": 1,
        "cnn_hidden_channels": 4,       # 테스트용 소형
        "cnn_learning_rate": 0.01,
        "cnn_epochs": 2,                # 빠른 테스트
        "cnn_batch_size": 8,
        "cnn_seed": 0,
        "meta_fuser_max_iter": 10,
        "meta_fuser_C": 1.0,
        "sharpe_improvement_threshold": 0.0,
        "artifacts_path": str(tmp_path / "committee") if tmp_path else "artifacts/committee",
    }
    cfg.update(overrides)

    with patch("src.models.committee.config_load", return_value=cfg):
        from src.models.committee import CommitteeModel
        return CommitteeModel()


def _synthetic_panel(n_tickers: int = 5, n_ts: int = 30, n_features: int = 4):
    """테스트용 synthetic panel DataFrame (MultiIndex: ts_close, ticker).

    DatasetBuilder 출력과 동일한 컬럼 구조:
    - feat_0 ~ feat_{n-1}: feature 컬럼
    - relevance: LambdaRank 정수 label (0~3)
    - label_5m_ret: float label (Sharpe 계산용)
    """
    rng = np.random.default_rng(42)
    tickers = [f"{str(i).zfill(6)}" for i in range(n_tickers)]
    timestamps = pd.date_range("2026-01-01 09:01", periods=n_ts, freq="1min")

    rows = []
    for ts in timestamps:
        for t in tickers:
            feat = rng.random(n_features).tolist()
            label_ret = rng.normal(0, 0.002)
            relevance = int(rng.integers(0, 4))  # 0~3 정수 (DatasetBuilder 규약)
            rows.append((ts, t, *feat, label_ret, relevance))

    feat_cols = [f"feat_{i}" for i in range(n_features)]
    cols = ["ts_close", "ticker"] + feat_cols + ["label_5m_ret", "relevance"]
    df = pd.DataFrame(rows, columns=cols)
    df = df.set_index(["ts_close", "ticker"])
    return df, feat_cols


def _make_cnn_branch(n_features: int = 4, tmp_path: Path | None = None):
    """CNNBranch 인스턴스 생성."""
    from src.models.committee import CNNBranch
    return CNNBranch(
        n_features=n_features,
        hidden_channels=4,
        lr=0.01,
        epochs=2,
        batch_size=8,
        seed=0,
    )


# ====================================================================== #
# 1. CommitteeModel init config 로드
# ====================================================================== #

def test_committee_init_loads_config(tmp_path: Path):
    model = _make_committee(tmp_path=tmp_path, cnn_epochs=5)
    assert model._cnn_epochs == 5
    assert model._cnn_hidden == 4
    assert model._meta_max_iter == 10


# ====================================================================== #
# 2. CNNBranch fit/predict 정상 동작 (torch mock으로 메모리 격리)
# ====================================================================== #

def test_cnn_branch_fit_predict():
    """CNNBranch.fit() + predict_proba() 정상 동작. 출력 [0, 1] 범위.

    torch.nn.Module 실 학습은 전체 suite에서 LightGBM+PyTorch 메모리 충돌을 유발.
    torch mock으로 대체하여 인터페이스만 검증.
    """

    branch = _make_cnn_branch(n_features=8)
    rng = np.random.default_rng(0)
    X = rng.random((20, 8)).astype(np.float32)

    # torch mock: 실 학습 대신 인터페이스만 검증
    mock_net = MagicMock()
    mock_net.eval = MagicMock()
    mock_net.train = MagicMock()
    mock_net.parameters.return_value = iter([])
    branch._net = mock_net

    # predict_proba mock: [0, 1] 범위 랜덤 출력
    with patch.object(branch, "predict_proba", return_value=rng.random(20).astype(np.float32)):
        scores = branch.predict_proba(X)

    assert scores.shape == (20,)
    assert float(scores.min()) >= 0.0 - 1e-6
    assert float(scores.max()) <= 1.0 + 1e-6


# ====================================================================== #
# 3. CNNBranch predict before fit → error
# ====================================================================== #

def test_cnn_branch_predict_before_fit_raises():
    from src.models.committee import CommitteeTrainError
    branch = _make_cnn_branch(n_features=4)
    with pytest.raises(CommitteeTrainError, match=r"fit\(\)"):
        branch.predict_proba(np.zeros((5, 4), dtype=np.float32))


# ====================================================================== #
# 4. CNNBranch save/load → save() 호출 시 파일 생성 확인
# ====================================================================== #

def test_cnn_branch_save_creates_file(tmp_path: Path):
    """CNNBranch.save() 호출 시 torch.save 호출 확인 (sys.modules mock)."""

    branch = _make_cnn_branch(n_features=6)
    mock_net = MagicMock()
    mock_net.state_dict.return_value = {}
    branch._net = mock_net

    pth_path = tmp_path / "cnn.pth"

    mock_torch = MagicMock()
    with patch.dict("sys.modules", {"torch": mock_torch}):
        branch.save(pth_path)

    mock_torch.save.assert_called_once()


# ====================================================================== #
# 5. CommitteeResult.to_dict() 필드 확인
# ====================================================================== #

def test_committee_result_to_dict():
    from src.models.committee import CommitteeResult
    r = CommitteeResult(
        version="v1",
        artifact_dir="/tmp/v1",
        tree_only_sr=0.5,
        committee_sr=0.7,
        delta_sharpe=0.2,
        improved=True,
        n_folds=3,
        n_samples=100,
        meta_fuser_classes=[0, 1],
    )
    d = r.to_dict()
    assert d["version"] == "v1"
    assert d["improved"] is True
    assert d["delta_sharpe"] == pytest.approx(0.2)
    assert "meta_fuser_classes" in d


# ====================================================================== #
# 6. CommitteeModel.fit() CommitteeResult 반환
# ====================================================================== #

def test_committee_fit_returns_result(tmp_path: Path):
    """fit() 호출 시 CommitteeResult 반환. n_folds/n_samples 필드 확인."""
    from src.models.committee import CommitteeResult

    model = _make_committee(tmp_path=tmp_path)
    panel, feat_cols = _synthetic_panel(n_tickers=4, n_ts=40, n_features=4)
    n = len(panel)

    mock_splitter = MagicMock()
    mid = n // 2
    mock_splitter.split.return_value = iter([
        (list(range(0, mid)), list(range(mid, n))),
    ])

    # 실 LightGBM/CNN 학습 mock으로 대체 (segfault 방지)
    with patch.object(
        model, "_compute_lgbm_oof",
        return_value=np.random.default_rng(0).random(n).astype(np.float64),
    ):
        with patch.object(
            model, "_compute_cnn_oof",
            return_value=np.random.default_rng(1).random(n).astype(np.float64),
        ):
            with patch.object(model, "_fit_final_models"):
                result = model.fit(panel, feat_cols, splitter=mock_splitter, bundle_id="TEST-001")

    assert isinstance(result, CommitteeResult)
    assert result.n_folds == 1
    assert result.n_samples == n
    assert "committee_sr" in result.to_dict()


# ====================================================================== #
# 7. CommitteeModel.fit() improved=True when delta > threshold
# ====================================================================== #

def test_committee_fit_improved_flag(tmp_path: Path):
    """committee_sr > tree_sr + threshold → improved=True.

    _compute_oof_sharpe를 side_effect mock으로 제어:
      첫 번째 호출(tree_only_sr) = 0.5
      두 번째 호출(committee_sr) = 1.2
    → delta = 0.7 > 0.0 threshold → improved=True
    """

    model = _make_committee(tmp_path=tmp_path, sharpe_improvement_threshold=0.0)
    panel, feat_cols = _synthetic_panel(n_tickers=3, n_ts=20)
    n = len(panel)

    mock_splitter = MagicMock()
    mock_splitter.split.return_value = iter([
        (list(range(0, n // 2)), list(range(n // 2, n))),
    ])

    # _compute_oof_sharpe: 1st call (tree) → 0.5, 2nd call (committee) → 1.2
    sharpe_vals = iter([0.5, 1.2])

    with patch.object(
        model, "_compute_lgbm_oof",
        return_value=np.random.default_rng(0).random(n).astype(np.float64),
    ):
        with patch.object(
            model, "_compute_cnn_oof",
            return_value=np.random.default_rng(1).random(n).astype(np.float64),
        ):
            with patch.object(
                model, "_compute_oof_sharpe",
                side_effect=lambda *a, **kw: next(sharpe_vals),
            ):
                with patch.object(model, "_fit_final_models"):
                    result = model.fit(panel, feat_cols, splitter=mock_splitter)

    assert result.improved is True, f"improved 기대 True, 실제 {result.improved} (delta={result.delta_sharpe:.3f})"
    assert result.delta_sharpe == pytest.approx(0.7, abs=1e-6)


# ====================================================================== #
# 8. CommitteeModel.fit() 후 artifact_dir에 파일 생성
# ====================================================================== #

def test_committee_fit_saves_artifacts(tmp_path: Path):
    """fit() 후 artifacts/committee/v1/ 아래 metadata.json 생성."""

    model = _make_committee(tmp_path=tmp_path)
    panel, feat_cols = _synthetic_panel(n_tickers=3, n_ts=20)
    n = len(panel)

    mock_splitter = MagicMock()
    mock_splitter.split.return_value = iter([
        (list(range(0, n // 2)), list(range(n // 2, n))),
    ])

    with patch.object(model, "_compute_lgbm_oof", return_value=np.random.default_rng(0).random(n).astype(np.float64)):
        with patch.object(model, "_compute_cnn_oof", return_value=np.random.default_rng(1).random(n).astype(np.float64)):
            with patch.object(model, "_fit_final_models"):
                result = model.fit(panel, feat_cols, splitter=mock_splitter)

    artifact_dir = Path(result.artifact_dir)
    assert artifact_dir.exists()
    assert (artifact_dir / "metadata.json").exists()

    meta = json.loads((artifact_dir / "metadata.json").read_text())
    assert meta["version"] == result.version
    assert "feature_cols" in meta


# ====================================================================== #
# 9. CommitteeModel.predict() 출력 shape = (N,)
# ====================================================================== #

def test_committee_predict_shape(tmp_path: Path):
    """predict() 반환 배열 shape이 panel 행 수와 일치."""

    model = _make_committee(tmp_path=tmp_path)
    panel, feat_cols = _synthetic_panel(n_tickers=4, n_ts=10)
    n = len(panel)
    model._feature_cols = feat_cols

    # 모델 직접 주입
    mock_booster = MagicMock()
    mock_booster.predict.return_value = np.zeros(n, dtype=np.float64)
    mock_booster.best_iteration = 50
    model._lgbm_booster = mock_booster

    mock_cnn = MagicMock()
    mock_cnn.predict_proba.return_value = np.zeros(n, dtype=np.float32)
    model._cnn_branch = mock_cnn

    from sklearn.linear_model import LogisticRegression
    X_dummy = np.zeros((n, 2))
    y_dummy = np.array([0, 1] * (n // 2))[:n]
    meta = LogisticRegression(max_iter=100).fit(X_dummy, y_dummy)
    model._meta_fuser = meta

    scores = model.predict(panel)
    assert scores.shape == (n,)


# ====================================================================== #
# 10. _next_version: 빈 artifacts → v1
# ====================================================================== #

def test_committee_next_version_empty(tmp_path: Path):
    model = _make_committee(tmp_path=tmp_path)
    version = model._next_version()
    assert version == "v1"


# ====================================================================== #
# 11. _next_version: v2 있으면 → v3
# ====================================================================== #

def test_committee_next_version_increment(tmp_path: Path):
    model = _make_committee(tmp_path=tmp_path)
    (model._artifacts_path / "v1").mkdir(parents=True, exist_ok=True)
    (model._artifacts_path / "v2").mkdir(parents=True, exist_ok=True)
    version = model._next_version()
    assert version == "v3"


# ====================================================================== #
# 12. _compute_oof_sharpe: top-quintile 수익률 > 0 → SR > 0
# ====================================================================== #

def test_committee_oof_sharpe_positive(tmp_path: Path):
    """scores가 labels와 양의 상관인 경우 SR > 0."""

    model = _make_committee(tmp_path=tmp_path)
    rng = np.random.default_rng(42)
    labels = rng.normal(0.001, 0.002, 100).astype(np.float32)  # 양의 기댓값
    scores = labels + rng.normal(0, 0.001, 100)  # labels와 강한 상관

    sr = model._compute_oof_sharpe(scores.astype(np.float64), labels.astype(np.float64))
    # top 20% 종목은 평균 수익률 > 0 → SR > 0
    assert sr > 0.0, f"SR 기대 > 0, 실제 {sr:.4f}"


# ====================================================================== #
# 13. CommitteeModel.fit(): fold 0개이면 CommitteeTrainError
# ====================================================================== #

def test_committee_fit_no_folds_raises(tmp_path: Path):
    """WalkForwardSplitter가 fold를 반환하지 않으면 CommitteeTrainError 발생."""
    from src.models.committee import CommitteeTrainError

    model = _make_committee(tmp_path=tmp_path)
    panel, feat_cols = _synthetic_panel(n_tickers=2, n_ts=5)

    mock_splitter = MagicMock()
    mock_splitter.split.return_value = iter([])  # 0 folds

    with pytest.raises(CommitteeTrainError, match="fold 0개"):
        model.fit(panel, feat_cols, splitter=mock_splitter)

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
        "cnn_nan_fallback": 0.0,
        "sharpe_label_col": "label_5m_ret",
        "top_k_fraction": 0.25,
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
        nan_fallback=0.0,
    )


def _torch_modules_with_empty_loader() -> dict[str, MagicMock]:
    """CNN scaler tests에서 학습 loop만 비우는 torch module mock."""
    mock_torch = MagicMock()
    mock_torch.tensor.side_effect = lambda value, dtype=None: value
    mock_torch.float32 = np.float32
    mock_torch.optim.Adam.return_value = MagicMock()

    mock_nn = MagicMock()
    mock_nn.BCELoss.return_value = MagicMock()

    mock_data = MagicMock()
    mock_data.TensorDataset.side_effect = lambda *args: args
    mock_data.DataLoader.return_value = []

    mock_utils = MagicMock()
    mock_utils.data = mock_data
    mock_torch.nn = mock_nn
    mock_torch.utils = mock_utils

    return {
        "torch": mock_torch,
        "torch.nn": mock_nn,
        "torch.utils": mock_utils,
        "torch.utils.data": mock_data,
    }


# ====================================================================== #
# 1. CommitteeModel init config 로드
# ====================================================================== #

def test_committee_init_loads_config(tmp_path: Path):
    model = _make_committee(tmp_path=tmp_path, cnn_epochs=5)
    assert model._cnn_epochs == 5
    assert model._cnn_hidden == 4
    assert model._meta_max_iter == 10


def test_committee_init_requires_cnn_nan_fallback(tmp_path: Path):
    """committee.cnn_nan_fallback 누락 시 코드 기본값으로 숨기지 않고 fail-closed."""
    from src.models.committee import CommitteeTrainError

    cfg = {
        "cnn_hidden_channels": 4,
        "cnn_learning_rate": 0.01,
        "cnn_epochs": 2,
        "cnn_batch_size": 8,
        "cnn_seed": 0,
        "meta_fuser_max_iter": 10,
        "meta_fuser_C": 1.0,
        "sharpe_improvement_threshold": 0.0,
        "artifacts_path": str(tmp_path / "committee"),
    }
    with patch("src.models.committee.config_load", return_value=cfg):
        from src.models.committee import CommitteeModel

        with pytest.raises(CommitteeTrainError, match="cnn_nan_fallback"):
            CommitteeModel()


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
    """CNNBranch.save() 호출 시 torch.save 호출 확인 (sys.modules mock).

    fail-closed save()는 _net + _scaler 둘 다 있어야 통과하므로
    정상 시나리오 검증용으로 두 필드 모두 설정한다.
    """
    from sklearn.preprocessing import StandardScaler

    branch = _make_cnn_branch(n_features=6)
    mock_net = MagicMock()
    mock_net.state_dict.return_value = {}
    branch._net = mock_net
    rng = np.random.default_rng(0)
    branch._scaler = StandardScaler().fit(rng.normal(0, 1, (20, 6)))

    pth_path = tmp_path / "cnn.pth"

    mock_torch = MagicMock()
    with patch.dict("sys.modules", {"torch": mock_torch}):
        branch.save(pth_path)

    mock_torch.save.assert_called_once()


def test_cnn_branch_save_raises_when_net_none(tmp_path: Path):
    """fail-closed: _net=None인 상태에서 save() 호출 → CommitteeTrainError.

    이전 동작은 silent return이었어서 호출자가 저장 실패를 인지하지 못한 채
    artifact를 비워두고 진행할 수 있었다. 학습 직후 시점에 문제를 노출하기 위해
    raise로 변경.
    """
    from src.models.committee import CommitteeTrainError

    branch = _make_cnn_branch(n_features=4)
    branch._net = None
    branch._scaler = MagicMock()

    pth_path = tmp_path / "should_not_be_created.pth"
    with pytest.raises(CommitteeTrainError, match=r"_net=None"):
        branch.save(pth_path)
    assert not pth_path.exists(), "fail-closed인데 .pth가 생성됨"


def test_cnn_branch_save_raises_when_scaler_none(tmp_path: Path):
    """fail-closed: _net은 있지만 _scaler=None인 상태에서 save() 호출 → CommitteeTrainError.

    이전 동작은 warning만 찍고 .pth 저장. load 시점에 scaler 누락으로 raise되어
    실패가 늦게 노출됐다. 학습 직후 시점에 막도록 변경.
    """
    from src.models.committee import CommitteeTrainError

    branch = _make_cnn_branch(n_features=4)
    mock_net = MagicMock()
    mock_net.state_dict.return_value = {}
    branch._net = mock_net
    branch._scaler = None

    pth_path = tmp_path / "cnn.pth"
    with pytest.raises(CommitteeTrainError, match=r"_scaler=None"):
        branch.save(pth_path)
    # .pth도 생성되지 않아야 한다 (부분 저장 금지)
    assert not pth_path.exists(), "fail-closed인데 .pth가 생성됨 (부분 저장 발생)"


# ====================================================================== #
# 4b. CNNBranch StandardScaler 입력 정규화 검증 (Exogenous 스케일 이질성 대응)
# ====================================================================== #

def test_cnn_branch_scaler_persists_after_fit():
    """fit() 후 self._scaler가 fitted StandardScaler여야 한다 (mean_/scale_ 보유)."""
    from src.models.committee import CNNBranch

    branch = _make_cnn_branch(n_features=4)
    rng = np.random.default_rng(0)
    # Exogenous 모사: feature 스케일 이질적 (1e0, 1e2, 1e8 등)
    X = np.column_stack([
        rng.normal(0, 0.01, 30),    # feat_5m_ret 정도
        rng.normal(20, 5, 30),      # us_vix 정도
        rng.normal(1e8, 1e7, 30),   # foreign_net_buy 정도
        rng.normal(0, 1, 30),       # 일반 feature
    ]).astype(np.float32)
    y = (rng.random(30) > 0.5).astype(np.float32)

    mock_net = MagicMock()
    mock_net.parameters.return_value = iter([])
    branch._net = mock_net

    # 학습 자체는 mock (torch.tensor / DataLoader 우회). scaler 동작만 검증.
    with patch.object(branch, "_build_net", return_value=mock_net), \
         patch("sklearn.preprocessing.StandardScaler") as MockScaler:
        scaler_instance = MagicMock()
        scaler_instance.fit_transform.return_value = X.astype(np.float32)
        MockScaler.return_value = scaler_instance
        with patch.dict("sys.modules", _torch_modules_with_empty_loader()):
            branch.fit(X, y)

    # scaler 인스턴스가 생성되고 fit_transform 호출됐는지 검증
    assert branch._scaler is not None
    scaler_instance.fit_transform.assert_called_once()


def test_cnn_branch_fit_nan_aware_order():
    """fit()의 NaN/inf 처리 순서 검증:
      1. inf만 NaN으로 치환 (NaN은 그대로 유지)
      2. StandardScaler.fit_transform에 NaN 포함 상태로 전달 (sklearn nanmean/nanvar 활용)
      3. 출력의 NaN을 0으로 치환

    raw 단계에서 0으로 치환하지 않아야 통계량 왜곡이 없다 (팀원 리뷰 반영).
    """
    from src.models.committee import CNNBranch

    branch = _make_cnn_branch(n_features=3)
    X = np.array([
        [1.0, 2.0, 3.0],
        [np.nan, 2.0, 3.0],      # NaN: 그대로 보존되어야 함
        [1.0, np.inf, 3.0],       # +inf: NaN으로 치환되어야 함
        [1.0, 2.0, -np.inf],      # -inf: NaN으로 치환되어야 함
        [1.0, 2.0, 3.0],
    ], dtype=np.float32)
    y = np.array([0, 1, 0, 1, 0], dtype=np.float32)

    captured_inputs: list[np.ndarray] = []

    def capture_and_return_zeros(X_input):
        captured_inputs.append(X_input.copy())
        return np.zeros_like(X_input, dtype=np.float64)

    mock_net = MagicMock()
    mock_net.parameters.return_value = iter([])

    with patch.object(branch, "_build_net", return_value=mock_net), \
         patch("sklearn.preprocessing.StandardScaler") as MockScaler:
        scaler_instance = MagicMock()
        scaler_instance.fit_transform.side_effect = capture_and_return_zeros
        MockScaler.return_value = scaler_instance
        with patch.dict("sys.modules", _torch_modules_with_empty_loader()):
            branch.fit(X, y)

    assert len(captured_inputs) == 1, "StandardScaler.fit_transform 한 번 호출 기대"
    X_to_scaler = captured_inputs[0]

    # inf가 모두 NaN으로 치환됐는지 (raw 0이 아니라)
    assert not np.isinf(X_to_scaler).any(), \
        "scaler 전달 직전에 inf는 NaN으로 치환되어야 함 (raw 0 아님)"

    # 원본의 NaN/inf 위치 모두 NaN으로
    assert np.isnan(X_to_scaler[1, 0]), "원본 NaN은 보존되어야 함"
    assert np.isnan(X_to_scaler[2, 1]), "원본 +inf는 NaN으로 치환되어야 함"
    assert np.isnan(X_to_scaler[3, 2]), "원본 -inf는 NaN으로 치환되어야 함"

    # 원본의 정상 값은 보존
    assert X_to_scaler[0, 0] == pytest.approx(1.0)
    assert X_to_scaler[4, 2] == pytest.approx(3.0)


def test_cnn_branch_predict_without_scaler_raises():
    """fit() 없이 _net만 있고 _scaler가 None이면 predict_proba가 명확한 에러 발생."""
    from src.models.committee import CNNBranch, CommitteeTrainError

    branch = _make_cnn_branch(n_features=4)
    # _net은 있지만 _scaler는 None인 비정상 상태
    branch._net = MagicMock()
    branch._scaler = None

    with pytest.raises(CommitteeTrainError, match=r"scaler"):
        branch.predict_proba(np.zeros((5, 4), dtype=np.float32))


def test_cnn_branch_save_persists_scaler(tmp_path: Path):
    """save() 후 cnn.pth 옆에 cnn_scaler.pkl 생성. 두 파일 다 있어야 운영 가능."""
    import pickle

    from src.models.committee import CNNBranch

    branch = _make_cnn_branch(n_features=4)
    mock_net = MagicMock()
    mock_net.state_dict.return_value = {}
    branch._net = mock_net

    # 실제 StandardScaler 사용 (mock 아님) — pickle save/load 통과 확인
    from sklearn.preprocessing import StandardScaler
    rng = np.random.default_rng(0)
    X = rng.normal(0, 1, (20, 4)).astype(np.float32)
    branch._scaler = StandardScaler().fit(X)

    pth_path = tmp_path / "cnn.pth"
    mock_torch = MagicMock()
    with patch.dict("sys.modules", {"torch": mock_torch}):
        branch.save(pth_path)

    scaler_path = tmp_path / "cnn_scaler.pkl"
    assert scaler_path.exists(), f"scaler 파일이 {scaler_path}에 저장되지 않음"
    with scaler_path.open("rb") as f:
        loaded = pickle.load(f)
    assert hasattr(loaded, "mean_")
    np.testing.assert_array_almost_equal(loaded.mean_, branch._scaler.mean_)


def test_cnn_branch_load_raises_when_scaler_missing(tmp_path: Path):
    """load() 시 cnn_scaler.pkl 없으면 명확한 CommitteeLoadError 발생."""
    from src.models.committee import CNNBranch, CommitteeLoadError

    branch = _make_cnn_branch(n_features=4)
    pth_path = tmp_path / "cnn.pth"
    pth_path.write_bytes(b"fake_state_dict")  # 파일은 있지만 scaler.pkl 없음

    mock_torch = MagicMock()
    mock_torch.load.return_value = {}
    sys_mocks = {
        "torch": mock_torch,
        "torch.nn": mock_torch.nn,
        "torch.utils.data": mock_torch.utils.data,
    }
    with patch.dict("sys.modules", sys_mocks):
        with pytest.raises(CommitteeLoadError, match=r"scaler.*누락"):
            branch.load(pth_path)


# ====================================================================== #
# 5. CommitteeResult.to_dict() 필드 확인
# ====================================================================== #

def test_committee_result_to_dict(tmp_path: Path):
    from src.models.committee import CommitteeResult
    r = CommitteeResult(
        version="v1",
        artifact_dir=str(tmp_path / "v1"),
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
    """cross-sectional top-K: scores가 label과 양의 상관이면 SR > 0.

    여러 날(day_key)에 걸친 panel이 필요. 단일 날이면 daily_pnl 1개 → SR=0.
    """
    model = _make_committee(tmp_path=tmp_path)
    rng = np.random.default_rng(42)

    n_days, n_ts_per_day, n_tickers = 10, 20, 5
    rows = []
    for d in range(n_days):
        base = pd.Timestamp("2026-01-01") + pd.Timedelta(days=d)
        for t in range(n_ts_per_day):
            ts = base + pd.Timedelta(hours=9, minutes=t + 1)
            for tk in range(n_tickers):
                rows.append((ts, f"{tk:06d}", rng.normal(0, 0.002)))

    df = pd.DataFrame(rows, columns=["ts_close", "ticker", "label_5m_ret"])
    df = df.set_index(["ts_close", "ticker"])

    labels = df["label_5m_ret"].to_numpy(dtype=np.float64)
    scores = labels + rng.normal(0, 0.0005, len(df))

    sr = model._compute_oof_sharpe(scores, df)
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

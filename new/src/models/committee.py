"""S3-8 Committee (AlphaGAT Stage II).

tree_core (LightGBM) + CNN confirmatory branch + MetaFuser (LogisticRegression OOF) 앙상블.

학습 흐름 (Walk-Forward OOF Stacking):
  1. WalkForwardSplitter로 fold 분할
  2. 각 fold에서 tree_core + CNN 독립 학습 → OOF predictions 생성
  3. MetaFuser = LogisticRegression([OOF_lgbm, OOF_cnn]) → 최종 앙상블 score
  4. 저장: artifacts/committee/v{n}/ (lgbm pkl + cnn pth + meta_fuser pkl)

Sharpe 비교 검증:
  committee_sr > tree_only_sr + threshold → improved=True

불변 원칙:
  1. 하드코딩 금지: 파라미터는 risk_config.yaml committee 섹션 경유
  2. PIT-Safety: WalkForwardSplitter purge/embargo 준수 (LGBMTrainer 내부 강제)
  3. Mode B 전용 (train에 @mode_b_only 없음 - scheduler가 Mode B에서 호출)

Note:
  CNN은 feature dimension을 1D conv로 처리 (cross-sectional feature convolution).
  AlphaGAT Stage II의 "CNN confirmatory branch" 개념 구현.
"""
from __future__ import annotations

import json
import pickle
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from src.models.metrics import sharpe_ratio
from src.models.splitter import WalkForwardSplitter
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.mode_guard import mode_b_only

logger = get_logger("committee")


# ====================================================================== #
# Errors
# ====================================================================== #


class CommitteeTrainError(RuntimeError):
    """Committee 학습 실패."""


class CommitteeLoadError(RuntimeError):
    """Committee artifact 로드 실패."""


# ====================================================================== #
# Result dataclass
# ====================================================================== #


@dataclass
class CommitteeResult:
    """Committee 학습/검증 결과.

    C13 PerformanceAnalyzer ablation_components에 'committee' 추가 예정 (Sprint 4).
    """

    version: str
    artifact_dir: str
    tree_only_sr: float
    committee_sr: float
    delta_sharpe: float
    improved: bool
    n_folds: int
    n_samples: int
    meta_fuser_classes: list[int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ====================================================================== #
# CNN Branch (PyTorch)
# ====================================================================== #


class CNNBranch:
    """1D CNN confirmatory branch. feature dim을 1D conv으로 처리.

    Input: (batch, n_features) → (batch, 1, n_features) → Conv1d → score [0, 1]
    Architecture: Conv1d → ReLU → Conv1d → AdaptiveAvgPool → FC → Sigmoid
    """

    def __init__(
        self,
        n_features: int,
        hidden_channels: int,
        lr: float,
        epochs: int,
        batch_size: int,
        seed: int,
    ) -> None:
        self._n_features = n_features
        self._hidden = hidden_channels
        self._lr = lr
        self._epochs = epochs
        self._batch_size = batch_size
        self._seed = seed
        self._net: Any = None

    def _build_net(self) -> Any:
        try:
            import torch
            import torch.nn as nn
        except ImportError as e:
            raise CommitteeTrainError(f"torch 미설치: {e}") from e

        torch.manual_seed(self._seed)

        hidden = self._hidden
        net = nn.Sequential(
            nn.Conv1d(1, hidden, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.Conv1d(hidden, hidden // 2, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.AdaptiveAvgPool1d(1),
            nn.Flatten(),
            nn.Linear(hidden // 2, 1),
            nn.Sigmoid(),
        )
        return net

    def fit(self, X: np.ndarray, y: np.ndarray) -> "CNNBranch":
        """Binary cross-entropy 학습. X: (N, n_features), y: (N,) 0/1.

        PIT-Safety: caller가 train/val 분리를 보장해야 한다.
        """
        try:
            import torch
            import torch.nn as nn
            from torch.utils.data import DataLoader, TensorDataset
        except ImportError as e:
            raise CommitteeTrainError(f"torch 미설치: {e}") from e

        torch.manual_seed(self._seed)
        self._net = self._build_net()
        optimizer = torch.optim.Adam(self._net.parameters(), lr=self._lr)
        criterion = nn.BCELoss()

        X_t = torch.tensor(X, dtype=torch.float32)
        y_t = torch.tensor(y, dtype=torch.float32)
        dataset = TensorDataset(X_t, y_t)
        loader = DataLoader(dataset, batch_size=self._batch_size, shuffle=True)

        self._net.train()
        for epoch in range(self._epochs):
            for xb, yb in loader:
                # (B, n_features) → (B, 1, n_features)
                xb = xb.unsqueeze(1)
                pred = self._net(xb).squeeze(1)
                loss = criterion(pred, yb)
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

        self._net.eval()
        logger.info("[committee.cnn] 학습 완료: epochs=%d n_samples=%d", self._epochs, len(X))
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """ranking score [0, 1] 반환. X: (N, n_features)."""
        if self._net is None:
            raise CommitteeTrainError("CNNBranch.fit() 먼저 호출 필요")
        try:
            import torch
        except ImportError as e:
            raise CommitteeTrainError(f"torch 미설치: {e}") from e

        self._net.eval()
        with torch.no_grad():
            xt = torch.tensor(X, dtype=torch.float32).unsqueeze(1)  # (N, 1, n_features)
            scores = self._net(xt).squeeze(1).numpy()
        return scores.astype(np.float32)

    def save(self, path: Path) -> None:
        """CNN 가중치 저장 (.pth)."""
        if self._net is None:
            return
        try:
            import torch
        except ImportError as e:
            raise CommitteeTrainError(f"torch 미설치: {e}") from e
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(self._net.state_dict(), str(path))
        logger.info("[committee.cnn] 가중치 저장: %s", path)

    def load(self, path: Path) -> "CNNBranch":
        """CNN 가중치 로드."""
        try:
            import torch
        except ImportError as e:
            raise CommitteeLoadError(f"torch 미설치: {e}") from e
        self._net = self._build_net()
        self._net.load_state_dict(torch.load(str(path), map_location="cpu", weights_only=True))
        self._net.eval()
        logger.info("[committee.cnn] 가중치 로드: %s", path)
        return self


# ====================================================================== #
# CommitteeModel
# ====================================================================== #


class CommitteeModel:
    """tree_core + CNN confirmatory + MetaFuser 앙상블. AlphaGAT Stage II.

    fit():
      1. WalkForwardSplitter fold에서 OOF 생성 (tree_core + CNN 각각)
      2. MetaFuser = LogisticRegression(OOF stacking)
      3. artifacts/committee/v{n}/ 저장

    predict(panel): OOF-learned MetaFuser로 전체 panel 점수 반환.
    compare_sharpe(panel, folds): tree_only vs committee Sharpe 비교.
    """

    def __init__(self) -> None:
        cfg = config_load("risk_config.yaml", "committee") or {}
        # cnn_lookback: Sprint 4 시계열 확장 시 사용 예정 (현재 point-wise CNN)
        # self._cnn_lookback = int(cfg.get("cnn_lookback", 1))
        self._cnn_hidden: int = int(cfg.get("cnn_hidden_channels", 32))
        self._cnn_lr: float = float(cfg.get("cnn_learning_rate", 0.001))
        self._cnn_epochs: int = int(cfg.get("cnn_epochs", 10))
        self._cnn_batch: int = int(cfg.get("cnn_batch_size", 64))
        self._cnn_seed: int = int(cfg.get("cnn_seed", 42))
        self._meta_max_iter: int = int(cfg.get("meta_fuser_max_iter", 100))
        self._meta_C: float = float(cfg.get("meta_fuser_C", 1.0))
        self._sharpe_threshold: float = float(cfg.get("sharpe_improvement_threshold", 0.0))
        self._artifacts_path = Path(cfg.get("artifacts_path", "artifacts/committee"))
        self._artifacts_path.mkdir(parents=True, exist_ok=True)

        # state
        self._lgbm_booster: Any = None
        self._cnn_branch: CNNBranch | None = None
        self._meta_fuser: Any = None
        self._feature_cols: list[str] = []
        self._label_col: str = "relevance"
        self._version: str = ""

        logger.info(
            "[committee] 초기화. cnn_epochs=%d meta_max_iter=%d",
            self._cnn_epochs,
            self._meta_max_iter,
        )

    # ================================================================== #
    # Public API
    # ================================================================== #

    @mode_b_only
    def fit(
        self,
        panel: Any,
        feature_cols: list[str],
        label_col: str = "relevance",
        splitter: WalkForwardSplitter | None = None,
        version: str | None = None,
        bundle_id: str | None = None,
    ) -> CommitteeResult:
        """OOF stacking 앙상블 학습. Mode B 전용 (W5 @mode_b_only 추가).

        Args:
            panel:        MultiIndex (ts_close, ticker) DataFrame. LGBMTrainer.train() 출력과 동일.
            feature_cols: feature 컬럼 목록.
            label_col:    label 컬럼명.
            splitter:     WalkForwardSplitter. None이면 기본값.
            version:      저장 버전. None이면 자동 결정.
            bundle_id:    Mode B bundle_id (메타데이터용).

        Returns: CommitteeResult
        """
        try:
            from sklearn.linear_model import LogisticRegression
        except ImportError as e:
            raise CommitteeTrainError(f"sklearn 미설치: {e}") from e

        self._feature_cols = list(feature_cols)
        self._label_col = label_col
        sp = splitter or WalkForwardSplitter()

        X_all = panel[feature_cols].to_numpy(dtype=np.float32)
        y_all = panel[label_col].to_numpy(dtype=np.float32)

        # OOF arrays
        lgbm_oof = np.zeros(len(panel), dtype=np.float64)
        cnn_oof = np.zeros(len(panel), dtype=np.float64)
        n_features = X_all.shape[1]

        folds = list(sp.split(panel))
        if not folds:
            raise CommitteeTrainError("walk-forward fold 0개. 데이터 부족.")

        logger.info("[committee] OOF stacking 시작: n_folds=%d n_samples=%d", len(folds), len(panel))

        # tree_core OOF (LightGBM)
        lgbm_oof = self._compute_lgbm_oof(panel, feature_cols, label_col, folds)

        # CNN OOF
        cnn_oof = self._compute_cnn_oof(X_all, y_all, folds, n_features)

        # tree_only Sharpe (OOF 기반 일별 PnL 근사)
        tree_only_sr = self._compute_oof_sharpe(lgbm_oof, y_all)

        # MetaFuser 학습
        meta_X = np.column_stack([lgbm_oof, cnn_oof])
        # binary label: top half = 1, bottom half = 0 (strict quantile)
        median_val = float(np.median(y_all))
        meta_y = (y_all > median_val).astype(int)
        if len(np.unique(meta_y)) < 2:
            raise CommitteeTrainError(
                "MetaFuser 학습 불가: meta_y가 단일 클래스. "
                "panel의 label 분산이 0이거나 데이터가 너무 적음."
            )

        self._meta_fuser = LogisticRegression(
            max_iter=self._meta_max_iter,
            C=self._meta_C,
            solver="lbfgs",
        )
        self._meta_fuser.fit(meta_X, meta_y)

        # committee score = MetaFuser probability of top class
        committee_scores = self._meta_fuser.predict_proba(meta_X)[:, 1]
        committee_sr = self._compute_oof_sharpe(committee_scores, y_all)

        delta_sharpe = float(committee_sr - tree_only_sr)
        improved = delta_sharpe > self._sharpe_threshold

        logger.info(
            "[committee] 학습 완료: tree_sr=%.4f committee_sr=%.4f delta=%.4f improved=%s",
            tree_only_sr,
            committee_sr,
            delta_sharpe,
            improved,
        )

        # 최종 모델 전체 데이터 재학습 (inference용)
        self._fit_final_models(X_all, y_all, n_features, panel)

        # 저장
        v = version or self._next_version()
        self._version = v
        artifact_dir = self._save(v, bundle_id, tree_only_sr, committee_sr, delta_sharpe)

        return CommitteeResult(
            version=v,
            artifact_dir=str(artifact_dir),
            tree_only_sr=float(tree_only_sr),
            committee_sr=float(committee_sr),
            delta_sharpe=delta_sharpe,
            improved=improved,
            n_folds=len(folds),
            n_samples=len(panel),
            meta_fuser_classes=list(self._meta_fuser.classes_),
        )

    def predict(self, panel: Any) -> np.ndarray:
        """committee ensemble score 반환. shape: (N,).

        MetaFuser가 없으면 tree_core 단독 fallback.
        """
        if self._meta_fuser is None or self._lgbm_booster is None:
            raise CommitteeLoadError("commit 먼저 fit() 또는 load() 필요")

        feature_cols = self._feature_cols
        X = panel[feature_cols].to_numpy(dtype=np.float32)

        # tree_core score
        lgbm_scores = self._lgbm_booster.predict(X, num_iteration=self._lgbm_booster.best_iteration)

        # cnn score
        if self._cnn_branch is not None:
            cnn_scores = self._cnn_branch.predict_proba(X).astype(np.float64)
        else:
            cnn_scores = np.zeros(len(X), dtype=np.float64)

        # meta_fuser
        meta_X = np.column_stack([lgbm_scores, cnn_scores])
        proba = self._meta_fuser.predict_proba(meta_X)
        # 클래스 2개 이상인 경우만 [:, 1], 아니면 단조 score 반환
        if proba.shape[1] >= 2:
            ensemble_scores = proba[:, 1]
        else:
            ensemble_scores = proba[:, 0]
        return ensemble_scores.astype(np.float32)

    def load(self, artifact_dir: str | Path) -> "CommitteeModel":
        """저장된 Committee 아티팩트 로드."""
        d = Path(artifact_dir)
        if not d.exists():
            raise CommitteeLoadError(f"artifact_dir 없음: {d}")

        # metadata
        meta_path = d / "metadata.json"
        if meta_path.exists():
            with meta_path.open(encoding="utf-8") as f:
                meta = json.load(f)
            self._feature_cols = meta.get("feature_cols", [])
            self._label_col = meta.get("label_col", "relevance")
            self._version = meta.get("version", "")

        # tree_core
        lgbm_path = d / "tree_core.pkl"
        if lgbm_path.exists():
            with lgbm_path.open("rb") as f:
                self._lgbm_booster = pickle.load(f)

        # cnn
        cnn_path = d / "cnn_branch.pth"
        if cnn_path.exists() and self._feature_cols:
            n_features = len(self._feature_cols)
            self._cnn_branch = CNNBranch(
                n_features, self._cnn_hidden, self._cnn_lr,
                self._cnn_epochs, self._cnn_batch, self._cnn_seed,
            )
            self._cnn_branch.load(cnn_path)

        # meta_fuser
        meta_fuser_path = d / "meta_fuser.pkl"
        if meta_fuser_path.exists():
            with meta_fuser_path.open("rb") as f:
                self._meta_fuser = pickle.load(f)

        logger.info("[committee] 아티팩트 로드 완료: %s", artifact_dir)
        return self

    # ================================================================== #
    # Internal: OOF computation
    # ================================================================== #

    def _compute_lgbm_oof(
        self,
        panel: Any,
        feature_cols: list[str],
        label_col: str,
        folds: list,
    ) -> np.ndarray:
        """LightGBM OOF 예측값 생성."""
        try:
            from src.models.ranking_loss import (
                build_lgbm_params,
                get_lightgbm,
                get_training_control,
                make_lgbm_dataset,
            )
        except Exception as e:
            logger.warning("[committee] ranking_loss import 실패: %s. lgbm_oof=0", e)
            return np.zeros(len(panel), dtype=np.float64)

        lgb = get_lightgbm()
        params = build_lgbm_params()
        tc = get_training_control()
        oof = np.zeros(len(panel), dtype=np.float64)
        booster = None

        if not folds:
            raise ValueError("[committee] _compute_lgbm_oof: folds 목록이 비어 있음. 최소 1-fold 필요")

        for train_idx, val_idx in folds:
            train_p = panel.iloc[train_idx].sort_index(level="ts_close")
            val_p = panel.iloc[val_idx].sort_index(level="ts_close")

            train_ds = make_lgbm_dataset(train_p, feature_cols, label_col)
            val_ds = make_lgbm_dataset(val_p, feature_cols, label_col)

            booster = lgb.train(
                params,
                train_set=train_ds,
                valid_sets=[val_ds],
                valid_names=["val"],
                num_boost_round=tc.get("n_estimators", 300),
                callbacks=[lgb.early_stopping(tc.get("early_stopping_rounds", 20), verbose=False)],
            )
            X_val = val_p[feature_cols].to_numpy(dtype=np.float32)
            oof[val_idx] = booster.predict(X_val, num_iteration=booster.best_iteration)

        # 마지막 fold 보스터를 self._lgbm_booster에 저장 (임시: 최종 재학습에서 교체됨)
        self._lgbm_booster = booster  # type: ignore[assignment]
        return oof

    def _compute_cnn_oof(
        self,
        X_all: np.ndarray,
        y_all: np.ndarray,
        folds: list,
        n_features: int,
    ) -> np.ndarray:
        """CNN OOF 예측값 생성."""
        oof = np.zeros(len(X_all), dtype=np.float64)
        y_binary = (y_all > np.median(y_all)).astype(np.float32)

        for train_idx, val_idx in folds:
            X_train, y_train = X_all[train_idx], y_binary[train_idx]
            X_val = X_all[val_idx]

            cnn = CNNBranch(
                n_features=n_features,
                hidden_channels=self._cnn_hidden,
                lr=self._cnn_lr,
                epochs=self._cnn_epochs,
                batch_size=self._cnn_batch,
                seed=self._cnn_seed,
            )
            try:
                cnn.fit(X_train, y_train)
                oof[val_idx] = cnn.predict_proba(X_val).astype(np.float64)
            except Exception as e:
                logger.warning("[committee] CNN fold 오류: %s. cnn_oof[fold]=0", e)

        return oof

    def _compute_oof_sharpe(self, scores: np.ndarray, labels: np.ndarray) -> float:
        """OOF score 기반 일별 top-quintile PnL 합산 → Sharpe 근사."""
        if len(scores) == 0:
            return 0.0
        # top 20% 종목 선택 → 해당 일의 레이블(수익률) 평균이 daily PnL 근사
        threshold = np.percentile(scores, 80)
        top_mask = scores >= threshold
        if top_mask.sum() == 0:
            return 0.0
        daily_pnl = labels[top_mask]
        return float(sharpe_ratio(daily_pnl))

    def _fit_final_models(
        self,
        X_all: np.ndarray,
        y_all: np.ndarray,
        n_features: int,
        panel: Any,
    ) -> None:
        """전체 데이터로 tree_core + CNN 최종 재학습 (inference용)."""
        # tree_core final 재학습: group 계산해서 lambdarank 올바르게 학습
        try:
            from src.models.ranking_loss import (
                build_lgbm_params,
                compute_group_sizes,
                get_lightgbm,
                get_training_control,
            )

            lgb = get_lightgbm()
            params = build_lgbm_params()
            tc = get_training_control()
            group_sizes = compute_group_sizes(panel, group_col="ts_close")
            y_int = y_all.astype(int)
            train_ds = lgb.Dataset(X_all, label=y_int, group=group_sizes, free_raw_data=False)
            self._lgbm_booster = lgb.train(
                params,
                train_set=train_ds,
                num_boost_round=tc.get("n_estimators", 300),
            )
        except Exception as e:
            logger.warning(
                "[committee] 최종 tree_core 재학습 오류: %s. OOF fold booster로 inference 진행", e
            )

        # CNN final 재학습
        y_binary = (y_all > np.median(y_all)).astype(np.float32)
        self._cnn_branch = CNNBranch(
            n_features=n_features,
            hidden_channels=self._cnn_hidden,
            lr=self._cnn_lr,
            epochs=self._cnn_epochs,
            batch_size=self._cnn_batch,
            seed=self._cnn_seed,
        )
        try:
            self._cnn_branch.fit(X_all, y_binary)
        except Exception as e:
            logger.warning("[committee] 최종 CNN 재학습 오류: %s. cnn_branch=None", e)
            self._cnn_branch = None

    # ================================================================== #
    # Internal: versioning + save
    # ================================================================== #

    def _next_version(self) -> str:
        """artifacts/committee/v{n}/ 기존 디렉토리 중 최대 n+1 반환. 없으면 v1."""
        try:
            existing = [
                d for d in self._artifacts_path.iterdir()
                if d.is_dir() and re.match(r"v\d+$", d.name)
            ]
            nums = [int(re.match(r"v(\d+)$", d.name).group(1)) for d in existing]
            return f"v{max(nums) + 1}" if nums else "v1"
        except Exception as e:
            logger.warning("[committee] 버전 결정 실패: %s. v1 사용", e)
            return "v1"

    def _save(
        self,
        version: str,
        bundle_id: str | None,
        tree_only_sr: float,
        committee_sr: float,
        delta_sharpe: float,
    ) -> Path:
        """artifacts/committee/v{n}/ 저장."""
        artifact_dir = self._artifacts_path / version
        artifact_dir.mkdir(parents=True, exist_ok=True)

        # tree_core
        if self._lgbm_booster is not None:
            with (artifact_dir / "tree_core.pkl").open("wb") as f:
                pickle.dump(self._lgbm_booster, f)

        # cnn
        if self._cnn_branch is not None:
            self._cnn_branch.save(artifact_dir / "cnn_branch.pth")

        # meta_fuser
        if self._meta_fuser is not None:
            with (artifact_dir / "meta_fuser.pkl").open("wb") as f:
                pickle.dump(self._meta_fuser, f)

        # metadata
        metadata = {
            "version": version,
            "bundle_id": bundle_id,
            "feature_cols": self._feature_cols,
            "label_col": self._label_col,
            "tree_only_sr": tree_only_sr,
            "committee_sr": committee_sr,
            "delta_sharpe": delta_sharpe,
            "improved": delta_sharpe > self._sharpe_threshold,
            "cnn_hidden_channels": self._cnn_hidden,
            "meta_fuser_max_iter": self._meta_max_iter,
        }
        with (artifact_dir / "metadata.json").open("w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        logger.info(
            "[committee] 저장 완료: version=%s dir=%s delta_sharpe=%.4f",
            version,
            artifact_dir,
            delta_sharpe,
        )
        return artifact_dir

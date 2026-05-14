"""S3-7 PPO Allocator 야간 재학습 오케스트레이터.

AllocationEnv: gymnasium.Env 기반 포트폴리오 배분 학습 환경.
NightlyPPORetrainer: stable-baselines3 PPO 야간 재학습 + artifacts/ppo/v{n}.zip 버전 관리.

설계:
  1. Synthetic quant scores + returns 생성 (실 데이터: S1-8 이후)
  2. AllocationEnv(scores, returns) 구성
  3. PPO("MlpPolicy", env).learn(total_timesteps) 실행
  4. artifacts/ppo/v{n}.zip 저장
  5. C12 allocator_candidate dict 반환

불변 원칙:
  1. 하드코딩 금지: 파라미터는 risk_config.yaml nightly_ppo_retrainer 섹션 경유
  2. Mode B 전용 (retrain에 @mode_b_only 적용)
  3. PIT-Safety: 학습 데이터 생성 시 미래 데이터 미사용 (synthetic random walk 또는 18:00 이전 스냅샷)

Note:
  실 backfill 데이터 없으면 (S1-8 blocker) synthetic random walk 사용.
  Sprint 4 S4-2에서 실 LGBM scores + 실 returns으로 교체 예정.
"""
from __future__ import annotations

import re
import shutil
from types import SimpleNamespace
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

try:
    import gymnasium as _gym
    from gymnasium import spaces as _spaces
    _GYM_BASE = _gym.Env
except ImportError:
    _gym = None  # type: ignore[assignment]

    class _FallbackEnv:
        """Small gymnasium-compatible base used for deterministic unit smoke."""

        def reset(self, *, seed: int | None = None, options: dict | None = None):
            self.np_random = np.random.default_rng(seed)
            return None

    class _FallbackBox:
        def __init__(self, low: float, high: float, shape: tuple[int, ...], dtype: Any):
            self.low = low
            self.high = high
            self.shape = shape
            self.dtype = dtype

        def sample(self) -> np.ndarray:
            if np.isfinite(self.low) and np.isfinite(self.high):
                return np.random.uniform(self.low, self.high, self.shape).astype(self.dtype)
            return np.zeros(self.shape, dtype=self.dtype)

    _spaces = SimpleNamespace(Box=_FallbackBox)  # type: ignore[assignment]
    _GYM_BASE = _FallbackEnv  # type: ignore[assignment, misc]

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.mode_guard import mode_b_only

logger = get_logger("nightly_ppo_retrainer")
_KST = ZoneInfo("Asia/Seoul")


class AllocationEnv(_GYM_BASE):  # type: ignore[valid-type]
    """gymnasium.Env 기반 포트폴리오 배분 학습 환경.

    Observation: [normalized_scores(n), current_weights(n)] = 2n dim
    Action: n continuous values [-1, 1] → softmax → target weights
    Reward: pnl - turnover_penalty * turnover - constraint_penalty * violation

    gymnasium API (step → obs, reward, terminated, truncated, info) 사용.
    stable-baselines3 2.x + gymnasium 1.x 호환.
    """

    metadata: dict = {"render_modes": []}

    def __init__(
        self,
        scores_data: np.ndarray,
        returns_data: np.ndarray,
        min_cash: float,
        max_single_name: float,
        turnover_penalty: float,
        constraint_penalty: float,
        episode_length: int,
    ) -> None:
        super().__init__()

        if scores_data.shape != returns_data.shape:
            raise ValueError(
                f"[AllocationEnv] scores/returns shape mismatch: "
                f"{scores_data.shape} vs {returns_data.shape}"
            )
        self._scores = scores_data.astype(np.float32)    # (T, n_stocks)
        self._returns = returns_data.astype(np.float32)  # (T, n_stocks)
        self._T, self._n = self._scores.shape
        self._min_cash = float(min_cash)
        self._max_single_name = float(max_single_name)
        self._turnover_penalty = float(turnover_penalty)
        self._constraint_penalty = float(constraint_penalty)
        self._episode_length = min(int(episode_length), self._T)

        self._t: int = 0
        self._ep_start: int = 0
        self._weights: np.ndarray = np.zeros(self._n, dtype=np.float32)

        self.observation_space = _spaces.Box(
            low=-np.inf,
            high=np.inf,
            shape=(2 * self._n,),
            dtype=np.float32,
        )
        self.action_space = _spaces.Box(
            low=-1.0,
            high=1.0,
            shape=(self._n,),
            dtype=np.float32,
        )

    def reset(self, *, seed: int | None = None, options: dict | None = None):
        super().reset(seed=seed)
        if not hasattr(self, "np_random"):
            self.np_random = np.random.default_rng(seed)
        max_start = max(0, self._T - self._episode_length)
        self._ep_start = int(self.np_random.integers(0, max_start + 1))
        self._t = 0
        self._weights = np.zeros(self._n, dtype=np.float32)
        return self._get_obs(), {}

    def step(self, action: np.ndarray):
        idx = min(self._ep_start + self._t, self._T - 1)
        ret = self._returns[idx]

        # action → softmax → target weights
        a = np.array(action, dtype=np.float64)
        a = a - a.max()  # numerical stability
        exp_a = np.exp(a)
        target_alloc = max(0.0, 1.0 - self._min_cash)
        new_w = (exp_a / exp_a.sum() * target_alloc).astype(np.float32)

        # PnL reward
        pnl = float(np.dot(new_w, ret))

        # turnover penalty
        turnover = float(np.sum(np.abs(new_w - self._weights)))
        turnover_cost = self._turnover_penalty * turnover

        # max_single_name constraint violation penalty (다중 위반 합산)
        violation = float(np.sum(np.maximum(0.0, new_w - self._max_single_name)))
        constraint_cost = self._constraint_penalty * violation

        reward = float(pnl - turnover_cost - constraint_cost)

        self._weights = new_w
        self._t += 1
        terminated = self._t >= self._episode_length
        return self._get_obs(), reward, terminated, False, {}

    def _get_obs(self) -> np.ndarray:
        idx = min(self._ep_start + self._t, self._T - 1)
        scores = self._scores[idx]
        s_min, s_max = float(scores.min()), float(scores.max())
        s_range = s_max - s_min
        if s_range > 1e-8:
            norm_scores = (2.0 * (scores - s_min) / s_range - 1.0).astype(np.float32)
        else:
            norm_scores = np.zeros(self._n, dtype=np.float32)
        return np.concatenate([norm_scores, self._weights])


class NightlyPPORetrainer:
    """PPO Allocator 야간 재학습 오케스트레이터.

    stable-baselines3 PPO + AllocationEnv 학습.
    결과를 v{n}.zip으로 artifacts/ppo/에 저장.
    bundle_id는 Mode B Scheduler가 발급한 값 사용.

    파라미터 출처: risk_config.yaml nightly_ppo_retrainer 섹션 (불변 원칙 5).
    """

    def __init__(self) -> None:
        cfg = config_load("risk_config.yaml", "nightly_ppo_retrainer") or {}
        self._n_stocks: int = int(cfg.get("n_stocks", 20))
        self._lookback_days: int = int(cfg.get("lookback_days", 30))
        self._total_timesteps: int = int(cfg.get("total_timesteps", 10000))
        self._episode_length: int = int(cfg.get("episode_length", 390))
        self._turnover_penalty: float = float(cfg.get("turnover_penalty", 0.001))
        self._constraint_penalty: float = float(cfg.get("constraint_penalty", 0.5))
        self._artifacts_path = Path(cfg.get("artifacts_path", "artifacts/ppo"))
        self._artifacts_path.mkdir(parents=True, exist_ok=True)

        # PPO 하이퍼파라미터 (yaml 경유, 하드코딩 금지)
        self._ppo_n_steps: int = int(cfg.get("ppo_n_steps", 2048))
        self._ppo_batch_size: int = int(cfg.get("ppo_batch_size", 64))
        self._ppo_n_epochs: int = int(cfg.get("ppo_n_epochs", 10))
        self._ppo_seed: int = int(cfg.get("ppo_seed", 42))
        self._synthetic_seed: int = int(cfg.get("synthetic_seed", 42))

        # position_limits에서 constraint 파라미터 로드
        pos_cfg = config_load("risk_config.yaml", "position_limits") or {}
        self._min_cash: float = float(pos_cfg.get("min_cash", 0.10))
        self._max_single_name: float = float(pos_cfg.get("max_single_name", 0.20))

        logger.info(
            "[nightly_ppo_retrainer] 초기화. n_stocks=%d lookback=%d timesteps=%d",
            self._n_stocks,
            self._lookback_days,
            self._total_timesteps,
        )

    # ================================================================== #
    # Public API
    # ================================================================== #

    @mode_b_only
    def retrain(
        self,
        bundle_id: str | None = None,
        scores_data: np.ndarray | None = None,
        returns_data: np.ndarray | None = None,
    ) -> dict[str, Any]:
        """PPO 야간 재학습 실행. artifacts/ppo/v{n}.zip 저장 후 allocator_candidate 반환.

        Args:
            bundle_id:    Mode B Scheduler 발급 bundle_id. 메타데이터 추적용.
            scores_data:  외부 LGBM ranking scores (T, n_stocks). None이면 synthetic 생성.
            returns_data: 실 1분봉 수익률 (T, n_stocks). None이면 synthetic 생성.

        Returns:
            {
                "version": "ppo_v2",
                "model_path": "artifacts/ppo/v2.zip",
                "bundle_id": bundle_id,
                "n_timesteps": 10000,
                "allocator_candidate": {
                    "allocator_ref": "ppo_v2",
                    "source": "ppo_retrain",
                    "bundle_id": bundle_id,
                    "model_path": "artifacts/ppo/v2.zip",
                    "policy_type": "MlpPolicy",
                    "n_stocks": 20,
                }
            }
        """
        from stable_baselines3 import PPO

        # 1. 다음 버전 결정
        next_v_num = self._next_version_number()
        version_key = f"v{next_v_num}"
        version_label = f"ppo_{version_key}"
        save_path = self._artifacts_path / version_key  # SB3 저장 시 .zip suffix 자동 추가

        # 2. 학습 데이터 준비 (synthetic fallback)
        s_data, r_data = self._prepare_data(scores_data, returns_data)
        synthetic_fallback = scores_data is None or returns_data is None

        # 3. AllocationEnv 구성
        env = AllocationEnv(
            scores_data=s_data,
            returns_data=r_data,
            min_cash=self._min_cash,
            max_single_name=self._max_single_name,
            turnover_penalty=self._turnover_penalty,
            constraint_penalty=self._constraint_penalty,
            episode_length=self._episode_length,
        )

        logger.info(
            "[nightly_ppo_retrainer] PPO 학습 시작. version=%s timesteps=%d env_shape=(%d, %d)",
            version_label,
            self._total_timesteps,
            *s_data.shape,
        )

        # 4. PPO 학습
        n_steps = min(self._ppo_n_steps, max(64, self._episode_length))
        batch_size = min(self._ppo_batch_size, max(8, self._episode_length // 6))
        model = PPO(
            "MlpPolicy",
            env,
            verbose=0,
            n_steps=n_steps,
            batch_size=batch_size,
            n_epochs=self._ppo_n_epochs,
            seed=self._ppo_seed,
        )
        model.learn(total_timesteps=self._total_timesteps)

        # 5. 저장 (SB3는 .zip suffix 자동 추가)
        model.save(str(save_path))
        model_path_str = str(save_path) + ".zip"
        candidate_bundle_path = None
        if bundle_id is not None and not synthetic_fallback:
            candidate_bundle_path = self._stage_candidate_bundle(bundle_id, Path(model_path_str))
        logger.info(
            "[nightly_ppo_retrainer] 재학습 완료. version=%s path=%s",
            version_label,
            model_path_str,
        )

        allocator_candidate = {
            "allocator_ref": version_label,
            "source": "ppo_retrain",
            "bundle_id": bundle_id,
            "model_path": model_path_str,
            "policy_type": "MlpPolicy",
            "n_stocks": self._n_stocks,
            "synthetic_fallback": synthetic_fallback,
        }

        return {
            "version": version_label,
            "model_path": model_path_str,
            "bundle_id": bundle_id,
            "n_timesteps": self._total_timesteps,
            "allocator_candidate": allocator_candidate,
            "synthetic_fallback": synthetic_fallback,
            "candidate_bundle_staged": candidate_bundle_path is not None,
            "candidate_bundle_path": str(candidate_bundle_path) if candidate_bundle_path else None,
        }

    # ================================================================== #
    # Internal helpers
    # ================================================================== #

    def _next_version_number(self) -> int:
        """artifacts/ppo/ 기존 v{n}.zip 중 최대 n + 1 반환. 없으면 1."""
        try:
            existing = list(self._artifacts_path.glob("v*.zip"))
            nums: list[int] = []
            for p in existing:
                m = re.match(r"v(\d+)\.zip$", p.name)
                if m:
                    nums.append(int(m.group(1)))
            return max(nums) + 1 if nums else 1
        except Exception as e:
            logger.warning("[nightly_ppo_retrainer] 버전 결정 실패: %s. v1 사용", e)
            return 1

    def _prepare_data(
        self,
        scores_data: np.ndarray | None,
        returns_data: np.ndarray | None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """학습 데이터 준비. 실 데이터 없으면 synthetic random walk 생성.

        PIT-Safety: synthetic 데이터는 seed 고정 random walk (미래 데이터 미사용).
        T = lookback_days * episode_length (최소 episode_length * 2 보장).
        """
        if scores_data is not None and returns_data is not None:
            if scores_data.shape[1] != self._n_stocks:
                logger.warning(
                    "[nightly_ppo_retrainer] scores_data n_stocks 불일치: %d != %d. synthetic 사용",
                    scores_data.shape[1],
                    self._n_stocks,
                )
            else:
                logger.info(
                    "[nightly_ppo_retrainer] 실 데이터 사용: shape=%s", scores_data.shape
                )
                return scores_data.astype(np.float32), returns_data.astype(np.float32)

        # synthetic 생성 (S1-8 blocker 대응)
        T = max(self._lookback_days * self._episode_length, self._episode_length * 2)
        n = self._n_stocks
        rng = np.random.default_rng(seed=self._synthetic_seed)

        # random walk returns: mean=0, std=0.002 (1분봉 현실적 범위)
        returns = rng.normal(loc=0.0, scale=0.002, size=(T, n)).astype(np.float32)

        # LGBM-style cross-sectional scores: 0~1 범위, 각 bar마다 re-randomize
        scores = rng.random(size=(T, n)).astype(np.float32)

        logger.info(
            "[nightly_ppo_retrainer] synthetic 데이터 생성: T=%d n=%d (S1-8 blocker 대응)",
            T,
            n,
        )
        return scores, returns

    def _stage_candidate_bundle(self, bundle_id: str, model_path: Path) -> Path:
        """C14 deployer가 참조하는 bundle/ppo/latest_policy.pkl에 후보 policy를 복사."""
        bundle_dir = self._artifacts_path.parent / "bundles" / bundle_id / "ppo"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        dest = bundle_dir / "latest_policy.pkl"
        shutil.copy2(model_path, dest)
        return dest

"""S3-7 NightlyPPORetrainer + AllocationEnv 유닛 테스트.

모든 외부 의존성(stable-baselines3 PPO 실 학습)은 Mock 사용.
AllocationEnv 직접 테스트는 실 gymnasium 사용 (설치 전제).

테스트 목록:
  1.  test_allocation_env_reset                       - reset() obs shape 확인
  2.  test_allocation_env_step_returns_shape           - step() obs/reward/terminated 구조
  3.  test_allocation_env_episode_terminates           - episode_length 후 terminated=True
  4.  test_allocation_env_weights_sum_leq_target_alloc - softmax 후 weights 합 ≤ target_alloc
  5.  test_allocation_env_reward_is_float              - reward는 float
  6.  test_next_version_empty_artifacts                - artifacts 비어있으면 v1
  7.  test_next_version_existing_v3                    - v3.zip 있으면 다음 v4
  8.  test_prepare_data_synthetic_shape                - synthetic 데이터 shape 확인
  9.  test_prepare_data_uses_real_if_provided          - 실 데이터 제공 시 그대로 사용
  10. test_retrain_returns_allocator_candidate         - retrain() allocator_candidate 포함
  11. test_retrain_returns_model_path_zip              - model_path .zip suffix
  12. test_retrain_includes_bundle_id                  - bundle_id 결과에 포함
  13. test_retrain_version_label_format                - version = "ppo_v{n}" 형식
  14. test_load_policy_file_not_found                  - 파일 없으면 PolicyNotLoadedError
  15. test_load_policy_load_failure                    - PPO.load() 실패 시 PolicyNotLoadedError
  16. test_violation_penalty_sum_multiple              - 다중 위반 시 violation 합산
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# mode_b_only 가드 우회
os.environ.setdefault("ELEPHANT_MODE", "mode_b")


# ====================================================================== #
# Helpers
# ====================================================================== #

def _make_retrainer(tmp_path: Path | None = None, **kwargs):
    """NightlyPPORetrainer 인스턴스 생성 with patched config."""
    cfg_defaults = {
        "n_stocks": 5,             # 테스트는 작은 크기
        "lookback_days": 2,
        "total_timesteps": 32,     # 빠른 테스트
        "episode_length": 10,
        "turnover_penalty": 0.001,
        "constraint_penalty": 0.5,
        "n_envs": 1,
        "artifacts_path": str(tmp_path / "ppo") if tmp_path else "artifacts/ppo",
    }
    cfg_defaults.update(kwargs)
    pos_cfg = {"min_cash": 0.10, "max_single_name": 0.20}

    def fake_config_load(fname, section):
        if section == "nightly_ppo_retrainer":
            return cfg_defaults
        if section == "position_limits":
            return pos_cfg
        return {}

    with patch(
        "src.mode_b.nightly_ppo_retrainer.config_load",
        side_effect=fake_config_load,
    ):
        from src.mode_b.nightly_ppo_retrainer import NightlyPPORetrainer
        return NightlyPPORetrainer()


def _make_env(n: int = 5, T: int = 50, episode_length: int = 10):
    """AllocationEnv 인스턴스 생성."""
    from src.mode_b.nightly_ppo_retrainer import AllocationEnv

    rng = np.random.default_rng(0)
    scores = rng.random((T, n)).astype(np.float32)
    returns = rng.normal(0, 0.002, (T, n)).astype(np.float32)
    return AllocationEnv(
        scores_data=scores,
        returns_data=returns,
        min_cash=0.10,
        max_single_name=0.20,
        turnover_penalty=0.001,
        constraint_penalty=0.5,
        episode_length=episode_length,
    )


# ====================================================================== #
# 1. AllocationEnv.reset(): obs shape = (2*n,)
# ====================================================================== #

def test_allocation_env_reset():
    env = _make_env(n=5)
    obs, info = env.reset(seed=0)
    assert obs.shape == (10,), f"obs shape 기대 (10,), 실제 {obs.shape}"
    assert isinstance(info, dict)


# ====================================================================== #
# 2. AllocationEnv.step(): 반환값 구조
# ====================================================================== #

def test_allocation_env_step_returns_shape():
    env = _make_env(n=5, episode_length=10)
    env.reset(seed=1)
    action = np.zeros(5, dtype=np.float32)
    obs, reward, terminated, truncated, info = env.step(action)
    assert obs.shape == (10,)
    assert isinstance(reward, float)
    assert isinstance(terminated, bool)
    assert isinstance(truncated, bool)
    assert not truncated  # AllocationEnv는 truncated=False


# ====================================================================== #
# 3. AllocationEnv: episode_length 후 terminated=True
# ====================================================================== #

def test_allocation_env_episode_terminates():
    episode_length = 5
    env = _make_env(n=3, T=30, episode_length=episode_length)
    env.reset(seed=2)
    action = np.zeros(3, dtype=np.float32)
    terminated = False
    for step_i in range(episode_length):
        _, _, terminated, _, _ = env.step(action)
    assert terminated, f"{episode_length} step 후 terminated=True 기대"


# ====================================================================== #
# 4. AllocationEnv: softmax 후 weights 합 ≤ target_alloc (1 - min_cash)
# ====================================================================== #

def test_allocation_env_weights_sum_leq_target_alloc():
    env = _make_env(n=5, episode_length=10)
    env.reset(seed=3)
    action = np.array([1.0, 2.0, -1.0, 0.5, -0.5], dtype=np.float32)
    env.step(action)
    # weights = softmax(action) * (1 - min_cash) → sum ≈ 0.9
    weights_sum = float(env._weights.sum())
    target_alloc = 1.0 - env._min_cash
    assert weights_sum <= target_alloc + 1e-6, (
        f"weights sum {weights_sum:.4f} > target_alloc {target_alloc}"
    )


# ====================================================================== #
# 5. AllocationEnv: reward는 float 타입
# ====================================================================== #

def test_allocation_env_reward_is_float():
    env = _make_env(n=4)
    env.reset(seed=4)
    _, reward, _, _, _ = env.step(np.zeros(4, dtype=np.float32))
    assert isinstance(reward, float)


# ====================================================================== #
# 6. _next_version_number: artifacts 비어있으면 1
# ====================================================================== #

def test_next_version_empty_artifacts(tmp_path: Path):
    retrainer = _make_retrainer(tmp_path=tmp_path)
    version_num = retrainer._next_version_number()
    assert version_num == 1


# ====================================================================== #
# 7. _next_version_number: v3.zip 있으면 다음 v4
# ====================================================================== #

def test_next_version_existing_v3(tmp_path: Path):
    retrainer = _make_retrainer(tmp_path=tmp_path)
    retrainer._artifacts_path.mkdir(parents=True, exist_ok=True)
    (retrainer._artifacts_path / "v1.zip").touch()
    (retrainer._artifacts_path / "v3.zip").touch()
    version_num = retrainer._next_version_number()
    assert version_num == 4


# ====================================================================== #
# 8. _prepare_data: synthetic 데이터 shape (T ≥ episode_length * 2, n = n_stocks)
# ====================================================================== #

def test_prepare_data_synthetic_shape(tmp_path: Path):
    retrainer = _make_retrainer(tmp_path=tmp_path)
    scores, returns = retrainer._prepare_data(None, None)
    n = retrainer._n_stocks
    ep = retrainer._episode_length
    T_min = ep * 2
    assert scores.shape[1] == n, f"n_stocks 기대 {n}, 실제 {scores.shape[1]}"
    assert returns.shape == scores.shape
    assert scores.shape[0] >= T_min, f"T {scores.shape[0]} < {T_min}"


# ====================================================================== #
# 9. _prepare_data: 실 데이터 제공 시 그대로 반환
# ====================================================================== #

def test_prepare_data_uses_real_if_provided(tmp_path: Path):
    retrainer = _make_retrainer(tmp_path=tmp_path)
    rng = np.random.default_rng(99)
    real_scores = rng.random((50, 5)).astype(np.float32)
    real_returns = rng.normal(0, 0.001, (50, 5)).astype(np.float32)
    s, r = retrainer._prepare_data(real_scores, real_returns)
    np.testing.assert_array_equal(s, real_scores)
    np.testing.assert_array_equal(r, real_returns)


# ====================================================================== #
# 10. retrain(): allocator_candidate 포함 (PPO mock)
# ====================================================================== #

def test_retrain_returns_allocator_candidate(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    retrainer = _make_retrainer(tmp_path=tmp_path)
    mock_model = MagicMock()

    with patch.dict("sys.modules", {
        "stable_baselines3": MagicMock(PPO=MagicMock(return_value=mock_model))
    }):
        result = retrainer.retrain(bundle_id="TEST-BUNDLE-001")

    assert "allocator_candidate" in result
    cand = result["allocator_candidate"]
    assert cand["source"] == "ppo_retrain"
    assert cand["bundle_id"] == "TEST-BUNDLE-001"
    assert "allocator_ref" in cand


# ====================================================================== #
# 11. retrain(): model_path .zip suffix 포함
# ====================================================================== #

def test_retrain_returns_model_path_zip(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    retrainer = _make_retrainer(tmp_path=tmp_path)

    mock_model = MagicMock()

    with patch.dict("sys.modules", {
        "stable_baselines3": MagicMock(PPO=MagicMock(return_value=mock_model))
    }):
        result = retrainer.retrain()

    assert result["model_path"].endswith(".zip"), (
        f"model_path는 .zip이어야 함: {result['model_path']}"
    )


# ====================================================================== #
# 12. retrain(): bundle_id 결과에 포함
# ====================================================================== #

def test_retrain_includes_bundle_id(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    retrainer = _make_retrainer(tmp_path=tmp_path)
    mock_model = MagicMock()

    with patch.dict("sys.modules", {
        "stable_baselines3": MagicMock(PPO=MagicMock(return_value=mock_model))
    }):
        result = retrainer.retrain(bundle_id="BUNDLE-XYZ-999")

    assert result["bundle_id"] == "BUNDLE-XYZ-999"
    assert result["allocator_candidate"]["bundle_id"] == "BUNDLE-XYZ-999"


# ====================================================================== #
# 13. retrain(): version label = "ppo_v{n}" 형식
# ====================================================================== #

def test_retrain_version_label_format(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    retrainer = _make_retrainer(tmp_path=tmp_path)
    mock_model = MagicMock()

    with patch.dict("sys.modules", {
        "stable_baselines3": MagicMock(PPO=MagicMock(return_value=mock_model))
    }):
        result = retrainer.retrain()

    version = result["version"]
    assert version.startswith("ppo_v"), f"version 형식 기대 'ppo_vN', 실제: {version}"
    # e.g. "ppo_v1"
    import re
    assert re.match(r"ppo_v\d+$", version), f"version 형식 불일치: {version}"


# ====================================================================== #
# 13-B. retrain(): CPU-safe torch/SB3 runtime policy is config-driven
# ====================================================================== #

def test_retrain_applies_cpu_safe_policy_kwargs(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    retrainer = _make_retrainer(
        tmp_path=tmp_path,
        ppo_ortho_init="false",
        ppo_torch_num_threads=1,
        ppo_torch_num_interop_threads=1,
    )
    mock_model = MagicMock()
    mock_ppo_ctor = MagicMock(return_value=mock_model)
    mock_torch = MagicMock()

    with patch.dict("sys.modules", {
        "stable_baselines3": MagicMock(PPO=mock_ppo_ctor),
        "torch": mock_torch,
    }):
        retrainer.retrain()

    mock_torch.set_num_threads.assert_called_once_with(1)
    mock_torch.set_num_interop_threads.assert_called_once_with(1)
    _, kwargs = mock_ppo_ctor.call_args
    assert kwargs["policy_kwargs"] == {"ortho_init": False}


# ====================================================================== #
# 14. _load_policy: 파일 없으면 PolicyNotLoadedError
# ====================================================================== #

def test_load_policy_file_not_found():
    from src.models.ppo_allocator import PPOAllocator, PolicyNotLoadedError

    allocator = PPOAllocator()
    with pytest.raises(PolicyNotLoadedError, match="policy 파일 없음"):
        allocator._load_policy(Path("/nonexistent/v99.zip"))


# ====================================================================== #
# 15. _load_policy: PPO.load() 실패 시 PolicyNotLoadedError
# ====================================================================== #

def test_load_policy_load_failure(tmp_path: Path):
    from src.models.ppo_allocator import PPOAllocator, PolicyNotLoadedError

    allocator = PPOAllocator()
    fake_zip = tmp_path / "v1.zip"
    fake_zip.touch()

    mock_sb3 = MagicMock()
    mock_sb3.PPO.load.side_effect = RuntimeError("corrupt file")

    with patch.dict("sys.modules", {"stable_baselines3": mock_sb3}):
        with pytest.raises(PolicyNotLoadedError, match="PPO policy 로드 실패"):
            allocator._load_policy(fake_zip)


# ====================================================================== #
# 16. AllocationEnv violation penalty: 다중 위반 시 합산
# ====================================================================== #

def test_violation_penalty_sum_multiple():
    """3종목 모두 max_single_name(0.20) 초과 시 violation 합산이 단일 종목보다 큼."""
    env = _make_env(n=3, episode_length=10)
    env.reset(seed=5)

    # 극단적 action → softmax 후 모든 종목이 0.20 초과
    # 3종목 균등: softmax → 각 0.30 → 3*max(0, 0.30-0.20) = 3*0.10 = 0.30
    action = np.zeros(3, dtype=np.float32)  # softmax → 균등 1/3 * 0.9 = 0.30
    # violation = sum(max(0, 0.30 - 0.20)) * 3 = 3 * 0.10 = 0.30
    env._weights = np.array([0.0, 0.0, 0.0], dtype=np.float32)  # 이전 weights

    # step 1회 실행
    _, reward1, _, _, _ = env.step(action)

    # 단일 violation만 보면 0.10이지만, sum이면 0.30 → constraint_cost가 더 큼
    # 정확한 검증: _weights 후 값 확인
    new_weights = env._weights
    assert float(new_weights.sum()) == pytest.approx(0.9, abs=1e-4)
    # constraint_cost = penalty * sum(max(0, w - 0.20))
    expected_violation = float(np.sum(np.maximum(0.0, new_weights - 0.20)))
    assert expected_violation > 0.0, "모든 종목이 0.20 초과해야 함"

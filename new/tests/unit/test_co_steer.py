"""Unit tests for S3-5 CoSteer / ThompsonSampler."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("ELEPHANT_MODE", "mode_b")


# ------------------------------------------------------------------ #
# ThompsonSampler 단위 테스트
# ------------------------------------------------------------------ #


def test_thompson_sampler_select_returns_valid_action():
    """select()가 actions 목록 내 값만 반환."""
    from src.mode_b.co_steer import ThompsonSampler

    sampler = ThompsonSampler(["a", "b"])
    result = sampler.select()
    assert result in ("a", "b")


def test_thompson_sampler_update_increases_alpha():
    """reward=1.0 업데이트 시 α 증가."""
    from src.mode_b.co_steer import ThompsonSampler

    sampler = ThompsonSampler(["a", "b"])
    before = sampler._alpha["a"]
    sampler.update("a", 1.0)
    assert sampler._alpha["a"] > before


def test_thompson_sampler_update_increases_beta_on_failure():
    """reward=0.0 업데이트 시 β 증가."""
    from src.mode_b.co_steer import ThompsonSampler

    sampler = ThompsonSampler(["a", "b"])
    before = sampler._beta["a"]
    sampler.update("a", 0.0)
    assert sampler._beta["a"] > before


def test_thompson_sampling_convergence():
    """50회 시뮬: factor reward=1.0, model reward=0.0 → factor 선택 > 80%.

    인스턴스 격리 RNG(seed=42) 사용. global np.random.seed 불필요.
    """
    from src.mode_b.co_steer import ThompsonSampler

    sampler = ThompsonSampler(["factor_evolution", "model_evolution"], seed=42)
    for _ in range(50):
        action = sampler.select()
        reward = 1.0 if action == "factor_evolution" else 0.0
        sampler.update(action, reward)

    factor_count = sum(
        1 for _ in range(20) if sampler.select() == "factor_evolution"
    )
    assert factor_count > 16, f"factor 선택 빈도 {factor_count}/20 < 80%"


def test_thompson_posteriors_format():
    """posteriors()가 alpha/beta/mean 3개 키를 포함."""
    from src.mode_b.co_steer import ThompsonSampler

    sampler = ThompsonSampler(["a", "b"])
    p = sampler.posteriors()
    assert "a" in p and "b" in p
    for v in p.values():
        assert "alpha" in v and "beta" in v and "mean" in v


def test_thompson_sampler_rng_isolation():
    """같은 seed로 두 인스턴스를 만들면 select()가 동일 arm 반환 (결정성).

    global np.random.beta가 아닌 인스턴스 격리 RNG 검증.
    """
    from src.mode_b.co_steer import ThompsonSampler

    s1 = ThompsonSampler(["x", "y"], seed=7)
    s2 = ThompsonSampler(["x", "y"], seed=7)
    # 첫 번째 select: 동일해야 함
    assert s1.select() == s2.select()


def test_thompson_sampler_rng_different_seeds():
    """다른 seed → 장기 실행 시 결과 분포가 독립 (같은 global RNG 안 씀)."""
    from src.mode_b.co_steer import ThompsonSampler

    s1 = ThompsonSampler(["x", "y"], seed=1)
    s2 = ThompsonSampler(["x", "y"], seed=999)
    # 독립 인스턴스이므로 update 결과가 서로 영향 없어야 함
    s1.update("x", 1.0)
    before_s2_alpha = s2._alpha["x"]
    # s1의 update가 s2에 영향 없음
    assert s2._alpha["x"] == before_s2_alpha


# ------------------------------------------------------------------ #
# CoSteer 단위 테스트
# ------------------------------------------------------------------ #


def _mock_cfg(tmp_path: Path) -> dict:
    return {
        "actions": ["factor_evolution", "model_evolution"],
        "alpha_init": 1.0,
        "beta_init": 1.0,
        "state_path": str(tmp_path / "state.json"),
        "history_path": str(tmp_path / "history.jsonl"),
        "complexity_delta": 0.1,
        "min_ic_reward": 0.02,
    }


def test_co_steer_select_direction(tmp_path, monkeypatch):
    """select_direction()이 actions 내 값 반환."""
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    with patch("src.mode_b.co_steer.config_load", return_value=_mock_cfg(tmp_path)):
        from src.mode_b.co_steer import CoSteer

        co = CoSteer()
        direction = co.select_direction()
    assert direction in ("factor_evolution", "model_evolution")


def test_co_steer_state_persistence(tmp_path, monkeypatch):
    """update_reward 후 재초기화해도 α 값이 유지됨."""
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    cfg = _mock_cfg(tmp_path)

    with patch("src.mode_b.co_steer.config_load", return_value=cfg):
        from src.mode_b.co_steer import CoSteer

        co1 = CoSteer()
        co1.update_reward("factor_evolution", 1.0)
        alpha_before = co1._sampler._alpha["factor_evolution"]

    # 캐시 문제 방지: 모듈 재임포트 없이 새 인스턴스
    with patch("src.mode_b.co_steer.config_load", return_value=cfg):
        from src.mode_b.co_steer import CoSteer as CoSteer2

        co2 = CoSteer2()
        assert co2._sampler._alpha["factor_evolution"] == pytest.approx(alpha_before)


def test_co_steer_history_appended(tmp_path, monkeypatch):
    """update_reward 후 history.jsonl에 항목이 기록됨."""
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    hist_path = tmp_path / "h.jsonl"
    cfg = {
        "actions": ["factor_evolution", "model_evolution"],
        "alpha_init": 1.0,
        "beta_init": 1.0,
        "state_path": str(tmp_path / "s.json"),
        "history_path": str(hist_path),
        "complexity_delta": 0.1,
        "min_ic_reward": 0.02,
    }
    with patch("src.mode_b.co_steer.config_load", return_value=cfg):
        from src.mode_b.co_steer import CoSteer

        co = CoSteer()
        co.update_reward("factor_evolution", 0.8)

    assert hist_path.exists()
    lines = hist_path.read_text().strip().splitlines()
    assert len(lines) >= 1
    entry = json.loads(lines[-1])
    assert entry["action"] == "factor_evolution"
    assert entry["reward"] == pytest.approx(0.8)


def test_co_steer_run_model_evolution(tmp_path, monkeypatch):
    """run_model_evolution이 direction/reward/status 키를 반환. S3-7 실구현 경로."""
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")

    mock_lgbm_inst = MagicMock()
    mock_lgbm_inst.retrain.return_value = {
        "version": "v2", "model_path": "artifacts/lgbm/v2.pkl", "metrics": {}
    }
    mock_ppo_inst = MagicMock()
    mock_ppo_inst.retrain.return_value = {
        "version": "ppo_v1",
        "model_path": "artifacts/ppo/v1.zip",
        "allocator_candidate": {"allocator_ref": "ppo_v1", "source": "ppo_retrain"},
    }

    mock_lgbm_mod = MagicMock()
    mock_lgbm_mod.NightlyLGBMRetrainer.return_value = mock_lgbm_inst
    mock_ppo_mod = MagicMock()
    mock_ppo_mod.NightlyPPORetrainer.return_value = mock_ppo_inst

    with patch("src.mode_b.co_steer.config_load", return_value=_mock_cfg(tmp_path)):
        with patch.dict("sys.modules", {
            "src.mode_b.nightly_lgbm_retrainer": mock_lgbm_mod,
            "src.mode_b.nightly_ppo_retrainer": mock_ppo_mod,
        }):
            from src.mode_b.co_steer import CoSteer

            co = CoSteer()
            result = co.run_model_evolution({})

    assert result["direction"] == "model_evolution"
    assert "reward" in result
    assert result["status"] in ("done", "partial", "error")


def test_co_steer_fallback_on_no_state(tmp_path, monkeypatch):
    """state.json 없어도 초기값(α=1.0)으로 정상 동작."""
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    cfg = {
        "actions": ["a", "b"],
        "alpha_init": 1.0,
        "beta_init": 1.0,
        "state_path": str(tmp_path / "nonexistent.json"),
        "history_path": str(tmp_path / "h.jsonl"),
        "complexity_delta": 0.1,
        "min_ic_reward": 0.02,
    }
    with patch("src.mode_b.co_steer.config_load", return_value=cfg):
        from src.mode_b.co_steer import CoSteer

        co = CoSteer()
        assert co._sampler._alpha == {"a": 1.0, "b": 1.0}

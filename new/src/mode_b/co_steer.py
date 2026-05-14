"""S3-5 Co-STEER Orchestrator (RD-Agent 논문 기반).

§8.2 Thompson Sampling 방향 결정 + §8.4 DAG 태스크 스케줄링.
A = {factor_evolution, model_evolution}: Beta(α, β) posterior.
상태 지속: artifacts/co_steer/thompson_state.json.
"""
from __future__ import annotations

import json
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.mode_guard import mode_b_only

logger = get_logger("co_steer")
_KST = ZoneInfo("Asia/Seoul")


class ThompsonSampler:
    """Beta(α, β) Multi-Armed Bandit."""

    def __init__(
        self,
        actions: list[str],
        alpha_init: float = 1.0,
        beta_init: float = 1.0,
        seed: int | None = None,
    ) -> None:
        self._alpha: dict[str, float] = {a: alpha_init for a in actions}
        self._beta: dict[str, float] = {a: beta_init for a in actions}
        # global numpy RNG 대신 격리된 Generator 사용 (결정성 보장).
        # seed=None이면 OS entropy 기반 (비결정적, 운영 기본값).
        self._rng: np.random.Generator = np.random.default_rng(seed)

    def select(self) -> str:
        """self._rng.beta(α, β) → argmax로 action 선택.

        global RNG 대신 인스턴스별 격리된 Generator 사용.
        같은 seed로 두 번 호출하면 동일 arm 반환 (결정성 보장).
        """
        samples = {
            a: float(self._rng.beta(self._alpha[a], self._beta[a]))
            for a in self._alpha
        }
        return max(samples, key=samples.__getitem__)

    def update(self, action: str, reward: float) -> None:
        """α += reward, β += (1 - reward)."""
        reward = float(np.clip(reward, 0.0, 1.0))
        self._alpha[action] = self._alpha[action] + reward
        self._beta[action] = self._beta[action] + (1.0 - reward)

    def posteriors(self) -> dict[str, dict[str, float]]:
        """{"action": {"alpha": α, "beta": β, "mean": α/(α+β)}} 반환."""
        return {
            a: {
                "alpha": self._alpha[a],
                "beta": self._beta[a],
                "mean": self._alpha[a] / (self._alpha[a] + self._beta[a]),
            }
            for a in self._alpha
        }

    def to_dict(self) -> dict[str, Any]:
        return {"alpha": dict(self._alpha), "beta": dict(self._beta)}

    def load_dict(self, d: dict[str, Any]) -> None:
        self._alpha.update(d.get("alpha", {}))
        self._beta.update(d.get("beta", {}))


class CoSteer:
    """Co-STEER Orchestrator.

    RD-Agent(Q) §8.2 Thompson Sampling 방향 결정 + §8.3/§8.4 DAG 실행.
    actions = {factor_evolution, model_evolution}.
    Beta posterior는 artifacts/co_steer/thompson_state.json에 지속 저장.
    """

    def __init__(self, llm_router: Any = None) -> None:
        cfg = config_load("risk_config.yaml", "co_steer") or {}
        actions: list[str] = cfg.get("actions", ["factor_evolution", "model_evolution"])
        alpha_init = float(cfg.get("alpha_init", 1.0))
        beta_init = float(cfg.get("beta_init", 1.0))
        self._state_path = Path(
            cfg.get("state_path", "artifacts/co_steer/thompson_state.json")
        )
        self._history_path = Path(
            cfg.get("history_path", "artifacts/co_steer/co_steer_history.jsonl")
        )
        self._complexity_delta = float(cfg.get("complexity_delta", 0.1))
        self._min_ic_reward = float(cfg.get("min_ic_reward", 0.02))
        self._model_evolution_stub_reward = float(cfg.get("model_evolution_stub_reward", 0.5))
        # thompson_seed: risk_config.yaml co_steer.thompson_seed에서 로드.
        # None(기본값)이면 비결정적 (운영 환경). 정수 지정 시 결정적 테스트 가능.
        _raw_seed = cfg.get("thompson_seed", None)
        thompson_seed: int | None = int(_raw_seed) if _raw_seed is not None else None
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        self._sampler = ThompsonSampler(actions, alpha_init, beta_init, seed=thompson_seed)
        self._load_state()
        self._llm_router = llm_router
        logger.info("[co_steer] 초기화. actions=%s thompson_seed=%s", actions, thompson_seed)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    @mode_b_only
    def select_direction(self, market_context: dict | None = None) -> str:
        """§8.2 Thompson Sampling으로 방향 선택."""
        direction = self._sampler.select()
        logger.info(
            "[co_steer] 방향 선택: %s (posteriors=%s)",
            direction,
            self._sampler.posteriors(),
        )
        return direction

    @mode_b_only
    def run_factor_evolution(
        self,
        market_context: dict,
        factor_zoo: Any = None,
    ) -> dict[str, Any]:
        """§8.3 Factor Evolution DAG: IdeaAgent → FactorAgent → EvalAgent → FactorZoo."""
        results: list[dict[str, Any]] = []
        best_ic = 0.0
        try:
            from src.mode_b.alpha_factor.idea_agent import IdeaAgent
            from src.mode_b.alpha_factor.factor_agent import FactorAgent
            from src.mode_b.alpha_factor.eval_agent import EvalAgent
            from src.mode_b.alpha_factor.factor_zoo import FactorZoo

            idea = IdeaAgent(llm_router=self._llm_router)
            factor_a = FactorAgent(llm_router=self._llm_router)
            eval_a = EvalAgent(llm_router=self._llm_router)
            zoo = factor_zoo or FactorZoo()

            anchors = idea.load_latest_hypotheses(n=1)
            hypotheses = idea.generate_batch(
                market_context,
                anchors=anchors or None,
            )
            active_entries = zoo.list_by_status("active")
            # W2: EvalAgent._compute_similarity가 e.get("status") 호출 → dataclass는 .get() 없음.
            # FactorZooEntry.to_dict()로 변환하여 dict list 전달.
            active = [e.to_dict() for e in active_entries]

            for hyp in hypotheses:
                candidate = factor_a.implement(hyp)
                if candidate.status != "active":
                    continue
                eval_result = eval_a.evaluate(candidate, hyp, active)
                if eval_result.passed:
                    zoo.add_candidate(candidate, hyp, eval_result)
                    best_ic = max(best_ic, abs(eval_result.ic))
                    results.append(
                        {"candidate_id": candidate.candidate_id, "ic": eval_result.ic}
                    )
        except Exception as e:
            logger.warning("[co_steer] factor_evolution DAG 오류: %s", e)

        reward = (
            1.0
            if best_ic >= self._min_ic_reward
            else (
                best_ic / self._min_ic_reward if self._min_ic_reward > 0 else 0.0
            )
        )
        self.update_reward("factor_evolution", reward)
        return {
            "direction": "factor_evolution",
            "candidates_added": len(results),
            "best_ic": best_ic,
            "reward": reward,
        }

    @mode_b_only
    def run_model_evolution(self, market_context: dict) -> dict[str, Any]:
        """§8.4 Model Evolution DAG: NightlyLGBMRetrainer + NightlyPPORetrainer.

        S3-6 LightGBM 재학습 + S3-7 PPO 재학습을 순차 실행.
        각 결과를 reward로 변환하여 Thompson posterior 업데이트.
        둘 중 하나 실패 시 fallback reward 사용 (graceful degradation).
        """
        bundle_id: str | None = market_context.get("bundle_id")
        lgbm_result: dict[str, Any] = {}
        ppo_result: dict[str, Any] = {}
        lgbm_ok = False
        ppo_ok = False

        # LightGBM 재학습 (S3-6)
        try:
            from src.mode_b.nightly_lgbm_retrainer import NightlyLGBMRetrainer

            lgbm_result = NightlyLGBMRetrainer().retrain(bundle_id=bundle_id)
            lgbm_ok = True
            logger.info(
                "[co_steer] LightGBM 재학습 완료: version=%s",
                lgbm_result.get("version"),
            )
        except Exception as e:
            logger.warning("[co_steer] LightGBM 재학습 오류: %s. 계속 진행", e)

        # PPO 재학습 (S3-7)
        try:
            from src.mode_b.nightly_ppo_retrainer import NightlyPPORetrainer

            ppo_result = NightlyPPORetrainer().retrain(bundle_id=bundle_id)
            ppo_ok = True
            logger.info(
                "[co_steer] PPO 재학습 완료: version=%s",
                ppo_result.get("version"),
            )
        except Exception as e:
            logger.warning("[co_steer] PPO 재학습 오류: %s. 계속 진행", e)

        # reward = 성공 여부 기반 (각 0.5 기여)
        if lgbm_ok and ppo_ok:
            reward = 1.0
        elif lgbm_ok or ppo_ok:
            reward = 0.5
        else:
            reward = self._model_evolution_stub_reward

        self.update_reward("model_evolution", reward)
        return {
            "direction": "model_evolution",
            "status": "done" if (lgbm_ok or ppo_ok) else "error",
            "reward": reward,
            "lgbm_version": lgbm_result.get("version"),
            "ppo_version": ppo_result.get("version"),
            "allocator_candidate": ppo_result.get("allocator_candidate"),
        }

    def posteriors(self) -> dict[str, dict[str, float]]:
        """ThompsonSampler posteriors의 public 접근자."""
        return self._sampler.posteriors()

    @mode_b_only
    def update_reward(self, action: str, reward: float) -> None:
        """Thompson Sampling posterior 업데이트 + 상태 저장."""
        self._sampler.update(action, reward)
        self._save_state()
        self._append_history(
            {
                "timestamp": datetime.now(_KST).isoformat(),
                "action": action,
                "reward": reward,
                "posteriors": self._sampler.posteriors(),
            }
        )
        logger.info(
            "[co_steer] reward 업데이트: action=%s reward=%.3f", action, reward
        )

    @mode_b_only
    def optimize(self, factor_candidates: list[dict]) -> dict[str, Any]:
        """기존 API 호환."""
        return {
            "factor_candidates": factor_candidates,
            "selected": factor_candidates[:1] if factor_candidates else [],
        }

    @mode_b_only
    def sample(self) -> str:
        """기존 API 호환. select_direction() 위임."""
        return self.select_direction()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _save_state(self) -> None:
        tmp = Path(str(self._state_path) + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._sampler.to_dict(), f, ensure_ascii=False, indent=2)
            tmp.rename(self._state_path)
        except Exception as e:
            logger.error("[co_steer] 상태 저장 실패: %s", e)
            if tmp.exists():
                tmp.unlink()

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            with self._state_path.open("r", encoding="utf-8") as f:
                d = json.load(f)
            self._sampler.load_dict(d)
            logger.info("[co_steer] 이전 상태 복원: %s", self._sampler.posteriors())
        except Exception as e:
            logger.warning("[co_steer] 상태 로드 실패: %s. 초기값 사용", e)

    def _append_history(self, entry: dict[str, Any]) -> None:
        with self._history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

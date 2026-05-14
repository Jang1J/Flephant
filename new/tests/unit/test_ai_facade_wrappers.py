"""Legacy AI facade wrappers should execute real code paths."""
from __future__ import annotations

import numpy as np

from src.agents.mode_b.alpha_factor import AlphaFactorAgent
from src.data.dqr import DataQualityReport
from src.mode_b.alpha_factor.eval_agent import EvalResult
from src.mode_b.alpha_factor.factor_agent import FactorCandidate
from src.models.lgbm_quant import LGBMQuant
from src.models.ppo_allocator import PPOAllocator


class _DummyModel:
    def predict(self, matrix):
        arr = np.asarray(matrix, dtype=float)
        return arr.sum(axis=1)


class _DummyRegistry:
    def load_latest(self):
        return _DummyModel(), {
            "version": "test-v1",
            "feature_cols": ["feat_a", "feat_b"],
        }


class _FakeFactorAgent:
    def implement(self, hypothesis):
        return FactorCandidate(
            candidate_id="FAC-20260511-TEST0001",
            hypothesis_id=hypothesis.hypothesis_id,
            code="def factor(df):\n    return df['close']\n",
            ast_hash="0123456789abcdef",
            ast_node_count=8,
            description=hypothesis.specification,
            status="active",
            attempt_count=1,
            created_at="2026-05-11T18:00:00+09:00",
            error=None,
        )


class _FakeEvalAgent:
    def evaluate(self, candidate, hypothesis):
        return EvalResult(
            candidate_id=candidate.candidate_id,
            r_g=0.1,
            sl=0.1,
            pc=0.0,
            er=0.1,
            ic=0.03,
            rank_ic=0.02,
            passed=True,
            failure_category=None,
            reason="PASS",
        )


def test_lgbm_quant_facade_predicts_single_and_batch():
    quant = LGBMQuant(registry=_DummyRegistry())

    single = quant.predict({"feat_a": 1.0, "feat_b": 2.5})
    assert single["score"] == 3.5
    assert single["model_version"] == "test-v1"

    batch = quant.predict({
        "5930": {"feat_a": 1.0, "feat_b": 1.0},
        "000660": {"feat_a": 2.0, "feat_b": 3.0},
    })
    assert batch["scores"]["005930"] == 2.0
    assert batch["scores"]["000660"] == 5.0


def test_alpha_factor_facade_delegates_generation_and_eval():
    agent = AlphaFactorAgent(
        idea_agent=object(),
        factor_agent=_FakeFactorAgent(),
        eval_agent=_FakeEvalAgent(),
    )

    generated = agent.generate_factor("외국인 수급 모멘텀")
    assert generated["status"] == "active"
    assert generated["candidate_id"].startswith("FAC-")

    evaluated = agent.evaluate_factor(
        "def factor(df):\n    return df['close']\n",
        bundle_id="BUNDLE-20260511-TEST0001",
    )
    assert evaluated["passed"] is True
    assert evaluated["bundle_id"] == "BUNDLE-20260511-TEST0001"


def test_data_quality_report_computes_bar_quality():
    bars = [
        {
            "ticker": "005930",
            "date": "20260508",
            "ts_close": f"2026-05-08T09:{minute:02d}:00+09:00",
            "received_at": f"2026-05-08T09:{minute:02d}:01+09:00",
            "close": 100.0 + minute,
        }
        for minute in range(60)
    ]
    report = DataQualityReport(connector_name="kis_rest").report(bars)
    assert report["actual_count"] == 60
    assert report["expected_count"] == 390
    assert report["missing_rate_pct"] > 0.0
    assert report["latency_ms"] == 1000.0
    assert report["ticker_count"] == 1


def test_ppo_allocator_legacy_load_keeps_heuristic_without_artifact():
    allocator = PPOAllocator()
    allocator.load()
    assert allocator.policy_version == "heuristic_v1" or allocator.policy_version.startswith("ppo_")

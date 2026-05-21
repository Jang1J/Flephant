"""S1-2 PPO Allocator unit tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.models.ppo_allocator import (
    PolicyNotLoadedError,
    PPOAllocator,
)


# ====================================================================== #
# Fixtures
# ====================================================================== #


@pytest.fixture
def allocator() -> PPOAllocator:
    return PPOAllocator()


def _quant_output(scores: dict[str, float], ts: str = "2026-04-20T10:00:00+09:00") -> dict:
    return {
        "tickers": list(scores.keys()),
        "scores": scores,
        "ts": ts,
        "mode": "active",
        "latency_ms": 5.0,
        "n_tickers": len(scores),
    }


# ====================================================================== #
# 1. 초기화 + config 로드
# ====================================================================== #


def test_init_loads_risk_config(allocator: PPOAllocator) -> None:
    assert allocator._max_names == 10
    assert allocator._max_single_name == pytest.approx(0.20)
    assert allocator._min_cash == pytest.approx(0.10)
    # min_confidence 0.30 → 0.03 (2026-04-20 Phase 2, dead zone 방지)
    assert allocator._min_confidence == pytest.approx(0.03)
    assert allocator.policy_version == "heuristic_v1"


def test_init_no_policy_path(allocator: PPOAllocator) -> None:
    assert allocator._policy is None


def test_init_with_missing_policy_path_raises(tmp_path: Path) -> None:
    fake = tmp_path / "nonexistent.zip"
    with pytest.raises(PolicyNotLoadedError):
        PPOAllocator(policy_path=fake)


# ====================================================================== #
# 2. 입력 정규화
# ====================================================================== #


def test_normalize_scores_from_quant_output_dict() -> None:
    qo = _quant_output({"005930": 0.5, "000660": 0.3})
    out = PPOAllocator._normalize_scores(qo)
    assert out == {"005930": 0.5, "000660": 0.3}


def test_normalize_scores_from_c7_list() -> None:
    c7 = [
        {"ticker": "005930", "score": 0.5, "confidence": 0.8},
        {"ticker": "000660", "score": 0.3, "confidence": 0.6},
    ]
    out = PPOAllocator._normalize_scores(c7)
    assert out == {"005930": 0.5, "000660": 0.3}


def test_normalize_scores_empty_input() -> None:
    assert PPOAllocator._normalize_scores({}) == {}
    assert PPOAllocator._normalize_scores([]) == {}


def test_normalize_scores_pads_ticker() -> None:
    qo = _quant_output({"5930": 0.5})  # 4자리 (zero-pad 대상)
    out = PPOAllocator._normalize_scores(qo)
    assert "005930" in out


def test_normalize_scores_skips_malformed_and_nonfinite_scores() -> None:
    qo = _quant_output({"005930": "N/A", "000660": float("inf"), "035420": 0.7})  # type: ignore[arg-type]
    out = PPOAllocator._normalize_scores(qo)
    assert out == {"035420": 0.7}


# ====================================================================== #
# 3. allocate (Happy path)
# ====================================================================== #


def test_allocate_happy_20_stocks(allocator: PPOAllocator) -> None:
    # 20 종목, score 내림차순 선형
    scores = {f"00000{i:01d}" if i < 10 else f"00001{i-10}": float(20 - i) / 20
              for i in range(20)}
    # ticker 형식 정리 (6자리)
    scores = {str(k).zfill(6): v for k, v in scores.items()}

    qo = _quant_output(scores)
    result = allocator.allocate(qo)
    plan = result["allocation_plan"]

    # n_holdings ≤ max_names (10)
    assert result["metadata"]["n_holdings"] <= 10
    # target_weights + cash = 1.0
    total = sum(plan["target_weights"].values()) + plan["cash_weight"]
    assert total == pytest.approx(1.0, abs=1e-6)
    # portfolio_patch_id 형식 PP-
    assert result["metadata"]["portfolio_patch_id"].startswith("PP-")


def test_allocate_empty_scores(allocator: PPOAllocator) -> None:
    result = allocator.allocate(_quant_output({}))
    plan = result["allocation_plan"]
    assert plan["target_weights"] == {}
    assert plan["cash_weight"] == 1.0
    assert result["metadata"]["n_holdings"] == 0


def test_allocate_all_below_min_confidence(allocator: PPOAllocator) -> None:
    """min_confidence threshold 초과 종목이 하나도 없는 시나리오.

    2026-04-20 Phase 2: min_confidence 0.03으로 재조정 후, 균등 분포로는
    확실히 reject를 유도하기 위해 artificial 낮은 분포 사용.
    600종목 균등 → softmax prob ≈ 0.00167 → min_conf 0.03 미만 → 전부 제외.
    """
    scores = {str(i).zfill(6): 1.0 for i in range(600)}
    result = allocator.allocate(_quant_output(scores))
    plan = result["allocation_plan"]
    assert plan["target_weights"] == {}
    assert plan["cash_weight"] == 1.0
    assert result["metadata"]["reason"] == "all_below_min_confidence"


def test_allocate_uses_quant_confidences_when_provided(allocator: PPOAllocator) -> None:
    """Quant이 emit한 confidences를 PPO가 우선 사용한다 (architecture.md L1239 SSOT).

    동일 score 3종목이지만 confidences가 다르면 min_confidence 필터가 다르게 동작.
    softmax fallback이라면 3종 균등(0.333)이 되어 모두 통과되지만, Quant confidence
    중 한 종목만 threshold 미달이면 그 종목만 reject된다.
    """
    scores = {"005930": 1.0, "000660": 1.0, "035420": 1.0}
    qo = _quant_output(scores)
    qo["confidences"] = {"005930": 0.5, "000660": 0.5, "035420": 0.001}

    result = allocator.allocate(qo)
    weights = result["allocation_plan"]["target_weights"]
    rejected = result["metadata"]["rejected"]
    assert "035420" not in weights
    assert {item["ticker"]: item["reason"] for item in rejected
            if item["reason"] == "below_min_confidence"} == {
        "035420": "below_min_confidence",
    }


def test_allocate_falls_back_to_softmax_when_confidences_missing(
    allocator: PPOAllocator,
) -> None:
    """quant_output에 confidences 키가 없으면 기존 softmax(scores) fallback 작동."""
    scores = {"005930": 10.0, "000660": 10.0, "035420": 10.0}
    qo = _quant_output(scores)  # confidences 미주입
    result = allocator.allocate(qo)
    # softmax 균등 ≈ 0.333 → min_confidence(0.03) 통과 → 3종목 전부 포함
    assert set(result["allocation_plan"]["target_weights"].keys()) == set(scores.keys())


def test_trade_probability_gate_filters_low_probability(allocator: PPOAllocator) -> None:
    allocator._trade_probability_gate_enabled = True
    allocator._min_trade_probability = 0.6
    qo = _quant_output({
        "005930": 10.0,
        "000660": 9.0,
        "035420": 8.0,
    })
    qo["trade_probs"] = {
        "005930": 0.2,
        "000660": 0.9,
        "035420": 0.1,
    }

    result = allocator.allocate(qo)
    weights = result["allocation_plan"]["target_weights"]
    rejected = result["metadata"]["rejected"]

    assert set(weights) == {"000660"}
    assert result["metadata"]["trade_probability_gate"]["applied"] is True
    assert result["metadata"]["trade_probability_gate"]["n_rejected"] == 2
    assert {
        (item["ticker"], item["reason"])
        for item in rejected
        if item["reason"] == "below_min_trade_probability"
    } == {
        ("005930", "below_min_trade_probability"),
        ("035420", "below_min_trade_probability"),
    }


def test_trade_probability_gate_keeps_existing_flow_when_probs_missing(
    allocator: PPOAllocator,
) -> None:
    allocator._trade_probability_gate_enabled = True
    allocator._min_trade_probability = 0.6
    scores = {
        "005930": 10.0,
        "000660": 10.0,
    }

    result = allocator.allocate(_quant_output(scores))

    assert result["allocation_plan"]["target_weights"]
    assert result["metadata"]["trade_probability_gate"] == {
        "enabled": True,
        "applied": False,
        "min_probability": 0.6,
        "reason": "trade_probs_missing",
    }


def test_allocate_few_dominant_scores(allocator: PPOAllocator) -> None:
    # 2 종목 극단적으로 높음 → softmax에서 0.5 이상 독점
    scores = {
        "005930": 10.0,    # dominant
        "000660": 10.0,    # dominant
        "035420": 0.01,
        "051910": 0.01,
    }
    result = allocator.allocate(_quant_output(scores))
    plan = result["allocation_plan"]
    assert len(plan["target_weights"]) > 0
    # 둘 중 하나는 max_single_name(0.2) cap 맞아야
    for w in plan["target_weights"].values():
        assert w <= allocator._max_single_name + 1e-9


# ====================================================================== #
# 4. 제약 적용
# ====================================================================== #


def test_max_single_name_cap_enforced(allocator: PPOAllocator) -> None:
    # 단일 dominant ticker
    scores = {
        "005930": 100.0,
        "000660": 0.001,
        "035420": 0.001,
        "051910": 0.001,
        "005380": 0.001,
    }
    result = allocator.allocate(_quant_output(scores))
    plan = result["allocation_plan"]
    for w in plan["target_weights"].values():
        assert w <= allocator._max_single_name + 1e-9


def test_max_sector_cap_enforced(allocator: PPOAllocator) -> None:
    scores = {
        "005930": 10.0,
        "000660": 10.0,
        "042700": 10.0,
        "403870": 10.0,
        "058470": 10.0,
    }

    result = allocator.allocate(_quant_output(scores))
    weights = result["allocation_plan"]["target_weights"]
    semiconductor_weight = sum(weights.values())

    assert set(weights) == set(scores)
    assert semiconductor_weight == pytest.approx(allocator._max_sector, abs=1e-9)
    assert result["allocation_plan"]["cash_weight"] == pytest.approx(
        1.0 - allocator._max_sector,
        abs=1e-9,
    )


def test_min_cash_ratio(allocator: PPOAllocator) -> None:
    scores = {
        "005930": 10.0,
        "000660": 10.0,
        "035420": 10.0,
    }
    result = allocator.allocate(_quant_output(scores))
    plan = result["allocation_plan"]
    assert plan["cash_weight"] >= allocator._min_cash - 1e-9


def test_regime_gate_red_zero_multiplier(allocator: PPOAllocator) -> None:
    scores = {
        "005930": 10.0,
        "000660": 10.0,
        "035420": 10.0,
    }
    result = allocator.allocate(
        _quant_output(scores),
        market_state={"regime_state": "red"},
    )
    plan = result["allocation_plan"]
    # red → new_entry_weight_multiplier=0.0 → 전부 0
    assert all(w == 0.0 for w in plan["target_weights"].values())
    assert plan["cash_weight"] == pytest.approx(1.0, abs=1e-6)


def test_regime_gate_yellow_half_multiplier(allocator: PPOAllocator) -> None:
    scores = {"005930": 10.0, "000660": 10.0, "035420": 10.0}
    base = allocator.allocate(_quant_output(scores))
    yellow = allocator.allocate(
        _quant_output(scores),
        market_state={"regime_state": "yellow"},
    )
    # yellow target_weights 합 ≈ base의 절반 (within margin)
    base_sum = sum(base["allocation_plan"]["target_weights"].values())
    yellow_sum = sum(yellow["allocation_plan"]["target_weights"].values())
    if base_sum > 0:
        assert yellow_sum < base_sum


# ====================================================================== #
# 5. C7 output 스키마
# ====================================================================== #


def test_output_schema_c7_fields(allocator: PPOAllocator) -> None:
    scores = {f"00{i:04d}": float(100 - i) for i in range(5)}
    result = allocator.allocate(_quant_output(scores))

    # C7 required fields
    plan = result["allocation_plan"]
    assert "target_weights" in plan
    assert "cash_weight" in plan
    assert "policy_version" in plan
    assert "constraints_applied" in plan

    constraints = plan["constraints_applied"]
    assert constraints["max_names"] == 10
    assert constraints["max_single_name"] == pytest.approx(0.20)
    assert constraints["max_sector"] == pytest.approx(0.40)
    assert constraints["min_cash"] == pytest.approx(0.10)


def test_output_metadata_portfolio_patch_id(allocator: PPOAllocator) -> None:
    scores = {f"00{i:04d}": float(i) for i in range(5)}
    result = allocator.allocate(_quant_output(scores))
    assert result["metadata"]["portfolio_patch_id"].startswith("PP-")
    assert "n_holdings" in result["metadata"]
    assert "n_rejected" in result["metadata"]
    assert "rejected" in result["metadata"]


def test_output_ts_propagation(allocator: PPOAllocator) -> None:
    scores = {"005930": 1.0}
    result = allocator.allocate(_quant_output(scores, ts="2026-04-20T11:00:00+09:00"))
    assert result["metadata"]["ts"] == "2026-04-20T11:00:00+09:00"


# ====================================================================== #
# 6. Policy loading (Sprint 3+)
# ====================================================================== #


def test_load_policy_uses_latest_artifact_or_keeps_heuristic() -> None:
    alloc = PPOAllocator()
    alloc.load()
    assert alloc.policy_version == "heuristic_v1" or alloc.policy_version.startswith("ppo_")


def test_policy_version_default_heuristic(allocator: PPOAllocator) -> None:
    assert allocator.policy_version == "heuristic_v1"


def test_ppo_policy_rejects_larger_dynamic_universe(allocator: PPOAllocator) -> None:
    allocator._policy = object()
    allocator._policy_n_stocks = 20
    scores = {str(i).zfill(6): float(i) for i in range(21)}

    result = allocator.allocate(_quant_output(scores))

    assert result["allocation_plan"]["target_weights"] == {}
    assert result["metadata"]["reason"] == "ppo_policy_universe_mismatch"


def test_ppo_policy_rejects_smaller_dynamic_universe(allocator: PPOAllocator) -> None:
    allocator._policy = object()
    allocator._policy_n_stocks = 20
    scores = {str(i).zfill(6): float(i) for i in range(19)}

    result = allocator.allocate(_quant_output(scores))

    assert result["allocation_plan"]["target_weights"] == {}
    assert result["metadata"]["reason"] == "ppo_policy_universe_mismatch"
    assert result["metadata"]["n_rejected"] == 19

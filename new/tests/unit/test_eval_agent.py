"""Unit tests for S3-3 EvalAgent.

AlphaAgent 3중 정규화 + IC 계산 + 중복 탐지 + 실패 카테고리 분류.

PIT-Safety: forward_returns = close.pct_change(1).shift(-1) 사용 확인.
하드코딩 금지: eval_agent.py는 risk_config.yaml에서 수치 로드.
"""
from __future__ import annotations

from dataclasses import fields
from unittest.mock import MagicMock

import pytest

from src.orchestration.llm_router import LLMCallResult
from src.mode_b.alpha_factor.eval_agent import (
    EvalAgent,
    EvalResult,
    _count_features,
)


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #

@pytest.fixture()
def simple_hypothesis():
    """단순 가설 fixture."""
    from src.mode_b.alpha_factor.idea_agent import Hypothesis
    return Hypothesis(
        observation="VWAP 편차가 클수록 단기 반전이 발생한다",
        knowledge="Mean-reversion 이론 기반",
        justification="VWAP 대비 과매도/과매수 구간에서 반전 수익",
        specification="vwap_deviation(close, vwap)를 rank() 처리",
        anchor_id=None,
        created_at="2026-04-28T18:00:00+09:00",
        hypothesis_id="HYP-20260428-TESTFIX1",
    )


def _make_simple_candidate(code: str | None = None, node_count: int | None = None) -> object:
    """FactorCandidate 유사 dataclass 인스턴스 생성.

    node_count가 None이면 실제 AST에서 계산.
    node_count를 명시하면 그 값을 ast_node_count로 사용 (오버라이드).
    """
    from src.mode_b.alpha_factor.factor_agent import FactorCandidate
    # 복잡도 낮은 코드 (AST node count < max_ast_complexity=10 가 되도록 단순하게)
    actual_code = code or (
        "def factor(df):\n"
        "    return rank(df['close'])\n"
    )
    import ast as _ast
    import hashlib
    tree = _ast.parse(actual_code)
    dump = _ast.dump(tree, annotate_fields=False)
    ast_hash = hashlib.sha256(dump.encode()).hexdigest()[:16]
    node_count_real = sum(1 for _ in _ast.walk(tree))
    actual_node_count = node_count if node_count is not None else node_count_real
    return FactorCandidate(
        candidate_id="FAC-20260428-TEST0001",
        hypothesis_id="HYP-20260428-TESTFIX1",
        code=actual_code,
        ast_hash=ast_hash,
        ast_node_count=actual_node_count,
        description="VWAP 편차 rank 팩터",
        status="active",
        attempt_count=1,
        created_at="2026-04-28T18:00:00+09:00",
        error=None,
    )


# ------------------------------------------------------------------ #
# Test 1: EvalResult dataclass 10 필드 확인
# ------------------------------------------------------------------ #

def test_eval_result_dataclass_fields():
    """EvalResult는 정확히 10개 필드를 가져야 한다."""
    field_names = {f.name for f in fields(EvalResult)}
    expected = {
        "candidate_id", "r_g", "sl", "pc", "er",
        "ic", "rank_ic", "passed", "failure_category", "reason",
    }
    assert field_names == expected, f"필드 불일치: {field_names ^ expected}"


# ------------------------------------------------------------------ #
# Test 2: evaluate() → EvalResult 반환
# ------------------------------------------------------------------ #

def test_evaluate_returns_eval_result(simple_hypothesis):
    """evaluate() 반환 타입은 EvalResult여야 한다."""
    agent = EvalAgent(llm_router=None)
    candidate = _make_simple_candidate()
    result = agent.evaluate(candidate, simple_hypothesis)
    assert isinstance(result, EvalResult), f"기대 EvalResult, 실제 {type(result)}"
    assert result.candidate_id == candidate.candidate_id


# ------------------------------------------------------------------ #
# Test 3: SL = ast_node_count / max_ast_complexity
# ------------------------------------------------------------------ #

def test_sl_normalized_by_max_complexity(simple_hypothesis):
    """SL(f) = ast_node_count / max_ast_complexity."""
    agent = EvalAgent(llm_router=None)
    # node_count=5 명시 오버라이드
    candidate = _make_simple_candidate(node_count=5)
    sl = agent._compute_sl(candidate)
    # max_ast_complexity = 10 (yaml 기본값)
    expected = 5 / agent._max_ast_complexity
    assert abs(sl - expected) < 1e-9, f"SL 기대값={expected:.4f}, 실제={sl:.4f}"


# ------------------------------------------------------------------ #
# Test 4: PC는 숫자 리터럴 카운트
# ------------------------------------------------------------------ #

def test_pc_counts_numeric_literals():
    """PC(f): window=20, window=30 등 int 리터럴 2개 → PC=2."""
    code = (
        "def factor(df):\n"
        "    m = rolling_mean(df['close'], 20)\n"
        "    s = rolling_std(df['close'], 30)\n"
        "    return (df['close'] - m) / (s + 1e-8)\n"
    )
    agent = EvalAgent(llm_router=None)
    pc_count = agent._compute_pc(code)
    # 20, 30, 1e-8 = 3개 (float도 포함). 최소 2 이상이어야 함.
    assert pc_count >= 2, f"PC count >= 2 기대, 실제={pc_count}"


# ------------------------------------------------------------------ #
# Test 5: IC 계산은 float 반환
# ------------------------------------------------------------------ #

def test_ic_computed_on_synthetic_data():
    """_compute_ic()는 (float, float) 튜플을 반환해야 한다."""
    agent = EvalAgent(llm_router=None)
    code = (
        "def factor(df):\n"
        "    dev = vwap_deviation(df['close'], df['vwap'])\n"
        "    return rank(dev)\n"
    )
    ic, rank_ic = agent._compute_ic(code)
    assert isinstance(ic, float), f"IC는 float여야 함. 실제 {type(ic)}"
    assert isinstance(rank_ic, float), f"RankIC는 float여야 함. 실제 {type(rank_ic)}"
    assert -1.0 <= ic <= 1.0, f"IC 범위 [-1, 1] 초과: {ic}"
    assert -1.0 <= rank_ic <= 1.0, f"RankIC 범위 초과: {rank_ic}"


# ------------------------------------------------------------------ #
# Test 6: |IC| < ic_min → failure_category='poor_ic'
# ------------------------------------------------------------------ #

def test_poor_ic_triggers_failure(simple_hypothesis):
    """IC가 거의 0인 팩터 → failure_category='poor_ic'."""
    agent = EvalAgent(llm_router=None)
    # node_count 낮게 (complexity_violation 방지), IC 패치
    original_compute_ic = agent._compute_ic
    agent._compute_ic = lambda code: (0.001, 0.001)  # |IC| < ic_min_threshold=0.02
    try:
        candidate = _make_simple_candidate(node_count=5)
        result = agent.evaluate(candidate, simple_hypothesis)
        assert result.failure_category == "poor_ic", (
            f"failure_category='poor_ic' 기대, 실제={result.failure_category}"
        )
        assert not result.passed
    finally:
        agent._compute_ic = original_compute_ic


# ------------------------------------------------------------------ #
# Test 7: SL > 1.0 → 'complexity_violation'
# ------------------------------------------------------------------ #

def test_complexity_violation_detected(simple_hypothesis):
    """ast_node_count > max_ast_complexity → failure_category='complexity_violation'."""
    agent = EvalAgent(llm_router=None)
    # max_ast_complexity = 10. node_count = 150이면 SL = 15 > 1.0
    candidate_big = _make_simple_candidate(node_count=150)
    result = agent.evaluate(candidate_big, simple_hypothesis)
    assert result.failure_category == "complexity_violation", (
        f"'complexity_violation' 기대, 실제={result.failure_category}"
    )
    assert result.sl > 1.0, f"sl > 1.0 기대, 실제={result.sl}"
    assert not result.passed


# ------------------------------------------------------------------ #
# Test 8: 동일 코드 두 번 → crowding_risk 또는 duplicate
# ------------------------------------------------------------------ #

def test_crowding_risk_detected(simple_hypothesis):
    """같은 코드를 factor_zoo에 포함하면 similarity >= ic_duplicate_threshold → crowding_risk."""
    agent = EvalAgent(llm_router=None)
    code = (
        "def factor(df):\n"
        "    dev = vwap_deviation(df['close'], df['vwap'])\n"
        "    return rank(dev)\n"
    )
    import ast
    import hashlib
    tree = ast.parse(code)
    dump = ast.dump(tree, annotate_fields=False)
    ast_hash = hashlib.sha256(dump.encode()).hexdigest()[:16]

    # 동일 코드를 factor_zoo에 active로 등록
    zoo_entry = {
        "candidate_id": "FAC-20260428-ZOOENTRY1",
        "hypothesis_id": "HYP-PREV",
        "code": code,
        "ast_hash": ast_hash,
        "ast_node_count": 10,
        "description": "기존 팩터",
        "status": "active",
        "attempt_count": 1,
        "created_at": "2026-04-27T18:00:00+09:00",
        "error": None,
    }

    # node_count 낮게 (complexity_violation 방지)
    candidate = _make_simple_candidate(code=code, node_count=5)
    # IC를 ic_min 이상으로 만들기 위해 패치
    agent._compute_ic = lambda c: (0.05, 0.05)

    result = agent.evaluate(candidate, simple_hypothesis, factor_zoo=[zoo_entry])
    # crowding_risk: similarity == 1.0 >= ic_duplicate_threshold=0.99
    assert result.failure_category == "crowding_risk", (
        f"'crowding_risk' 기대, 실제={result.failure_category}. "
        f"similarity 확인 필요"
    )
    assert not result.passed


# ------------------------------------------------------------------ #
# Test 9: 정상 팩터 → passed=True, R_g < r_g_threshold
# ------------------------------------------------------------------ #

def test_passed_result_when_all_pass(simple_hypothesis):
    """IC 충분 + 복잡도 낮음 + 중복 없음 → passed=True, r_g < r_g_threshold."""
    agent = EvalAgent(llm_router=None)
    # IC 강제 패치 (ic_min = 0.02 이상)
    agent._compute_ic = lambda code: (0.05, 0.05)
    # similarity = 0 (zoo 비어있음)
    # alignment = 0.5 (llm_router=None fallback)
    # node_count 낮게 (SL < 1.0 보장: 5/10=0.5)
    candidate = _make_simple_candidate(node_count=5)
    result = agent.evaluate(candidate, simple_hypothesis, factor_zoo=[])
    assert result.failure_category is None, f"failure 기대 없음, 실제={result.failure_category}"
    assert result.passed, "passed=True 기대"
    assert result.r_g < agent._r_g_threshold, (
        f"R_g={result.r_g} < threshold={agent._r_g_threshold} 기대"
    )


# ------------------------------------------------------------------ #
# Test 10: llm_router=None → alignment fallback = 0.5
# ------------------------------------------------------------------ #

def test_no_llm_router_fallback_alignment(simple_hypothesis):
    """llm_router=None이면 C(h,d,f) = 0.5 (fallback)."""
    agent = EvalAgent(llm_router=None)
    candidate = _make_simple_candidate(node_count=5)
    alignment = agent._compute_alignment(simple_hypothesis, candidate)
    assert alignment == 0.5, f"alignment fallback=0.5 기대, 실제={alignment}"


# ------------------------------------------------------------------ #
# Test 11 (보너스): EvalResult.to_dict()
# ------------------------------------------------------------------ #

def test_eval_result_to_dict(simple_hypothesis):
    """to_dict()는 dict를 반환하고 모든 10 필드를 포함해야 한다."""
    agent = EvalAgent(llm_router=None)
    agent._compute_ic = lambda code: (0.05, 0.05)
    candidate = _make_simple_candidate(node_count=5)
    result = agent.evaluate(candidate, simple_hypothesis, factor_zoo=[])
    d = result.to_dict()
    assert isinstance(d, dict)
    for f in fields(EvalResult):
        assert f.name in d, f"to_dict() 누락 필드: {f.name}"


# ------------------------------------------------------------------ #
# Test 12 (보너스): _count_features
# ------------------------------------------------------------------ #

def test_count_features_unique():
    """동일 컬럼 중복 참조는 1개로 카운트."""
    code = (
        "def factor(df):\n"
        "    a = df['close']\n"
        "    b = df['close'] + df['volume']\n"
        "    return a + b\n"
    )
    count = _count_features(code)
    # close, volume → unique 2개
    assert count == 2, f"unique feature count=2 기대, 실제={count}"


# ------------------------------------------------------------------ #
# Test 13: hypothesis_misalignment 카테고리
# ------------------------------------------------------------------ #

def test_hypothesis_misalignment_failure(simple_hypothesis, monkeypatch):
    """alignment > threshold 이면 failure_category='hypothesis_misalignment'."""
    agent = EvalAgent(llm_router=None)
    # alignment를 무조건 1.0 반환하도록 패치
    monkeypatch.setattr(agent, "_compute_alignment", lambda h, c: 1.0)
    agent._compute_ic = lambda code: (0.10, 0.10)  # IC는 충분히 큼
    candidate = _make_simple_candidate(node_count=5)
    result = agent.evaluate(candidate, simple_hypothesis, factor_zoo=[])
    assert result.failure_category == "hypothesis_misalignment", (
        f"expected 'hypothesis_misalignment', got '{result.failure_category}'"
    )
    assert result.passed is False


def test_llm_score_alignment_uses_mode_b_and_llm_call_result() -> None:
    """EvalAgent alignment GPT 호출은 mode_b/factor_evaluation이며 content를 파싱한다."""
    router = MagicMock()
    router.call.return_value = LLMCallResult(
        success=True,
        model_used="gpt-4o",
        content="0.25",
        latency_ms=1.0,
    )
    agent = EvalAgent(llm_router=router)

    score = agent._llm_score_alignment("hypothesis", "factor code", "C")

    assert score == 0.25
    _, kwargs = router.call.call_args
    assert kwargs["mode"] == "mode_b"
    assert kwargs["caller"] == "factor_evaluation"


# ------------------------------------------------------------------ #
# Test 14: execution_failure 카테고리
# ------------------------------------------------------------------ #

def test_execution_failure_category(simple_hypothesis):
    """실행 불가 코드 → failure_category='execution_failure'."""
    agent = EvalAgent(llm_router=None)
    # 실행 시 예외를 발생시키는 코드
    bad_code = "def factor(df):\n    raise RuntimeError('intentional')\n"
    from src.mode_b.alpha_factor.factor_agent import FactorCandidate
    candidate = FactorCandidate(
        candidate_id="FAC-TEST-EXEC",
        hypothesis_id="HYP-TEST",
        code=bad_code,
        ast_hash="deadbeef12345678",
        ast_node_count=5,
        description="실행 실패 테스트",
        status="active",
        attempt_count=1,
        created_at="2026-04-28T18:00:00+09:00",
        error=None,
    )
    result = agent.evaluate(candidate, simple_hypothesis, factor_zoo=[])
    assert result.failure_category == "execution_failure", (
        f"expected 'execution_failure', got '{result.failure_category}'"
    )
    assert result.passed is False

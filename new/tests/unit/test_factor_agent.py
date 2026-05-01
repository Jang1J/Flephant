"""S3-2 FactorAgent unit tests.

done_criteria:
- test >= 8개 PASS
- artifacts/alpha_factor/factor_zoo.jsonl 실 생성
- pytest 전체 PASS
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ------------------------------------------------------------------ #
# 경로 설정
# ------------------------------------------------------------------ #

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "new"))

# ------------------------------------------------------------------ #
# import
# ------------------------------------------------------------------ #

from src.mode_b.alpha_factor.factor_agent import (
    FactorAgent,
    FactorCandidate,
)
from src.mode_b.alpha_factor.idea_agent import Hypothesis


# ------------------------------------------------------------------ #
# 공통 fixture
# ------------------------------------------------------------------ #

@pytest.fixture
def sample_hypothesis() -> Hypothesis:
    return Hypothesis(
        observation="외국인 순매수 급증 패턴",
        knowledge="외국인 수급은 단기 모멘텀 선행 지표",
        justification="외국인 순매수 증가 시 기관 추종 매수 발생",
        specification="ts_zscore(df['foreign_net_buy'], 20)",
        anchor_id=None,
        created_at="2026-04-28T00:00:00+09:00",
        hypothesis_id="HYP-20260428-TESTTEST",
    )


@pytest.fixture
def tmp_factor_agent(tmp_path: Path) -> FactorAgent:
    """factor_zoo_path를 tmp_path로 오버라이드한 FactorAgent."""
    zoo_path = tmp_path / "factor_zoo.jsonl"
    with patch(
        "src.mode_b.alpha_factor.factor_agent.config_load"
    ) as mock_cfg:
        mock_cfg.return_value = {
            "factor_zoo_path": str(zoo_path),
            "max_retries": 3,
            "max_ast_complexity": 100,  # 테스트에선 넉넉하게
        }
        agent = FactorAgent(llm_router=None)
        agent._factor_zoo_path = zoo_path
    return agent


# ------------------------------------------------------------------ #
# T1: FactorCandidate dataclass 필드 확인
# ------------------------------------------------------------------ #

def test_factor_candidate_dataclass_fields():
    """FactorCandidate에 required 9개 필드 + error 필드 존재."""
    required_fields = {
        "candidate_id", "hypothesis_id", "code", "ast_hash",
        "ast_node_count", "description", "status", "attempt_count",
        "created_at", "error",
    }
    fc = FactorCandidate(
        candidate_id="FAC-20260428-ABCD1234",
        hypothesis_id="HYP-20260428-XYZ",
        code="def factor(df): return df['close'].rank(pct=True)",
        ast_hash="abcdef0123456789",
        ast_node_count=10,
        description="test factor",
        status="active",
        attempt_count=1,
        created_at="2026-04-28T00:00:00+09:00",
        error=None,
    )
    d = fc.to_dict()
    assert required_fields.issubset(set(d.keys()))
    assert d["status"] == "active"
    assert d["error"] is None


# ------------------------------------------------------------------ #
# T2: implement() → FactorCandidate 반환
# ------------------------------------------------------------------ #

def test_implement_returns_candidate(
    tmp_factor_agent: FactorAgent, sample_hypothesis: Hypothesis
):
    """implement() 호출 시 FactorCandidate 인스턴스 반환."""
    candidate = tmp_factor_agent.implement(sample_hypothesis)
    assert isinstance(candidate, FactorCandidate)
    assert candidate.hypothesis_id == sample_hypothesis.hypothesis_id
    assert candidate.status in ("active", "duplicate", "failed")


# ------------------------------------------------------------------ #
# T3: ast_hash는 16자 hex
# ------------------------------------------------------------------ #

def test_ast_hash_computed(tmp_factor_agent: FactorAgent):
    """유효한 코드에 대해 ast_hash가 16자 hex 문자열."""
    code = "def factor(df):\n    return df['close'].rank(pct=True)"
    ast_hash, node_count, err = tmp_factor_agent._compute_ast_hash(code)
    assert err == ""
    assert len(ast_hash) == 16
    assert all(c in "0123456789abcdef" for c in ast_hash)
    assert node_count > 0


# ------------------------------------------------------------------ #
# T4: 실행 가능한 코드 → status=active
# ------------------------------------------------------------------ #

def test_valid_code_exec_passes(
    tmp_factor_agent: FactorAgent, sample_hypothesis: Hypothesis
):
    """실행 가능한 factor 코드 → status=active, factor_zoo.jsonl에 기록."""
    # 명확히 동작하는 코드를 LLM 응답으로 주입
    valid_code = (
        "```python\n"
        "def factor(df):\n"
        "    return rank(df['close'])\n"
        "```"
    )
    mock_router = MagicMock()
    mock_router.call.return_value = valid_code
    tmp_factor_agent._llm_router = mock_router

    candidate = tmp_factor_agent.implement(sample_hypothesis)
    assert candidate.status == "active", f"예상 active, 실제 {candidate.status}: {candidate.error}"
    assert tmp_factor_agent._factor_zoo_path.exists()


# ------------------------------------------------------------------ #
# T5: SyntaxError → 재시도 후 fallback (failed 또는 active)
# ------------------------------------------------------------------ #

def test_invalid_code_retried(
    tmp_factor_agent: FactorAgent, sample_hypothesis: Hypothesis
):
    """SyntaxError 코드를 max_retries+1 번 반환 → status=failed."""
    bad_code = "```python\ndef factor(df):\n    return @@@@\n```"
    mock_router = MagicMock()
    mock_router.call.return_value = bad_code
    tmp_factor_agent._llm_router = mock_router
    # max_retries=0 → 즉시 1회 시도 후 실패
    tmp_factor_agent._max_retries = 0

    candidate = tmp_factor_agent.implement(sample_hypothesis, max_retries=0)
    assert candidate.status == "failed"
    assert candidate.error is not None


# ------------------------------------------------------------------ #
# T6: 같은 코드 재입력 → status=duplicate
# ------------------------------------------------------------------ #

def test_duplicate_detected_by_ast_hash(
    tmp_factor_agent: FactorAgent, sample_hypothesis: Hypothesis
):
    """동일 코드 → 두 번째 implement() 에서 status=duplicate."""
    valid_code = (
        "```python\n"
        "def factor(df):\n"
        "    return rank(df['close'])\n"
        "```"
    )
    mock_router = MagicMock()
    mock_router.call.return_value = valid_code
    tmp_factor_agent._llm_router = mock_router

    c1 = tmp_factor_agent.implement(sample_hypothesis)
    assert c1.status == "active"

    # 두 번째: 같은 코드, 다른 hypothesis_id
    h2 = Hypothesis(
        observation="other",
        knowledge="other",
        justification="other",
        specification="other",
        hypothesis_id="HYP-20260428-YYYYZZZZ",
        created_at="2026-04-28T00:00:00+09:00",
    )
    c2 = tmp_factor_agent.implement(h2)
    assert c2.status == "duplicate"


# ------------------------------------------------------------------ #
# T7: factor_zoo.jsonl에 candidate 기록
# ------------------------------------------------------------------ #

def test_candidate_saved_to_jsonl(
    tmp_factor_agent: FactorAgent, sample_hypothesis: Hypothesis
):
    """implement() 후 factor_zoo.jsonl에 1줄 이상 기록."""
    candidate = tmp_factor_agent.implement(sample_hypothesis)
    assert tmp_factor_agent._factor_zoo_path.exists()
    lines = [
        l.strip()
        for l in tmp_factor_agent._factor_zoo_path.read_text().splitlines()
        if l.strip()
    ]
    assert len(lines) >= 1
    entry = json.loads(lines[-1])
    assert entry["candidate_id"] == candidate.candidate_id
    assert "ast_hash" in entry


# ------------------------------------------------------------------ #
# T8: caller='factor_implementation' 확인
# ------------------------------------------------------------------ #

def test_llm_router_called_with_factor_implementation(
    tmp_factor_agent: FactorAgent, sample_hypothesis: Hypothesis
):
    """LLMRouter.call이 caller='factor_implementation' 으로 호출됨."""
    valid_code = (
        "```python\n"
        "def factor(df):\n"
        "    return rank(df['close'])\n"
        "```"
    )
    mock_router = MagicMock()
    mock_router.call.return_value = valid_code
    tmp_factor_agent._llm_router = mock_router

    tmp_factor_agent.implement(sample_hypothesis)

    # call() 이 mode='mode_b', caller='factor_implementation' 으로 호출됐는지
    calls = mock_router.call.call_args_list
    assert len(calls) >= 1
    _, kwargs = calls[0]
    assert kwargs.get("caller") == "factor_implementation"


# ------------------------------------------------------------------ #
# T9: AST 복잡도 초과 코드 거부
# ------------------------------------------------------------------ #

def test_ast_complexity_limit(
    tmp_factor_agent: FactorAgent, sample_hypothesis: Hypothesis
):
    """max_ast_complexity=5 로 설정, 노드 수 초과 코드 → failed."""
    tmp_factor_agent._max_ast_complexity = 5
    tmp_factor_agent._max_retries = 0

    # 복잡한 코드 (노드 수 5 초과)
    complex_code = (
        "```python\n"
        "def factor(df):\n"
        "    a = ts_zscore(df['close'], 20)\n"
        "    b = rolling_mean(df['volume'], 10)\n"
        "    c = correlation(a, b, 5)\n"
        "    return rank(c)\n"
        "```"
    )
    mock_router = MagicMock()
    mock_router.call.return_value = complex_code
    tmp_factor_agent._llm_router = mock_router

    candidate = tmp_factor_agent.implement(sample_hypothesis, max_retries=0)
    assert candidate.status == "failed"
    assert "초과" in (candidate.error or "")


# ------------------------------------------------------------------ #
# T10: llm_router=None → fallback candidate
# ------------------------------------------------------------------ #

def test_fallback_when_no_llm_router(
    tmp_path: Path, sample_hypothesis: Hypothesis
):
    """llm_router=None → fallback 코드로 active 또는 valid candidate 반환."""
    zoo_path = tmp_path / "fallback_zoo.jsonl"
    with patch(
        "src.mode_b.alpha_factor.factor_agent.config_load"
    ) as mock_cfg:
        mock_cfg.return_value = {
            "factor_zoo_path": str(zoo_path),
            "max_retries": 1,
            "max_ast_complexity": 100,
        }
        agent = FactorAgent(llm_router=None)
        agent._factor_zoo_path = zoo_path

    candidate = agent.implement(sample_hypothesis)
    # fallback 코드가 실행 가능하면 active, 불가하면 failed
    assert isinstance(candidate, FactorCandidate)
    assert candidate.status in ("active", "failed")
    # zoo에 기록됨
    assert zoo_path.exists()

"""S3-1 IdeaAgent unit tests.

AlphaAgent 4요소 가설 생성 에이전트 검증 (≥ 9 케이스).
mock LLM router 사용. tmp_path fixture로 실제 파일 I/O 검증.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.orchestration.llm_router import LLMCallResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_hypothesis_path(tmp_path: Path) -> Path:
    return tmp_path / "hypotheses.jsonl"


@pytest.fixture()
def mock_cfg(tmp_hypothesis_path: Path) -> dict:
    return {
        "hypothesis_path": str(tmp_hypothesis_path),
        "factor_zoo_path": str(tmp_hypothesis_path.parent / "factor_zoo.jsonl"),
        "max_hypotheses_per_round": 3,
        "max_ast_complexity": 10,
        "ic_duplicate_threshold": 0.99,
        "alpha_decay_warning_months": 3,
        "alpha_decay_retire_months": 6,
    }


@pytest.fixture()
def agent(mock_cfg):
    """IdeaAgent 인스턴스. config_load mock."""
    with patch("src.mode_b.alpha_factor.idea_agent.config_load", return_value=mock_cfg):
        from src.mode_b.alpha_factor.idea_agent import IdeaAgent
        return IdeaAgent(llm_router=None)


@pytest.fixture()
def mock_router():
    """GPT-4o 응답을 흉내 내는 mock LLM router."""
    router = MagicMock()
    router.call.return_value = json.dumps(
        {
            "observation": "외국인 순매도 1000억 + VIX 상승 동시 발생",
            "knowledge": "외국인 매도는 단기 모멘텀 반전을 선행하는 경향이 있다",
            "justification": "외국인 수급 변화가 기관 추종 매수/매도를 유발하여 1~5분 수익률 예측에 유효",
            "specification": "ts_zscore(investor_flow, window=20) with cs_rank neutralize(sector)",
        },
        ensure_ascii=False,
    )
    return router


@pytest.fixture()
def agent_with_router(mock_cfg, mock_router):
    """LLMRouter 주입된 IdeaAgent."""
    with patch("src.mode_b.alpha_factor.idea_agent.config_load", return_value=mock_cfg):
        from src.mode_b.alpha_factor.idea_agent import IdeaAgent
        return IdeaAgent(llm_router=mock_router)


@pytest.fixture()
def market_ctx() -> dict:
    return {
        "date": "2026-04-27",
        "news_summary": "외국인 순매도 급증, VIX 상승",
        "risk_level": "high",
        "sector_moves": "반도체 -2.3%, 금융 -1.1%",
        "macro_signals": "USD/KRW 1350, 미국채 10Y 4.5%",
    }


# ---------------------------------------------------------------------------
# Test 1: Hypothesis dataclass 4요소 필드 존재
# ---------------------------------------------------------------------------

def test_hypothesis_dataclass_has_4_fields():
    """Hypothesis dataclass에 observation/knowledge/justification/specification 4필드가 있다."""
    from src.mode_b.alpha_factor.idea_agent import Hypothesis

    h = Hypothesis(
        observation="obs",
        knowledge="know",
        justification="just",
        specification="spec",
    )
    assert h.observation == "obs"
    assert h.knowledge == "know"
    assert h.justification == "just"
    assert h.specification == "spec"
    assert h.anchor_id is None
    assert h.hypothesis_id == ""


# ---------------------------------------------------------------------------
# Test 2: generate() → Hypothesis 반환
# ---------------------------------------------------------------------------

def test_generate_returns_hypothesis(agent_with_router, market_ctx):
    """generate()가 Hypothesis 인스턴스를 반환한다."""
    from src.mode_b.alpha_factor.idea_agent import Hypothesis

    result = agent_with_router.generate(market_ctx)
    assert isinstance(result, Hypothesis)
    assert result.observation != ""
    assert result.knowledge != ""
    assert result.justification != ""
    assert result.specification != ""
    assert result.hypothesis_id.startswith("HYP-")


# ---------------------------------------------------------------------------
# Test 3: generate() → JSONL 파일에 기록됨
# ---------------------------------------------------------------------------

def test_hypothesis_saved_to_jsonl(agent_with_router, market_ctx, mock_cfg):
    """generate() 후 hypothesis_path JSONL에 1줄 기록된다."""
    path = Path(mock_cfg["hypothesis_path"])
    assert not path.exists()

    agent_with_router.generate(market_ctx)

    assert path.exists()
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    d = json.loads(lines[0])
    assert "observation" in d
    assert "hypothesis_id" in d
    assert d["hypothesis_id"].startswith("HYP-")


# ---------------------------------------------------------------------------
# Test 4: anchor 있으면 프롬프트에 포함
# ---------------------------------------------------------------------------

def test_evolving_anchor_included_in_prompt(agent_with_router, market_ctx, mock_router):
    """anchor를 넘기면 LLM call 프롬프트에 anchor 내용이 포함된다."""
    from src.mode_b.alpha_factor.idea_agent import Hypothesis

    anchor = Hypothesis(
        observation="anchor_obs",
        knowledge="anchor_know",
        justification="anchor_just",
        specification="anchor_spec",
        hypothesis_id="HYP-20260427-TESTANCH",
    )

    agent_with_router.generate(market_ctx, anchor=anchor)

    call_args = mock_router.call.call_args
    prompt = call_args[0][0]  # 첫 번째 위치 인자
    assert "anchor_obs" in prompt
    assert "Evolving Anchor" in prompt


# ---------------------------------------------------------------------------
# Test 5: LLMRouter가 mode='mode_b', caller='factor_hypothesis'로 호출
# ---------------------------------------------------------------------------

def test_llm_router_called_with_mode_b(agent_with_router, market_ctx, mock_router):
    """LLMRouter.call()이 mode='mode_b', caller='factor_hypothesis'로 호출된다."""
    agent_with_router.generate(market_ctx)

    mock_router.call.assert_called_once()
    call_kwargs = mock_router.call.call_args
    # positional 또는 keyword 인자 확인
    args = call_kwargs[0]
    kwargs = call_kwargs[1]
    assert kwargs.get("mode") == "mode_b" or (len(args) > 1 and args[1] == "mode_b")
    assert kwargs.get("caller") == "factor_hypothesis" or (
        len(args) > 2 and args[2] == "factor_hypothesis"
    )
    assert "structured_schema" in kwargs


def test_generate_parses_llm_call_result_content(mock_cfg, market_ctx):
    """실제 LLMRouter 반환 타입(LLMCallResult.content)을 파싱한다."""
    from src.mode_b.alpha_factor.idea_agent import Hypothesis, IdeaAgent

    router = MagicMock()
    router.call.return_value = LLMCallResult(
        success=True,
        model_used="gpt-4o",
        content=json.dumps(
            {
                "observation": "수급과 변동성 동시 상승",
                "knowledge": "수급 충격은 단기 모멘텀을 만들 수 있다",
                "justification": "거래량 확대와 위험 회피가 동시에 관측된다",
                "specification": "ts_zscore(foreign_net_buy, 20)",
            },
            ensure_ascii=False,
        ),
        latency_ms=1.0,
    )
    with patch("src.mode_b.alpha_factor.idea_agent.config_load", return_value=mock_cfg):
        agent = IdeaAgent(llm_router=router)

    result = agent.generate(market_ctx)

    assert isinstance(result, Hypothesis)
    assert result.observation == "수급과 변동성 동시 상승"


# ---------------------------------------------------------------------------
# Test 6: LLM 실패 시 fallback 가설 반환
# ---------------------------------------------------------------------------

def test_fallback_on_llm_failure(mock_cfg, market_ctx):
    """LLMRouter.call()이 예외를 던지면 fallback 가설을 반환한다."""
    from src.mode_b.alpha_factor.idea_agent import Hypothesis

    failing_router = MagicMock()
    failing_router.call.side_effect = RuntimeError("LLM timeout")

    with patch("src.mode_b.alpha_factor.idea_agent.config_load", return_value=mock_cfg):
        from src.mode_b.alpha_factor.idea_agent import IdeaAgent
        agent = IdeaAgent(llm_router=failing_router)

    result = agent.generate(market_ctx)
    assert isinstance(result, Hypothesis)
    # fallback 가설은 specification 필드가 채워져야 함
    assert result.specification != ""
    assert result.hypothesis_id.startswith("HYP-")


# ---------------------------------------------------------------------------
# Test 7: generate_batch() → max_hypotheses_per_round개 반환
# ---------------------------------------------------------------------------

def test_generate_batch_count(agent_with_router, market_ctx, mock_cfg):
    """generate_batch()가 max_hypotheses_per_round(=3)개 가설을 반환한다."""
    results = agent_with_router.generate_batch(market_ctx)
    assert len(results) == mock_cfg["max_hypotheses_per_round"]


# ---------------------------------------------------------------------------
# Test 8: load_latest_hypotheses() → JSONL에서 최근 N개 로드
# ---------------------------------------------------------------------------

def test_load_latest_hypotheses(agent_with_router, market_ctx, mock_cfg):
    """3개 가설을 generate 후 load_latest_hypotheses(n=2)가 2개를 반환한다."""
    from src.mode_b.alpha_factor.idea_agent import Hypothesis

    # 3개 생성
    agent_with_router.generate_batch(market_ctx)

    loaded = agent_with_router.load_latest_hypotheses(n=2)
    assert len(loaded) == 2
    for h in loaded:
        assert isinstance(h, Hypothesis)
        assert h.hypothesis_id.startswith("HYP-")


# ---------------------------------------------------------------------------
# Test 9: llm_router=None → fallback 가설 반환 (no error)
# ---------------------------------------------------------------------------

def test_no_llm_router_still_returns_hypothesis(agent, market_ctx):
    """llm_router=None (agent fixture)이어도 generate()가 Hypothesis를 반환한다."""
    from src.mode_b.alpha_factor.idea_agent import Hypothesis

    result = agent.generate(market_ctx)
    assert isinstance(result, Hypothesis)
    assert result.observation != ""
    assert result.specification != ""
    assert result.hypothesis_id.startswith("HYP-")


# ---------------------------------------------------------------------------
# Test 10: to_dict() 직렬화 + JSONL 재파싱 일관성
# ---------------------------------------------------------------------------

def test_hypothesis_to_dict_and_reload(agent_with_router, market_ctx, mock_cfg):
    """to_dict()로 직렬화한 뒤 JSONL 재파싱해도 동일 데이터."""
    h = agent_with_router.generate(market_ctx)
    original_dict = h.to_dict()

    path = Path(mock_cfg["hypothesis_path"])
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    reloaded = json.loads(lines[-1])

    assert reloaded["hypothesis_id"] == original_dict["hypothesis_id"]
    assert reloaded["observation"] == original_dict["observation"]
    assert reloaded["specification"] == original_dict["specification"]


# ---------------------------------------------------------------------------
# Test 11: anchor_id가 이전 가설 hypothesis_id를 참조
# ---------------------------------------------------------------------------

def test_anchor_id_links_to_previous_hypothesis(agent_with_router, market_ctx, mock_cfg):
    """anchor를 넘기면 생성된 가설의 anchor_id가 anchor.hypothesis_id와 일치한다."""
    from src.mode_b.alpha_factor.idea_agent import Hypothesis

    anchor = Hypothesis(
        observation="이전 관찰",
        knowledge="이전 이론",
        justification="이전 근거",
        specification="ts_mean(volume, window=10)",
        hypothesis_id="HYP-20260426-PREV0001",
        created_at="2026-04-26T18:00:00+09:00",
    )

    result = agent_with_router.generate(market_ctx, anchor=anchor)
    assert result.anchor_id == "HYP-20260426-PREV0001"

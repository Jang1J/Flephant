"""S2-2 LLM Router unit tests.

불변 원칙 4 코드 레벨 강제 검증:
  - Hot Path call() → RuntimeError
  - Kanana-o 100회/일 한도 (caller별 allocation 포함)
  - Mode B → GPT-4o 전용
  - circuit breaker (3회 실패 → OPEN 5분 → HALF_OPEN)
  - KST 자정 예산 리셋
  - API 키 누락 시 graceful failure
"""
from __future__ import annotations

import time

import pytest

from src.orchestration.llm_router import (
    CircuitState,
    LLMCallResult,
    LLMModel,
    LLMRouter,
    LLMRouterConfigError,
    _BudgetTracker,
    _CircuitBreaker,
)


# ====================================================================== #
# Fixtures
# ====================================================================== #


@pytest.fixture
def minimal_config() -> dict:
    """테스트용 최소 config. risk_config.yaml 로드 없이 독립 동작."""
    return {
        "llm_budget": {
            "kanana_daily_limit": 10,
            "overflow_to": "gpt-4o",
            "budget_allocation": {
                "news_analysis": 5,
                "fda_cold_path": 3,
            },
            "circuit_breaker": {
                "failure_threshold": 3,
                "open_duration_sec": 300,
            },
            "sla": {
                "timeout_sec": 30.0,
            },
            "allow_mock_provider": True,
        }
    }


@pytest.fixture
def router_with_keys(minimal_config, monkeypatch) -> LLMRouter:
    """API 키가 주입된 LLMRouter."""
    monkeypatch.setenv("KANANA_API_KEY", "test-kanana-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    return LLMRouter(config=minimal_config)


@pytest.fixture
def router_no_keys(minimal_config, monkeypatch) -> LLMRouter:
    """API 키 미설정 LLMRouter."""
    monkeypatch.delenv("KANANA_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return LLMRouter(config=minimal_config)


# ====================================================================== #
# 1. _BudgetTracker 단위 테스트
# ====================================================================== #


def test_budget_tracker_daily_limit_enforced() -> None:
    """총 daily_limit 초과 시 can_call() False 반환."""
    tracker = _BudgetTracker(daily_limit=3, allocation={})

    for i in range(3):
        ok, _ = tracker.can_call("caller_a")
        assert ok, f"{i}회차는 허용이어야 함"
        tracker.record("caller_a")

    ok, reason = tracker.can_call("caller_a")
    assert not ok
    assert "DAILY_LIMIT_REACHED" in reason
    assert "3/3" in reason


def test_router_requires_llm_budget_section() -> None:
    """LLM 운영 임계값은 기본값으로 부활하지 않고 config 누락 시 실패한다."""
    with pytest.raises(LLMRouterConfigError, match="llm_budget"):
        LLMRouter(config={})


def test_router_requires_circuit_breaker_and_sla(minimal_config: dict) -> None:
    """circuit/timeout 누락은 하드코딩 기본값 대신 fail-closed."""
    missing_circuit = {"llm_budget": dict(minimal_config["llm_budget"])}
    missing_circuit["llm_budget"].pop("circuit_breaker")
    with pytest.raises(LLMRouterConfigError, match="circuit_breaker"):
        LLMRouter(config=missing_circuit)

    missing_sla = {"llm_budget": dict(minimal_config["llm_budget"])}
    missing_sla["llm_budget"].pop("sla")
    with pytest.raises(LLMRouterConfigError, match="sla"):
        LLMRouter(config=missing_sla)


def test_router_requires_known_overflow_policy(minimal_config: dict) -> None:
    """fallback 대상은 gpt-4o 또는 none만 허용한다."""
    bad = {"llm_budget": dict(minimal_config["llm_budget"])}
    bad["llm_budget"]["overflow_to"] = "gpt-3"
    with pytest.raises(LLMRouterConfigError, match="overflow_to"):
        LLMRouter(config=bad)


def test_budget_tracker_caller_allocation_enforced() -> None:
    """caller별 allocation 초과 시 can_call() False 반환."""
    tracker = _BudgetTracker(daily_limit=100, allocation={"news_analysis": 2})

    for i in range(2):
        ok, _ = tracker.can_call("news_analysis")
        assert ok
        tracker.record("news_analysis")

    ok, reason = tracker.can_call("news_analysis")
    assert not ok
    assert "CALLER_QUOTA_EXCEEDED" in reason
    assert "news_analysis" in reason
    assert "2/2" in reason


def test_budget_tracker_caller_without_allocation_rejected_without_buffer() -> None:
    """allocation 미지정 caller는 buffer 정책이 없으면 차단."""
    tracker = _BudgetTracker(daily_limit=5, allocation={"a": 3})
    ok, reason = tracker.can_call("b_no_alloc")
    assert not ok
    assert reason == "CALLER_NOT_CONFIGURED: b_no_alloc"


def test_budget_tracker_caller_without_allocation_uses_buffer() -> None:
    """allocation 미지정 caller는 명시 buffer quota로만 허용."""
    tracker = _BudgetTracker(daily_limit=5, allocation={"a": 3, "buffer": 1})

    ok, reason = tracker.can_call("b_no_alloc")
    assert ok
    assert reason == "OK"
    tracker.record("b_no_alloc")

    ok, reason = tracker.can_call("another_no_alloc")
    assert not ok
    assert "CALLER_QUOTA_EXCEEDED: buffer 1/1" in reason


def test_budget_tracker_daily_reset_at_midnight(monkeypatch) -> None:
    """KST 날짜 변경 시 카운터 리셋."""
    from src.orchestration import llm_router as llm_mod
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # 오늘 날짜로 고정
    day1 = datetime(2026, 4, 21, 9, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    day2 = datetime(2026, 4, 22, 0, 1, 0, tzinfo=ZoneInfo("Asia/Seoul"))

    def fake_now_kst_day1():
        return day1

    def fake_now_kst_day2():
        return day2

    tracker = _BudgetTracker(daily_limit=3, allocation={})

    # day1: 3회 모두 소진
    monkeypatch.setattr(llm_mod, "now_kst", fake_now_kst_day1)
    for _ in range(3):
        ok, _ = tracker.can_call("c")
        assert ok
        tracker.record("c")

    ok, _ = tracker.can_call("c")
    assert not ok  # 한도 초과

    # day2: 자정 이후 → 리셋
    monkeypatch.setattr(llm_mod, "now_kst", fake_now_kst_day2)
    ok, reason = tracker.can_call("c")
    assert ok, f"자정 이후 리셋이어야 함, got: {reason}"
    assert tracker.remaining_total() == 3


# ====================================================================== #
# 2. _CircuitBreaker 단위 테스트
# ====================================================================== #


def test_circuit_breaker_opens_after_3_failures() -> None:
    """연속 3회 실패 시 OPEN 전이."""
    cb = _CircuitBreaker(failure_threshold=3, open_duration_sec=300)

    assert cb.state == CircuitState.CLOSED.value

    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED.value  # 아직 2회

    cb.record_failure()
    assert cb.state == CircuitState.OPEN.value


def test_circuit_breaker_half_open_after_5_min(monkeypatch) -> None:
    """open_duration_sec 경과 후 HALF_OPEN 전이."""
    cb = _CircuitBreaker(failure_threshold=3, open_duration_sec=300)

    # 강제 OPEN
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN.value

    # 시간 경과 전: 차단
    assert not cb.can_attempt()

    # open_duration_sec 이상 경과 시뮬레이션
    monkeypatch.setattr(time, "time", lambda: cb._opened_at + 301)  # type: ignore[union-attr]
    assert cb.can_attempt()
    assert cb.state == CircuitState.HALF_OPEN.value


def test_circuit_breaker_closes_on_success_in_half_open(monkeypatch) -> None:
    """HALF_OPEN에서 성공 시 CLOSED 복귀."""
    cb = _CircuitBreaker(failure_threshold=3, open_duration_sec=300)

    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    monkeypatch.setattr(time, "time", lambda: cb._opened_at + 301)  # type: ignore[union-attr]
    cb.can_attempt()  # HALF_OPEN 전이
    assert cb.state == CircuitState.HALF_OPEN.value

    cb.record_success()
    assert cb.state == CircuitState.CLOSED.value
    assert cb.failure_count == 0


def test_circuit_breaker_reopens_on_half_open_failure(monkeypatch) -> None:
    """HALF_OPEN에서 실패 시 즉시 OPEN 재차단."""
    cb = _CircuitBreaker(failure_threshold=3, open_duration_sec=300)

    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    monkeypatch.setattr(time, "time", lambda: cb._opened_at + 301)  # type: ignore[union-attr]
    cb.can_attempt()  # HALF_OPEN 전이
    assert cb.state == CircuitState.HALF_OPEN.value

    cb.record_failure()
    assert cb.state == CircuitState.OPEN.value


# ====================================================================== #
# 3. LLMRouter 동작 테스트
# ====================================================================== #


def test_hot_mode_raises(router_with_keys: LLMRouter) -> None:
    """mode='hot' → RuntimeError (불변 원칙 4)."""
    with pytest.raises(RuntimeError, match="HOT_PATH_LLM_FORBIDDEN"):
        router_with_keys.call("test prompt", mode="hot")


def test_mode_b_uses_gpt4o_directly(router_with_keys: LLMRouter) -> None:
    """mode='mode_b' → GPT-4o 직접 호출, Kanana 예산 소모 없음."""
    result = router_with_keys.call("test", mode="mode_b", caller="backtest_reasoning")

    assert result.success
    assert result.model_used == LLMModel.GPT_4O.value
    assert not result.fallback_used

    # Kanana 예산은 줄지 않아야 함
    assert router_with_keys.budget_remaining() == 10


def test_cold_mode_uses_kanana_first(router_with_keys: LLMRouter) -> None:
    """mode='cold' 기본 경로: Kanana-o 호출."""
    result = router_with_keys.call("test", mode="cold", caller="news_analysis")

    assert result.success
    assert result.model_used == LLMModel.KANANA_O.value
    assert not result.fallback_used
    assert result.circuit_state == CircuitState.CLOSED.value


def test_cold_mode_fallback_on_budget_exhausted(
    minimal_config: dict, monkeypatch
) -> None:
    """Kanana 예산 소진 후 GPT-4o fallback."""
    monkeypatch.setenv("KANANA_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    router = LLMRouter(config=minimal_config)

    # news_analysis allocation=5 소진
    for _ in range(5):
        r = router.call("p", mode="cold", caller="news_analysis")
        assert r.model_used == LLMModel.KANANA_O.value

    # 6번째: 예산 초과 → fallback
    result = router.call("p", mode="cold", caller="news_analysis")
    assert result.success
    assert result.model_used == LLMModel.GPT_4O.value
    assert result.fallback_used


def test_cold_mode_fallback_on_kanana_circuit_open(
    minimal_config: dict, monkeypatch
) -> None:
    """Kanana-o circuit OPEN 시 GPT-4o fallback."""
    monkeypatch.setenv("KANANA_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    router = LLMRouter(config=minimal_config)

    # Kanana circuit 강제 OPEN
    router._kanana_cb.record_failure()
    router._kanana_cb.record_failure()
    router._kanana_cb.record_failure()
    assert router.circuit_state("kanana-o") == CircuitState.OPEN.value

    result = router.call("p", mode="cold", caller="fda_cold_path")
    assert result.success
    assert result.model_used == LLMModel.GPT_4O.value
    assert result.fallback_used


def test_cold_mode_overflow_none_disables_fallback(
    minimal_config: dict, monkeypatch
) -> None:
    """overflow_to=none이면 예산 차단 시 GPT-4o를 호출하지 않고 실패한다."""
    monkeypatch.setenv("KANANA_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    minimal_config["llm_budget"]["overflow_to"] = "none"
    router = LLMRouter(config=minimal_config)

    for _ in range(5):
        assert router.call("p", mode="cold", caller="news_analysis").success

    result = router.call("p", mode="cold", caller="news_analysis")

    assert result.success is False
    assert result.model_used == LLMModel.KANANA_O.value
    assert result.fallback_used is False
    assert result.error.startswith("FALLBACK_DISABLED: CALLER_QUOTA_EXCEEDED")


def test_unknown_caller_rejected_without_buffer(minimal_config: dict, monkeypatch) -> None:
    """caller 오타는 암묵적으로 total budget을 쓰지 못한다."""
    monkeypatch.setenv("KANANA_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    router = LLMRouter(config=minimal_config)

    result = router.call("p", mode="cold", caller="news_anlaysis_typo")

    assert result.success is False
    assert result.error == "CALLER_NOT_CONFIGURED: news_anlaysis_typo"
    assert result.fallback_used is False
    assert router.budget_remaining() == 10


def test_missing_kanana_key_returns_failure(
    minimal_config: dict, monkeypatch
) -> None:
    """KANANA_API_KEY 미설정 시 graceful failure + GPT-4o fallback."""
    monkeypatch.delenv("KANANA_API_KEY", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ELEPHANT_ALLOW_LLM_MOCK", raising=False)
    minimal_config["llm_budget"]["allow_mock_provider"] = False
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    router = LLMRouter(config=minimal_config)

    result = router.call("p", mode="cold", caller="news_analysis")
    # 키 누락 → Kanana 실패 → GPT-4o fallback
    assert result.model_used == LLMModel.GPT_4O.value
    assert result.fallback_used


def test_missing_openai_key_returns_failure(
    minimal_config: dict, monkeypatch
) -> None:
    """OPENAI_API_KEY 미설정 + Kanana circuit OPEN: 최종 failure."""
    monkeypatch.setenv("KANANA_API_KEY", "k")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ELEPHANT_ALLOW_LLM_MOCK", raising=False)
    minimal_config["llm_budget"]["allow_mock_provider"] = False
    router = LLMRouter(config=minimal_config)

    # Kanana circuit OPEN → GPT-4o fallback 시도 → 키 없음
    router._kanana_cb.record_failure()
    router._kanana_cb.record_failure()
    router._kanana_cb.record_failure()

    result = router.call("p", mode="cold", caller="news_analysis")
    assert not result.success
    assert result.error == "OPENAI_API_KEY_MISSING"


def test_kanana_malformed_tokens_out_does_not_fail_call(
    minimal_config: dict,
    monkeypatch,
) -> None:
    """Provider가 tokens_out을 비정상 문자열로 줘도 호출 자체는 성공 처리한다."""
    import requests

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"content": "ok", "tokens_out": "many"}

    monkeypatch.setenv("KANANA_API_KEY", "k")
    monkeypatch.setenv("KANANA_API_URL", "https://kanana.example.invalid")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ELEPHANT_ALLOW_LLM_MOCK", raising=False)
    minimal_config["llm_budget"]["allow_mock_provider"] = False
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: _Response())
    router = LLMRouter(config=minimal_config)

    result = router.call("p", mode="cold", caller="news_analysis")

    assert result.success is True
    assert result.tokens_out is None
    assert result.content == "ok"


def test_budget_remaining_tracks_calls(router_with_keys: LLMRouter) -> None:
    """호출 시마다 budget_remaining() 감소 확인."""
    initial = router_with_keys.budget_remaining()
    assert initial == 10  # kanana_daily_limit=10

    router_with_keys.call("p", mode="cold", caller="news_analysis")
    assert router_with_keys.budget_remaining() == initial - 1

    router_with_keys.call("p", mode="cold", caller="news_analysis")
    assert router_with_keys.budget_remaining() == initial - 2

    # caller별 잔여량
    assert router_with_keys.budget_remaining("news_analysis") == 3  # 5 - 2


def test_structured_schema_passed_through(router_with_keys: LLMRouter) -> None:
    """structured_schema 인자가 오류 없이 처리됨."""
    schema = {
        "type": "object",
        "properties": {"signal": {"type": "string"}},
        "required": ["signal"],
    }
    result = router_with_keys.call(
        "분석 결과 반환", mode="cold", caller="fda_cold_path", structured_schema=schema
    )
    assert result.success
    assert result.model_used == LLMModel.KANANA_O.value


def test_circuit_state_query(router_with_keys: LLMRouter) -> None:
    """circuit_state() 정상 조회 + 잘못된 모델명 ValueError."""
    assert router_with_keys.circuit_state("kanana-o") == CircuitState.CLOSED.value
    assert router_with_keys.circuit_state("gpt-4o") == CircuitState.CLOSED.value

    with pytest.raises(ValueError, match="UNKNOWN_MODEL"):
        router_with_keys.circuit_state("unknown-model")


def test_mode_b_no_kanana_budget_consumed(router_with_keys: LLMRouter) -> None:
    """Mode B 호출이 Kanana 예산에 영향 없음."""
    before = router_with_keys.budget_remaining()

    for _ in range(5):
        router_with_keys.call("test", mode="mode_b", caller="backtest_reasoning")

    assert router_with_keys.budget_remaining() == before


def test_force_model_gpt4o_bypasses_kanana(
    minimal_config: dict, monkeypatch
) -> None:
    """force_model='gpt-4o' 지정 시 Kanana 건너뛰고 GPT-4o 직접 호출."""
    monkeypatch.setenv("KANANA_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    router = LLMRouter(config=minimal_config)

    before = router.budget_remaining()
    result = router.call(
        "test", mode="cold", caller="news_analysis", force_model="gpt-4o"
    )

    assert result.success
    assert result.model_used == LLMModel.GPT_4O.value
    assert not result.fallback_used  # 강제 지정이므로 fallback 아님
    # Kanana 예산 소모 없음
    assert router.budget_remaining() == before


def test_allow_mock_provider_without_api_keys(monkeypatch, minimal_config: dict) -> None:
    """명시적 mock provider는 API key 없이도 cold/mode_b smoke를 허용한다."""
    monkeypatch.delenv("KANANA_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("ELEPHANT_ALLOW_LLM_MOCK", "1")
    minimal_config["llm_budget"]["allow_mock_provider"] = True

    router = LLMRouter(config=minimal_config)
    cold = router.call("test", mode="cold", caller="news_analysis")
    mode_b = router.call("test", mode="mode_b", caller="backtest_reasoning")

    assert cold.success is True
    assert cold.model_used == LLMModel.KANANA_O.value
    assert mode_b.success is True
    assert mode_b.model_used == LLMModel.GPT_4O.value


def test_allow_mock_provider_string_false_does_not_enable_mock(
    monkeypatch,
    minimal_config: dict,
) -> None:
    """config 문자열 'false'는 mock provider enable로 해석하지 않는다."""
    monkeypatch.delenv("KANANA_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ELEPHANT_ALLOW_LLM_MOCK", raising=False)
    minimal_config["llm_budget"]["allow_mock_provider"] = "false"

    router = LLMRouter(config=minimal_config)
    result = router.call("test", mode="cold", caller="news_analysis")

    assert result.success is False
    assert result.error == "OPENAI_API_KEY_MISSING"


def test_llm_call_result_dataclass() -> None:
    """LLMCallResult 기본 필드 초기값 확인."""
    r = LLMCallResult(
        success=True,
        model_used="kanana-o",
        content="hello",
        latency_ms=12.3,
    )
    assert r.fallback_used is False
    assert r.circuit_state == "closed"
    assert r.error is None
    assert r.tokens_in is None
    assert r.cost_usd is None


# ====================================================================== #
# 4. Mode B caller 화이트리스트 테스트 (불변 원칙 3 강제, S2-2)
# ====================================================================== #


def _mock_env(monkeypatch) -> None:
    """API 키 환경변수 설정 헬퍼."""
    monkeypatch.setenv("KANANA_API_KEY", "dummy")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")


def test_mode_b_unauthorized_caller_raises(monkeypatch) -> None:
    """Mode B 허용 caller 아닌 에이전트 호출 시 raise (불변 원칙 3 강제)."""
    _mock_env(monkeypatch)
    config = {
        "llm_budget": {
            "kanana_daily_limit": 10,
            "overflow_to": "gpt-4o",
            "budget_allocation": {},
            "circuit_breaker": {"failure_threshold": 3, "open_duration_sec": 300},
            "sla": {"timeout_sec": 30.0},
            "allow_mock_provider": True,
            "mode_b_allowed_callers": [
                "backtest_reasoning",
                "factor_hypothesis",
                "factor_implementation",
                "factor_evaluation",
                "mode_b_scheduler",
            ],
        }
    }
    router = LLMRouter(config=config)
    with pytest.raises(RuntimeError, match="MODE_B_CALLER_FORBIDDEN"):
        router.call("test", mode="mode_b", caller="news_analysis")


def test_mode_b_authorized_caller_passes(monkeypatch) -> None:
    """Mode B 허용 caller는 GPT-4o 성공 반환."""
    _mock_env(monkeypatch)
    config = {
        "llm_budget": {
            "kanana_daily_limit": 10,
            "overflow_to": "gpt-4o",
            "budget_allocation": {},
            "circuit_breaker": {"failure_threshold": 3, "open_duration_sec": 300},
            "sla": {"timeout_sec": 30.0},
            "allow_mock_provider": True,
            "mode_b_allowed_callers": [
                "backtest_reasoning",
                "factor_hypothesis",
                "factor_implementation",
                "factor_evaluation",
                "mode_b_scheduler",
            ],
        }
    }
    router = LLMRouter(config=config)
    result = router.call("test", mode="mode_b", caller="backtest_reasoning")
    assert result.success
    assert result.model_used == LLMModel.GPT_4O.value

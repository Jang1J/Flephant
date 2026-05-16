"""S1-4 FDA Hot Path unit tests."""
from __future__ import annotations

import pytest

from src.agents import fda as fda_module
from src.agents.fda import (
    FDAConfigError,
    FDAAgent,
    IllegalDeltaModificationError,
    MissingReasonCodeError,
)

_VALID_REASON_CODES = [
    "NEWS_DIVERGENCE",
    "RISK_FAST_TRIGGER",
    "DEBATE_CONFLICT",
    "NORMAL_APPROVE",
    "TIMEOUT",
    "QUANT_ANOMALY",
    "MISSING_PORTFOLIO_PATCH",
]


@pytest.fixture
def fda() -> FDAAgent:
    return FDAAgent()


def _valid_fda_config() -> dict[str, dict]:
    return {
        "debate": {"uncertainty_threshold": 0.7},
        "fda_uncertainty_link": {
            "uncertainty_threshold": 0.5,
            "veto_prior_boost": 0.15,
        },
        "reason_code_catalog": {
            "status": "final",
            "candidates": list(_VALID_REASON_CODES),
        },
    }


def _patch_fda_config(monkeypatch: pytest.MonkeyPatch, config: dict[str, dict]) -> None:
    def fake_config_load(config_file: str = "risk_config.yaml", key: str | None = None):
        assert config_file == "risk_config.yaml"
        if key is None:
            return config
        if key not in config:
            raise KeyError(key)
        return config[key]

    monkeypatch.setattr(fda_module, "config_load", fake_config_load)


# ====================================================================== #
# 1. 불변 원칙 2
# ====================================================================== #


def test_can_change_weight_false(fda: FDAAgent) -> None:
    """Hard constraint: FDA는 weight 수정 불가."""
    assert fda.CAN_CHANGE_WEIGHT is False


def test_allowed_publish_channels(fda: FDAAgent) -> None:
    assert fda.ALLOWED_PUBLISH_CHANNELS == frozenset({"final_decision"})


# ====================================================================== #
# 1-A. Config fail-closed
# ====================================================================== #


def test_config_missing_debate_section_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _valid_fda_config()
    del config["debate"]
    _patch_fda_config(monkeypatch, config)

    with pytest.raises(FDAConfigError, match="fail-closed"):
        FDAAgent()


def test_config_malformed_threshold_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _valid_fda_config()
    config["fda_uncertainty_link"]["uncertainty_threshold"] = "0.5"
    _patch_fda_config(monkeypatch, config)

    with pytest.raises(FDAConfigError, match="fail-closed"):
        FDAAgent()


def test_config_missing_reason_code_candidate_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _valid_fda_config()
    config["reason_code_catalog"]["candidates"].remove("QUANT_ANOMALY")
    _patch_fda_config(monkeypatch, config)

    with pytest.raises(FDAConfigError, match="QUANT_ANOMALY"):
        FDAAgent()


# ====================================================================== #
# 2. Approve path
# ====================================================================== #


def test_decide_approve_clean_state(fda: FDAAgent) -> None:
    result = fda.decide(
        portfolio_patch_ref="PP-20260420-001",
        target_weights={"005930": 0.1},
        order_deltas=[{"ticker": "005930", "side": "buy", "qty": 10, "reason": "rebalance"}],
        dependency_status={"news": "done", "risk": "done", "quant": "done", "debate": "skipped"},
    )
    fd = result["final_decision"]
    assert fd["approved"] is True
    assert fd["reason_code"] == "NORMAL_APPROVE"
    assert fd["veto_reason"] is None
    assert fd["decision_id"].startswith("DEC-")
    assert result["mode"] == "hot"
    assert result["latency_ms"] > 0


def test_decide_approve_echoes_weights_readonly(fda: FDAAgent) -> None:
    tw = {"005930": 0.10, "000660": 0.05}
    od = [{"ticker": "005930", "side": "buy", "qty": 10, "reason": "rebalance"}]
    result = fda.decide(
        portfolio_patch_ref="PP-001",
        target_weights=tw,
        order_deltas=od,
    )
    # echo는 복사본
    echo_tw = result["final_decision"]["target_weights"]
    echo_od = result["final_decision"]["order_deltas"]
    assert echo_tw == tw
    assert echo_od == od
    # 원본 dict 아닌 복사본
    assert echo_tw is not tw


# ====================================================================== #
# 3. Veto paths
# ====================================================================== #


def test_veto_missing_portfolio_patch(fda: FDAAgent) -> None:
    result = fda.decide(portfolio_patch_ref=None)
    fd = result["final_decision"]
    assert fd["approved"] is False
    assert fd["reason_code"] == "MISSING_PORTFOLIO_PATCH"


def test_veto_dependency_timeout(fda: FDAAgent) -> None:
    result = fda.decide(
        portfolio_patch_ref="PP-001",
        dependency_status={"news": "done", "risk": "timeout", "quant": "done"},
    )
    fd = result["final_decision"]
    assert fd["approved"] is False
    assert fd["reason_code"] == "TIMEOUT"
    assert "risk" in fd["veto_reason"]
    assert len(fd["risk_overrides"]) == 1


def test_veto_risk_high_severity(fda: FDAAgent) -> None:
    result = fda.decide(
        portfolio_patch_ref="PP-001",
        risk_warnings=[{"ticker": "005930", "severity": "high", "reason": "spike"}],
    )
    fd = result["final_decision"]
    assert fd["approved"] is False
    assert fd["reason_code"] == "RISK_FAST_TRIGGER"


def test_veto_quant_anomaly(fda: FDAAgent) -> None:
    result = fda.decide(
        portfolio_patch_ref="PP-001",
        anomalies=[{"ticker": "005930", "anomaly_type": "intraday_drop", "z_score": -4.5}],
    )
    fd = result["final_decision"]
    assert fd["approved"] is False
    assert fd["reason_code"] == "QUANT_ANOMALY"


def test_veto_priority_order_timeout_before_risk(fda: FDAAgent) -> None:
    """Timeout이 risk보다 우선 체크."""
    result = fda.decide(
        portfolio_patch_ref="PP-001",
        dependency_status={"quant": "timeout"},
        risk_warnings=[{"ticker": "005930", "severity": "high"}],
    )
    assert result["final_decision"]["reason_code"] == "TIMEOUT"


# ====================================================================== #
# 4. Hot Path <10ms SLA
# ====================================================================== #


def test_hot_path_latency_under_10ms(fda: FDAAgent) -> None:
    """비LLM 규칙 기반이므로 Hot Path SLA 10ms 이내."""
    for _ in range(20):
        result = fda.decide(
            portfolio_patch_ref="PP-001",
            target_weights={f"00{i:04d}": 0.05 for i in range(10)},
            order_deltas=[
                {"ticker": f"00{i:04d}", "side": "buy", "qty": 10, "reason": "rebalance"}
                for i in range(10)
            ],
            dependency_status={"news": "done", "risk": "done", "quant": "done", "debate": "done"},
        )
        assert result["latency_ms"] < 10.0, f"latency={result['latency_ms']}ms"


# ====================================================================== #
# 5. Cold mode not implemented
# ====================================================================== #


def test_cold_mode_without_llm_router_fail_closed(fda: FDAAgent) -> None:
    """Cold Path router 미주입은 approve fallback 없이 veto로 닫는다."""
    result = fda.decide(portfolio_patch_ref="PP-001", mode="cold")
    # Cold Path 결과는 final_decision 포함 딕셔너리
    assert "final_decision" in result
    assert result["final_decision"]["approved"] is False
    assert result["final_decision"]["reason_code"] == "NEWS_DIVERGENCE"


def test_cold_llm_failure_fail_closed() -> None:
    """Cold Path LLM 실패는 approve fallback이 아니라 veto로 닫는다."""
    class FailedRouter:
        def call(self, *args, **kwargs):
            return type(
                "Result",
                (),
                {
                    "success": False,
                    "content": "",
                    "error": "timeout",
                },
            )()

    fda = FDAAgent(llm_router=FailedRouter())
    result = fda.decide(
        portfolio_patch_ref="PP-001",
        mode="cold",
        risk_warnings=[],
        debate_result={"conflict_detected": False},
        agent_signals=[],
    )
    fd = result["final_decision"]
    assert fd["approved"] is False
    assert fd["reason_code"] == "NEWS_DIVERGENCE"


def test_cold_llm_invalid_reason_code_fail_closed() -> None:
    """LLM이 승인과 invalid reason_code를 같이 보내도 approve하지 않는다."""
    class InvalidReasonRouter:
        def call(self, *args, **kwargs):
            return type(
                "Result",
                (),
                {
                    "success": True,
                    "content": (
                        '{"approved": true, "reason_code": "BAD_CODE", '
                        '"veto_reason": null, "confidence": 0.9}'
                    ),
                    "error": None,
                },
            )()

    fda = FDAAgent(llm_router=InvalidReasonRouter())
    result = fda.decide(
        portfolio_patch_ref="PP-001",
        mode="cold",
        risk_warnings=[],
        debate_result={"conflict_detected": False},
        agent_signals=[],
    )
    fd = result["final_decision"]
    assert fd["approved"] is False
    assert fd["reason_code"] == "NEWS_DIVERGENCE"


# ====================================================================== #
# 6. reason_code 필수
# ====================================================================== #


def test_reason_code_always_set_on_approve(fda: FDAAgent) -> None:
    result = fda.decide(portfolio_patch_ref="PP-001")
    assert result["final_decision"]["reason_code"] is not None
    assert len(result["final_decision"]["reason_code"]) > 0


def test_reason_code_always_set_on_veto(fda: FDAAgent) -> None:
    result = fda.decide(portfolio_patch_ref=None)
    assert result["final_decision"]["reason_code"] is not None


def test_invalid_reason_code_internal_raises(fda: FDAAgent) -> None:
    """_finalize_decision에 잘못된 reason_code 직접 넘기면 에러."""
    with pytest.raises(MissingReasonCodeError):
        fda._finalize_decision(
            approved=True,
            reason_code="INVALID_CODE",
            veto_reason=None,
            target_weights={},
            order_deltas=[],
            portfolio_patch_ref="PP-001",
            t0=0.0,
        )


# ====================================================================== #
# 7. Readonly enforcement
# ====================================================================== #


def test_readonly_assert_non_dict_tw_raises(fda: FDAAgent) -> None:
    with pytest.raises(IllegalDeltaModificationError):
        fda._assert_readonly("not_a_dict", [])


def test_readonly_assert_non_list_od_raises(fda: FDAAgent) -> None:
    with pytest.raises(IllegalDeltaModificationError):
        fda._assert_readonly({}, "not_a_list")


# ====================================================================== #
# 8. report() API
# ====================================================================== #


def test_report_final_decision(fda: FDAAgent) -> None:
    r = fda.report("final_decision", {"decision_id": "DEC-001", "approved": True})
    assert r["report_type"] == "final_decision"
    assert r["agent"] == "FDAAgent"


def test_report_invalid_type_raises(fda: FDAAgent) -> None:
    with pytest.raises(ValueError, match="invalid report_type"):
        fda.report("news_signal", {})   # FDA publishes 아님

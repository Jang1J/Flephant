"""C9 FDADecisionContract contract tests."""
from __future__ import annotations


def test_c09_reason_code_required():
    """C9: reason_code_required=true. reason_code 없는 판단은 거부."""
    from src.agents.fda import FDAAgent, MissingReasonCodeError

    fda = FDAAgent()
    t0 = 0.0
    try:
        fda._finalize_decision(  # noqa: SLF001 - contract-level negative check
            approved=True,
            reason_code="",
            veto_reason=None,
            target_weights={},
            order_deltas=[],
            portfolio_patch_ref="PP-TEST",
            t0=t0,
        )
    except MissingReasonCodeError:
        return
    raise AssertionError("C9 reason_code_required=true 위반: 빈 reason_code가 통과함")


def test_c09_cannot_change_weight():
    """불변 원칙 2 enforcement: FDAAgent.CAN_CHANGE_WEIGHT == False.

    FDA는 비중을 직접 수정할 수 없다. approve/veto 판정만.
    """
    from src.agents.fda import FDAAgent

    assert FDAAgent.CAN_CHANGE_WEIGHT is False, (
        "불변 원칙 2 위반: FDA가 weight를 직접 변경할 수 있음"
    )


def test_c09_approve_veto_only():
    """C9: action은 approved=true/false만. order_deltas는 PM 소유, FDA는 read-only."""
    from src.agents.fda import FDAAgent

    fda = FDAAgent()
    order_deltas = [{"ticker": "005930", "side": "buy", "qty": 1, "reason": "rebalance"}]
    approved = fda.decide(
        portfolio_patch_ref="PP-TEST",
        target_weights={"005930": 0.10},
        order_deltas=order_deltas,
        active_reports=["MSG-20260516-ABCDEF12"],
    )["final_decision"]
    vetoed = fda.decide(portfolio_patch_ref=None)["final_decision"]

    expected_keys = {
        "decision_id",
        "approved",
        "target_weights",
        "order_deltas",
        "veto_reason",
        "reason_code",
        "risk_overrides",
        "confidence",
        "expiry",
        "portfolio_patch_ref",
        "active_reports",
    }
    assert set(approved) == expected_keys
    assert isinstance(approved["approved"], bool)
    assert isinstance(vetoed["approved"], bool)
    assert approved["approved"] is True
    assert vetoed["approved"] is False
    assert approved["order_deltas"] == order_deltas

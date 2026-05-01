"""C9 FDADecisionContract contract test skeleton. S0-1 body=pass + 불변 원칙 2 실제 검증."""
from __future__ import annotations



def test_c09_reason_code_required():
    """C9: reason_code_required=true. reason_code 없는 판단은 거부."""
    pass


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
    pass

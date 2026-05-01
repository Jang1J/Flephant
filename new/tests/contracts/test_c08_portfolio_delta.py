"""C8 PortfolioDeltaPlannerContract contract test skeleton. S0-1 body=pass + 불변 원칙 2 실제 검증."""
from __future__ import annotations



def test_c08_order_deltas_generated():
    """C8: target_weights → order_deltas 생성."""
    pass


def test_c08_fda_cannot_edit():
    """불변 원칙 2 enforcement: PortfolioManager.can_fda_edit == False.

    FDA는 approve/veto만. order_deltas 수정 불가.
    """
    from src.portfolio.portfolio_manager import PortfolioManager

    assert PortfolioManager.can_fda_edit is False, (
        "불변 원칙 2 위반: FDA가 PortfolioManager.order_deltas를 수정할 수 있음"
    )

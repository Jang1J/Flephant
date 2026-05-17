"""C8 PortfolioDeltaPlannerContract contract tests."""
from __future__ import annotations


def test_c08_order_deltas_generated():
    """C8: target_weights → order_deltas 생성."""
    from src.portfolio.portfolio_manager import PortfolioManager

    pm = PortfolioManager()
    result = pm.plan(
        target_weights={"005930": 0.10},
        current_positions=[],
        latest_prices={"005930": 50_000.0},
        portfolio_value=1_000_000.0,
        based_on_ts="2026-05-16T09:01:00+09:00",
    )

    patch = result["portfolio_patch"]
    assert {"portfolio_patch_id", "based_on_ts", "target_weights", "order_deltas"} <= set(patch)
    assert patch["target_weights"] == {"005930": 0.10}
    assert len(patch["order_deltas"]) == 1
    order = patch["order_deltas"][0]
    assert {"ticker", "side", "qty", "reason"} <= set(order)
    assert order["ticker"] == "005930"
    assert order["side"] == "buy"
    assert order["qty"] == 2
    assert order["reason"] == "rebalance"


def test_c08_fda_cannot_edit():
    """불변 원칙 2 enforcement: PortfolioManager.can_fda_edit == False.

    FDA는 approve/veto만. order_deltas 수정 불가.
    """
    from src.portfolio.portfolio_manager import PortfolioManager

    assert PortfolioManager.can_fda_edit is False, (
        "불변 원칙 2 위반: FDA가 PortfolioManager.order_deltas를 수정할 수 있음"
    )

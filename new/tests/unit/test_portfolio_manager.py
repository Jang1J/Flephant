"""S1-3 Portfolio Manager unit tests."""
from __future__ import annotations

import pytest

from src.portfolio.portfolio_manager import PortfolioManager


@pytest.fixture
def pm() -> PortfolioManager:
    return PortfolioManager()


# ====================================================================== #
# 1. 초기화 + 불변 원칙 2
# ====================================================================== #


def test_can_fda_edit_false(pm: PortfolioManager) -> None:
    """불변 원칙 2: FDA는 PM 결과 수정 못 한다."""
    assert pm.can_fda_edit is False


def test_init_loads_config(pm: PortfolioManager) -> None:
    assert pm._max_names == 10
    assert pm._max_single_name == pytest.approx(0.20)
    assert pm._daily_turnover_max == pytest.approx(0.30)
    assert pm._respect_ppo_weights is True


# ====================================================================== #
# 2. Empty input
# ====================================================================== #


def test_plan_empty_target_no_positions(pm: PortfolioManager) -> None:
    result = pm.plan(
        target_weights={},
        current_positions=[],
        latest_prices={},
        portfolio_value=1_000_000.0,
        based_on_ts="2026-04-20T10:00:00+09:00",
    )
    assert result["n_orders"] == 0
    assert result["portfolio_patch"]["order_deltas"] == []
    assert result["portfolio_patch"]["portfolio_patch_id"].startswith("PP-")


def test_plan_all_exits(pm: PortfolioManager) -> None:
    """보유 전부 exit: target=empty, current=전체 포지션 → sell deltas."""
    result = pm.plan(
        target_weights={},
        current_positions=[
            {"ticker": "005930", "qty": 10, "weight": 0.2},
            {"ticker": "000660", "qty": 20, "weight": 0.3},
        ],
        latest_prices={"005930": 70000.0, "000660": 50000.0},
        portfolio_value=10_000_000.0,
    )
    assert result["n_orders"] >= 1
    # 전부 sell + reason=exit
    for od in result["portfolio_patch"]["order_deltas"]:
        assert od["side"] == "sell"
        assert od["reason"] in ("exit", "risk_reduce")


# ====================================================================== #
# 3. Happy path (rebalance)
# ====================================================================== #


def test_plan_rebalance_new_positions(pm: PortfolioManager) -> None:
    result = pm.plan(
        target_weights={"005930": 0.15, "000660": 0.10},
        current_positions=[],
        latest_prices={"005930": 70000.0, "000660": 50000.0},
        portfolio_value=10_000_000.0,
    )
    assert result["n_orders"] == 2
    # 전부 buy + rebalance
    for od in result["portfolio_patch"]["order_deltas"]:
        assert od["side"] == "buy"
        assert od["reason"] == "rebalance"
        assert od["qty"] > 0


def test_plan_order_qty_calculation(pm: PortfolioManager) -> None:
    """qty = delta_weight × portfolio_value / price 계산."""
    result = pm.plan(
        target_weights={"005930": 0.10},
        current_positions=[],
        latest_prices={"005930": 50000.0},
        portfolio_value=10_000_000.0,
    )
    od = result["portfolio_patch"]["order_deltas"][0]
    # delta_weight=0.10, value=10M × 0.10 = 1M, qty=1M/50K=20
    assert od["qty"] == 20
    assert od["side"] == "buy"


# ====================================================================== #
# 4. Turnover cap
# ====================================================================== #


def test_turnover_under_cap(pm: PortfolioManager) -> None:
    # turnover=0.10 (target 0.10 신규 진입)
    result = pm.plan(
        target_weights={"005930": 0.10},
        current_positions=[],
        latest_prices={"005930": 50000.0},
        portfolio_value=10_000_000.0,
    )
    assert result["turnover_exceeded"] is False
    assert result["scale_factor"] == pytest.approx(1.0)


def test_turnover_over_cap_scales_down(pm: PortfolioManager) -> None:
    # 5 종목 × 0.2 신규 = turnover 1.0 → cap 0.30 → scale_factor ~0.3
    result = pm.plan(
        target_weights={
            "005930": 0.20, "000660": 0.20, "035420": 0.20,
            "051910": 0.20, "005380": 0.20,
        },
        current_positions=[],
        latest_prices={
            "005930": 50000.0, "000660": 50000.0, "035420": 50000.0,
            "051910": 50000.0, "005380": 50000.0,
        },
        portfolio_value=10_000_000.0,
    )
    assert result["turnover_exceeded"] is True
    assert result["scale_factor"] < 1.0
    assert result["scale_factor"] > 0.0
    # 실제 주문 qty는 축소된 delta 기준
    for od in result["portfolio_patch"]["order_deltas"]:
        # scale 0.3 × 0.20 × 10M / 50K = 12주 (원래 40주)
        assert od["qty"] < 40


# ====================================================================== #
# 5. Error 처리
# ====================================================================== #


def test_price_unavailable_error(pm: PortfolioManager) -> None:
    result = pm.plan(
        target_weights={"005930": 0.10},
        current_positions=[],
        latest_prices={},  # 가격 없음
        portfolio_value=1_000_000.0,
    )
    assert result["n_errors"] >= 1
    assert any(e["error"] == "PRICE_UNAVAILABLE" for e in result["errors"])
    assert result["n_orders"] == 0


def test_lot_size_error_tiny_weight(pm: PortfolioManager) -> None:
    """너무 작은 delta → qty=0 → LOT_SIZE_ERROR."""
    result = pm.plan(
        target_weights={"005930": 0.000001},  # 1 microweight
        current_positions=[],
        latest_prices={"005930": 50000.0},
        portfolio_value=1_000_000.0,
    )
    assert result["n_errors"] >= 1
    assert any(e["error"] == "LOT_SIZE_ERROR" for e in result["errors"])


def test_zero_price_treated_as_unavailable(pm: PortfolioManager) -> None:
    result = pm.plan(
        target_weights={"005930": 0.10},
        current_positions=[],
        latest_prices={"005930": 0.0},
        portfolio_value=1_000_000.0,
    )
    assert result["n_errors"] >= 1
    assert any(e["error"] == "PRICE_UNAVAILABLE" for e in result["errors"])


# ====================================================================== #
# 6. Cold path exit 처리
# ====================================================================== #


def test_cold_path_exit_sets_risk_reduce_reason(pm: PortfolioManager) -> None:
    result = pm.plan(
        target_weights={},
        current_positions=[{"ticker": "005930", "qty": 10, "weight": 0.2}],
        latest_prices={"005930": 70000.0},
        portfolio_value=10_000_000.0,
        cold_path_exits=["005930"],
    )
    od = result["portfolio_patch"]["order_deltas"][0]
    assert od["reason"] == "risk_reduce"
    assert od["side"] == "sell"


def test_cold_path_exit_overrides_positive_target_to_zero(pm: PortfolioManager) -> None:
    """Cold Path exit 명령은 PPO target이 남아 있어도 실제 청산 delta로 변환한다."""
    result = pm.plan(
        target_weights={"005930": 0.10},
        current_positions=[{"ticker": "005930", "qty": 8, "weight": 0.2}],
        latest_prices={"005930": 50000.0},
        portfolio_value=10_000_000.0,
        cold_path_exits=["005930"],
    )

    od = result["portfolio_patch"]["order_deltas"][0]
    assert result["portfolio_patch"]["target_weights"]["005930"] == 0.0
    assert result["cold_path_exit_overrides"] == ["005930"]
    assert od["side"] == "sell"
    assert od["reason"] == "risk_reduce"


def test_sell_qty_is_capped_to_current_position_qty(pm: PortfolioManager) -> None:
    """PM이 계산한 sell 수량이 보유 수량보다 크면 broker 제출 전 보유 수량으로 제한한다."""
    result = pm.plan(
        target_weights={},
        current_positions=[{"ticker": "005930", "qty": 10, "weight": 0.2}],
        latest_prices={"005930": 50000.0},
        portfolio_value=100_000_000.0,
    )

    od = result["portfolio_patch"]["order_deltas"][0]
    assert od["side"] == "sell"
    assert od["qty"] == 10
    assert result["sell_caps_applied"] == [{
        "ticker": "005930",
        "requested_qty": 400,
        "capped_qty": 10,
    }]


def test_negative_target_weight_does_not_create_unheld_sell(pm: PortfolioManager) -> None:
    """malformed PPO 음수 weight는 미보유 종목 sell 주문으로 변환하지 않는다."""
    result = pm.plan(
        target_weights={"005930": -0.10},
        current_positions=[],
        latest_prices={"005930": 50000.0},
        portfolio_value=10_000_000.0,
    )

    assert result["portfolio_patch"]["order_deltas"] == []
    assert result["portfolio_patch"]["target_weights"]["005930"] == 0.0
    assert result["errors"][0]["error"] == "NEGATIVE_TARGET_WEIGHT"
    assert result["ppo_violations"][0]["type"] == "negative_target_weight"


def test_malformed_position_qty_is_fail_safe(monkeypatch) -> None:
    """외부/BE 포지션 qty가 깨져도 예외 대신 sell 불가 오류로 닫는다."""
    monkeypatch.setattr(
        "src.portfolio.portfolio_manager.config_load",
        lambda _file, section: {
            "position_limits": {
                "max_names": 10,
                "max_single_name": 0.2,
                "max_sector": 0.4,
                "min_cash": 0.1,
            },
            "turnover_cap": {"daily_max": 1.0},
            "portfolio_manager": {"respect_ppo_weights": "false"},
        }[section],
    )
    pm = PortfolioManager()

    result = pm.plan(
        target_weights={},
        current_positions=[{"ticker": "005930", "qty": "abc", "weight": 0.1}],
        latest_prices={"005930": 50000.0},
        portfolio_value=10_000_000.0,
    )

    assert result["respect_ppo_weights"] is False
    assert result["portfolio_patch"]["order_deltas"] == []
    assert result["errors"][0]["error"] == "SELL_QTY_UNAVAILABLE"


# ====================================================================== #
# 7. Output schema (C8)
# ====================================================================== #


def test_output_schema_c8_fields(pm: PortfolioManager) -> None:
    result = pm.plan(
        target_weights={"005930": 0.10},
        current_positions=[],
        latest_prices={"005930": 50000.0},
        portfolio_value=10_000_000.0,
        based_on_ts="2026-04-20T10:00:00+09:00",
    )
    patch = result["portfolio_patch"]
    assert "portfolio_patch_id" in patch
    assert "based_on_ts" in patch
    assert "target_weights" in patch
    assert "order_deltas" in patch
    assert patch["based_on_ts"] == "2026-04-20T10:00:00+09:00"


def test_target_weights_echo_readonly(pm: PortfolioManager) -> None:
    """C8: target_weights는 read-only echo. PM이 덮어쓰지 않음."""
    tw = {"005930": 0.10, "000660": 0.05}
    result = pm.plan(
        target_weights=tw,
        current_positions=[],
        latest_prices={"005930": 50000.0, "000660": 40000.0},
        portfolio_value=10_000_000.0,
    )
    assert result["portfolio_patch"]["target_weights"] == tw


def test_target_weights_not_clipped_when_ppo_weight_exceeds_limit(pm: PortfolioManager) -> None:
    """C8: PM은 PPO weight를 clip하지 않고 violation만 보고한다."""
    tw = {"005930": 0.25}
    result = pm.plan(
        target_weights=tw,
        current_positions=[],
        latest_prices={"005930": 50000.0},
        portfolio_value=10_000_000.0,
    )
    assert result["respect_ppo_weights"] is True
    assert result["portfolio_patch"]["target_weights"] == tw
    assert any(
        v["type"] == "max_single_name_exceeded"
        and v["ticker"] == "005930"
        and v["weight"] == pytest.approx(0.25)
        for v in result["ppo_violations"]
    )


def test_order_delta_fields(pm: PortfolioManager) -> None:
    result = pm.plan(
        target_weights={"005930": 0.10},
        current_positions=[],
        latest_prices={"005930": 50000.0},
        portfolio_value=10_000_000.0,
    )
    od = result["portfolio_patch"]["order_deltas"][0]
    assert "ticker" in od
    assert od["side"] in ("buy", "sell")
    assert "qty" in od and od["qty"] > 0
    assert od["reason"] in ("rebalance", "exit", "risk_reduce", "cash_raise")


# ====================================================================== #
# 8. Turnover 계산
# ====================================================================== #


def test_compute_turnover_basic() -> None:
    current = {"005930": 0.2, "000660": 0.1}
    target = {"005930": 0.1, "035420": 0.15}
    # delta: 005930 -0.1, 000660 -0.1, 035420 +0.15 → abs sum=0.35 / 2 = 0.175
    t = PortfolioManager._compute_turnover(current, target)
    assert t == pytest.approx(0.175, abs=1e-6)


def test_compute_turnover_zero_no_change() -> None:
    w = {"005930": 0.15}
    t = PortfolioManager._compute_turnover(w, w)
    assert t == 0.0

"""S1-3 Portfolio Manager (C8 PortfolioDeltaPlannerContract).

불변 원칙 2 enforcement:
  - PortfolioManager.can_fda_edit = False
  - PortfolioManager가 유일한 order_deltas 생성자
  - FDA는 approve/veto만, 이 클래스 output을 수정 못 함

책임:
  target_weights (PPO Allocator 출력) + current_positions + latest_prices
  → order_deltas (buy/sell qty) + turnover 검증

모든 수치는 risk_config.yaml 경유 (position_limits, turnover_cap).
"""
from __future__ import annotations

import math
from typing import Any

from src.utils.config_loader import load as config_load
from src.utils.id_factory import generate_portfolio_patch_id
from src.utils.logger import get_logger
from src.utils.ticker_utils import pad_ticker

logger = get_logger("portfolio_manager")


class PriceUnavailableError(ValueError):
    """C8 error: PRICE_UNAVAILABLE."""


class LotSizeError(ValueError):
    """C8 error: LOT_SIZE_ERROR. qty=0 또는 계산 불가."""


class NegativeQtyError(ValueError):
    """C8 error: NEGATIVE_QTY. 내부 계산 버그 방어."""


class TurnoverCapExceededError(ValueError):
    """turnover_cap.daily_max 초과. 내부 정책."""


class PortfolioManager:
    """C8 PortfolioDeltaPlannerContract 구현. target_weights → order_deltas.

    불변 원칙 2: can_fda_edit = False. FDA가 이 class의 instance를 수정할 수 없다.
    generate order_deltas is source-of-truth for execution (api_contracts.md C8 rules).
    """

    # 불변 원칙 2 enforcement. Sprint 0 contract test가 이 값을 assert.
    can_fda_edit: bool = False

    def __init__(self) -> None:
        pos_cfg = config_load("risk_config.yaml", "position_limits")
        turnover_cfg = config_load("risk_config.yaml", "turnover_cap")

        self._max_names: int = int(pos_cfg["max_names"])
        self._max_single_name: float = float(pos_cfg["max_single_name"])
        self._max_sector: float = float(pos_cfg.get("max_sector", 0.40))
        self._min_cash: float = float(pos_cfg["min_cash"])

        self._daily_turnover_max: float = float(turnover_cfg["daily_max"])

        logger.info(
            "[portfolio_manager] 초기화: max_names=%d, max_single=%.2f, "
            "max_sector=%.2f, min_cash=%.2f, turnover_max=%.2f",
            self._max_names, self._max_single_name,
            self._max_sector, self._min_cash, self._daily_turnover_max,
        )

    # ================================================================== #
    # Public API
    # ================================================================== #

    def plan(
        self,
        target_weights: dict[str, float],
        current_positions: list[dict[str, Any]] | None = None,
        latest_prices: dict[str, float] | None = None,
        portfolio_value: float = 0.0,
        based_on_ts: str = "",
        cold_path_exits: list[str] | None = None,
    ) -> dict[str, Any]:
        """C8 portfolio_patch 생성.

        Args:
            target_weights: PPO Allocator 출력 {ticker: weight}
            current_positions: [{ticker, qty, weight}]. None이면 빈 포지션
            latest_prices: {ticker: price}. qty 계산 필수 (있는 티커만)
            portfolio_value: 전체 포트폴리오 평가액 (KRW 등). qty = delta_weight × value / price
            based_on_ts: signal 생성 시각 ISO8601
            cold_path_exits: Cold Path에서 exit 트리거된 종목 (quant_anomaly/risk_veto)

        Returns: C8 output + metadata (turnover, turnover_exceeded, errors)
        """
        target_weights_norm = {
            pad_ticker(str(t)): float(w)
            for t, w in (target_weights or {}).items()
        }
        current_positions = current_positions or []
        latest_prices = latest_prices or {}
        latest_prices_norm = {
            pad_ticker(str(t)): float(p) for t, p in latest_prices.items()
        }
        cold_path_exits_set = {pad_ticker(str(t)) for t in (cold_path_exits or [])}

        # max_names 초과 시 하위 종목 제거
        if len(target_weights_norm) > self._max_names:
            sorted_by_weight = sorted(
                target_weights_norm.items(), key=lambda x: x[1], reverse=True
            )
            keep = dict(sorted_by_weight[: self._max_names])
            removed = len(target_weights_norm) - len(keep)
            logger.info(
                "[portfolio_manager] max_names(%d) 초과 %d종목 제거",
                self._max_names, removed,
            )
            target_weights_norm = keep

        # max_single_name clip
        clipped = False
        for ticker in list(target_weights_norm.keys()):
            if target_weights_norm[ticker] > self._max_single_name:
                target_weights_norm[ticker] = self._max_single_name
                clipped = True
        if clipped:
            logger.info(
                "[portfolio_manager] max_single_name(%.2f) clip 적용",
                self._max_single_name,
            )
            # clip 후 재정규화
            total = sum(target_weights_norm.values())
            if total > 1e-12:
                target_weights_norm = {
                    t: w / total for t, w in target_weights_norm.items()
                }

        current_weights = {
            pad_ticker(str(p["ticker"])): float(p.get("weight", 0.0))
            for p in current_positions
            if "ticker" in p
        }

        # Turnover 산출
        turnover = self._compute_turnover(current_weights, target_weights_norm)

        # Turnover cap 적용 (초과 시 delta 비례 축소)
        scale_factor = 1.0
        turnover_exceeded = False
        if turnover > self._daily_turnover_max and self._daily_turnover_max > 1e-12:
            scale_factor = float(self._daily_turnover_max / turnover)
            turnover_exceeded = True
            logger.warning(
                "[portfolio_manager] turnover=%.4f > cap=%.2f. scale=%.4f",
                turnover, self._daily_turnover_max, scale_factor,
            )

        # order_deltas 생성
        all_tickers = sorted(
            set(current_weights.keys()) | set(target_weights_norm.keys())
        )
        order_deltas: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for ticker in all_tickers:
            target_w = target_weights_norm.get(ticker, 0.0)
            current_w = current_weights.get(ticker, 0.0)
            delta_w = (target_w - current_w) * scale_factor

            if abs(delta_w) < 1e-9:
                continue

            price = latest_prices_norm.get(ticker)
            if price is None or price <= 0:
                errors.append({
                    "ticker": ticker,
                    "error": "PRICE_UNAVAILABLE",
                    "price": price,
                })
                continue

            delta_value = delta_w * float(portfolio_value)
            qty_float = abs(delta_value) / price
            qty = int(math.floor(qty_float))

            if qty <= 0:
                errors.append({
                    "ticker": ticker,
                    "error": "LOT_SIZE_ERROR",
                    "delta_weight": delta_w,
                    "price": price,
                })
                continue

            side = "buy" if delta_w > 0 else "sell"

            if ticker in cold_path_exits_set:
                reason = "risk_reduce"
            elif target_w == 0.0 and current_w > 0.0:
                reason = "exit"
            else:
                reason = "rebalance"

            order_deltas.append({
                "ticker": ticker,
                "side": side,
                "qty": qty,
                "reason": reason,
                "delta_weight": delta_w,
                "price": price,
            })

        # NEGATIVE_QTY 방어 (내부 버그)
        for od in order_deltas:
            if od["qty"] < 0:
                raise NegativeQtyError(
                    f"[portfolio_manager] NEGATIVE_QTY 내부 버그: {od}"
                )

        pp_id = generate_portfolio_patch_id()
        return {
            "portfolio_patch": {
                "portfolio_patch_id": pp_id,
                "based_on_ts": based_on_ts,
                "target_weights": dict(target_weights_norm),
                "order_deltas": order_deltas,
            },
            "turnover": turnover,
            "turnover_exceeded": turnover_exceeded,
            "scale_factor": scale_factor,
            "n_orders": len(order_deltas),
            "errors": errors,
            "n_errors": len(errors),
            "constraints_applied": {
                "max_names": self._max_names,
                "max_single_name": self._max_single_name,
                "max_sector": self._max_sector,
                "min_cash": self._min_cash,
            },
        }

    # ================================================================== #
    # Internal
    # ================================================================== #

    @staticmethod
    def _compute_turnover(
        current_weights: dict[str, float],
        target_weights: dict[str, float],
    ) -> float:
        """sum(|Δw|) / 2. 양방향 delta 합의 절반."""
        all_tickers = set(current_weights.keys()) | set(target_weights.keys())
        abs_delta = sum(
            abs(target_weights.get(t, 0.0) - current_weights.get(t, 0.0))
            for t in all_tickers
        )
        return float(abs_delta / 2.0)

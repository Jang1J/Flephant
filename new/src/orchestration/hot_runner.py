"""S1-5 Hot Path 1분 루프 오케스트레이터. S2-8: RiskFast sidecar 연결.

실행 순서 (매 1분):
  bar_arrived → QuantAgent(on_bar × N) → score_cross_section + detect_anomalies
             → PPOAllocator.allocate
             → PortfolioManager.plan
             → RiskFastAgent.evaluate (sidecar, <50ms, 비LLM)
             → FDAAgent.decide (risk_fast_eval optional 전달)

전체 루프 <100ms SLA. 동기 LLM 호출 금지 (불변 원칙 4).
RiskFast는 Hot Path 주 코어를 block하지 않는 순차 sidecar.

PipelineState 전이 관리: BOOTSTRAP → HOT_RUNNING → (장 마감) MODE_B_IDLE.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import numpy as np

from src.agents.fda import FDAAgent
from src.agents.hot.quant import QuantAgent
from src.agents.hot.risk_fast import RiskFastAgent
from src.models.ppo_allocator import PPOAllocator
from src.ops.state_machine import PipelineState, StateMachine
from src.portfolio.portfolio_manager import PortfolioManager
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger

logger = get_logger("hot_runner")


class HotRunner:
    """Hot Path 1분 루프 오케스트레이터.

    의존: QuantAgent + PPOAllocator + PortfolioManager + FDAAgent.
    전이 관리: PipelineState (BOOTSTRAP → HOT_RUNNING → MODE_B_IDLE).
    """

    def __init__(
        self,
        quant: QuantAgent | None = None,
        ppo: PPOAllocator | None = None,
        pm: PortfolioManager | None = None,
        fda: FDAAgent | None = None,
        state_machine: StateMachine | None = None,
        risk_fast: RiskFastAgent | None = None,
    ) -> None:
        self._quant = quant or QuantAgent()
        self._ppo = ppo or PPOAllocator()
        self._pm = pm or PortfolioManager()
        self._fda = fda or FDAAgent()
        self._sm = state_machine or StateMachine()
        self._risk_fast = risk_fast or RiskFastAgent()

        # SLA 임계값 yaml 경유 (불변 원칙 5)
        qa_cfg = config_load("risk_config.yaml", "quant_agent")
        self._sla_ms: float = float(qa_cfg["latency_p95_target_ms"])

        self._latency_records: list[float] = []
        logger.info(
            "[hot_runner] 초기화. 상태=%s, quant_model=%s, sla_ms=%.1f",
            self._sm.state.value, self._quant.has_model, self._sla_ms,
        )

    # ================================================================== #
    # Lifecycle
    # ================================================================== #

    @property
    def state(self) -> PipelineState:
        return self._sm.state

    def start(self) -> None:
        """BOOTSTRAP → HOT_RUNNING 전이. 장 시작 시 호출."""
        self._sm.transition(PipelineState.HOT_RUNNING)

    def stop_for_mode_b(self) -> None:
        """HOT_RUNNING → MODE_B_IDLE 전이. 15:30 장 마감 시."""
        self._sm.transition(PipelineState.MODE_B_IDLE)

    def shutdown(self) -> None:
        """현재 상태에서 SHUTDOWN."""
        self._sm.transition(PipelineState.SHUTDOWN)

    # ================================================================== #
    # Per-minute loop
    # ================================================================== #

    def run_once(
        self,
        tickers: list[str],
        bars_batch: list[dict[str, Any]],
        current_positions: list[dict[str, Any]] | None = None,
        latest_prices: dict[str, float] | None = None,
        portfolio_value: float = 0.0,
        market_state: dict[str, Any] | None = None,
        risk_warnings: list[dict[str, Any]] | None = None,
        dependency_status: dict[str, str] | None = None,
        asof: str = "",
        recent_bars: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        """매 1분 1회 오케스트레이션.

        실행 순서: QuantAgent → PPOAllocator → PortfolioManager
                   → RiskFastAgent(sidecar) → FDAAgent → ExecutionGateway

        Args:
            tickers: universe (20 종목)
            bars_batch: 이번 분에 수신한 1분봉 list (ticker별로 여러개 가능)
            current_positions: [{ticker, qty, weight}]
            latest_prices: {ticker: price}
            portfolio_value: KRW 총 평가액
            market_state: {regime_state, cash_ratio, sector_exposure}
            risk_warnings: Cold Path에서 publish된 [{ticker, severity, ...}]
            dependency_status: {news, risk, quant, debate: done|skipped|timeout}
            asof: 루프 기준 시각 ISO8601
            recent_bars: {ticker: [bar_dict, ...]} — RiskFast sidecar 규칙 평가용.
                         None이면 규칙 1~3 skip (risk_level=low).

        Returns: 전체 단계 결과 통합 dict.
        """
        if self._sm.state != PipelineState.HOT_RUNNING:
            logger.warning(
                "[hot_runner] run_once 호출되었으나 상태=%s (HOT_RUNNING 아님). skip",
                self._sm.state.value,
            )
            return {
                "pipeline_state": self._sm.state.value,
                "skipped": True,
                "reason": "not_hot_running",
            }

        t0 = time.perf_counter()

        # 1. on_bar 호출 (BarBuffer 저장, 경량)
        n_bars_consumed = 0
        bar_errors: list[str] = []
        for bar in bars_batch:
            try:
                self._quant.on_bar(bar)
                n_bars_consumed += 1
            except ValueError as e:
                # BarBuffer 필수 필드 누락 등 → 기록하고 루프 계속
                bar_errors.append(str(e))

        # 2. QuantAgent: score + anomaly
        quant_output = self._quant.score_cross_section(tickers, asof)
        anomalies = self._quant.detect_anomalies(tickers, asof)

        # 3. PPOAllocator
        allocation = self._ppo.allocate(
            quant_output=quant_output,
            current_positions=current_positions,
            market_state=market_state,
        )
        target_weights = allocation["allocation_plan"]["target_weights"]

        # 4. PortfolioManager (anomaly tickers = cold_path_exits)
        cold_path_exits = [a["ticker"] for a in anomalies]
        pm_result = self._pm.plan(
            target_weights=target_weights,
            current_positions=current_positions or [],
            latest_prices=latest_prices or {},
            portfolio_value=portfolio_value,
            based_on_ts=asof,
            cold_path_exits=cold_path_exits,
        )
        portfolio_patch = pm_result["portfolio_patch"]

        # 5. RiskFast sidecar (PM 이후, FDA 이전)
        ts_dt = datetime.now(tz=timezone.utc)
        try:
            ts_dt = datetime.fromisoformat(asof).replace(tzinfo=timezone.utc) if asof else ts_dt
        except ValueError:
            pass  # asof 파싱 실패 시 now() 사용

        risk_eval = self._risk_fast.evaluate(
            snapshot={
                "ranking": quant_output.get("scores", {}),
                "portfolio_patch": portfolio_patch,
                "recent_bars": recent_bars,
            },
            ts=ts_dt,
        )
        if risk_eval["risk_level"] == "critical":
            logger.warning(
                "[hot_runner] RiskFast CRITICAL: rules=%s, tickers=%s",
                risk_eval["triggered_rules"],
                risk_eval["affected_tickers"],
            )
        elif risk_eval["risk_level"] == "high":
            logger.warning(
                "[hot_runner] RiskFast HIGH: rules=%s, tickers=%s",
                risk_eval["triggered_rules"],
                risk_eval["affected_tickers"],
            )

        # 6. FDA Hot Path (risk_fast_eval 전달 — weight 수정 금지, reason_code 결정에만 활용)
        fda_result = self._fda.decide(
            portfolio_patch_ref=portfolio_patch["portfolio_patch_id"],
            target_weights=target_weights,
            order_deltas=portfolio_patch["order_deltas"],
            dependency_status=dependency_status or {
                "news": "done", "risk": "done", "quant": "done", "debate": "skipped"
            },
            anomalies=anomalies,
            risk_warnings=risk_warnings or [],
            mode="hot",
            risk_fast_eval=risk_eval,
        )

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self._latency_records.append(elapsed_ms)

        if elapsed_ms > self._sla_ms:
            logger.warning(
                "[hot_runner] loop 레이턴시 %.2fms > %.1fms SLA (asof=%s, tickers=%d)",
                elapsed_ms, self._sla_ms, asof, len(tickers),
            )

        return {
            "pipeline_state": self._sm.state.value,
            "asof": asof,
            "n_bars_consumed": n_bars_consumed,
            "bar_errors": bar_errors,
            "quant_output": quant_output,
            "anomalies": anomalies,
            "allocation": allocation,
            "pm_result": pm_result,
            "risk_eval": risk_eval,
            "fda_result": fda_result,
            "final_decision": fda_result["final_decision"],
            "latency_ms": elapsed_ms,
        }

    def latency_stats(self) -> dict[str, float]:
        """최근 run_once 레이턴시 통계."""
        if not self._latency_records:
            return {"count": 0, "avg": 0.0, "max": 0.0, "min": 0.0}
        arr = np.asarray(self._latency_records, dtype=float)
        return {
            "count": int(arr.size),
            "avg": float(arr.mean()),
            "max": float(arr.max()),
            "min": float(arr.min()),
            "p95": float(np.percentile(arr, 95)),
        }

"""S1-5 Hot Path 1분 루프 오케스트레이터. S2-8: RiskFast sidecar 연결. S4-8: 부트스트랩 메모리 복원.

실행 순서 (매 1분):
  bar_arrived → QuantAgent(on_bar × N) → score_cross_section + detect_anomalies
             → PPOAllocator.allocate
             → PortfolioManager.plan
             → RiskFastAgent.evaluate (sidecar, <50ms, 비LLM)
             → FDAAgent.decide (risk_fast_eval optional 전달)

전체 루프 <100ms SLA. 동기 LLM 호출 금지 (불변 원칙 4).
RiskFast는 Hot Path 주 코어를 block하지 않는 순차 sidecar.

PipelineState 전이 관리: BOOTSTRAP → HOT_RUNNING → (장 마감) MODE_B_IDLE.

S4-8 부트스트랩 메모리 복원:
  bootstrap(agents) 호출 시 AgentMemoryRestorer.restore_all(agents) 실행.
  KnowledgeBase에서 window_days 이내 메모리를 각 에이전트에 inject.
  start() 전에 bootstrap(agents)를 호출해야 restore가 보장됨.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from src.agents.fda import FDAAgent
from src.agents.hot.quant import QuantAgent
from src.agents.hot.risk_fast import RiskFastAgent
from src.agents.memory_restorer import AgentMemoryRestorer
from src.knowledge.kb import KnowledgeBase
from src.models.ppo_allocator import PPOAllocator
from src.ops.profiler import HotPathProfiler
from src.ops.state_machine import PipelineState, StateMachine
from src.portfolio.portfolio_manager import PortfolioManager
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger

logger = get_logger("hot_runner")
_KST = ZoneInfo("Asia/Seoul")


def _parse_hot_ts(value: Any) -> datetime:
    """Hot Path timestamp parser. naive 값은 KST로 간주."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_KST)
    return dt.astimezone(timezone.utc)


class HotRunner:
    """Hot Path 1분 루프 오케스트레이터.

    의존: QuantAgent + PPOAllocator + PortfolioManager + FDAAgent.
    전이 관리: PipelineState (BOOTSTRAP → HOT_RUNNING → MODE_B_IDLE).
    S4-8: bootstrap(agents) → AgentMemoryRestorer.restore_all → start() 순서.
    """

    def __init__(
        self,
        quant: QuantAgent | None = None,
        ppo: PPOAllocator | None = None,
        pm: PortfolioManager | None = None,
        fda: FDAAgent | None = None,
        state_machine: StateMachine | None = None,
        risk_fast: RiskFastAgent | None = None,
        kb: KnowledgeBase | None = None,
        profiler: HotPathProfiler | None = None,
    ) -> None:
        self._quant = quant or QuantAgent()
        self._ppo = ppo or PPOAllocator()
        self._pm = pm or PortfolioManager()
        self._fda = fda or FDAAgent()
        self._sm = state_machine or StateMachine()
        self._risk_fast = risk_fast or RiskFastAgent()

        # S4-8: KnowledgeBase + AgentMemoryRestorer (BOOTSTRAP 단계용)
        self._kb = kb or KnowledgeBase()
        _mem_cfg = config_load("risk_config.yaml", "agent_memory") or {}
        self._memory_restorer = AgentMemoryRestorer(self._kb, _mem_cfg)
        self._bootstrap_restore_counts: dict[str, int] = {}

        # SLA 임계값 yaml 경유 (불변 원칙 5)
        qa_cfg = config_load("risk_config.yaml", "quant_agent")
        self._sla_ms: float = float(qa_cfg["latency_p95_target_ms"])

        # S4-4 stage-level profiler (공유 또는 전용 인스턴스)
        self._profiler: HotPathProfiler = profiler or HotPathProfiler()

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

    @property
    def profiler(self) -> HotPathProfiler:
        """S4-4 단계별 프로파일러. 외부에서 percentiles/report/check_sla 접근."""
        return self._profiler

    def bootstrap(self, agents: dict[str, Any] | None = None) -> dict[str, int]:
        """BOOTSTRAP 단계. KB → 에이전트 메모리 복원.

        start() 전에 호출해야 함. BOOTSTRAP 상태에서만 실행.
        HOT_RUNNING 중 호출 시 경고 후 skip.

        Args:
            agents: {agent_name: agent_instance} dict.
                    None이면 내부 핵심 에이전트로 구성 (fda, quant 등).
                    Cold Path 에이전트(news, debate, risk_slow)는 외부에서 주입 필요.

        Returns:
            {agent_name: 복원된 항목 수} dict.
        """
        if self._sm.state != PipelineState.BOOTSTRAP:
            logger.warning(
                "[hot_runner] bootstrap은 BOOTSTRAP 상태에서만 호출 가능. "
                "현재 상태=%s. skip",
                self._sm.state.value,
            )
            return {}

        effective_agents: dict[str, Any] = {}
        if agents:
            effective_agents.update(agents)

        # 내부 에이전트 자동 포함 (외부 주입이 없는 경우)
        if "fda" not in effective_agents:
            effective_agents["fda"] = self._fda

        logger.info("[hot_runner] BOOTSTRAP: AgentMemoryRestorer.restore_all 시작")
        counts = self._memory_restorer.restore_all(effective_agents)
        self._bootstrap_restore_counts = counts

        for agent_name, count in counts.items():
            logger.info(
                "[hot_runner] BOOTSTRAP 복원 완료: agent=%s count=%d",
                agent_name, count,
            )

        total = sum(counts.values())
        logger.info("[hot_runner] BOOTSTRAP 완료. 총 %d건 복원", total)
        return counts

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
        asof_dt: datetime | None = None
        if asof:
            try:
                asof_dt = _parse_hot_ts(asof)
            except (TypeError, ValueError):
                asof_dt = None

        # 1. on_bar 호출 (BarBuffer 저장, 경량)
        n_bars_consumed = 0
        bar_errors: list[str] = []
        for bar in bars_batch:
            try:
                if asof_dt is not None and isinstance(bar, dict) and bar.get("ts_close"):
                    bar_ts = _parse_hot_ts(bar["ts_close"])
                    if bar_ts > asof_dt:
                        bar_errors.append(
                            "future_bar_rejected: "
                            f"ticker={bar.get('ticker', '')} "
                            f"ts_close={bar.get('ts_close')} asof={asof}"
                        )
                        continue
                self._quant.on_bar(bar)
                n_bars_consumed += 1
            except ValueError as e:
                # BarBuffer 필수 필드 누락 등 → 기록하고 루프 계속
                bar_errors.append(str(e))

        # 2. QuantAgent: score + anomaly (S4-4 stage timer)
        t_quant = self._profiler.start_stage("quant")
        quant_output = self._quant.score_cross_section(tickers, asof)
        anomalies = self._quant.detect_anomalies(tickers, asof)
        quant_ms = self._profiler.end_stage("quant", t_quant)

        # 3. PPOAllocator (S4-4 stage timer)
        t_ppo = self._profiler.start_stage("ppo")
        allocation = self._ppo.allocate(
            quant_output=quant_output,
            current_positions=current_positions,
            market_state=market_state,
        )
        target_weights = allocation["allocation_plan"]["target_weights"]
        ppo_ms = self._profiler.end_stage("ppo", t_ppo)

        # 4. PortfolioManager (anomaly tickers = cold_path_exits, S4-4 stage timer)
        t_pm = self._profiler.start_stage("pm")
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
        pm_guard_warnings = self._portfolio_manager_guard_warnings(pm_result)
        pm_ms = self._profiler.end_stage("pm", t_pm)

        # 5. RiskFast sidecar (PM 이후, FDA 이전, S4-4 stage timer)
        ts_dt = asof_dt or datetime.now(tz=timezone.utc)

        t_rf = self._profiler.start_stage("risk_fast")
        risk_eval = self._risk_fast.evaluate(
            snapshot={
                "ranking": quant_output.get("scores", {}),
                "portfolio_patch": portfolio_patch,
                "recent_bars": recent_bars,
            },
            ts=ts_dt,
        )
        risk_fast_ms = self._profiler.end_stage("risk_fast", t_rf)

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

        # 6. FDA Hot Path (risk_fast_eval 전달, S4-4 stage timer)
        combined_risk_warnings = list(risk_warnings or [])
        combined_risk_warnings.extend(pm_guard_warnings)
        t_fda = self._profiler.start_stage("fda")
        fda_result = self._fda.decide(
            portfolio_patch_ref=portfolio_patch["portfolio_patch_id"],
            target_weights=portfolio_patch["target_weights"],
            order_deltas=portfolio_patch["order_deltas"],
            dependency_status=dependency_status or {
                "news": "done", "risk": "done", "quant": "done", "debate": "skipped"
            },
            anomalies=anomalies,
            risk_warnings=combined_risk_warnings,
            mode="hot",
            risk_fast_eval=risk_eval,
        )
        fda_ms = self._profiler.end_stage("fda", t_fda)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self._profiler.end_stage("hot_loop", t0)
        self._latency_records.append(elapsed_ms)

        # tick 메타 기록 (S4-4 리포트 트레이스)
        self._profiler.record_tick(
            n_tickers=len(tickers),
            ts=asof,
            stage_ms={
                "quant": quant_ms,
                "ppo": ppo_ms,
                "pm": pm_ms,
                "risk_fast": risk_fast_ms,
                "fda": fda_ms,
                "hot_loop": elapsed_ms,
            },
        )

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
            "pm_guard_warnings": pm_guard_warnings,
            "risk_eval": risk_eval,
            "fda_result": fda_result,
            "final_decision": fda_result["final_decision"],
            "latency_ms": elapsed_ms,
            "stage_ms": {
                "quant": quant_ms,
                "ppo": ppo_ms,
                "pm": pm_ms,
                "risk_fast": risk_fast_ms,
                "fda": fda_ms,
            },
        }

    @staticmethod
    def _portfolio_manager_guard_warnings(
        pm_result: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Convert PM planning failures into FDA veto warnings."""
        warnings: list[dict[str, Any]] = []
        errors = list(pm_result.get("errors") or [])
        if int(pm_result.get("n_errors") or len(errors)) > 0 or errors:
            tickers = sorted({
                str(item.get("ticker", "")).strip()
                for item in errors
                if str(item.get("ticker", "")).strip()
            })
            warnings.append({
                "ticker": ",".join(tickers),
                "severity": "high",
                "reason": "portfolio_manager_errors",
                "details": errors,
            })

        ppo_violations = list(pm_result.get("ppo_violations") or [])
        if ppo_violations:
            tickers = sorted({
                str(item.get("ticker", "")).strip()
                for item in ppo_violations
                if str(item.get("ticker", "")).strip()
            })
            warnings.append({
                "ticker": ",".join(tickers),
                "severity": "high",
                "reason": "ppo_constraint_violation",
                "details": ppo_violations,
            })
        return warnings

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

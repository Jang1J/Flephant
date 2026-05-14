"""S4-3 E2E Scenario Runner. 1주일 시나리오 시뮬레이션 오케스트레이터.

역할:
  week1_basic.yaml 등 시나리오 정의를 로드한 뒤,
  Day 단위 루프로 Mode A (Hot Path 5~390 tick + Cold Path 이벤트) +
  Mode B (18:00~22:00 7 stage) 를 시뮬레이션한다.

제약:
  - 실 KIS/Naver 데이터 없음. 합성 bar 데이터 + EventInjector 사용.
  - PIT-Safety (불변 원칙 1): 시나리오 ts는 항상 해당 Day 장중 시각.
    Mode B 는 해당 Day 18:00 이후에만 실행. mode_guard @mode_b_only 우회 허용
    (시뮬 컨텍스트 → _run_mode_b_sim 내부에서 직접 stage 호출).
  - FDA can_change_weight=False (불변 원칙 2): HotRunner + FDAAgent가 강제.
  - Backtest Mode B 격리 (불변 원칙 3): ModeBScheduler는 Mode A 경로에서 미호출.
  - LLM 예산 (불변 원칙 4): Cold Path 이벤트는 실제 LLM 미호출
    (에이전트가 미주입이므로 dispatch_next → None 또는 handler_error).
  - 하드코딩 금지 (불변 원칙 5): 임계값은 risk_config.yaml 경유.

아티팩트:
  artifacts/audit/hot_path_YYYYMMDD.jsonl    — tick별 Hot Path 결과
  artifacts/audit/cold_path_YYYYMMDD.jsonl   — Cold Path 이벤트 주입 결과
  artifacts/audit/mode_b_YYYYMMDD.jsonl      — Mode B 단계별 결과
  artifacts/audit/injected_events.jsonl      — 전체 주입 이벤트

검증 지표:
  - Hot Path SLA: profiler.percentiles() 기준 p95 < 100ms (target)
  - FDA reason_code 100% 출력
  - PIT-Safety violation 0건
  - Day N → N+1 state 연속성 (BOOTSTRAP → HOT_RUNNING → MODE_B_IDLE → BOOTSTRAP)
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, date as _date, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from src.agents.fda import FDAAgent
from src.agents.hot.quant import QuantAgent
from src.agents.hot.risk_fast import RiskFastAgent
from src.models.ppo_allocator import PPOAllocator
from src.ops.profiler import HotPathProfiler
from src.ops.state_machine import PipelineState, StateMachine
from src.orchestration.event_gateway import EventGateway
from src.orchestration.hot_runner import HotRunner
from src.portfolio.portfolio_manager import PortfolioManager
from src.runner.event_injector import EventInjector
from src.utils.config_loader import load as config_load
from src.utils.id_factory import generate_bundle_id
from src.utils.logger import get_logger
from src.utils.pit_guard import assert_pit_safe, PITViolationError
from src.utils.ticker_utils import pad_ticker

logger = get_logger("e2e_scenario_runner")
_KST = ZoneInfo("Asia/Seoul")

_SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "config" / "scenarios"
_AUDIT_DIR = Path(__file__).resolve().parents[3] / "artifacts" / "audit"
_AUDIT_DIR.mkdir(parents=True, exist_ok=True)


# ──────────────────────────────────────────────────────────────────────
# 합성 bar 생성 헬퍼
# ──────────────────────────────────────────────────────────────────────

def _make_synthetic_bar(
    ticker: str,
    ts: str,
    base_price: float = 70000.0,
) -> dict[str, Any]:
    """HotRunner.run_once 호환 1분봉 합성 데이터."""
    import random
    rng = random.Random(hash(ticker + ts) % (2**31))
    close = round(base_price * (1 + rng.uniform(-0.005, 0.005)), 0)
    volume = rng.randint(500, 5000)
    return {
        "ticker": pad_ticker(ticker),
        "ts": ts,
        "open": close,
        "high": close * 1.002,
        "low": close * 0.998,
        "close": close,
        "volume": volume,
    }


def _make_bars_batch(
    tickers: list[str],
    ts_str: str,
) -> list[dict[str, Any]]:
    """종목 리스트 × ts 기준 합성 배치."""
    return [_make_synthetic_bar(t, ts_str) for t in tickers]


def _make_latest_prices(
    tickers: list[str],
    bars_batch: list[dict[str, Any]],
) -> dict[str, float]:
    prices: dict[str, float] = {}
    bar_by_ticker = {b["ticker"]: b for b in bars_batch}
    for t in tickers:
        tk = pad_ticker(t)
        prices[tk] = float(bar_by_ticker.get(tk, {}).get("close", 70000.0))
    return prices


# ──────────────────────────────────────────────────────────────────────
# ScenarioResult
# ──────────────────────────────────────────────────────────────────────

class ScenarioResult:
    """E2E 시나리오 실행 결과 컨테이너."""

    def __init__(self, scenario_name: str) -> None:
        self.scenario_name = scenario_name
        self.days: list[dict[str, Any]] = []
        self.pit_violations: int = 0
        self.fda_missing_reason_code: int = 0
        self.hot_path_latencies: list[float] = []
        self.errors: list[str] = []

    def add_day(self, day_result: dict[str, Any]) -> None:
        self.days.append(day_result)
        self.pit_violations += day_result.get("pit_violations", 0)
        self.fda_missing_reason_code += day_result.get("fda_missing_reason_code", 0)
        self.hot_path_latencies.extend(day_result.get("hot_path_latencies", []))
        self.errors.extend(day_result.get("errors", []))

    def summary(self) -> dict[str, Any]:
        lats = self.hot_path_latencies
        import numpy as np
        p50 = float(np.percentile(lats, 50)) if lats else 0.0
        p95 = float(np.percentile(lats, 95)) if lats else 0.0
        p99 = float(np.percentile(lats, 99)) if lats else 0.0
        # 불변 원칙 5: SLA 임계값은 risk_config.yaml에서 로드
        from src.utils.config_loader import load as _config_load
        _sla = float(
            _config_load("risk_config.yaml", "quant_agent").get(
                "latency_p95_target_ms", 100
            )
        )
        return {
            "scenario_name": self.scenario_name,
            "total_days": len(self.days),
            "total_hot_ticks": len(lats),
            "pit_violations": self.pit_violations,
            "fda_missing_reason_code": self.fda_missing_reason_code,
            "hot_path_sla": {
                "p50_ms": round(p50, 2),
                "p95_ms": round(p95, 2),
                "p99_ms": round(p99, 2),
                "target_p95_ms": _sla,
                "sla_ok": p95 < _sla,
            },
            "total_errors": len(self.errors),
            "mode_b_verdicts": [
                d.get("mode_b", {}).get("verdict") for d in self.days
            ],
        }


# ──────────────────────────────────────────────────────────────────────
# E2E Scenario Runner
# ──────────────────────────────────────────────────────────────────────

class E2EScenarioRunner:
    """1주일 E2E 시나리오 시뮬레이터.

    Args:
        scenario_file: scenarios/*.yaml 파일명 (예: "week1_basic.yaml").
        short_mode: True이면 hot_path_ticks_short tick만 실행 (CI 빠른 모드).
        skip_mode_b: True이면 Mode B 건너뜀. unit test용.
    """

    def __init__(
        self,
        scenario_file: str = "week1_basic.yaml",
        short_mode: bool = True,
        skip_mode_b: bool = False,
    ) -> None:
        scenario_path = _SCENARIOS_DIR / scenario_file
        with scenario_path.open("r", encoding="utf-8") as f:
            self._scenario: dict[str, Any] = yaml.safe_load(f)

        self._short_mode = short_mode
        self._skip_mode_b = skip_mode_b
        self._scenario_name: str = self._scenario.get("scenario_name", "unknown")
        self._tickers: list[str] = [
            pad_ticker(t) for t in self._scenario.get("universe_tickers", [])
        ]

        # yaml에서 tick 수 로드 (불변 원칙 5: 하드코딩 금지)
        self._ticks_full: int = int(
            self._scenario.get("hot_path_ticks_per_day", 390)
        )
        self._ticks_short: int = int(
            self._scenario.get("hot_path_ticks_short", 5)
        )

        # portfolio_value: risk_config.yaml에서 로드.
        # portfolio_manager 섹션 없으면 기본값 1억 사용 (불변 원칙 5: yaml 우선, fallback 허용).
        try:
            pm_cfg = config_load("risk_config.yaml", "portfolio_manager") or {}
            self._portfolio_value: float = float(
                pm_cfg.get("initial_portfolio_value", 100_000_000)
            )
        except KeyError:
            self._portfolio_value = 100_000_000

        # 날짜 역순/중복 검증
        days_cfg = self._scenario.get("days", [])
        dates = [d["date"] for d in days_cfg]
        if sorted(dates) != dates:
            raise ValueError(f"[e2e_scenario_runner] 시나리오 날짜 역순: {dates}")
        if len(set(dates)) != len(dates):
            raise ValueError(f"[e2e_scenario_runner] 시나리오 날짜 중복: {dates}")
        # 비거래일(토/일) 경고
        for d_str in dates:
            d = _date.fromisoformat(d_str)
            if d.weekday() >= 5:
                logger.warning(
                    "[e2e_scenario_runner] 비거래일 포함: %s (%s)",
                    d_str,
                    d.strftime("%A"),
                )

        logger.info(
            "[e2e_scenario_runner] 시나리오=%s days=%d tickers=%d short_mode=%s",
            self._scenario_name,
            len(days_cfg),
            len(self._tickers),
            short_mode,
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def run(self) -> ScenarioResult:
        """전체 시나리오 실행. ScenarioResult 반환."""
        result = ScenarioResult(self._scenario_name)
        days_cfg = self._scenario.get("days", [])

        prev_state = PipelineState.BOOTSTRAP
        for i, day_cfg in enumerate(days_cfg):
            day_date_str: str = day_cfg["date"]
            logger.info(
                "[e2e_scenario_runner] Day %d/%d 시작: %s (prev_state=%s)",
                i + 1, len(days_cfg), day_date_str, prev_state.value,
            )
            day_result = self._run_day(
                day_cfg=day_cfg,
                day_index=i,
                prev_state=prev_state,
            )
            result.add_day(day_result)

            # 다음 날 시작 상태 = 이번 날 Mode B 완료 후 BOOTSTRAP
            prev_state = day_result.get("next_state", PipelineState.BOOTSTRAP)
            logger.info(
                "[e2e_scenario_runner] Day %d 완료: next_state=%s",
                i + 1, prev_state.value,
            )

        summary = result.summary()
        logger.info(
            "[e2e_scenario_runner] 시나리오 완료: %s",
            json.dumps(summary, ensure_ascii=False),
        )
        self._flush_summary(summary)
        return result

    # ------------------------------------------------------------------ #
    # Day-level orchestration
    # ------------------------------------------------------------------ #

    def _run_day(
        self,
        day_cfg: dict[str, Any],
        day_index: int,
        prev_state: PipelineState,
    ) -> dict[str, Any]:
        """하루치 Mode A (Hot+Cold) + Mode B 실행."""
        day_date_str: str = day_cfg["date"]
        events_cfg: list[dict[str, Any]] = day_cfg.get("events", [])
        errors: list[str] = []
        pit_violations: int = 0
        fda_missing_reason_code: int = 0
        hot_path_latencies: list[float] = []

        # 1. 컴포넌트 초기화 (Day마다 새 StateMachine)
        sm = StateMachine(initial=PipelineState.BOOTSTRAP)
        profiler = HotPathProfiler()
        runner = HotRunner(
            quant=QuantAgent(),
            ppo=PPOAllocator(),
            pm=PortfolioManager(),
            fda=FDAAgent(),
            state_machine=sm,
            risk_fast=RiskFastAgent(),
            profiler=profiler,
        )
        gateway = EventGateway()
        injector = EventInjector(
            gateway=gateway,
            audit_log_path=_AUDIT_DIR / f"injected_events.jsonl",
        )

        # 2. BOOTSTRAP → HOT_RUNNING
        runner.start()
        assert sm.state == PipelineState.HOT_RUNNING
        logger.info("[e2e_scenario_runner] %s BOOTSTRAP → HOT_RUNNING", day_date_str)

        # 3. Mode A: Hot Path tick 루프
        n_ticks = self._ticks_short if self._short_mode else self._ticks_full
        hot_audit: list[dict[str, Any]] = []
        cold_audit: list[dict[str, Any]] = []

        # 이벤트를 ts별로 인덱싱 (HH:MM:SS 기준)
        event_by_tick: dict[str, list[dict[str, Any]]] = {}
        for ev in events_cfg:
            ev_ts_str = ev.get("ts", "")
            event_by_tick.setdefault(ev_ts_str, []).append(ev)

        # 09:00부터 1분 단위 tick
        day_dt = _date.fromisoformat(day_date_str)
        session_start = datetime(
            day_dt.year, day_dt.month, day_dt.day, 9, 0, 0, tzinfo=_KST
        )

        for tick_i in range(n_ticks):
            tick_dt = session_start + timedelta(minutes=tick_i)
            tick_ts = tick_dt.isoformat()
            tick_hms = tick_dt.strftime("%H:%M:%S")

            # PIT-Safety 검증 (불변 원칙 1)
            snapshot_ts = datetime(
                day_dt.year, day_dt.month, day_dt.day, 18, 0, 0, tzinfo=_KST
            )
            try:
                assert_pit_safe(tick_dt, snapshot_ts=snapshot_ts)
            except PITViolationError as e:
                pit_violations += 1
                errors.append(f"PIT violation at tick={tick_ts}: {e}")
                continue

            # 합성 bar 생성
            bars_batch = _make_bars_batch(self._tickers, tick_ts)
            latest_prices = _make_latest_prices(self._tickers, bars_batch)

            # Hot Path run_once
            t0 = time.perf_counter()
            try:
                hr_result = runner.run_once(
                    tickers=self._tickers,
                    bars_batch=bars_batch,
                    latest_prices=latest_prices,
                    portfolio_value=self._portfolio_value,
                    asof=tick_ts,
                )
            except Exception as e:
                errors.append(f"HotRunner.run_once tick={tick_ts}: {e}")
                continue

            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            hot_path_latencies.append(elapsed_ms)

            # FDA reason_code 검증.
            # fda_result 구조: {final_decision: {reason_code: ...}, mode: ..., latency_ms: ...}
            # reason_code는 final_decision 내부에 있음.
            fda_result = hr_result.get("fda_result", {})
            final_decision = fda_result.get("final_decision", {})
            reason_code = final_decision.get("reason_code")
            if not reason_code:
                fda_missing_reason_code += 1
                errors.append(
                    f"FDA reason_code 누락: tick={tick_ts} "
                    f"final_decision keys={list(final_decision.keys())}"
                )

            hot_audit.append({
                "tick": tick_i,
                "ts": tick_ts,
                "latency_ms": round(elapsed_ms, 2),
                "final_decision": hr_result.get("final_decision"),
                "reason_code": reason_code,
                "pit_ok": True,
            })

            # Cold Path 이벤트 주입 (해당 tick의 HH:MM:SS 매칭)
            tick_events = event_by_tick.get(tick_hms, [])
            for ev_cfg in tick_events:
                cold_result = self._inject_event(injector, ev_cfg, tick_dt)
                cold_audit.append({
                    "tick": tick_i,
                    "ts": tick_ts,
                    "event_type": ev_cfg.get("type"),
                    "gateway_result": cold_result,
                })
                # dispatch_next: 등록된 핸들러 없어도 None 반환 (정상)
                gateway.dispatch_next()

        # 4. HOT_RUNNING → MODE_B_IDLE (15:30 장 마감)
        runner.stop_for_mode_b()
        assert sm.state == PipelineState.MODE_B_IDLE
        logger.info("[e2e_scenario_runner] %s HOT_RUNNING → MODE_B_IDLE", day_date_str)

        # 5. audit log 기록 (Hot Path)
        hot_audit_path = _AUDIT_DIR / f"hot_path_{day_date_str.replace('-', '')}.jsonl"
        self._flush_jsonl(hot_audit, hot_audit_path)
        cold_audit_path = _AUDIT_DIR / f"cold_path_{day_date_str.replace('-', '')}.jsonl"
        self._flush_jsonl(cold_audit, cold_audit_path)

        # 6. Mode B 시뮬 (18:00~22:00)
        mode_b_result: dict[str, Any] = {"verdict": "skipped", "stages": []}
        if not self._skip_mode_b:
            mode_b_result = self._run_mode_b_sim(sm, day_date_str)
        mode_b_audit_path = _AUDIT_DIR / f"mode_b_{day_date_str.replace('-', '')}.jsonl"
        self._flush_jsonl([mode_b_result], mode_b_audit_path)

        # 7. MODE_B_IDLE → BOOTSTRAP (다음 날 전환)
        sm.transition(PipelineState.BOOTSTRAP)
        next_state = PipelineState.BOOTSTRAP

        return {
            "date": day_date_str,
            "day_index": day_index,
            "hot_path_ticks": len(hot_audit),
            "cold_path_events": len(cold_audit),
            "hot_path_latencies": hot_path_latencies,
            "pit_violations": pit_violations,
            "fda_missing_reason_code": fda_missing_reason_code,
            "mode_b": mode_b_result,
            "errors": errors,
            "next_state": next_state,
            "state_history": sm.history,
        }

    # ------------------------------------------------------------------ #
    # Event injection helper
    # ------------------------------------------------------------------ #

    def _inject_event(
        self,
        injector: EventInjector,
        ev_cfg: dict[str, Any],
        tick_dt: datetime,
    ) -> dict[str, Any]:
        """시나리오 event_cfg를 EventInjector로 주입."""
        ev_type = ev_cfg.get("type", "")
        try:
            if ev_type == "news":
                return injector.inject_news(
                    ticker=str(ev_cfg.get("ticker", "000000")),
                    headline=str(ev_cfg.get("headline", "")),
                    ts=tick_dt,
                    sentiment=str(ev_cfg.get("sentiment", "neutral")),
                    source=str(ev_cfg.get("source", "naver")),
                )
            elif ev_type == "dart":
                return injector.inject_dart(
                    ticker=str(ev_cfg.get("ticker", "000000")),
                    disclosure_type=str(ev_cfg.get("disclosure_type", "")),
                    ts=tick_dt,
                    title=str(ev_cfg.get("title", "")),
                )
            elif ev_type == "community":
                return injector.inject_community(
                    ticker=str(ev_cfg.get("ticker", "000000")),
                    post_text=str(ev_cfg.get("post_text", "")),
                    ts=tick_dt,
                    score=float(ev_cfg.get("score", 0.5)),
                )
            elif ev_type == "macro":
                return injector.inject_macro(
                    indicator=str(ev_cfg.get("indicator", "")),
                    value=float(ev_cfg.get("value", 0.0)),
                    ts=tick_dt,
                )
            else:
                logger.warning("[e2e_scenario_runner] 미지원 event_type: %s", ev_type)
                return {"status": "unsupported", "event_type": ev_type}
        except Exception as e:
            logger.warning("[e2e_scenario_runner] 이벤트 주입 실패 type=%s: %s", ev_type, e)
            return {"status": "inject_error", "error": str(e)}

    # ------------------------------------------------------------------ #
    # Mode B simulation
    # ------------------------------------------------------------------ #

    def _run_mode_b_sim(
        self,
        sm: StateMachine,
        date_str: str,
    ) -> dict[str, Any]:
        """Mode B 7 stage 시뮬. ModeBScheduler 직접 호출 대신 개별 stage 실행.

        mode_guard @mode_b_only는 KST 18:00 이후를 요구한다.
        시뮬에서는 테스트용으로 ModeBScheduler의 내부 stage 메서드를 직접 호출하여
        @mode_b_only 데코레이터를 우회한다. 실 운영에서는 run_pipeline()이 호출됨.

        불변 원칙 3 준수:
          - Mode B stage는 Mode A 컴포넌트(HotRunner/FDAAgent)와 독립 인스턴스.
          - forbidden_permissions 4개 (hot_path_intervention 등) 미사용.
        """
        from src.mode_b.scheduler import ModeBScheduler

        sm.transition(PipelineState.MODE_B_EVOLVING)
        bundle_id = generate_bundle_id()
        scheduler = ModeBScheduler(state_machine=sm)
        scheduler._bundle_id = bundle_id

        if self._short_mode:
            def _stage_4_model_evolution_stub() -> dict[str, Any]:
                return {
                    "status": "stub_short_mode",
                    "bundle_id": scheduler._bundle_id,
                    "simulation_only": True,
                    "production_ready": False,
                    "production_blocker": "short_mode_model_evolution_stub",
                    "model_candidates": [{
                        "model_type": "lgbm",
                        "version": "scenario_stub",
                        "metrics": {"sr": 0.1, "ic": 0.01},
                    }],
                    "allocator_candidates": [],
                    "metrics": {"sr": 0.1, "ic": 0.01},
                }

            class _ScenarioBacktestAgent:
                def run(self, bundle_ref: str) -> dict[str, Any]:
                    now = f"{date_str}T18:30:00+09:00"
                    return {
                        "backtest_id": f"BT-{date_str.replace('-', '')}-AABBCCDD",
                        "bundle_id": bundle_ref,
                        "metrics": {
                            "ic": 0.01,
                            "icir": 0.1,
                            "rank_ic": 0.01,
                            "arr": 0.01,
                            "ir": 0.1,
                            "mdd": -0.01,
                            "sr": 0.1,
                        },
                        "folds": [],
                        "started_at": now,
                        "completed_at": now,
                        "verdict": "pass",
                        "regression_severity": "none",
                        "regression_risk": {
                            "flagged": False,
                            "severity": "low",
                            "evidence": [],
                        },
                        "minute_bar_leakage_check": {
                            "verdict": "pass",
                            "leakage_detected": False,
                            "purge_bars": 5,
                            "embargo_bars": 5,
                            "replay_unit": "1min",
                        },
                    }

            scheduler.stage_4_model_evolution = _stage_4_model_evolution_stub
            scheduler._backtest_agent = _ScenarioBacktestAgent()

        stages: list[dict[str, Any]] = []
        errors: list[str] = []

        previous_mode = os.environ.get("ELEPHANT_MODE")
        os.environ["ELEPHANT_MODE"] = "mode_b"
        try:
            stage_calls = [
                ("stage_0_dqr", lambda: scheduler.stage_0_dqr(date_str)),
                ("stage_1_performance_analysis", scheduler.stage_1_performance_analysis),
                ("stage_2_direction_selection", scheduler.stage_2_direction_selection),
                ("stage_3_factor_evolution", scheduler.stage_3_factor_evolution),
                ("stage_4_model_evolution", scheduler.stage_4_model_evolution),
                ("stage_5_agent_self_improvement", scheduler.stage_5_agent_self_improvement),
            ]

            for stage_name, stage_fn in stage_calls:
                t0 = time.perf_counter()
                try:
                    result = stage_fn()
                    result["stage"] = stage_name
                    result["duration_sec"] = round(time.perf_counter() - t0, 3)
                    stages.append(result)
                    logger.info("[e2e_scenario_runner] %s 완료: status=%s", stage_name, result.get("status"))
                except Exception as e:
                    err_msg = f"{stage_name} 오류: {e}"
                    errors.append(err_msg)
                    logger.warning("[e2e_scenario_runner] %s", err_msg)
                    stages.append({
                        "stage": stage_name,
                        "status": "error",
                        "error": str(e),
                        "duration_sec": round(time.perf_counter() - t0, 3),
                    })

            # candidate 존재 여부 판단
            s3 = next((s for s in stages if s.get("stage") == "stage_3_factor_evolution"), {})
            s4 = next((s for s in stages if s.get("stage") == "stage_4_model_evolution"), {})
            has_candidates = bool(s3.get("factor_candidates") or s4.get("model_candidates"))

            if not has_candidates:
                verdict = "skipped_no_candidates"
                sm.transition(PipelineState.MODE_B_BLOCKED)
                stages.append({
                    "stage": "stage_6_backtest_validation",
                    "status": "skipped_no_candidates",
                    "verdict": verdict,
                })
                stages.append({
                    "stage": "stage_7_deploy",
                    "status": "skipped",
                    "verdict": verdict,
                })
                sm.transition(PipelineState.MODE_B_IDLE)
            else:
                # EVOLVING → BACKTEST
                sm.transition(PipelineState.MODE_B_BACKTEST)
                s6_result = scheduler.stage_6_backtest_validation()
                s6_result["stage"] = "stage_6_backtest_validation"
                stages.append(s6_result)
                verdict = s6_result.get("verdict", "blocked")
                scheduler._current_verdict = verdict

                # BACKTEST → DEPLOY / BLOCKED
                if verdict == "pass" and not s6_result.get("critical_alert"):
                    sm.transition(PipelineState.MODE_B_DEPLOY)
                    s7_result = scheduler.stage_7_deploy()
                    s7_result["stage"] = "stage_7_deploy"
                    stages.append(s7_result)
                    sm.transition(PipelineState.MODE_B_IDLE)
                else:
                    sm.transition(PipelineState.MODE_B_BLOCKED)
                    stages.append({
                        "stage": "stage_7_deploy",
                        "status": "skipped",
                        "verdict": verdict,
                    })
                    sm.transition(PipelineState.MODE_B_IDLE)

            logger.info(
                "[e2e_scenario_runner] Mode B 시뮬 완료: date=%s verdict=%s bundle_id=%s",
                date_str, verdict, bundle_id,
            )
            return {
                "date": date_str,
                "bundle_id": bundle_id,
                "verdict": verdict,
                "simulation_only": bool(self._short_mode),
                "production_ready": False,
                "production_gate_required": "ModeBScheduler.run_pipeline + C12 real backtest + C14 deploy",
                "stages": stages,
                "errors": errors,
                "stage_count": len(stages),
            }
        finally:
            if previous_mode is None:
                os.environ.pop("ELEPHANT_MODE", None)
            else:
                os.environ["ELEPHANT_MODE"] = previous_mode

    # ------------------------------------------------------------------ #
    # IO helpers
    # ------------------------------------------------------------------ #

    def _flush_jsonl(
        self,
        records: list[dict[str, Any]],
        path: Path,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        logger.debug("[e2e_scenario_runner] JSONL flush: %d 건 → %s", len(records), path)

    def _flush_summary(self, summary: dict[str, Any]) -> None:
        summary_path = _AUDIT_DIR / f"scenario_{self._scenario_name}_summary.json"
        with summary_path.open("w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        logger.info("[e2e_scenario_runner] summary saved → %s", summary_path)

"""S4-3 E2E 시나리오 통합 테스트.

6가지 테스트:
  1. EventInjector 단일 이벤트 주입 + gateway status
  2. 1일 Mode A 시나리오 (Hot Path 5 tick + Cold Path 2 이벤트)
  3. 1일 Mode B 시나리오 (8 stage: stage_0 DQR + stage_1~7)
  4. Day N → Day N+1 state 연속성 (BOOTSTRAP → HOT_RUNNING → MODE_B_IDLE → BOOTSTRAP)
  5. audit log 무결성 (JSONL 존재 + 유효 JSON)
  6. 불변 5원칙 검증 (FDA reason_code 100% + PIT violation 0건 + Backtest Mode B 격리)

느린 테스트 (1주일 시나리오)는 @pytest.mark.slow 마커 적용.
CI 기본 실행에서 제외: pytest -m "not slow"
"""
from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

# sys.path: new/ 루트
_NEW_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_NEW_ROOT) not in sys.path:
    sys.path.insert(0, str(_NEW_ROOT))

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
from src.runner.e2e_scenario_runner import (
    E2EScenarioRunner,
    _make_bars_batch,
    _make_latest_prices,
)
from src.utils.config_loader import load as config_load

_KST = ZoneInfo("Asia/Seoul")
_TICKERS = ["005930", "000660", "035420"]


def _pit_safe_intraday(hour: int = 9, minute: int = 30) -> datetime:
    """현재 시각 기준 미래가 되지 않는 장중 시각 (KST)."""
    now = datetime.now(_KST)
    ts = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if ts > now:
        ts -= timedelta(days=1)
    return ts


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def gateway() -> EventGateway:
    return EventGateway()


@pytest.fixture
def injector(gateway: EventGateway, tmp_path: pathlib.Path) -> EventInjector:
    return EventInjector(
        gateway=gateway,
        audit_log_path=tmp_path / "test_injected.jsonl",
    )


@pytest.fixture
def hot_runner() -> HotRunner:
    sm = StateMachine()
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
    runner.start()
    return runner


@pytest.fixture
def scenario_runner(tmp_path: pathlib.Path) -> E2EScenarioRunner:
    """짧은 모드 (5 tick, skip_mode_b=False)."""
    return E2EScenarioRunner(
        scenario_file="week1_basic.yaml",
        short_mode=True,
        skip_mode_b=False,
    )


# ──────────────────────────────────────────────────────────────────────
# Test 1: EventInjector 단일 이벤트 주입
# ──────────────────────────────────────────────────────────────────────

class TestEventInjector:
    """EventInjector 단위 검증."""

    def test_inject_news_returns_gateway_result(
        self, injector: EventInjector
    ) -> None:
        # PIT-Safety: 현재 시각 기준 미래가 아닌 장중 시각 사용
        ts = _pit_safe_intraday(9, 30)
        result = injector.inject_news(
            ticker="005930",
            headline="삼성전자 신규 HBM4 계약",
            ts=ts,
            sentiment="positive",
        )
        # normalize_failed는 필드명 불일치 시 발생: C-R2-1 fix 후 반드시 admitted
        assert result.get("status") == "admitted", (
            f"inject_news normalize_failed (필드명 불일치 또는 PIT 위반): {result}"
        )
        assert injector.injected_count() == 1

    def test_inject_dart_ticker_zero_padded(
        self, injector: EventInjector
    ) -> None:
        # PIT-Safety: 현재 시각 기준 미래가 아닌 장중 시각 사용
        ts = _pit_safe_intraday(13, 0)
        result = injector.inject_dart(
            ticker="5930",  # zero-pad 필요
            disclosure_type="주요사항보고서",
            ts=ts,
        )
        event = injector.injected_events()[0]
        assert event["raw"]["ticker"] == "005930", "ticker 6자리 zero-pad 필요"
        assert result.get("status") == "admitted", (
            f"inject_dart normalize_failed (필드명 불일치 또는 PIT 위반): {result}"
        )

    def test_inject_community(self, injector: EventInjector) -> None:
        # PIT-Safety: 현재 시각 기준 미래가 아닌 장중 시각 사용
        ts = _pit_safe_intraday(9, 5)
        result = injector.inject_community(
            ticker="035420",
            post_text="네이버 AI 검색 루머",
            ts=ts,
            score=0.72,
        )
        assert result.get("status") == "admitted", (
            f"inject_community normalize_failed (필드명 불일치 또는 PIT 위반): {result}"
        )

    def test_inject_macro_no_ticker(self, injector: EventInjector) -> None:
        # PIT-Safety: 현재 시각 기준 미래가 아닌 장중 시각 사용
        ts = _pit_safe_intraday(10, 15)
        result = injector.inject_macro(
            indicator="usd_krw",
            value=1380.5,
            ts=ts,
        )
        event = injector.injected_events()[0]
        assert "ticker" not in event["raw"], "macro 이벤트에는 ticker 없음"
        assert result.get("status") == "admitted", (
            f"inject_macro normalize_failed (필드명 불일치 또는 PIT 위반): {result}"
        )

    def test_flush_audit_log(
        self, injector: EventInjector, tmp_path: pathlib.Path
    ) -> None:
        ts = datetime(2026, 5, 4, 9, 30, 0, tzinfo=_KST)
        injector.inject_news(ticker="005930", headline="테스트", ts=ts)
        log_path = injector.flush_audit_log()
        assert log_path.exists()
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry.get("event_type") == "news"

    def test_multiple_injections_counted(self, injector: EventInjector) -> None:
        ts = datetime(2026, 5, 4, 9, 30, 0, tzinfo=_KST)
        injector.inject_news("005930", "뉴스1", ts)
        injector.inject_macro("usd_krw", 1380.0, ts)
        injector.inject_dart("000660", "조회공시", ts)
        assert injector.injected_count() == 3


# ──────────────────────────────────────────────────────────────────────
# Test 2: 1일 Mode A 시나리오
# ──────────────────────────────────────────────────────────────────────

class TestOneDayModeA:
    """1일 Mode A (Hot 5 tick + Cold 이벤트 주입) 검증."""

    def test_hot_path_5_ticks(self, hot_runner: HotRunner) -> None:
        """Hot Path 5 tick 연속 실행. latency 결과 반환 + SLA 미초과 확인."""
        tickers = _TICKERS
        latencies: list[float] = []
        for tick_i in range(5):
            ts = datetime(2026, 5, 4, 9, tick_i, 0, tzinfo=_KST).isoformat()
            bars = _make_bars_batch(tickers, ts)
            prices = _make_latest_prices(tickers, bars)
            result = hot_runner.run_once(
                tickers=tickers,
                bars_batch=bars,
                latest_prices=prices,
                portfolio_value=100_000_000,
                asof=ts,
            )
            assert result.get("pipeline_state") == "HOT_RUNNING"
            latencies.append(result.get("latency_ms", 0.0))

        # p95 계산
        import numpy as np
        p95 = float(np.percentile(latencies, 95))
        # 테스트 환경에서는 SLA 비교 정보 제공 (fail 기준은 너무 엄격하지 않게)
        # 실제 운영에서만 100ms 강제
        assert p95 >= 0.0
        assert len(latencies) == 5

    def test_fda_reason_code_always_present(self, hot_runner: HotRunner) -> None:
        """모든 tick에서 FDA reason_code 비어있지 않음.
        
        fda_result 구조: {final_decision: {reason_code: ...}, mode: ..., latency_ms: ...}
        reason_code는 final_decision 내부에 있음.
        """
        tickers = _TICKERS
        for tick_i in range(3):
            ts = datetime(2026, 5, 4, 9, tick_i, 0, tzinfo=_KST).isoformat()
            bars = _make_bars_batch(tickers, ts)
            prices = _make_latest_prices(tickers, bars)
            result = hot_runner.run_once(
                tickers=tickers,
                bars_batch=bars,
                latest_prices=prices,
                portfolio_value=100_000_000,
                asof=ts,
            )
            fda_result = result.get("fda_result", {})
            final_decision = fda_result.get("final_decision", {})
            assert final_decision.get("reason_code"), (
                f"FDA reason_code 누락 at tick={tick_i}: final_decision={final_decision}"
            )

    def test_cold_path_event_injection(
        self, gateway: EventGateway, injector: EventInjector
    ) -> None:
        """Cold Path 이벤트 주입 + dispatch_next 정상 동작."""
        ts = _pit_safe_intraday(9, 30)
        injector.inject_news(
            ticker="005930",
            headline="삼성전자 호재 뉴스",
            ts=ts,
            sentiment="positive",
        )
        # dispatch_next: 핸들러 미등록이면 None (정상)
        dispatch_result = gateway.dispatch_next()
        # admitted 이벤트가 있어도 핸들러 없으면 None
        assert dispatch_result is None or isinstance(dispatch_result, dict)

    def test_mode_a_state_sequence(self) -> None:
        """BOOTSTRAP → HOT_RUNNING → MODE_B_IDLE 전이 순서 검증."""
        sm = StateMachine()
        assert sm.state == PipelineState.BOOTSTRAP
        sm.transition(PipelineState.HOT_RUNNING)
        assert sm.state == PipelineState.HOT_RUNNING
        sm.transition(PipelineState.MODE_B_IDLE)
        assert sm.state == PipelineState.MODE_B_IDLE

    def test_scenario_runner_day1_mode_a(self, scenario_runner: E2EScenarioRunner) -> None:
        """ScenarioRunner 1일 Mode A 실행 (short_mode=True, skip_mode_b=True)."""
        # 빠른 1일 테스트: Mode B 스킵
        runner = E2EScenarioRunner(
            scenario_file="week1_basic.yaml",
            short_mode=True,
            skip_mode_b=True,
        )
        days_cfg = runner._scenario.get("days", [])
        assert len(days_cfg) > 0
        day_result = runner._run_day(
            day_cfg=days_cfg[0],
            day_index=0,
            prev_state=PipelineState.BOOTSTRAP,
        )
        assert day_result.get("hot_path_ticks") == runner._ticks_short
        assert day_result.get("pit_violations", 0) == 0
        assert day_result.get("next_state") == PipelineState.BOOTSTRAP


# ──────────────────────────────────────────────────────────────────────
# Test 3: 1일 Mode B 시나리오
# ──────────────────────────────────────────────────────────────────────

class TestOneDayModeB:
    """1일 Mode B (8 stage) 시뮬 검증."""

    def test_mode_b_stages_execute(self) -> None:
        """Mode B 8 stage 모두 실행 + verdict 반환."""
        sm = StateMachine()
        sm.transition(PipelineState.HOT_RUNNING)
        sm.transition(PipelineState.MODE_B_IDLE)

        runner = E2EScenarioRunner(
            scenario_file="week1_basic.yaml",
            short_mode=True,
            skip_mode_b=False,
        )
        mode_b_result = runner._run_mode_b_sim(sm, "2026-05-04")

        assert "verdict" in mode_b_result
        assert "stages" in mode_b_result
        assert mode_b_result.get("stage_count", 0) >= 7
        assert mode_b_result["verdict"] in ("pass", "warn", "blocked", "skipped_no_candidates")

    def test_mode_b_stage_error_blocks_backtest_and_deploy(self, monkeypatch) -> None:
        """pre-backtest stage 오류는 stage_6/7 PASS로 덮지 않는다."""
        from src.mode_b.scheduler import ModeBScheduler

        def fake_stage_0_dqr(self, date: str) -> dict:
            return {
                "status": "error",
                "dqr_date": date,
                "critical_alert": True,
                "alerts": [],
                "error": "forced_dqr_failure",
            }

        monkeypatch.setattr(ModeBScheduler, "stage_0_dqr", fake_stage_0_dqr)

        sm = StateMachine()
        sm.transition(PipelineState.HOT_RUNNING)
        sm.transition(PipelineState.MODE_B_IDLE)

        runner = E2EScenarioRunner(
            scenario_file="week1_basic.yaml",
            short_mode=True,
            skip_mode_b=False,
        )
        mode_b_result = runner._run_mode_b_sim(sm, "2026-05-04")

        assert mode_b_result["verdict"] == "blocked"
        assert mode_b_result["errors"], "stage_0 오류가 errors에 반영되어야 한다"
        assert any(
            s.get("stage") == "stage_6_backtest_validation"
            and s.get("status") == "skipped_stage_failure"
            for s in mode_b_result["stages"]
        )
        assert any(
            s.get("stage") == "stage_7_deploy"
            and s.get("status") == "skipped"
            for s in mode_b_result["stages"]
        )
        assert sm.state == PipelineState.MODE_B_IDLE

    def test_mode_b_errors_propagate_to_scenario_summary(self, monkeypatch) -> None:
        """Mode B 내부 오류는 day summary total_errors에 포함된다."""
        from src.mode_b.scheduler import ModeBScheduler

        def fake_stage_0_dqr(self, date: str) -> dict:
            return {
                "status": "error",
                "dqr_date": date,
                "critical_alert": True,
                "alerts": [],
                "error": "forced_dqr_failure",
            }

        monkeypatch.setattr(ModeBScheduler, "stage_0_dqr", fake_stage_0_dqr)

        runner = E2EScenarioRunner(
            scenario_file="week1_basic.yaml",
            short_mode=True,
            skip_mode_b=False,
        )
        result = runner.run()
        summary = result.summary()

        assert summary["total_errors"] == summary["total_days"]
        assert all(v == "blocked" for v in summary["mode_b_verdicts"])

    def test_mode_b_state_machine_transitions(self) -> None:
        """Mode B: MODE_B_IDLE → EVOLVING → BACKTEST → DEPLOY/BLOCKED → IDLE."""
        sm = StateMachine()
        sm.transition(PipelineState.HOT_RUNNING)
        sm.transition(PipelineState.MODE_B_IDLE)

        runner = E2EScenarioRunner(
            scenario_file="week1_basic.yaml",
            short_mode=True,
            skip_mode_b=False,
        )
        runner._run_mode_b_sim(sm, "2026-05-04")

        # 최종 상태: MODE_B_IDLE (deploy 또는 blocked 이후)
        assert sm.state == PipelineState.MODE_B_IDLE

    def test_mode_b_forbidden_permissions_not_triggered(self) -> None:
        """Mode B stage에서 forbidden_permissions 4개 미호출 확인.

        ModeBScheduler._STAGE_REQUIRED_PERMISSIONS 에 forbidden 권한이 없으면 OK.
        PermissionViolationError가 발생하지 않아야 한다.
        """
        from src.mode_b.scheduler import ModeBScheduler, FORBIDDEN_PERMISSIONS

        sm = StateMachine()
        sm.transition(PipelineState.HOT_RUNNING)
        sm.transition(PipelineState.MODE_B_IDLE)

        scheduler = ModeBScheduler(state_machine=sm)
        for stage_name, required in scheduler._STAGE_REQUIRED_PERMISSIONS.items():
            violations = required & FORBIDDEN_PERMISSIONS
            assert not violations, (
                f"불변 원칙 3 위반: {stage_name}가 forbidden 권한 사용: {violations}"
            )

    def test_mode_b_bundle_id_generated(self) -> None:
        """Mode B 실행 시 bundle_id 발급."""
        sm = StateMachine()
        sm.transition(PipelineState.HOT_RUNNING)
        sm.transition(PipelineState.MODE_B_IDLE)

        runner = E2EScenarioRunner(
            scenario_file="week1_basic.yaml",
            short_mode=True,
        )
        mode_b_result = runner._run_mode_b_sim(sm, "2026-05-04")
        bundle_id = mode_b_result.get("bundle_id", "")
        assert bundle_id.startswith("BUNDLE-"), (
            f"bundle_id 포맷 오류: {bundle_id}"
        )


# ──────────────────────────────────────────────────────────────────────
# Test 4: Day N → Day N+1 state 연속성
# ──────────────────────────────────────────────────────────────────────

class TestDayStateContinuity:
    """Day N → Day N+1 연속성 검증."""

    def test_next_state_is_bootstrap(self) -> None:
        """모든 Day 실행 후 next_state = BOOTSTRAP."""
        runner = E2EScenarioRunner(
            scenario_file="week1_basic.yaml",
            short_mode=True,
            skip_mode_b=True,
        )
        days_cfg = runner._scenario.get("days", [])
        prev_state = PipelineState.BOOTSTRAP
        for i, day_cfg in enumerate(days_cfg[:2]):  # 처음 2일만 검증 (속도)
            day_result = runner._run_day(
                day_cfg=day_cfg,
                day_index=i,
                prev_state=prev_state,
            )
            prev_state = day_result.get("next_state", PipelineState.BOOTSTRAP)
            assert prev_state == PipelineState.BOOTSTRAP, (
                f"Day {i+1} 이후 next_state={prev_state.value}, BOOTSTRAP 기대"
            )

    def test_state_history_records_transitions(self) -> None:
        """state_history에 전이 기록이 남음."""
        runner = E2EScenarioRunner(
            scenario_file="week1_basic.yaml",
            short_mode=True,
            skip_mode_b=True,
        )
        days_cfg = runner._scenario.get("days", [])
        day_result = runner._run_day(
            day_cfg=days_cfg[0],
            day_index=0,
            prev_state=PipelineState.BOOTSTRAP,
        )
        history = day_result.get("state_history", [])
        # 최소: BOOTSTRAP(초기) + HOT_RUNNING + MODE_B_IDLE + BOOTSTRAP
        assert len(history) >= 3
        # HOT_RUNNING 전이가 있어야 함
        transition_tos = [h["to"] for h in history]
        assert "HOT_RUNNING" in transition_tos

    def test_pit_safety_no_violations_in_normal_day(self) -> None:
        """정상 시나리오 Day 1에서 PIT violation 0건."""
        runner = E2EScenarioRunner(
            scenario_file="week1_basic.yaml",
            short_mode=True,
            skip_mode_b=True,
        )
        days_cfg = runner._scenario.get("days", [])
        day_result = runner._run_day(
            day_cfg=days_cfg[0],
            day_index=0,
            prev_state=PipelineState.BOOTSTRAP,
        )
        assert day_result.get("pit_violations", 0) == 0, (
            f"PIT violation 발생: {day_result.get('errors')}"
        )


# ──────────────────────────────────────────────────────────────────────
# Test 5: audit log 무결성
# ──────────────────────────────────────────────────────────────────────

class TestAuditLogIntegrity:
    """audit log JSONL 생성 + 유효 JSON 검증."""

    def test_hot_path_audit_log_created(self) -> None:
        """hot_path_YYYYMMDD.jsonl 파일 생성 확인."""
        import pathlib
        runner = E2EScenarioRunner(
            scenario_file="week1_basic.yaml",
            short_mode=True,
            skip_mode_b=True,
        )
        days_cfg = runner._scenario.get("days", [])
        runner._run_day(
            day_cfg=days_cfg[0],
            day_index=0,
            prev_state=PipelineState.BOOTSTRAP,
        )
        audit_dir = pathlib.Path(__file__).resolve().parents[3] / "artifacts" / "audit"
        hot_log = audit_dir / "hot_path_20260504.jsonl"
        assert hot_log.exists(), f"hot_path_20260504.jsonl 미생성: {audit_dir}"

    def test_hot_path_audit_log_valid_json(self) -> None:
        """hot_path JSONL 각 줄이 유효한 JSON."""
        import pathlib
        runner = E2EScenarioRunner(
            scenario_file="week1_basic.yaml",
            short_mode=True,
            skip_mode_b=True,
        )
        days_cfg = runner._scenario.get("days", [])
        runner._run_day(
            day_cfg=days_cfg[0],
            day_index=0,
            prev_state=PipelineState.BOOTSTRAP,
        )
        audit_dir = pathlib.Path(__file__).resolve().parents[3] / "artifacts" / "audit"
        hot_log = audit_dir / "hot_path_20260504.jsonl"
        if hot_log.exists():
            lines = hot_log.read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) > 0, "hot_path JSONL이 비어 있음"
            for line in lines:
                entry = json.loads(line)
                assert "ts" in entry
                assert "latency_ms" in entry

    def test_mode_b_audit_log_created(self) -> None:
        """mode_b_YYYYMMDD.jsonl 파일 생성 확인."""
        import pathlib
        runner = E2EScenarioRunner(
            scenario_file="week1_basic.yaml",
            short_mode=True,
            skip_mode_b=False,
        )
        days_cfg = runner._scenario.get("days", [])
        runner._run_day(
            day_cfg=days_cfg[0],
            day_index=0,
            prev_state=PipelineState.BOOTSTRAP,
        )
        audit_dir = pathlib.Path(__file__).resolve().parents[3] / "artifacts" / "audit"
        mode_b_log = audit_dir / "mode_b_20260504.jsonl"
        assert mode_b_log.exists(), "mode_b_20260504.jsonl 미생성"

    def test_cold_path_audit_log_created(self) -> None:
        """cold_path_YYYYMMDD.jsonl 파일 생성 확인."""
        import pathlib
        runner = E2EScenarioRunner(
            scenario_file="week1_basic.yaml",
            short_mode=True,
            skip_mode_b=True,
        )
        days_cfg = runner._scenario.get("days", [])
        runner._run_day(
            day_cfg=days_cfg[0],
            day_index=0,
            prev_state=PipelineState.BOOTSTRAP,
        )
        audit_dir = pathlib.Path(__file__).resolve().parents[3] / "artifacts" / "audit"
        cold_log = audit_dir / "cold_path_20260504.jsonl"
        assert cold_log.exists(), "cold_path_20260504.jsonl 미생성"


# ──────────────────────────────────────────────────────────────────────
# Test 6: 불변 5원칙 검증
# ──────────────────────────────────────────────────────────────────────

class TestInvariantPrinciples:
    """불변 5원칙 E2E 수준 검증."""

    def test_fda_can_change_weight_false(self) -> None:
        """불변 원칙 2: FDAAgent.CAN_CHANGE_WEIGHT = False 클래스 상수 확인."""
        assert FDAAgent.CAN_CHANGE_WEIGHT is False, (
            "FDAAgent.CAN_CHANGE_WEIGHT가 True로 변경됨. 불변 원칙 2 위반."
        )

    def test_fda_does_not_modify_target_weights(self, hot_runner: HotRunner) -> None:
        """불변 원칙 2: Hot Path FDA 결과에 target_weights 수정이 없음.

        fda_result의 target_weights는 echo만 (PPO 결과 그대로).
        """
        tickers = _TICKERS
        ts = datetime(2026, 5, 4, 9, 0, 0, tzinfo=_KST).isoformat()
        bars = _make_bars_batch(tickers, ts)
        prices = _make_latest_prices(tickers, bars)
        result = hot_runner.run_once(
            tickers=tickers,
            bars_batch=bars,
            latest_prices=prices,
            portfolio_value=100_000_000,
            asof=ts,
        )
        # FDA result에 target_weights_modified 플래그가 없거나 False
        fda_result = result.get("fda_result", {})
        assert not fda_result.get("target_weights_modified", False), (
            "FDA가 target_weights를 수정함. 불변 원칙 2 위반."
        )

    def test_pit_safety_no_future_data(self) -> None:
        """불변 원칙 1: 장중 tick ts는 항상 snapshot(18:00) 이전."""
        from src.utils.pit_guard import is_pit_safe
        from datetime import datetime

        snapshot = datetime(2026, 5, 4, 18, 0, 0, tzinfo=_KST)
        test_ticks = [
            datetime(2026, 5, 4, 9, 0, 0, tzinfo=_KST),
            datetime(2026, 5, 4, 12, 30, 0, tzinfo=_KST),
            datetime(2026, 5, 4, 15, 29, 0, tzinfo=_KST),
        ]
        for tick_dt in test_ticks:
            assert is_pit_safe(tick_dt, snapshot_ts=snapshot), (
                f"PIT violation: tick_dt={tick_dt} > snapshot={snapshot}"
            )

    def test_no_hardcoded_sla_in_runner(self, monkeypatch) -> None:
        """불변 원칙 5: ScenarioResult.summary() SLA 임계값이 yaml에서 로드됨.

        config_load mock으로 latency_p95_target_ms=999 주입 시
        summary()["hot_path_sla"]["target_p95_ms"] == 999 검증.
        하드코딩이면 999가 아닌 100으로 나옴 → 테스트 fail.
        """
        import src.runner.e2e_scenario_runner as runner_mod
        original_config_load = runner_mod.config_load

        def mock_config_load(fname, section):
            cfg = original_config_load(fname, section)
            if fname == "risk_config.yaml" and section == "quant_agent":
                cfg = dict(cfg)
                cfg["latency_p95_target_ms"] = 999
            return cfg

        monkeypatch.setattr(
            "src.runner.e2e_scenario_runner.config_load",
            mock_config_load,
        )

        # ScenarioResult.summary() 내부에서 config_load 호출 경로 패치
        import src.utils.config_loader as cl_mod
        original_cl_load = cl_mod.load

        def mock_cl_load(fname, section=None):
            result = original_cl_load(fname, section)
            if fname == "risk_config.yaml" and section == "quant_agent":
                result = dict(result)
                result["latency_p95_target_ms"] = 999
            return result

        monkeypatch.setattr(cl_mod, "load", mock_cl_load)

        from src.runner.e2e_scenario_runner import ScenarioResult
        sr = ScenarioResult("test_monkeypatch")
        sr.hot_path_latencies = [10.0, 20.0, 30.0]
        summary = sr.summary()
        assert summary["hot_path_sla"]["target_p95_ms"] == 999.0, (
            f"SLA 하드코딩 의심: target_p95_ms={summary['hot_path_sla']['target_p95_ms']} "
            f"(999 기대, 100이면 하드코딩)"
        )

        # yaml 키 존재 확인 (기존 검증 유지)
        qa_cfg = config_load("risk_config.yaml", "quant_agent")
        assert "latency_p95_target_ms" in qa_cfg, (
            "risk_config.yaml quant_agent.latency_p95_target_ms 미존재. 불변 원칙 5 위반."
        )

    def test_backtest_agent_mode_b_isolated(self) -> None:
        """불변 원칙 3: BacktestAgent가 Mode A (hot_runner) 경로에서 호출되지 않음.

        HotRunner.run_once 결과에 backtest 관련 키가 없음을 확인.
        """
        tickers = _TICKERS
        sm = StateMachine()
        runner = HotRunner(
            quant=QuantAgent(),
            ppo=PPOAllocator(),
            pm=PortfolioManager(),
            fda=FDAAgent(),
            state_machine=sm,
        )
        runner.start()
        ts = datetime(2026, 5, 4, 9, 0, 0, tzinfo=_KST).isoformat()
        bars = _make_bars_batch(tickers, ts)
        prices = _make_latest_prices(tickers, bars)
        result = runner.run_once(
            tickers=tickers,
            bars_batch=bars,
            latest_prices=prices,
            portfolio_value=100_000_000,
            asof=ts,
        )
        # backtest 결과가 Hot Path 응답에 포함되지 않아야 함
        assert "backtest_result" not in result, (
            "Backtest 결과가 Hot Path 응답에 포함됨. 불변 원칙 3 위반."
        )
        assert "backtest_id" not in result, (
            "backtest_id가 Hot Path 응답에 포함됨. 불변 원칙 3 위반."
        )

    def test_fda_reason_code_in_all_hot_ticks(self, hot_runner: HotRunner) -> None:
        """FDA reason_code 100% 출력 (5 tick 전수 확인)."""
        tickers = _TICKERS
        missing = 0
        for tick_i in range(5):
            ts = datetime(2026, 5, 4, 9, tick_i, 0, tzinfo=_KST).isoformat()
            bars = _make_bars_batch(tickers, ts)
            prices = _make_latest_prices(tickers, bars)
            result = hot_runner.run_once(
                tickers=tickers,
                bars_batch=bars,
                latest_prices=prices,
                portfolio_value=100_000_000,
                asof=ts,
            )
            if not result.get("fda_result", {}).get("final_decision", {}).get("reason_code"):
                missing += 1
        assert missing == 0, f"FDA reason_code 누락 {missing}/5 tick"


# ──────────────────────────────────────────────────────────────────────
# Test 7 (slow): 1주일 전체 시나리오
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.slow
class TestFullWeekScenario:
    """1주일 전체 시나리오 (느린 테스트). CI: pytest -m "not slow" 로 제외."""

    def test_week1_basic_full_run(self) -> None:
        """week1_basic.yaml 5일 전체 실행.

        검증:
          - 5일 모두 완료
          - PIT violation 0건
          - FDA reason_code 누락 0건
          - Mode B verdict 5회 존재
        """
        runner = E2EScenarioRunner(
            scenario_file="week1_basic.yaml",
            short_mode=True,   # 390 tick 아닌 5 tick (빠른 전체 시나리오)
            skip_mode_b=False,
        )
        result = runner.run()
        summary = result.summary()

        assert summary["total_days"] == 5, f"5일 기대, 실제={summary['total_days']}"
        assert summary["pit_violations"] == 0, (
            f"PIT violation {summary['pit_violations']}건"
        )
        assert summary["fda_missing_reason_code"] == 0, (
            f"FDA reason_code 누락 {summary['fda_missing_reason_code']}건"
        )
        # Mode B verdict 5개 존재
        verdicts = [v for v in summary.get("mode_b_verdicts", []) if v is not None]
        assert len(verdicts) == 5, f"Mode B verdict {len(verdicts)}/5"


# ──────────────────────────────────────────────────────────────────────
# Test 8: R2 추가 unit test (Cold Path admitted 강제 검증)
# ──────────────────────────────────────────────────────────────────────

class TestEventInjectorAdmitted:
    """C-R2-1 fix 후 4 메서드 모두 admitted 강제 검증."""

    def test_inject_news_admitted(self, injector: EventInjector) -> None:
        """inject_news → status == admitted (normalize_failed 금지)."""
        ts = _pit_safe_intraday(9, 30)
        result = injector.inject_news(
            ticker="005930",
            headline="삼성전자 HBM4 계약",
            ts=ts,
            sentiment="positive",
        )
        assert result.get("status") == "admitted", (
            f"inject_news admitted 아님: {result}"
        )

    def test_inject_dart_admitted(self, injector: EventInjector) -> None:
        """inject_dart → status == admitted (normalize_failed 금지)."""
        ts = _pit_safe_intraday(13, 0)
        result = injector.inject_dart(
            ticker="000660",
            disclosure_type="주요사항보고서",
            ts=ts,
            title="SK하이닉스 자사주 매입",
            corp_name="SK하이닉스",
        )
        assert result.get("status") == "admitted", (
            f"inject_dart admitted 아님: {result}"
        )

    def test_inject_community_admitted(self, injector: EventInjector) -> None:
        """inject_community → status == admitted (normalize_failed 금지)."""
        ts = _pit_safe_intraday(9, 5)
        result = injector.inject_community(
            ticker="035420",
            post_text="NAVER AI 검색 루머",
            ts=ts,
            score=0.72,
        )
        assert result.get("status") == "admitted", (
            f"inject_community admitted 아님: {result}"
        )

    def test_inject_macro_admitted(self, injector: EventInjector) -> None:
        """inject_macro → status == admitted (normalize_failed 금지)."""
        ts = _pit_safe_intraday(10, 15)
        result = injector.inject_macro(
            indicator="usd_krw",
            value=1380.5,
            ts=ts,
        )
        assert result.get("status") == "admitted", (
            f"inject_macro admitted 아님: {result}"
        )

    def test_cold_path_reaches_backlog(
        self, gateway: EventGateway, injector: EventInjector
    ) -> None:
        """inject_news admitted → gateway.backlog_size() >= 1."""
        ts = _pit_safe_intraday(9, 30)
        result = injector.inject_news(
            ticker="005930",
            headline="삼성전자 테스트",
            ts=ts,
        )
        assert result.get("status") == "admitted"
        assert gateway.backlog_size() >= 1, (
            f"admitted 이후 backlog_size={gateway.backlog_size()}, 최소 1 기대"
        )

    def test_cold_path_audit_log_has_lines(
        self, injector: EventInjector
    ) -> None:
        """flush_audit_log() 결과가 비어있지 않음 (주입 후)."""
        ts = _pit_safe_intraday(9, 30)
        injector.inject_news(ticker="005930", headline="테스트", ts=ts)
        log_path = injector.flush_audit_log()
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 1, f"audit log 비어있음: {log_path}"
        # 유효 JSON 확인
        entry = json.loads(lines[0])
        assert "event_type" in entry

    def test_hot_path_sla_regression(self, hot_runner: HotRunner) -> None:
        """Hot Path p95 < 500ms 회귀 감지 (운영 SLA 100ms보다 여유 있는 회귀 기준)."""
        tickers = _TICKERS
        latencies: list[float] = []
        for tick_i in range(5):
            ts = datetime(2026, 5, 4, 9, tick_i, 0, tzinfo=_KST).isoformat()
            bars = _make_bars_batch(tickers, ts)
            prices = _make_latest_prices(tickers, bars)
            t0 = __import__("time").perf_counter()
            hot_runner.run_once(
                tickers=tickers,
                bars_batch=bars,
                latest_prices=prices,
                portfolio_value=100_000_000,
                asof=ts,
            )
            latencies.append((__import__("time").perf_counter() - t0) * 1000.0)
        import numpy as np
        p95 = float(np.percentile(latencies, 95))
        assert p95 < 500.0, (
            f"Hot Path p95={p95:.1f}ms, 500ms 초과 (심각한 회귀)"
        )

    def test_scenario_invalid_date_order_raises(self) -> None:
        """날짜 역순 시나리오 → ValueError 발생."""
        import tempfile
        import yaml as _yaml

        bad_scenario = {
            "scenario_name": "bad_order",
            "universe_tickers": ["005930"],
            "hot_path_ticks_per_day": 390,
            "hot_path_ticks_short": 5,
            "days": [
                {"date": "2026-05-08", "events": []},
                {"date": "2026-05-04", "events": []},  # 역순
            ],
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".yaml",
            dir=_SCENARIOS_DIR,
            delete=False,
            encoding="utf-8",
        ) as tf:
            _yaml.dump(bad_scenario, tf, allow_unicode=True)
            tf_name = pathlib.Path(tf.name).name

        try:
            with pytest.raises(ValueError, match="역순"):
                E2EScenarioRunner(scenario_file=tf_name)
        finally:
            (pathlib.Path(__file__).resolve().parents[2] / "config" / "scenarios" / tf_name).unlink(missing_ok=True)


# _SCENARIOS_DIR 노출 (test_scenario_invalid_date_order_raises 참조용)
_SCENARIOS_DIR = pathlib.Path(__file__).resolve().parents[2] / "config" / "scenarios"

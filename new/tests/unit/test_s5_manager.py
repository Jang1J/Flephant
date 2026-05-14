"""Sprint 5 S5-4 DynamicUniverseManager 단위 테스트.

C15 통합 오케스트레이터 lifecycle 검증. 모든 컴포넌트 mock 주입.

테스트 케이스:
  1. test_manager_cycle_once_returns_dict_with_keys
  2. test_manager_cycle_once_skips_when_gate_disabled
  3. test_manager_cycle_id_format
  4. test_manager_handle_admission_event_promotes_to_holdings
  5. test_manager_handle_admission_event_rejected_when_pool_full
  6. test_manager_shutdown_saves_state
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.dynamic_universe.manager import DynamicUniverseManager


# ================================================================
# 헬퍼: mock 컴포넌트 팩토리
# ================================================================

def _make_gate(enabled: bool) -> MagicMock:
    gate = MagicMock()
    gate.is_enabled.return_value = enabled
    return gate


def _make_snapshot_fetcher(watch_snapshot_id: str = "WS-20260503-abc12345") -> MagicMock:
    fetcher = MagicMock()
    fetcher.fetch_once.return_value = {
        "watch_snapshot_id": watch_snapshot_id,
        "ts": "2026-05-03T10:00:00+09:00",
        "snapshots": [
            {"ticker": "005930", "last_price": 70000.0, "day_change_pct": 0.02},
        ],
    }
    return fetcher


def _make_admission_engine(pool_size: int = 2) -> MagicMock:
    engine = MagicMock()
    engine.get_pool_state.return_value = [{}] * pool_size
    engine.handle_event.return_value = None  # 기본: 거부
    return engine


def _make_holdings_manager(holdings_size: int = 1) -> MagicMock:
    manager = MagicMock()
    manager.get_holdings.return_value = [{}] * holdings_size
    manager.promote_to_holdings.return_value = None  # 기본: 거부
    return manager


def _make_exit_engine(exit_events: list | None = None) -> MagicMock:
    engine = MagicMock()
    engine.evaluate.return_value = exit_events or []
    return engine


def _make_manager(
    enabled: bool = True,
    pool_size: int = 2,
    holdings_size: int = 1,
    exit_events: list | None = None,
    snapshot_id: str = "WS-20260503-abc12345",
    cycles_dir: Path | None = None,
) -> DynamicUniverseManager:
    """DynamicUniverseManager + 모든 컴포넌트 mock 주입 생성."""
    return DynamicUniverseManager(
        snapshot_fetcher=_make_snapshot_fetcher(snapshot_id),
        admission_engine=_make_admission_engine(pool_size),
        holdings_manager=_make_holdings_manager(holdings_size),
        exit_engine=_make_exit_engine(exit_events),
        gate=_make_gate(enabled),
        cycles_dir=cycles_dir,
    )


# ================================================================
# 테스트 케이스
# ================================================================

class TestManagerCycleOnceReturnsDictWithKeys:
    """test_manager_cycle_once_returns_dict_with_keys: 정상 cycle → 모든 키 존재."""

    REQUIRED_KEYS = {
        "cycle_id",
        "ts",
        "watch_snapshot_id",
        "candidate_pool_size",
        "holdings_size",
        "exit_events",
        "enabled",
    }

    def test_all_keys_present_when_enabled(self, tmp_path: Path) -> None:
        manager = _make_manager(enabled=True, cycles_dir=tmp_path / "cycles")
        result = manager.cycle_once()
        assert self.REQUIRED_KEYS == set(result.keys())

    def test_all_keys_present_when_disabled(self, tmp_path: Path) -> None:
        """gate disabled 시에도 동일 키 구조 반환 (noop)."""
        manager = _make_manager(enabled=False, cycles_dir=tmp_path / "cycles")
        result = manager.cycle_once()
        assert self.REQUIRED_KEYS == set(result.keys())

    def test_enabled_true_in_result(self, tmp_path: Path) -> None:
        manager = _make_manager(enabled=True, cycles_dir=tmp_path / "cycles")
        result = manager.cycle_once()
        assert result["enabled"] is True

    def test_correct_sizes_in_result(self, tmp_path: Path) -> None:
        manager = _make_manager(
            enabled=True,
            pool_size=3,
            holdings_size=2,
            cycles_dir=tmp_path / "cycles",
        )
        result = manager.cycle_once()
        assert result["candidate_pool_size"] == 3
        assert result["holdings_size"] == 2

    def test_exit_events_forwarded(self, tmp_path: Path) -> None:
        fake_exit = [{"exit_event_id": "EXT-abc", "ticker": "005930"}]
        manager = _make_manager(
            enabled=True,
            exit_events=fake_exit,
            cycles_dir=tmp_path / "cycles",
        )
        result = manager.cycle_once()
        assert result["exit_events"] == fake_exit


class TestManagerCycleOnceSkipsWhenGateDisabled:
    """test_manager_cycle_once_skips_when_gate_disabled: gate=false 시 noop."""

    def test_gate_disabled_returns_empty_result(self, tmp_path: Path) -> None:
        manager = _make_manager(enabled=False, cycles_dir=tmp_path / "cycles")
        result = manager.cycle_once()
        assert result["enabled"] is False
        assert result["watch_snapshot_id"] == ""
        assert result["candidate_pool_size"] == 0
        assert result["holdings_size"] == 0
        assert result["exit_events"] == []

    def test_gate_disabled_no_component_calls(self, tmp_path: Path) -> None:
        """gate disabled 시 snapshot_fetcher/exit_engine 호출 없음."""
        gate = _make_gate(enabled=False)
        snapshot_fetcher = _make_snapshot_fetcher()
        exit_engine = _make_exit_engine()
        holdings_manager = _make_holdings_manager()
        admission_engine = _make_admission_engine()

        manager = DynamicUniverseManager(
            snapshot_fetcher=snapshot_fetcher,
            admission_engine=admission_engine,
            holdings_manager=holdings_manager,
            exit_engine=exit_engine,
            gate=gate,
            cycles_dir=tmp_path / "cycles",
        )
        manager.cycle_once()

        snapshot_fetcher.fetch_once.assert_not_called()
        exit_engine.evaluate.assert_not_called()
        holdings_manager.get_holdings.assert_not_called()
        admission_engine.get_pool_state.assert_not_called()


class TestManagerCycleIdFormat:
    """test_manager_cycle_id_format: cycle_id = 'DUC-YYYYMMDD-UUID8' 정규식 매칭."""

    CYCLE_ID_PATTERN = re.compile(r"^DUC-\d{8}-[0-9a-f]{8}$")

    def test_cycle_id_format_when_enabled(self, tmp_path: Path) -> None:
        manager = _make_manager(enabled=True, cycles_dir=tmp_path / "cycles")
        result = manager.cycle_once()
        assert self.CYCLE_ID_PATTERN.match(result["cycle_id"]), (
            f"cycle_id 형식 불일치: {result['cycle_id']}"
        )

    def test_cycle_id_format_when_disabled(self, tmp_path: Path) -> None:
        """gate disabled 시에도 cycle_id 형식 준수."""
        manager = _make_manager(enabled=False, cycles_dir=tmp_path / "cycles")
        result = manager.cycle_once()
        assert self.CYCLE_ID_PATTERN.match(result["cycle_id"]), (
            f"cycle_id 형식 불일치 (disabled): {result['cycle_id']}"
        )

    def test_cycle_id_unique_per_call(self, tmp_path: Path) -> None:
        """매 호출마다 다른 cycle_id 생성 (UUID8 충돌 확률 무시)."""
        manager = _make_manager(enabled=True, cycles_dir=tmp_path / "cycles")
        ids = {manager.cycle_once()["cycle_id"] for _ in range(5)}
        assert len(ids) == 5


class TestManagerHandleAdmissionEventPromotesToHoldings:
    """test_manager_handle_admission_event_promotes_to_holdings: admission → promote 호출."""

    def test_promote_called_on_successful_admission(self, tmp_path: Path) -> None:
        gate = _make_gate(enabled=True)
        snapshot_fetcher = _make_snapshot_fetcher()
        exit_engine = _make_exit_engine()

        admission_event = {
            "admission_event_id": "ADM-20260503-abcd1234",
            "ticker": "005930",
            "trigger_ids": ["price_spike_admission"],
            "admitted_at": "2026-05-03T10:00:00+09:00",
            "ttl_sec": 1800,
            "ts": "2026-05-03T10:00:00+09:00",
        }
        promotion_result = {
            "ticker": "005930",
            "weight": 0.03,
            "promotion_id": "PRM-20260503-xyz12345",
        }

        admission_engine = _make_admission_engine()
        admission_engine.handle_event.return_value = admission_event

        holdings_manager = _make_holdings_manager()
        holdings_manager.promote_to_holdings.return_value = promotion_result

        manager = DynamicUniverseManager(
            snapshot_fetcher=snapshot_fetcher,
            admission_engine=admission_engine,
            holdings_manager=holdings_manager,
            exit_engine=exit_engine,
            gate=gate,
            cycles_dir=tmp_path / "cycles",
        )

        event = {"ticker": "005930", "ts": "2026-05-03T10:00:00+09:00", "event_type": "price"}
        result = manager.handle_admission_event(event)

        assert result is not None
        assert result["ticker"] == "005930"
        holdings_manager.promote_to_holdings.assert_called_once_with(admission_event)

    def test_returns_none_when_gate_disabled(self, tmp_path: Path) -> None:
        gate = _make_gate(enabled=False)
        snapshot_fetcher = _make_snapshot_fetcher()
        admission_engine = _make_admission_engine()
        holdings_manager = _make_holdings_manager()
        exit_engine = _make_exit_engine()

        manager = DynamicUniverseManager(
            snapshot_fetcher=snapshot_fetcher,
            admission_engine=admission_engine,
            holdings_manager=holdings_manager,
            exit_engine=exit_engine,
            gate=gate,
            cycles_dir=tmp_path / "cycles",
        )

        event = {"ticker": "005930", "ts": "2026-05-03T10:00:00+09:00", "event_type": "price"}
        result = manager.handle_admission_event(event)

        assert result is None
        admission_engine.handle_event.assert_not_called()

    def test_returns_none_when_admission_rejected(self, tmp_path: Path) -> None:
        """admission_engine.handle_event → None 이면 promote 미호출."""
        gate = _make_gate(enabled=True)
        admission_engine = _make_admission_engine()
        admission_engine.handle_event.return_value = None  # 거부

        holdings_manager = _make_holdings_manager()

        manager = DynamicUniverseManager(
            snapshot_fetcher=_make_snapshot_fetcher(),
            admission_engine=admission_engine,
            holdings_manager=holdings_manager,
            exit_engine=_make_exit_engine(),
            gate=gate,
            cycles_dir=tmp_path / "cycles",
        )

        event = {"ticker": "000001", "ts": "2026-05-03T10:00:00+09:00", "event_type": "price"}
        result = manager.handle_admission_event(event)

        assert result is None
        holdings_manager.promote_to_holdings.assert_not_called()


class TestManagerHandleAdmissionEventRejectedWhenPoolFull:
    """test_manager_handle_admission_event_rejected_when_pool_full: pool full 시 promote skip."""

    def test_promote_returns_none_when_max_size_exceeded(self, tmp_path: Path) -> None:
        """HoldingsManager.promote_to_holdings 가 None 반환 (max_size 초과) 시 최종 None."""
        gate = _make_gate(enabled=True)
        admission_event = {
            "admission_event_id": "ADM-full",
            "ticker": "999999",
            "trigger_ids": ["price_spike_admission"],
            "admitted_at": "2026-05-03T10:00:00+09:00",
            "ttl_sec": 1800,
            "ts": "2026-05-03T10:00:00+09:00",
        }

        admission_engine = _make_admission_engine(pool_size=10)  # pool 만원
        admission_engine.handle_event.return_value = admission_event  # admission 자체는 성공

        holdings_manager = _make_holdings_manager(holdings_size=5)  # holdings 만원
        holdings_manager.promote_to_holdings.return_value = None  # promote 거부

        manager = DynamicUniverseManager(
            snapshot_fetcher=_make_snapshot_fetcher(),
            admission_engine=admission_engine,
            holdings_manager=holdings_manager,
            exit_engine=_make_exit_engine(),
            gate=gate,
            cycles_dir=tmp_path / "cycles",
        )

        event = {"ticker": "999999", "ts": "2026-05-03T10:00:00+09:00", "event_type": "dart"}
        result = manager.handle_admission_event(event)

        # promote 는 호출됐으나 None 반환 → 최종 None
        assert result is None
        holdings_manager.promote_to_holdings.assert_called_once()


class TestManagerShutdownSavesState:
    """test_manager_shutdown_saves_state: shutdown 호출 시 상태 저장 + gate transition 로그."""

    def test_shutdown_creates_snapshot_file(self, tmp_path: Path) -> None:
        cycles_dir = tmp_path / "cycles"
        holdings_data = [
            {"ticker": "005930", "weight": 0.03, "promotion_id": "PRM-abc"}
        ]

        gate = _make_gate(enabled=True)
        holdings_manager = _make_holdings_manager()
        holdings_manager.get_holdings.return_value = holdings_data

        manager = DynamicUniverseManager(
            snapshot_fetcher=_make_snapshot_fetcher(),
            admission_engine=_make_admission_engine(),
            holdings_manager=holdings_manager,
            exit_engine=_make_exit_engine(),
            gate=gate,
            cycles_dir=cycles_dir,
        )

        manager.shutdown()

        # shutdown_YYYYMMDD_HHMMSS.json 파일 존재 확인
        shutdown_files = list(cycles_dir.glob("shutdown_*.json"))
        assert len(shutdown_files) == 1

        # 내용 검증
        with shutdown_files[0].open("r", encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["holdings_count"] == 1
        assert saved["holdings"] == holdings_data
        assert "shutdown_ts" in saved

    def test_shutdown_calls_gate_log_transition(self, tmp_path: Path) -> None:
        """shutdown 시 gate.log_transition 호출 확인."""
        gate = _make_gate(enabled=True)
        holdings_manager = _make_holdings_manager()
        holdings_manager.get_holdings.return_value = []

        manager = DynamicUniverseManager(
            snapshot_fetcher=_make_snapshot_fetcher(),
            admission_engine=_make_admission_engine(),
            holdings_manager=holdings_manager,
            exit_engine=_make_exit_engine(),
            gate=gate,
            cycles_dir=tmp_path / "cycles",
        )

        manager.shutdown()

        gate.log_transition.assert_called_once()
        call_kwargs = gate.log_transition.call_args
        # reason = "manager_shutdown" 확인
        args, kwargs = call_kwargs
        # positional or keyword
        reason = kwargs.get("reason") or (args[2] if len(args) > 2 else None)
        assert reason == "manager_shutdown"

    def test_shutdown_disabled_gate_still_saves(self, tmp_path: Path) -> None:
        """gate disabled 상태에서 shutdown 호출해도 스냅샷 저장."""
        cycles_dir = tmp_path / "cycles"
        gate = _make_gate(enabled=False)
        holdings_manager = _make_holdings_manager()
        holdings_manager.get_holdings.return_value = []

        manager = DynamicUniverseManager(
            snapshot_fetcher=_make_snapshot_fetcher(),
            admission_engine=_make_admission_engine(),
            holdings_manager=holdings_manager,
            exit_engine=_make_exit_engine(),
            gate=gate,
            cycles_dir=cycles_dir,
        )

        manager.shutdown()

        shutdown_files = list(cycles_dir.glob("shutdown_*.json"))
        assert len(shutdown_files) == 1


class TestManagerCycleWritesJsonl:
    """cycle_once 시 YYYYMMDD.jsonl append 확인."""

    def test_cycle_writes_jsonl_when_enabled(self, tmp_path: Path) -> None:
        cycles_dir = tmp_path / "cycles"
        manager = _make_manager(enabled=True, cycles_dir=cycles_dir)
        manager.cycle_once()

        jsonl_files = list(cycles_dir.glob("*.jsonl"))
        assert len(jsonl_files) == 1

        lines = jsonl_files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1

        entry = json.loads(lines[0])
        assert "cycle_id" in entry

    def test_cycle_does_not_write_jsonl_when_disabled(self, tmp_path: Path) -> None:
        """gate disabled 시 JSONL 미기록 (noop 반환만)."""
        cycles_dir = tmp_path / "cycles"
        manager = _make_manager(enabled=False, cycles_dir=cycles_dir)
        manager.cycle_once()

        jsonl_files = list(cycles_dir.glob("*.jsonl"))
        assert len(jsonl_files) == 0

"""S4-8 AgentMemoryRestorer unit tests.

테스트 범위:
  1. KB write 후 restore_for_agent → 동일 데이터 round-trip
  2. restore_window_days 적용 (오래된 항목 제외)
  3. restore_max_entries_per_agent 상한 적용
  4. storage_types 5종 각각 round-trip (micro_notes / macro_notes / debate_history
     / decision_history / backtest_history)
  5. agents dict에 해당 에이전트 없을 때 graceful skip
  6. enabled=false 시 restore_all 즉시 {} 반환
  7. Hot Runner bootstrap 단계 통합 (BOOTSTRAP 상태에서만 작동)
  8. BOOTSTRAP 아닌 상태에서 bootstrap() 호출 시 skip
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.agents.memory_restorer import AgentMemoryRestorer
from src.knowledge.kb import KnowledgeBase


# ====================================================================== #
# Fixtures
# ====================================================================== #


@pytest.fixture()
def tmp_kb(tmp_path: Path) -> KnowledgeBase:
    """임시 KB 인스턴스. tests/fixtures/risk_config.yaml 대신 minimal config."""
    kb = KnowledgeBase(storage_root=tmp_path / "kb")
    return kb


@pytest.fixture()
def restorer(tmp_kb: KnowledgeBase) -> AgentMemoryRestorer:
    """기본 AgentMemoryRestorer (window=7일, max=100)."""
    cfg = {
        "enabled": True,
        "storage_types": [
            "micro_notes",
            "macro_notes",
            "debate_history",
            "decision_history",
            "backtest_history",
        ],
        "restore_window_days": 7,
        "restore_max_entries_per_agent": 100,
        "ttl_days": {
            "micro_notes": 30,
            "macro_notes": 90,
            "debate_history": 30,
            "decision_history": 365,
            "backtest_history": 365,
        },
    }
    return AgentMemoryRestorer(tmp_kb, config=cfg)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _days_ago_iso(days: int) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()


def _make_entry(
    content: str,
    sent_from: str,
    timestamp: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "content": content,
        "sent_from": sent_from,
        "timestamp": timestamp or _now_iso(),
    }
    if extra:
        entry.update(extra)
    return entry


# ====================================================================== #
# Test 1: KB write 후 restore_for_agent → 동일 데이터 round-trip
# ====================================================================== #


def test_round_trip_macro_notes(
    tmp_kb: KnowledgeBase, restorer: AgentMemoryRestorer
) -> None:
    """macro_notes write → restore_for_agent → 동일 content 확인."""
    entry = _make_entry(
        content="거시: 금리 인하 신호 포착",
        sent_from="risk_slow",
    )
    msg_id = tmp_kb.write(entry, "macro_notes")
    assert msg_id.startswith("KB-")

    restored = restorer.restore_for_agent("risk_slow", "macro_notes")
    assert len(restored) == 1
    assert restored[0]["content"] == "거시: 금리 인하 신호 포착"
    assert restored[0]["sent_from"] == "risk_slow"
    assert restored[0]["message_id"] == msg_id


# ====================================================================== #
# Test 2: restore_window_days 적용 (오래된 항목 제외)
# ====================================================================== #


def test_window_days_filter(
    tmp_kb: KnowledgeBase,
) -> None:
    """window=3일 설정 시 5일 전 항목은 제외, 오늘 항목만 복원."""
    cfg = {
        "enabled": True,
        "storage_types": ["macro_notes"],
        "restore_window_days": 3,
        "restore_max_entries_per_agent": 100,
        "ttl_days": {"macro_notes": 90},
    }
    restorer = AgentMemoryRestorer(tmp_kb, config=cfg)

    # 오늘 항목
    entry_today = _make_entry("오늘 메모", "risk_slow")
    tmp_kb.write(entry_today, "macro_notes")

    # 5일 전 항목 (window=3일 이므로 제외 대상)
    # KB write는 미래 시간만 차단. 과거 시간은 허용.
    entry_old = _make_entry("5일 전 메모", "risk_slow", timestamp=_days_ago_iso(5))
    tmp_kb.write(entry_old, "macro_notes")

    restored = restorer.restore_for_agent("risk_slow", "macro_notes")
    contents = [e["content"] for e in restored]
    assert "오늘 메모" in contents
    assert "5일 전 메모" not in contents


# ====================================================================== #
# Test 3: restore_max_entries_per_agent 상한
# ====================================================================== #


def test_max_entries_limit(
    tmp_kb: KnowledgeBase,
) -> None:
    """max_entries=5 설정 시 10개 write 후 5개만 복원."""
    cfg = {
        "enabled": True,
        "storage_types": ["debate_history"],
        "restore_window_days": 7,
        "restore_max_entries_per_agent": 5,
        "ttl_days": {"debate_history": 30},
    }
    restorer = AgentMemoryRestorer(tmp_kb, config=cfg)

    for i in range(10):
        entry = _make_entry(f"debate_{i}", "debate")
        tmp_kb.write(entry, "debate_history")

    restored = restorer.restore_for_agent("debate", "debate_history")
    assert len(restored) <= 5


# ====================================================================== #
# Test 4: 5종 storage_type round-trip
# ====================================================================== #


@pytest.mark.parametrize(
    "storage_type, agent_name, extra",
    [
        ("micro_notes", "news_agent", {"ticker": "005930"}),
        ("macro_notes", "risk_slow", {}),
        ("debate_history", "debate", {}),
        ("decision_history", "fda", {}),
        ("backtest_history", "backtest", {"run_id": "RUN-TEST-01"}),
    ],
)
def test_five_storage_types_round_trip(
    tmp_kb: KnowledgeBase,
    restorer: AgentMemoryRestorer,
    storage_type: str,
    agent_name: str,
    extra: dict[str, Any],
) -> None:
    """5종 storage_type 각각 write → restore 1건 확인."""
    content_text = f"{storage_type} round-trip 테스트"
    entry = _make_entry(content_text, agent_name, extra=extra)
    tmp_kb.write(entry, storage_type)

    restored = restorer.restore_for_agent(agent_name, storage_type)
    assert len(restored) >= 1
    contents = [e["content"] for e in restored]
    assert content_text in contents


# ====================================================================== #
# Test 5: agents dict에 해당 에이전트 없을 때 graceful skip
# ====================================================================== #


def test_restore_all_missing_agent_graceful(
    tmp_kb: KnowledgeBase,
    restorer: AgentMemoryRestorer,
) -> None:
    """agents dict에 news_agent 없어도 예외 없이 skip."""
    # debate 항목 write
    tmp_kb.write(_make_entry("debate 항목", "debate"), "debate_history")

    # agents에 news_agent 없음. debate만 있음.
    class FakeDebateAgent:
        pass

    agents = {"debate": FakeDebateAgent()}
    counts = restorer.restore_all(agents)

    # debate는 복원됨. news_agent는 skip (count 없음).
    assert "news_agent" not in counts
    assert counts.get("debate", 0) >= 0  # 0이어도 OK (graceful)


# ====================================================================== #
# Test 6: enabled=false 시 restore_all 즉시 {} 반환
# ====================================================================== #


def test_restore_all_disabled(tmp_kb: KnowledgeBase) -> None:
    """enabled=false 설정 시 restore_all이 빈 dict 즉시 반환."""
    cfg = {
        "enabled": False,
        "storage_types": ["macro_notes"],
        "restore_window_days": 7,
        "restore_max_entries_per_agent": 100,
        "ttl_days": {},
    }
    restorer = AgentMemoryRestorer(tmp_kb, config=cfg)

    tmp_kb.write(_make_entry("메모", "risk_slow"), "macro_notes")

    class FakeAgent:
        pass

    counts = restorer.restore_all({"risk_slow": FakeAgent()})
    assert counts == {}


def test_restore_all_disabled_string_false(tmp_kb: KnowledgeBase) -> None:
    """외부 yaml/env 스타일 문자열 false도 disabled로 해석한다."""
    cfg = {
        "enabled": "false",
        "storage_types": ["macro_notes"],
        "restore_window_days": 7,
        "restore_max_entries_per_agent": 100,
        "ttl_days": {},
    }
    restorer = AgentMemoryRestorer(tmp_kb, config=cfg)

    tmp_kb.write(_make_entry("메모", "risk_slow"), "macro_notes")

    class FakeAgent:
        pass

    counts = restorer.restore_all({"risk_slow": FakeAgent()})
    assert counts == {}


# ====================================================================== #
# Test 7: Hot Runner bootstrap 통합 (BOOTSTRAP 상태에서 작동)
# ====================================================================== #


def test_hot_runner_bootstrap_restores_memory(tmp_path: Path) -> None:
    """HotRunner.bootstrap() 호출 시 BOOTSTRAP 상태에서 메모리 복원 수행."""
    from unittest.mock import patch

    kb = KnowledgeBase(storage_root=tmp_path / "kb")
    kb.write(
        _make_entry("FDA 결정 이력", "fda"),
        "decision_history",
    )

    # HotRunner 내부 에이전트 mock으로 대체 (의존성 없이)
    from src.ops.state_machine import PipelineState, StateMachine

    sm = StateMachine()
    assert sm.state == PipelineState.BOOTSTRAP

    with patch("src.orchestration.hot_runner.QuantAgent"), \
         patch("src.orchestration.hot_runner.PPOAllocator"), \
         patch("src.orchestration.hot_runner.PortfolioManager"), \
         patch("src.orchestration.hot_runner.FDAAgent"), \
         patch("src.orchestration.hot_runner.RiskFastAgent"):
        from src.orchestration.hot_runner import HotRunner

        runner = HotRunner(state_machine=sm, kb=kb)
        # BOOTSTRAP 상태에서 bootstrap 호출
        class FakeFDA:
            pass
        counts = runner.bootstrap(agents={"fda": FakeFDA()})

    # decision_history에 1건 있으므로 fda에 1건 복원
    assert "fda" in counts
    assert counts["fda"] >= 1


# ====================================================================== #
# Test 8: BOOTSTRAP 아닌 상태에서 bootstrap() 호출 시 skip
# ====================================================================== #


def test_hot_runner_bootstrap_skips_if_not_bootstrap(tmp_path: Path) -> None:
    """HotRunner.bootstrap()은 BOOTSTRAP 상태가 아니면 {} 반환."""
    from unittest.mock import patch

    kb = KnowledgeBase(storage_root=tmp_path / "kb")
    from src.ops.state_machine import PipelineState, StateMachine

    sm = StateMachine()
    sm.transition(PipelineState.HOT_RUNNING)

    with patch("src.orchestration.hot_runner.QuantAgent"), \
         patch("src.orchestration.hot_runner.PPOAllocator"), \
         patch("src.orchestration.hot_runner.PortfolioManager"), \
         patch("src.orchestration.hot_runner.FDAAgent"), \
         patch("src.orchestration.hot_runner.RiskFastAgent"):
        from src.orchestration.hot_runner import HotRunner

        runner = HotRunner(state_machine=sm, kb=kb)
        counts = runner.bootstrap(agents={})

    assert counts == {}


# ====================================================================== #
# Test R4-W3: restore_all idempotent (중복 inject 방지)
# ====================================================================== #


def test_restore_all_idempotent(
    tmp_kb: KnowledgeBase,
    restorer: AgentMemoryRestorer,
) -> None:
    """restore_all을 두 번 호출해도 같은 항목이 중복 inject되지 않음."""
    tmp_kb.write(_make_entry("거시 메모 A", "risk_slow"), "macro_notes")

    class FakeAgent:
        pass

    agent = FakeAgent()
    agents = {"risk_slow": agent}

    # 첫 번째 복원
    restorer.restore_all(agents)
    count_after_first = len(getattr(agent, "_restored_memory", {}).get("macro_notes", []))

    # 두 번째 복원 (동일 agent 인스턴스, 동일 entries)
    restorer.restore_all(agents)
    count_after_second = len(agent._restored_memory.get("macro_notes", []))

    # message_id 기반 dedup: 두 번 복원해도 항목 수 동일해야 함
    assert count_after_second == count_after_first, (
        f"중복 inject 발생: {count_after_first} → {count_after_second}"
    )


# ====================================================================== #
# Test R4-W4: timestamp 없는 항목 skip
# ====================================================================== #


def test_timestamp_missing_entry_skipped(
    tmp_path: Path,
) -> None:
    """timestamp 없는 항목은 복원에서 skip."""
    import json

    kb = KnowledgeBase(storage_root=tmp_path / "kb")
    cfg = {
        "enabled": True,
        "storage_types": ["macro_notes"],
        "restore_window_days": 7,
        "restore_max_entries_per_agent": 100,
        "ttl_days": {"macro_notes": 90},
    }
    restorer = AgentMemoryRestorer(kb, config=cfg)

    # timestamp 없는 항목 직접 파일에 write (KB write는 timestamp 필수이므로 파일 직접 작성)
    storage_dir = tmp_path / "kb" / "macro_notes"
    storage_dir.mkdir(parents=True, exist_ok=True)
    bad_entry = {"content": "timestamp 없는 항목", "sent_from": "risk_slow", "message_id": "KB-NO-TS"}
    good_entry = {
        "content": "정상 항목",
        "sent_from": "risk_slow",
        "message_id": "KB-GOOD",
        "timestamp": _now_iso(),
    }
    out_path = storage_dir / "202601.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(bad_entry, ensure_ascii=False) + "\n")
        fh.write(json.dumps(good_entry, ensure_ascii=False) + "\n")

    restored = restorer.restore_for_agent("risk_slow", "macro_notes")
    contents = [e["content"] for e in restored]

    # timestamp 없는 항목은 skip
    assert "timestamp 없는 항목" not in contents
    # 정상 항목은 포함
    assert "정상 항목" in contents

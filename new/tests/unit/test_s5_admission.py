"""Sprint 5 S5-2 AdmissionEngine 단위 테스트.

C15 DynamicUniverseContract candidate_pool 편입 엔진 검증.

테스트 케이스:
  1. test_admission_engine_admits_on_price_spike
  2. test_admission_engine_admits_on_dart_hot_ticker
  3. test_admission_engine_rejects_non_watch_ticker (trade_universe ticker)
  4. test_admission_engine_rejects_when_pool_full
  5. test_admission_engine_rejects_during_cooldown
  6. test_admission_engine_pit_safety_violation
  7. test_admission_engine_writes_admission_event_jsonl
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import yaml

_KST = ZoneInfo("Asia/Seoul")


# ================================================================
# 헬퍼: yaml fixture 생성
# ================================================================

def _make_dynamic_cfg(
    min_trigger_count: int = 1,
    cooldown_sec: int = 300,
    candidate_pool_max: int = 10,
    ttl_sec: int = 1800,
) -> dict:
    return {
        "version": "0.1.0",
        "admission": {
            "min_trigger_count": min_trigger_count,
            "cooldown_sec": cooldown_sec,
            "candidate_pool_max": candidate_pool_max,
        },
        "holdings": {
            "max_size": 5,
            "per_stock_max_weight": 0.03,
            "total_max_weight": 0.10,
            "allocator_mode": "fixed_rule_only",
        },
        "exit": {
            "ttl_sec": ttl_sec,
            "stop_loss_pct": 0.02,
        },
        "snapshot_cache": {"ttl_sec": 55, "max_entries": 1000},
    }


def _make_universe_yaml(active_tickers: list[str]) -> dict:
    """trade_universe (active 20) yaml 모사."""
    stocks = [{"ticker": t, "name": f"종목{t}", "status": "active"} for t in active_tickers]
    return {
        "sectors": {
            "반도체": {"status": "confirmed", "stocks": stocks},
        }
    }


def _make_watch_yaml(watch_tickers: list[str]) -> dict:
    """watch_universe_kospi200.yaml 모사."""
    return {
        "watch_rules": {"exclude_trade_universe": True},
        "tickers": [{"ticker": t, "name": f"감시{t}"} for t in watch_tickers],
    }


def _make_price_spike_event(ticker: str, return_pct: float = 0.06) -> dict:
    """price_spike_admission 조건 만족 이벤트."""
    return {
        "event_type": "price",
        "ticker": ticker,
        "ts": datetime.now(_KST).isoformat(),
        "payload": {"return_pct": return_pct},
    }


def _make_dart_event(ticker: str, priority: str = "urgent") -> dict:
    """dart_hot_ticker_admission 조건 만족 이벤트."""
    return {
        "event_type": "dart",
        "ticker": ticker,
        "ts": datetime.now(_KST).isoformat(),
        "priority": priority,
        "payload": {"title": "중요 공시"},
    }


def _make_admission_rules() -> list[dict]:
    """admit_candidate action rule 2개 mock."""
    return [
        {
            "id": "price_spike_admission",
            "condition": "watch_universe ticker 1min return > +5% OR < -5%",
            "action": "admit_candidate",
            "risk_level": "medium",
            "stance": "opportunity",
        },
        {
            "id": "dart_hot_ticker_admission",
            "condition": "dart 공시 + watch universe ticker AND dart_rules.yaml material 매칭",
            "action": "admit_candidate",
            "risk_level": "medium",
            "stance": "opportunity",
        },
    ]


def _make_engine(
    tmp_path: Path,
    active_tickers: list[str] | None = None,
    watch_tickers: list[str] | None = None,
    min_trigger_count: int = 1,
    cooldown_sec: int = 300,
    candidate_pool_max: int = 10,
) -> "AdmissionEngine":
    """테스트용 AdmissionEngine 생성 헬퍼."""
    from src.dynamic_universe.admission_engine import AdmissionEngine

    if active_tickers is None:
        active_tickers = [str(i).zfill(6) for i in range(1, 21)]  # active 20종목
    if watch_tickers is None:
        watch_tickers = [str(i).zfill(6) for i in range(1, 1000)]

    dynamic_cfg_path = tmp_path / "dynamic_universe_config.yaml"
    universe_yaml_path = tmp_path / "universe_config.yaml"
    watch_yaml_path = tmp_path / "watch_universe_kospi200.yaml"
    artifacts_dir = tmp_path / "dynamic_holdings"

    with dynamic_cfg_path.open("w", encoding="utf-8") as fh:
        yaml.dump(
            _make_dynamic_cfg(
                min_trigger_count=min_trigger_count,
                cooldown_sec=cooldown_sec,
                candidate_pool_max=candidate_pool_max,
            ),
            fh,
        )
    with universe_yaml_path.open("w", encoding="utf-8") as fh:
        yaml.dump(_make_universe_yaml(active_tickers), fh)
    with watch_yaml_path.open("w", encoding="utf-8") as fh:
        yaml.dump(_make_watch_yaml(watch_tickers), fh)

    with patch(
        "src.utils.trigger_loader.config_load",
        return_value={"rules": _make_admission_rules()},
    ) as mock_cfg, patch(
        "src.dynamic_universe.admission_engine.load_trigger_rules",
        return_value=_make_admission_rules(),
    ):
        engine = AdmissionEngine(
            dynamic_config_path=dynamic_cfg_path,
            trade_universe_path=universe_yaml_path,
            watch_universe_path=watch_yaml_path,
            artifacts_dir=artifacts_dir,
        )

    return engine


# ================================================================
# 1. price_spike_admission rule 매칭 → admit
# ================================================================

def test_admission_engine_admits_on_price_spike(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """return_pct=+6% 이벤트 → admission_event 반환."""
    monkeypatch.setenv("ELEPHANT_TEST_PIT_SKIP", "true")

    # watch universe 외 ticker (000200 — trade_universe 에 없는 종목)
    watch_ticker = "000200"
    engine = _make_engine(tmp_path)

    with patch(
        "src.dynamic_universe.admission_engine.load_trigger_rules",
        return_value=_make_admission_rules(),
    ):
        engine._admission_rules = _make_admission_rules()

    event = _make_price_spike_event(watch_ticker, return_pct=0.06)
    result = engine.handle_event(event)

    assert result is not None, "price_spike 이벤트 → admit 해야 함"
    assert result["ticker"] == watch_ticker.zfill(6)
    assert result["admission_event_id"].startswith("ADM-")
    assert "price_spike_admission" in result["trigger_ids"]
    assert "ttl_sec" in result
    assert "admitted_at" in result


# ================================================================
# 2. dart_hot_ticker_admission 매칭 → admit
# ================================================================

def test_admission_engine_admits_on_dart_hot_ticker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DART urgent 이벤트 → admission_event 반환."""
    monkeypatch.setenv("ELEPHANT_TEST_PIT_SKIP", "true")

    watch_ticker = "000300"
    engine = _make_engine(tmp_path)
    engine._admission_rules = _make_admission_rules()

    event = _make_dart_event(watch_ticker, priority="urgent")
    result = engine.handle_event(event)

    assert result is not None, "dart urgent 이벤트 → admit 해야 함"
    assert result["ticker"] == watch_ticker.zfill(6)
    assert "dart_hot_ticker_admission" in result["trigger_ids"]


# ================================================================
# 3. trade_universe ticker → reject (Cold Path 처리 대상)
# ================================================================

def test_admission_engine_rejects_non_watch_ticker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """active 20종목 ticker → AdmissionEngine 거부 (trade_universe 이므로)."""
    monkeypatch.setenv("ELEPHANT_TEST_PIT_SKIP", "true")

    # trade_universe active 20 중 하나
    trade_ticker = "000001"
    active_tickers = [str(i).zfill(6) for i in range(1, 21)]
    engine = _make_engine(tmp_path, active_tickers=active_tickers)
    engine._admission_rules = _make_admission_rules()

    event = _make_price_spike_event(trade_ticker, return_pct=0.06)
    result = engine.handle_event(event)

    assert result is None, "trade_universe ticker 는 reject 해야 함"


def test_admission_engine_rejects_outside_watch_universe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KOSPI200 watch universe 외 종목은 candidate_pool 편입 금지."""
    monkeypatch.setenv("ELEPHANT_TEST_PIT_SKIP", "true")

    engine = _make_engine(
        tmp_path,
        active_tickers=["000001", "000002"],
        watch_tickers=["000100", "000200"],
    )
    engine._admission_rules = _make_admission_rules()

    event = _make_price_spike_event("999999", return_pct=0.06)
    result = engine.handle_event(event)

    assert result is None, "watch_universe 외 ticker 는 reject 해야 함"


def test_admission_engine_admits_c2_price_snapshot_scope_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2 price_snapshot scope/payload bridge가 C15 admission까지 이어져야 한다."""
    monkeypatch.setenv("ELEPHANT_TEST_PIT_SKIP", "true")

    engine = _make_engine(
        tmp_path,
        active_tickers=["000001", "000002"],
        watch_tickers=["000200", "000300"],
    )
    engine._admission_rules = _make_admission_rules()

    event = {
        "event_type": "price_snapshot",
        "scope": "ticker:000200",
        "occurred_at": datetime.now(_KST).isoformat(),
        "payload": {"day_change_pct": 0.061},
    }
    result = engine.handle_event(event)

    assert result is not None
    assert result["ticker"] == "000200"
    assert "price_spike_admission" in result["trigger_ids"]


def test_admission_engine_admits_market_snapshot_strongest_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """market-scoped C16 snapshots에서 가장 큰 변동 후보를 추출해 admit한다."""
    monkeypatch.setenv("ELEPHANT_TEST_PIT_SKIP", "true")

    engine = _make_engine(
        tmp_path,
        active_tickers=["000001", "000002"],
        watch_tickers=["000200", "000300"],
    )
    engine._admission_rules = _make_admission_rules()

    event = {
        "event_type": "price_snapshot",
        "scope": "market",
        "occurred_at": datetime.now(_KST).isoformat(),
        "payload": {
            "watch_snapshot_id": "WS-20260509-AABBCCDD",
            "snapshots": [
                {"ticker": "000200", "day_change_pct": 0.02},
                {"ticker": "000300", "day_change_pct": -0.07},
            ],
        },
    }
    result = engine.handle_event(event)

    assert result is not None
    assert result["ticker"] == "000300"
    assert "price_spike_admission" in result["trigger_ids"]


# ================================================================
# 4. candidate_pool 10/10 → 11번째 admit reject
# ================================================================

def test_admission_engine_rejects_when_pool_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """candidate_pool 10/10 만원 시 11번째 ticker admit 거부."""
    monkeypatch.setenv("ELEPHANT_TEST_PIT_SKIP", "true")

    engine = _make_engine(tmp_path, candidate_pool_max=10)
    engine._admission_rules = _make_admission_rules()

    # pool 10개 강제 채우기 (999900~999909)
    for i in range(10):
        fake_ticker = f"9999{i:02d}"
        engine._candidate_pool[fake_ticker] = {
            "admission_event_id": f"ADM-fake-{i}",
            "ticker": fake_ticker,
        }

    assert len(engine._candidate_pool) == 10

    # 11번째 admit 시도
    watch_ticker = "000400"
    event = _make_price_spike_event(watch_ticker, return_pct=0.07)
    result = engine.handle_event(event)

    assert result is None, "pool 만원 시 admit 거부해야 함"


# ================================================================
# 5. cooldown 미경과 → 재진입 reject
# ================================================================

def test_admission_engine_rejects_during_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """청산 후 cooldown_sec 내 재진입 → reject."""
    monkeypatch.setenv("ELEPHANT_TEST_PIT_SKIP", "true")

    cooldown_sec = 300
    watch_ticker = "000500"
    engine = _make_engine(tmp_path, cooldown_sec=cooldown_sec)
    engine._admission_rules = _make_admission_rules()

    # 방금 청산된 것처럼 cooldown_map 에 기록 (30초 전에 청산)
    recently_exited = datetime.now(_KST) - timedelta(seconds=30)
    engine._cooldown_map[watch_ticker.zfill(6)] = recently_exited

    event = _make_price_spike_event(watch_ticker, return_pct=0.06)
    result = engine.handle_event(event)

    assert result is None, "cooldown 기간 중 재진입 → reject 해야 함"


# ================================================================
# 6. 미래 ts → PITViolationError
# ================================================================

def test_admission_engine_pit_safety_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """미래 ts 이벤트 → PITViolationError 발생."""
    monkeypatch.delenv("ELEPHANT_TEST_PIT_SKIP", raising=False)

    from src.utils.pit_guard import PITViolationError

    watch_ticker = "000600"
    engine = _make_engine(tmp_path)
    engine._admission_rules = _make_admission_rules()

    # is_pit_safe 를 False 로 패치 → 미래 ts 강제
    with patch(
        "src.dynamic_universe.admission_engine.is_pit_safe", return_value=False
    ):
        event = {
            "event_type": "price",
            "ticker": watch_ticker,
            "ts": (datetime.now(_KST) + timedelta(hours=5)).isoformat(),
            "payload": {"return_pct": 0.06},
        }
        with pytest.raises(PITViolationError):
            engine.handle_event(event)


# ================================================================
# 7. admission_events.jsonl append 검증
# ================================================================

def test_admission_engine_writes_admission_event_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """admit 성공 시 artifacts/dynamic_holdings/admission_events.jsonl 에 기록."""
    monkeypatch.setenv("ELEPHANT_TEST_PIT_SKIP", "true")

    watch_ticker = "000700"
    artifacts_dir = tmp_path / "dynamic_holdings"

    dynamic_cfg_path = tmp_path / "dynamic_universe_config.yaml"
    universe_yaml_path = tmp_path / "universe_config.yaml"
    watch_yaml_path = tmp_path / "watch_universe_kospi200.yaml"

    active_tickers = [str(i).zfill(6) for i in range(1, 21)]
    with dynamic_cfg_path.open("w", encoding="utf-8") as fh:
        yaml.dump(_make_dynamic_cfg(), fh)
    with universe_yaml_path.open("w", encoding="utf-8") as fh:
        yaml.dump(_make_universe_yaml(active_tickers), fh)
    with watch_yaml_path.open("w", encoding="utf-8") as fh:
        yaml.dump(_make_watch_yaml([watch_ticker]), fh)

    from src.dynamic_universe.admission_engine import AdmissionEngine

    with patch(
        "src.dynamic_universe.admission_engine.load_trigger_rules",
        return_value=_make_admission_rules(),
    ):
        engine = AdmissionEngine(
            dynamic_config_path=dynamic_cfg_path,
            trade_universe_path=universe_yaml_path,
            watch_universe_path=watch_yaml_path,
            artifacts_dir=artifacts_dir,
        )

    event = _make_price_spike_event(watch_ticker, return_pct=0.08)
    result = engine.handle_event(event)

    assert result is not None, "admit 성공해야 함"

    jsonl_path = artifacts_dir / "admission_events.jsonl"
    assert jsonl_path.exists(), "admission_events.jsonl 파일 존재해야 함"

    with jsonl_path.open("r", encoding="utf-8") as fh:
        line = fh.readline().strip()

    parsed = json.loads(line)
    assert parsed["ticker"] == watch_ticker.zfill(6)
    assert parsed["admission_event_id"].startswith("ADM-")
    assert "trigger_ids" in parsed
    assert "ttl_sec" in parsed
    assert "admitted_at" in parsed

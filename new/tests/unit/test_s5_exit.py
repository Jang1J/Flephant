"""Sprint 5 S5-3 ExitEngine 단위 테스트.

C15 DynamicUniverseContract 청산 4조건 검증:
  market_close / ttl_expiry / stop_loss / spike_resolved

테스트 케이스:
  1. test_exit_market_close_after_1530_kst
  2. test_exit_market_close_before_1530_skip
  3. test_exit_ttl_expiry
  4. test_exit_ttl_not_expired_skip
  5. test_exit_stop_loss_triggered
  6. test_exit_stop_loss_within_threshold_skip
  7. test_exit_spike_resolved
  8. test_exit_spike_resolved_too_early_skip
  9. test_exit_evaluate_calls_holdings_manager_remove
  10. test_exit_evaluate_calls_admission_remove_from_pool
  11. test_exit_kst_timezone_consistency
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import yaml

_KST = ZoneInfo("Asia/Seoul")


# ================================================================
# 헬퍼
# ================================================================

def _make_dynamic_cfg(
    ttl_sec: int = 1800,
    stop_loss_pct: float = 0.02,
    market_close_kst: str = "15:30",
    community_zscore_threshold: float = 1.5,
    price_change_threshold: float = 0.015,
    min_holding_sec: int = 600,
) -> dict:
    """테스트용 dynamic_universe_config.yaml 내용."""
    return {
        "version": "0.1.0",
        "admission": {
            "min_trigger_count": 1,
            "cooldown_sec": 300,
            "candidate_pool_max": 10,
        },
        "holdings": {
            "max_size": 5,
            "per_stock_max_weight": 0.03,
            "total_max_weight": 0.10,
            "allocator_mode": "fixed_rule_only",
        },
        "exit": {
            "ttl_sec": ttl_sec,
            "stop_loss_pct": stop_loss_pct,
            "market_close_kst": market_close_kst,
            "spike_resolved": {
                "community_zscore_threshold": community_zscore_threshold,
                "price_change_threshold": price_change_threshold,
                "min_holding_sec": min_holding_sec,
            },
        },
        "snapshot_cache": {"ttl_sec": 55, "max_entries": 1000},
    }


def _make_engine(
    tmp_path: Path,
    holdings_manager=None,
    admission_engine=None,
    **cfg_overrides,
) -> object:
    """테스트용 ExitEngine 생성 헬퍼.

    holdings_manager / admission_engine 이 None 이면 MagicMock 사용.
    """
    from src.dynamic_universe.exit_engine import ExitEngine

    dynamic_cfg_path = tmp_path / "dynamic_universe_config.yaml"
    with dynamic_cfg_path.open("w", encoding="utf-8") as fh:
        yaml.dump(_make_dynamic_cfg(**cfg_overrides), fh)

    if holdings_manager is None:
        holdings_manager = MagicMock()
        holdings_manager.get_holdings.return_value = []

    if admission_engine is None:
        admission_engine = MagicMock()
        admission_engine.remove_from_pool.return_value = True

    return ExitEngine(
        holdings_manager=holdings_manager,
        admission_engine=admission_engine,
        dynamic_config_path=dynamic_cfg_path,
    )


def _kst(h: int, m: int, s: int = 0) -> datetime:
    """KST aware datetime (오늘 날짜 기준)."""
    today = datetime.now(_KST).date()
    return datetime(today.year, today.month, today.day, h, m, s, tzinfo=_KST)


def _holdings_entry(
    ticker: str,
    admitted_offset_sec: int = 0,
    entry_price: float = 10000.0,
    base_now: datetime | None = None,
) -> dict:
    """테스트용 holdings entry dict.

    admitted_offset_sec: 양수 → 과거(N초 전), 음수 → 미래(N초 후).
    base_now: admitted_at 계산 기준 시각. None 이면 datetime.now(_KST).
              evaluate 에 전달할 now_kst 와 동일한 값을 사용해야
              TTL/spike 판단이 올바르게 동작한다.
    """
    ref = base_now if base_now is not None else datetime.now(_KST)
    admitted_at = ref - timedelta(seconds=admitted_offset_sec)
    return {
        "ticker": str(ticker).zfill(6),
        "weight": 0.03,
        "admitted_at": admitted_at.isoformat(),
        "entry_price": entry_price,
        "exit_policy": {
            "ttl_sec": 1800,
            "stop_loss_pct": 0.02,
            "spike_resolved": True,
            "market_close": True,
        },
        "promotion_id": f"PRM-test-{ticker}",
    }


# ================================================================
# 1. market_close: 15:30:01 KST → exit
# ================================================================

def test_exit_market_close_after_1530_kst(tmp_path: Path) -> None:
    """now_kst = 15:30:01 → market_close exit 발생."""
    holdings_mgr = MagicMock()
    holdings_mgr.get_holdings.return_value = [_holdings_entry("000200")]
    exit_ev = {
        "exit_event_id": "EXT-test-001",
        "ticker": "000200",
        "exit_reason": "market_close",
        "exit_ts": "2026-05-03T15:30:01+09:00",
        "weight_freed": 0.03,
    }
    holdings_mgr.remove.return_value = exit_ev

    admission_mgr = MagicMock()
    admission_mgr.remove_from_pool.return_value = True

    engine = _make_engine(tmp_path, holdings_manager=holdings_mgr, admission_engine=admission_mgr)

    now = _kst(15, 30, 1)
    result = engine.evaluate({}, trigger_state=None, now_kst=now)

    assert len(result) == 1
    holdings_mgr.remove.assert_called_once_with("000200", "market_close")
    admission_mgr.remove_from_pool.assert_called_once_with("000200")


# ================================================================
# 2. market_close: 15:29:59 KST → no exit
# ================================================================

def test_exit_market_close_before_1530_skip(tmp_path: Path) -> None:
    """now_kst = 15:29:59 → market_close 미발동."""
    now = _kst(15, 29, 59)
    holdings_mgr = MagicMock()
    # admitted_offset_sec=60: 1분 전 admitted, ttl_sec=1800 → ttl 미만
    holdings_mgr.get_holdings.return_value = [
        _holdings_entry("000200", admitted_offset_sec=60, base_now=now)
    ]

    engine = _make_engine(tmp_path, holdings_manager=holdings_mgr)

    result = engine.evaluate({}, trigger_state=None, now_kst=now)

    assert result == []
    holdings_mgr.remove.assert_not_called()


# ================================================================
# 3. ttl_expiry: admitted_at + ttl_sec 경과 → exit
# ================================================================

def test_exit_ttl_expiry(tmp_path: Path) -> None:
    """admitted_at + 1801s 경과 (ttl_sec=1800) → ttl_expiry exit."""
    now = _kst(9, 0, 0)
    holdings_mgr = MagicMock()
    # ttl_sec=1800. now 기준 1801초 전에 admitted
    entry = _holdings_entry("000201", admitted_offset_sec=1801, entry_price=10000.0, base_now=now)
    holdings_mgr.get_holdings.return_value = [entry]
    exit_ev = {
        "exit_event_id": "EXT-test-002",
        "ticker": "000201",
        "exit_reason": "ttl_expiry",
        "exit_ts": datetime.now(_KST).isoformat(),
        "weight_freed": 0.03,
    }
    holdings_mgr.remove.return_value = exit_ev

    admission_mgr = MagicMock()
    admission_mgr.remove_from_pool.return_value = True

    engine = _make_engine(tmp_path, holdings_manager=holdings_mgr, admission_engine=admission_mgr)

    # market_close 미발동 시각 (9:00 KST)
    now = _kst(9, 0, 0)
    result = engine.evaluate(
        {"000201": {"last_price": 10000.0, "day_change_pct": 0.0}},
        trigger_state=None,
        now_kst=now,
    )

    assert len(result) == 1
    holdings_mgr.remove.assert_called_once_with("000201", "ttl_expiry")


# ================================================================
# 4. ttl_not_expired: ttl_sec - 1s 미경과 → no exit
# ================================================================

def test_exit_ttl_not_expired_skip(tmp_path: Path) -> None:
    """admitted_at + 1799s 미경과 (ttl_sec=1800) → ttl_expiry 미발동."""
    now = _kst(9, 0, 0)
    holdings_mgr = MagicMock()
    entry = _holdings_entry("000202", admitted_offset_sec=1799, base_now=now)
    holdings_mgr.get_holdings.return_value = [entry]

    engine = _make_engine(tmp_path, holdings_manager=holdings_mgr)

    result = engine.evaluate({}, trigger_state=None, now_kst=now)

    assert result == []
    holdings_mgr.remove.assert_not_called()


# ================================================================
# 5. stop_loss: current_price 대비 -2.5% → exit
# ================================================================

def test_exit_stop_loss_triggered(tmp_path: Path) -> None:
    """entry_price=10000, current_price=9740 (-2.6%) → stop_loss exit."""
    now = _kst(10, 0, 0)
    holdings_mgr = MagicMock()
    entry = _holdings_entry("000203", admitted_offset_sec=60, entry_price=10000.0, base_now=now)
    holdings_mgr.get_holdings.return_value = [entry]
    exit_ev = {
        "exit_event_id": "EXT-test-003",
        "ticker": "000203",
        "exit_reason": "stop_loss",
        "exit_ts": datetime.now(_KST).isoformat(),
        "weight_freed": 0.03,
    }
    holdings_mgr.remove.return_value = exit_ev

    admission_mgr = MagicMock()
    admission_mgr.remove_from_pool.return_value = True

    engine = _make_engine(tmp_path, holdings_manager=holdings_mgr, admission_engine=admission_mgr)

    snapshot = {"000203": {"last_price": 9740.0, "day_change_pct": -0.026}}
    result = engine.evaluate(snapshot, trigger_state=None, now_kst=now)

    assert len(result) == 1
    holdings_mgr.remove.assert_called_once_with("000203", "stop_loss")


# ================================================================
# 6. stop_loss: -1.5% → no exit (threshold=2%)
# ================================================================

def test_exit_stop_loss_within_threshold_skip(tmp_path: Path) -> None:
    """entry_price=10000, current_price=9850 (-1.5%) → stop_loss 미발동 (threshold=2%)."""
    now = _kst(10, 0, 0)
    holdings_mgr = MagicMock()
    entry = _holdings_entry("000204", admitted_offset_sec=60, entry_price=10000.0, base_now=now)
    holdings_mgr.get_holdings.return_value = [entry]

    engine = _make_engine(tmp_path, holdings_manager=holdings_mgr)

    snapshot = {"000204": {"last_price": 9850.0, "day_change_pct": -0.015}}
    result = engine.evaluate(snapshot, trigger_state=None, now_kst=now)

    assert result == []
    holdings_mgr.remove.assert_not_called()


# ================================================================
# 7. spike_resolved: 600s 경과 + zscore < 1.5 + price 변화 < 1.5% → exit
# ================================================================

def test_exit_spike_resolved(tmp_path: Path) -> None:
    """min_holding_sec 경과 + zscore 0.5 + price_change 0.5% → spike_resolved exit."""
    now = _kst(10, 0, 0)
    holdings_mgr = MagicMock()
    # now 기준 700초 전 admitted (min_holding_sec=600 경과). ttl_sec=1800 이내.
    entry = _holdings_entry("000205", admitted_offset_sec=700, entry_price=10000.0, base_now=now)
    holdings_mgr.get_holdings.return_value = [entry]
    exit_ev = {
        "exit_event_id": "EXT-test-004",
        "ticker": "000205",
        "exit_reason": "spike_resolved",
        "exit_ts": datetime.now(_KST).isoformat(),
        "weight_freed": 0.03,
    }
    holdings_mgr.remove.return_value = exit_ev

    admission_mgr = MagicMock()
    admission_mgr.remove_from_pool.return_value = True

    engine = _make_engine(tmp_path, holdings_manager=holdings_mgr, admission_engine=admission_mgr)

    snapshot = {"000205": {"last_price": 10050.0, "day_change_pct": 0.005}}
    trigger = {
        "000205": {
            "community_zscore": 0.5,   # < 1.5 threshold
            "day_change_pct": 0.005,   # < 0.015 threshold
            "active_triggers": [],     # 소멸
        }
    }
    result = engine.evaluate(snapshot, trigger_state=trigger, now_kst=now)

    assert len(result) == 1
    holdings_mgr.remove.assert_called_once_with("000205", "spike_resolved")


# ================================================================
# 8. spike_resolved: 보유 시간 < 600s → skip
# ================================================================

def test_exit_spike_resolved_too_early_skip(tmp_path: Path) -> None:
    """보유 시간 500s < min_holding_sec 600s → spike_resolved 미발동."""
    now = _kst(10, 0, 0)
    holdings_mgr = MagicMock()
    # now 기준 500초 전 admitted (min_holding_sec=600 미경과). ttl_sec=1800 이내.
    entry = _holdings_entry("000206", admitted_offset_sec=500, entry_price=10000.0, base_now=now)
    holdings_mgr.get_holdings.return_value = [entry]

    engine = _make_engine(tmp_path, holdings_manager=holdings_mgr)

    snapshot = {"000206": {"last_price": 10050.0, "day_change_pct": 0.005}}
    trigger = {
        "000206": {
            "community_zscore": 0.3,
            "day_change_pct": 0.003,
            "active_triggers": [],
        }
    }
    result = engine.evaluate(snapshot, trigger_state=trigger, now_kst=now)

    assert result == []
    holdings_mgr.remove.assert_not_called()


# ================================================================
# 9. evaluate: holdings_manager.remove 호출 검증
# ================================================================

def test_exit_evaluate_calls_holdings_manager_remove(tmp_path: Path) -> None:
    """market_close 시 holdings_manager.remove(ticker, 'market_close') 정확히 호출."""
    now = _kst(15, 31, 0)
    holdings_mgr = MagicMock()
    entry = _holdings_entry("000207", admitted_offset_sec=60, base_now=now)
    holdings_mgr.get_holdings.return_value = [entry]
    holdings_mgr.remove.return_value = {
        "exit_event_id": "EXT-test-005",
        "ticker": "000207",
        "exit_reason": "market_close",
        "exit_ts": "2026-05-03T15:31:00+09:00",
        "weight_freed": 0.03,
    }

    admission_mgr = MagicMock()

    engine = _make_engine(tmp_path, holdings_manager=holdings_mgr, admission_engine=admission_mgr)

    engine.evaluate({}, trigger_state=None, now_kst=now)

    # remove 가 정확한 인자로 호출됐는지 검증
    holdings_mgr.remove.assert_called_once_with("000207", "market_close")


# ================================================================
# 10. evaluate: admission_engine.remove_from_pool 호출 검증
# ================================================================

def test_exit_evaluate_calls_admission_remove_from_pool(tmp_path: Path) -> None:
    """청산 후 admission_engine.remove_from_pool(ticker) 호출 검증."""
    now = _kst(15, 31, 0)
    holdings_mgr = MagicMock()
    entry = _holdings_entry("000208", admitted_offset_sec=60, base_now=now)
    holdings_mgr.get_holdings.return_value = [entry]
    holdings_mgr.remove.return_value = {
        "exit_event_id": "EXT-test-006",
        "ticker": "000208",
        "exit_reason": "market_close",
        "exit_ts": "2026-05-03T15:31:00+09:00",
        "weight_freed": 0.03,
    }

    admission_mgr = MagicMock()
    admission_mgr.remove_from_pool.return_value = True

    engine = _make_engine(tmp_path, holdings_manager=holdings_mgr, admission_engine=admission_mgr)

    engine.evaluate({}, trigger_state=None, now_kst=now)

    # cooldown 시작: remove_from_pool 호출 검증
    admission_mgr.remove_from_pool.assert_called_once_with("000208")


# ================================================================
# 11. KST 시간대 일관성: naive datetime 입력 → KST 부여
# ================================================================

def test_exit_kst_timezone_consistency(tmp_path: Path) -> None:
    """now_kst 가 naive datetime 이어도 KST 로 처리 (15:30:01 naive → market_close 발동)."""
    today = datetime.now(_KST).date()
    # naive_now 와 동일 시각의 KST-aware 버전으로 entry 생성
    aware_now = datetime(today.year, today.month, today.day, 15, 30, 1, tzinfo=_KST)
    holdings_mgr = MagicMock()
    entry = _holdings_entry("000209", admitted_offset_sec=60, base_now=aware_now)
    holdings_mgr.get_holdings.return_value = [entry]
    holdings_mgr.remove.return_value = {
        "exit_event_id": "EXT-test-007",
        "ticker": "000209",
        "exit_reason": "market_close",
        "exit_ts": "2026-05-03T15:30:01+09:00",
        "weight_freed": 0.03,
    }

    admission_mgr = MagicMock()
    admission_mgr.remove_from_pool.return_value = True

    engine = _make_engine(tmp_path, holdings_manager=holdings_mgr, admission_engine=admission_mgr)

    # naive datetime (timezone 없음) → ExitEngine 이 KST 부여 후 처리
    today = datetime.now(_KST).date()
    naive_now = datetime(today.year, today.month, today.day, 15, 30, 1)
    assert naive_now.tzinfo is None

    result = engine.evaluate({}, trigger_state=None, now_kst=naive_now)

    # market_close 조건 발동 (15:30:01 >= 15:30)
    assert len(result) == 1
    holdings_mgr.remove.assert_called_once_with("000209", "market_close")


# ================================================================
# 12. C15 forbidden: exit_engine.py ppo/lightgbm/submit_order 없음 (정적)
# ================================================================

def test_exit_engine_forbidden_imports_blocked() -> None:
    """exit_engine.py 가 ppo / lightgbm import 하지 않음을 AST 검증."""
    module_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "dynamic_universe"
        / "exit_engine.py"
    )
    assert module_path.exists(), f"exit_engine.py 없음: {module_path}"

    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if any(kw in alias.name.lower() for kw in ("ppo", "lightgbm")):
                        forbidden_imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if any(kw in node.module.lower() for kw in ("ppo", "lightgbm")):
                    forbidden_imports.append(node.module)

    assert len(forbidden_imports) == 0, (
        f"[C15 forbidden] exit_engine 가 금지 모듈 import: {forbidden_imports}"
    )


def test_exit_engine_no_direct_submit_order() -> None:
    """exit_engine.py 에 submit_order 실제 호출 없음을 AST 검증."""
    module_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "dynamic_universe"
        / "exit_engine.py"
    )
    assert module_path.exists(), f"exit_engine.py 없음: {module_path}"

    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    submit_calls: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "submit_order":
                submit_calls.append(getattr(node, "lineno", -1))
            elif isinstance(func, ast.Name) and func.id == "submit_order":
                submit_calls.append(getattr(node, "lineno", -1))

    assert len(submit_calls) == 0, (
        f"[C15 forbidden] direct_trade_execution_bypass_pm: "
        f"exit_engine 에 submit_order 실제 호출 발견: lines={submit_calls}"
    )

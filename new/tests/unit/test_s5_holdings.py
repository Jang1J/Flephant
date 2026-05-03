"""Sprint 5 S5-2 HoldingsManager 단위 테스트.

C15 DynamicUniverseContract dynamic_holdings 비중 관리 검증.

테스트 케이스:
  1. test_holdings_manager_promotes_within_limits
  2. test_holdings_manager_rejects_when_max_size
  3. test_holdings_manager_rejects_when_total_weight_exceeds
  4. test_holdings_manager_remove_returns_exit_event
  5. test_holdings_manager_forbidden_ppo_import_blocked
  6. test_holdings_manager_no_direct_submit_order
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import yaml

_KST = ZoneInfo("Asia/Seoul")


# ================================================================
# 헬퍼
# ================================================================

def _make_dynamic_cfg(
    max_size: int = 5,
    per_stock_max_weight: float = 0.03,
    total_max_weight: float = 0.10,
) -> dict:
    return {
        "version": "0.1.0",
        "admission": {
            "min_trigger_count": 1,
            "cooldown_sec": 300,
            "candidate_pool_max": 10,
        },
        "holdings": {
            "max_size": max_size,
            "per_stock_max_weight": per_stock_max_weight,
            "total_max_weight": total_max_weight,
            "allocator_mode": "fixed_rule_only",
        },
        "exit": {
            "ttl_sec": 1800,
            "stop_loss_pct": 0.02,
        },
        "snapshot_cache": {"ttl_sec": 55, "max_entries": 1000},
    }


def _make_admission_event(ticker: str) -> dict:
    """테스트용 admission_event dict."""
    now_iso = datetime.now(_KST).isoformat()
    return {
        "admission_event_id": f"ADM-test-{ticker}",
        "ts": now_iso,
        "ticker": str(ticker).zfill(6),
        "trigger_ids": ["price_spike_admission"],
        "ttl_sec": 1800,
        "admitted_at": now_iso,
    }


def _make_manager(
    tmp_path: Path,
    max_size: int = 5,
    per_stock_max_weight: float = 0.03,
    total_max_weight: float = 0.10,
) -> "HoldingsManager":
    """테스트용 HoldingsManager 생성 헬퍼."""
    from src.dynamic_universe.holdings_manager import HoldingsManager

    dynamic_cfg_path = tmp_path / "dynamic_universe_config.yaml"
    artifacts_dir = tmp_path / "dynamic_holdings"

    with dynamic_cfg_path.open("w", encoding="utf-8") as fh:
        yaml.dump(
            _make_dynamic_cfg(
                max_size=max_size,
                per_stock_max_weight=per_stock_max_weight,
                total_max_weight=total_max_weight,
            ),
            fh,
        )

    return HoldingsManager(
        dynamic_config_path=dynamic_cfg_path,
        artifacts_dir=artifacts_dir,
    )


# ================================================================
# 1. per_stock_max=0.03, max_size=5 정상 promote
# ================================================================

def test_holdings_manager_promotes_within_limits(tmp_path: Path) -> None:
    """정상 promote: weight=0.03, max_size=5 내에서 1개 promote."""
    mgr = _make_manager(tmp_path)

    admission_event = _make_admission_event("000200")
    result = mgr.promote_to_holdings(admission_event)

    assert result is not None, "정상 promote 해야 함"
    assert result["ticker"] == "000200"
    assert result["weight"] == pytest.approx(0.03)
    assert result["promotion_id"].startswith("PRM-")
    assert "exit_policy" in result
    assert "ttl_sec" in result["exit_policy"]
    assert "stop_loss_pct" in result["exit_policy"]

    # get_holdings 반영 확인
    holdings = mgr.get_holdings()
    assert len(holdings) == 1
    assert holdings[0]["ticker"] == "000200"


# ================================================================
# 2. max_size=5 / 5/5 시 6번째 promote reject
# ================================================================

def test_holdings_manager_rejects_when_max_size(tmp_path: Path) -> None:
    """5개 만원 시 6번째 promote 거부."""
    mgr = _make_manager(tmp_path, max_size=5, per_stock_max_weight=0.01, total_max_weight=0.10)

    # 5개 promote
    for i in range(200, 205):
        ev = _make_admission_event(str(i).zfill(6))
        res = mgr.promote_to_holdings(ev)
        assert res is not None, f"{i}번째 promote 실패"

    assert len(mgr.get_holdings()) == 5

    # 6번째 거부
    ev6 = _make_admission_event("000210")
    result = mgr.promote_to_holdings(ev6)
    assert result is None, "max_size 초과 시 promote 거부해야 함"


# ================================================================
# 3. total 0.10 초과 시 reject
# ================================================================

def test_holdings_manager_rejects_when_total_weight_exceeds(tmp_path: Path) -> None:
    """per_stock=0.04, total_max=0.10 → 3개째 promote 시 reject."""
    # 0.04 * 3 = 0.12 > 0.10
    mgr = _make_manager(
        tmp_path,
        max_size=10,
        per_stock_max_weight=0.04,
        total_max_weight=0.10,
    )

    # 첫 번째: 0.04 → total=0.04 OK
    r1 = mgr.promote_to_holdings(_make_admission_event("000200"))
    assert r1 is not None

    # 두 번째: 0.04+0.04=0.08 → total=0.08 OK
    r2 = mgr.promote_to_holdings(_make_admission_event("000201"))
    assert r2 is not None

    # 세 번째: 0.08+0.04=0.12 > 0.10 → reject
    r3 = mgr.promote_to_holdings(_make_admission_event("000202"))
    assert r3 is None, "total_max_weight 초과 시 reject 해야 함"


# ================================================================
# 4. remove(ticker, "stop_loss") → exit_event dict 반환
# ================================================================

def test_holdings_manager_remove_returns_exit_event(tmp_path: Path) -> None:
    """remove 호출 → EXT- ID exit_event dict + exit_events.jsonl 기록."""
    artifacts_dir = tmp_path / "dynamic_holdings"
    mgr = _make_manager(tmp_path)
    mgr._artifacts_dir = artifacts_dir

    # promote 먼저
    ev = _make_admission_event("000200")
    mgr.promote_to_holdings(ev)

    # remove
    exit_event = mgr.remove("000200", "stop_loss")

    assert exit_event is not None, "remove → exit_event 반환해야 함"
    assert exit_event["exit_event_id"].startswith("EXT-")
    assert exit_event["ticker"] == "000200"
    assert exit_event["exit_reason"] == "stop_loss"
    assert "exit_ts" in exit_event
    assert exit_event["weight_freed"] == pytest.approx(0.03)

    # holdings 에서 제거됐는지 확인
    assert len(mgr.get_holdings()) == 0

    # exit_events.jsonl 기록 확인
    jsonl_path = artifacts_dir / "exit_events.jsonl"
    assert jsonl_path.exists(), "exit_events.jsonl 존재해야 함"
    with jsonl_path.open("r", encoding="utf-8") as fh:
        line = fh.readline().strip()
    parsed = json.loads(line)
    assert parsed["exit_event_id"].startswith("EXT-")
    assert parsed["ticker"] == "000200"


# ================================================================
# 5. PPO import 금지 동작 확인 (정적 검증)
# ================================================================

def test_holdings_manager_forbidden_ppo_import_blocked() -> None:
    """holdings_manager.py 가 ppo 모듈 import 하지 않음을 AST 검증."""
    module_path = Path(__file__).resolve().parents[2] / "src" / "dynamic_universe" / "holdings_manager.py"
    assert module_path.exists(), f"holdings_manager.py 없음: {module_path}"

    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    ppo_imports = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "ppo" in alias.name.lower():
                        ppo_imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if "ppo" in node.module.lower():
                    ppo_imports.append(node.module)

    assert len(ppo_imports) == 0, (
        f"[C15 forbidden] holdings_manager 가 ppo 모듈 import: {ppo_imports}"
    )


# ================================================================
# 6. submit_order 호출 없음 정적 검증
# ================================================================

def test_holdings_manager_no_direct_submit_order() -> None:
    """holdings_manager.py 에 submit_order 호출 없음을 소스 검색으로 검증."""
    module_path = Path(__file__).resolve().parents[2] / "src" / "dynamic_universe" / "holdings_manager.py"
    assert module_path.exists(), f"holdings_manager.py 없음: {module_path}"

    source = module_path.read_text(encoding="utf-8")

    # submit_order 실제 호출 여부를 AST 로 검증 (docstring / 주석 / assert 문자열은 제외).
    tree = ast.parse(source)

    submit_calls: list[int] = []
    for node in ast.walk(tree):
        # 함수 호출에서 submit_order 탐지
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "submit_order":
                submit_calls.append(getattr(node, "lineno", -1))
            elif isinstance(func, ast.Name) and func.id == "submit_order":
                submit_calls.append(getattr(node, "lineno", -1))

    assert len(submit_calls) == 0, (
        f"[C15 forbidden] direct_trade_execution_bypass_pm: "
        f"holdings_manager 에 submit_order 실제 호출 발견: lines={submit_calls}"
    )

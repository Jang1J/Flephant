"""Sprint 5 S5-4 DynamicUniverseGate 단위 테스트.

C15 activation_gate 준수 검증.

테스트 케이스:
  1. test_gate_disabled_by_default
  2. test_gate_assert_raises_when_disabled
  3. test_gate_assert_passes_when_enabled
  4. test_gate_log_transition_writes_jsonl
  5. test_gate_caches_enabled_state_60s
  6. test_gate_assert_raises_for_forbidden_callers
  7. test_gate_invalidate_cache_forces_reload
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.dynamic_universe.gate import DynamicUniverseGate


# ================================================================
# 헬퍼
# ================================================================

def _make_risk_config(enabled: bool, tmp_path: Path) -> Path:
    """risk_config.yaml 최소 버전 생성 (dynamic_universe.enabled 포함)."""
    cfg = {
        "dynamic_universe": {
            "enabled": enabled,
            "holdings_max": 5,
        }
    }
    path = tmp_path / "risk_config.yaml"
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(cfg, fh)
    return path


# ================================================================
# 테스트 케이스
# ================================================================

class TestGateDisabledByDefault:
    """test_gate_disabled_by_default: enabled=false 일 때 is_enabled() = False."""

    def test_gate_disabled_by_default(self, tmp_path: Path) -> None:
        config_path = _make_risk_config(enabled=False, tmp_path=tmp_path)
        gate = DynamicUniverseGate(risk_config_path=config_path)
        assert gate.is_enabled() is False

    def test_gate_returns_false_when_section_missing(self, tmp_path: Path) -> None:
        """dynamic_universe 섹션 자체가 없을 때도 False (안전 기본값)."""
        config_path = tmp_path / "risk_config.yaml"
        with config_path.open("w", encoding="utf-8") as fh:
            yaml.dump({"other_section": {}}, fh)
        gate = DynamicUniverseGate(risk_config_path=config_path)
        assert gate.is_enabled() is False

    def test_gate_treats_string_false_as_disabled(self, tmp_path: Path) -> None:
        """enabled='false' 문자열이 dynamic universe를 켜지 않도록 방어."""
        config_path = tmp_path / "risk_config.yaml"
        with config_path.open("w", encoding="utf-8") as fh:
            yaml.dump({"dynamic_universe": {"enabled": "false"}}, fh)
        gate = DynamicUniverseGate(risk_config_path=config_path)
        assert gate.is_enabled() is False


class TestGateAssertRaisesWhenDisabled:
    """test_gate_assert_raises_when_disabled: assert_enabled → RuntimeError."""

    def test_raises_runtime_error_when_disabled(self, tmp_path: Path) -> None:
        config_path = _make_risk_config(enabled=False, tmp_path=tmp_path)
        gate = DynamicUniverseGate(risk_config_path=config_path)

        with pytest.raises(RuntimeError, match="enabled=false"):
            gate.assert_enabled("test_caller")

    def test_error_message_contains_caller(self, tmp_path: Path) -> None:
        """에러 메시지에 caller 식별자 포함 확인."""
        config_path = _make_risk_config(enabled=False, tmp_path=tmp_path)
        gate = DynamicUniverseGate(risk_config_path=config_path)

        with pytest.raises(RuntimeError) as exc_info:
            gate.assert_enabled("my_caller_xyz")
        assert "my_caller_xyz" in str(exc_info.value)


class TestGateAssertPassesWhenEnabled:
    """test_gate_assert_passes_when_enabled: enabled=true 시 assert_enabled OK."""

    def test_assert_passes_when_enabled(self, tmp_path: Path) -> None:
        config_path = _make_risk_config(enabled=True, tmp_path=tmp_path)
        gate = DynamicUniverseGate(risk_config_path=config_path)
        # RuntimeError 없이 통과해야 함
        gate.assert_enabled("manager")

    def test_assert_passes_for_various_callers(self, tmp_path: Path) -> None:
        """enabled=true 상태에서 일반 caller 전부 통과."""
        config_path = _make_risk_config(enabled=True, tmp_path=tmp_path)
        gate = DynamicUniverseGate(risk_config_path=config_path)
        for caller in ["manager", "runner", "test_caller", "operator"]:
            gate.assert_enabled(caller)  # 예외 없이 통과


class TestGateAssertRaisesForForbiddenCallers:
    """C15 activation_gate: FDA/Mode B Scheduler 가 호출 시 즉시 거부."""

    @pytest.mark.parametrize("forbidden_caller", ["fda", "FDA", "mode_b_scheduler", "backtest_agent"])
    def test_raises_for_forbidden_caller_even_when_enabled(
        self, tmp_path: Path, forbidden_caller: str
    ) -> None:
        """enabled=true 여도 forbidden caller 는 RuntimeError."""
        config_path = _make_risk_config(enabled=True, tmp_path=tmp_path)
        gate = DynamicUniverseGate(risk_config_path=config_path)

        with pytest.raises(RuntimeError, match="C15 activation_gate"):
            gate.assert_enabled(forbidden_caller)

    @pytest.mark.parametrize("forbidden_caller", ["fda", "FDA", "mode_b_scheduler"])
    def test_raises_for_forbidden_caller_when_disabled(
        self, tmp_path: Path, forbidden_caller: str
    ) -> None:
        """enabled=false 여도 forbidden caller 는 activation_gate 위반 먼저 체크."""
        config_path = _make_risk_config(enabled=False, tmp_path=tmp_path)
        gate = DynamicUniverseGate(risk_config_path=config_path)

        with pytest.raises(RuntimeError, match="C15 activation_gate"):
            gate.assert_enabled(forbidden_caller)


class TestGateLogTransitionWritesJsonl:
    """test_gate_log_transition_writes_jsonl: transitions.jsonl append 검증."""

    def test_log_transition_creates_file(self, tmp_path: Path) -> None:
        config_path = _make_risk_config(enabled=False, tmp_path=tmp_path)
        gate = DynamicUniverseGate(risk_config_path=config_path)
        # gate_dir 를 tmp_path 로 교체
        gate._gate_dir = tmp_path

        gate.log_transition(from_state=False, to_state=True, reason="operator_edit")

        transitions_path = tmp_path / "gate_transitions.jsonl"
        assert transitions_path.exists()

    def test_log_transition_appends_valid_json(self, tmp_path: Path) -> None:
        config_path = _make_risk_config(enabled=False, tmp_path=tmp_path)
        gate = DynamicUniverseGate(risk_config_path=config_path)
        gate._gate_dir = tmp_path

        gate.log_transition(from_state=False, to_state=True, reason="test_reason")

        transitions_path = tmp_path / "gate_transitions.jsonl"
        lines = transitions_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1

        entry = json.loads(lines[0])
        assert entry["from_enabled"] is False
        assert entry["to_enabled"] is True
        assert entry["reason"] == "test_reason"
        assert "ts" in entry

    def test_log_transition_appends_multiple(self, tmp_path: Path) -> None:
        """여러 번 호출 시 모두 append 확인."""
        config_path = _make_risk_config(enabled=False, tmp_path=tmp_path)
        gate = DynamicUniverseGate(risk_config_path=config_path)
        gate._gate_dir = tmp_path

        gate.log_transition(False, True, "reason_1")
        gate.log_transition(True, False, "reason_2")

        transitions_path = tmp_path / "gate_transitions.jsonl"
        lines = transitions_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2

        entry1 = json.loads(lines[0])
        entry2 = json.loads(lines[1])
        assert entry1["reason"] == "reason_1"
        assert entry2["reason"] == "reason_2"


class TestGateCachesEnabledState60s:
    """test_gate_caches_enabled_state_60s: 캐시 동작 검증."""

    def test_cache_avoids_yaml_reload_within_ttl(self, tmp_path: Path) -> None:
        """캐시 유효 시간 내에는 yaml 재로드 없이 캐시 반환."""
        config_path = _make_risk_config(enabled=False, tmp_path=tmp_path)
        gate = DynamicUniverseGate(risk_config_path=config_path)

        # 1차 호출 → yaml 로드
        result1 = gate.is_enabled()
        # yaml 파일을 enabled=True 로 변경
        cfg = {"dynamic_universe": {"enabled": True}}
        with config_path.open("w", encoding="utf-8") as fh:
            yaml.dump(cfg, fh)

        # 2차 호출 → 캐시 TTL 내이면 False 그대로 반환
        result2 = gate.is_enabled()
        assert result1 is False
        assert result2 is False  # 캐시 hit, yaml 변경 반영 안 됨

    def test_invalidate_cache_forces_reload(self, tmp_path: Path) -> None:
        """invalidate_cache() 후 다음 is_enabled() 에서 yaml 재로드."""
        config_path = _make_risk_config(enabled=False, tmp_path=tmp_path)
        gate = DynamicUniverseGate(risk_config_path=config_path)

        # 1차 호출 → False 캐시
        assert gate.is_enabled() is False

        # yaml 파일 enabled=True 로 변경
        cfg = {"dynamic_universe": {"enabled": True}}
        with config_path.open("w", encoding="utf-8") as fh:
            yaml.dump(cfg, fh)

        # 캐시 무효화 후 재조회 → True
        gate.invalidate_cache()
        assert gate.is_enabled() is True

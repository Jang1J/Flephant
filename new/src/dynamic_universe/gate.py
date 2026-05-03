"""C15 dynamic_universe.enabled 전환 게이트. Sprint 5 S5-4.

## 동작
    1. risk_config.yaml dynamic_universe.enabled 읽기
    2. enabled=false → 모든 dynamic_universe 경로 noop
    3. enabled=true 전환은 operator 직접 yaml 편집만 (자동 전환 금지)
    4. 전환 audit log: artifacts/dynamic_holdings/gate_transitions.jsonl

## activation_command
    risk_config.yaml dynamic_universe.enabled: false → true 직접 편집 후
    python -m new.src.ops.enable_dynamic_universe --approve (Sprint 5 종료 후 별도 작업)

## C15 weight_decision_authority + activation_gate 준수
    - operator 만 enabled 변경 가능 (mode_b_editable=false)
    - FDA / Mode B Scheduler 가 enabled 변경 시도 시 즉시 거부 (RuntimeError)

SSOT: new/specs/api_contracts.md C15 DynamicUniverseContract
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from src.utils.logger import get_logger

logger = get_logger("gate")

_KST = ZoneInfo("Asia/Seoul")

# 기본 경로
_RISK_CONFIG_PATH_DEFAULT = (
    Path(__file__).resolve().parents[3] / "config" / "risk_config.yaml"
)
_GATE_TRANSITIONS_DIR_DEFAULT = (
    Path(__file__).resolve().parents[3] / "artifacts" / "dynamic_holdings"
)

# is_enabled() 캐시 TTL (초). IO 부하 vs 토글 즉시 반영 trade-off.
_CACHE_TTL_SEC = 60

# C15 activation_gate: 자동 전환 금지 호출자 목록
_FORBIDDEN_CALLERS = frozenset({"fda", "mode_b_scheduler", "backtest_agent"})


def _load_yaml(path: Path) -> dict:
    """yaml 파일 로드. 실패 시 빈 dict 반환."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception as e:
        logger.error("[gate] yaml 로드 실패: path=%s error=%s", path, e)
        return {}


class DynamicUniverseGate:
    """C15 dynamic_universe.enabled 전환 게이트.

    Sprint 5 S5-4.

    ## 동작
        1. risk_config.yaml dynamic_universe.enabled 읽기
        2. enabled=false → 모든 dynamic_universe 경로 noop
        3. enabled=true 전환은 operator 직접 yaml 편집만 (자동 전환 금지)
        4. 전환 audit log: artifacts/dynamic_holdings/gate_transitions.jsonl

    ## C15 weight_decision_authority + activation_gate 준수
        - operator 만 enabled 변경 가능 (mode_b_editable=false)
        - FDA / Mode B Scheduler 가 enabled 변경 시도 시 즉시 거부 (RuntimeError)
    """

    def __init__(self, risk_config_path: Path | None = None) -> None:
        """DynamicUniverseGate 초기화.

        Args:
            risk_config_path: risk_config.yaml 경로. None이면 기본값.
        """
        self._risk_config_path = risk_config_path or _RISK_CONFIG_PATH_DEFAULT
        self._gate_dir = _GATE_TRANSITIONS_DIR_DEFAULT

        # 캐시: (enabled_bool, loaded_at_monotonic)
        self._cached_enabled: bool | None = None
        self._cache_loaded_at: float = 0.0

        logger.info(
            "[gate] 초기화 완료: risk_config_path=%s cache_ttl_sec=%d",
            self._risk_config_path,
            _CACHE_TTL_SEC,
        )

    def _reload_enabled(self) -> bool:
        """risk_config.yaml dynamic_universe.enabled 재로드.

        Returns:
            enabled bool. 섹션/키 없으면 False (안전 기본값).
        """
        cfg = _load_yaml(self._risk_config_path)
        du_cfg = cfg.get("dynamic_universe", {})
        enabled: bool = bool(du_cfg.get("enabled", False))
        return enabled

    def is_enabled(self) -> bool:
        """현재 enabled 상태 read.

        호출마다 yaml 재로드 대신 60s 캐시 사용.
        운영 중 토글 시 최대 60s 지연 후 반영.

        Returns:
            dynamic_universe.enabled 값 (bool).
        """
        now_mono = time.monotonic()
        if (
            self._cached_enabled is None
            or (now_mono - self._cache_loaded_at) > _CACHE_TTL_SEC
        ):
            prev = self._cached_enabled
            self._cached_enabled = self._reload_enabled()
            self._cache_loaded_at = now_mono

            if prev is not None and prev != self._cached_enabled:
                # 캐시 갱신 중 상태 변경 감지 → 전환 로그
                logger.info(
                    "[gate] enabled 상태 변경 감지: %s → %s",
                    prev,
                    self._cached_enabled,
                )
                self.log_transition(
                    from_state=prev,
                    to_state=self._cached_enabled,
                    reason="yaml_reload_detected_change",
                )

        return self._cached_enabled  # type: ignore[return-value]

    def assert_enabled(self, caller: str) -> None:
        """enabled=false 면 RuntimeError. caller 식별자 로그.

        C15 activation_gate: FDA/Mode B Scheduler 가 호출하면 즉시 거부.

        Args:
            caller: 호출자 식별자 (예: "manager", "fda", "mode_b_scheduler").

        Raises:
            RuntimeError: enabled=false 이거나 forbidden caller 인 경우.
        """
        # C15 forbidden_callers 가드: FDA, Mode B Scheduler 가 게이트를 우회 시도 시 즉시 거부
        caller_lower = caller.lower()
        if caller_lower in _FORBIDDEN_CALLERS:
            msg = (
                f"[gate] C15 activation_gate 위반: caller='{caller}'는 "
                "dynamic_universe enabled 변경 권한 없음. "
                "operator 직접 yaml 편집만 허용."
            )
            logger.error(msg)
            raise RuntimeError(msg)

        if not self.is_enabled():
            msg = (
                f"[gate] dynamic_universe disabled (enabled=false). "
                f"caller='{caller}'. "
                "활성화하려면 risk_config.yaml dynamic_universe.enabled: true 직접 편집 후 재시작."
            )
            logger.warning(msg)
            raise RuntimeError(msg)

        logger.debug("[gate] assert_enabled 통과: caller=%s", caller)

    def log_transition(self, from_state: bool, to_state: bool, reason: str) -> None:
        """gate_transitions.jsonl 에 전환 이력 append.

        Args:
            from_state: 전환 전 enabled 값.
            to_state: 전환 후 enabled 값.
            reason: 전환 원인 (예: "yaml_reload_detected_change", "operator_edit").
        """
        self._gate_dir.mkdir(parents=True, exist_ok=True)
        out_path = self._gate_dir / "gate_transitions.jsonl"

        now_kst = datetime.now(_KST)
        entry = {
            "ts": now_kst.isoformat(),
            "from_enabled": from_state,
            "to_enabled": to_state,
            "reason": reason,
        }
        try:
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            logger.info(
                "[gate] transition 로그: %s → %s reason=%s ts=%s",
                from_state,
                to_state,
                reason,
                now_kst.isoformat(),
            )
        except Exception as e:
            logger.error("[gate] gate_transitions.jsonl 쓰기 실패: %s", e)

    def invalidate_cache(self) -> None:
        """캐시 강제 무효화. 다음 is_enabled() 호출 시 yaml 재로드."""
        self._cached_enabled = None
        self._cache_loaded_at = 0.0
        logger.debug("[gate] 캐시 무효화 완료.")

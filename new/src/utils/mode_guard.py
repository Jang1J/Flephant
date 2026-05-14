"""Mode B 전용 가드 데코레이터. 불변 원칙 3: Backtest Agent Mode B 격리."""
from __future__ import annotations

import functools
import os
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from src.utils.config_loader import load as config_load

_F = TypeVar("_F", bound=Callable[..., Any])

_MODE_ENV_VAR = "ELEPHANT_MODE"
_MODE_B_VALUE = "mode_b"
_KST = ZoneInfo("Asia/Seoul")


def _is_test_context() -> bool:
    # 2026-05-12 audit C-R1 fix (불변 원칙 3 우회 차단):
    # PYTEST_CURRENT_TEST만 시간창 가드 스킵 trigger.
    # ELEPHANT_TEST_PIT_SKIP는 PIT 날짜 검증 전용 (audit_logger 등),
    # mode_b_only 18:00~22:00 KST 시간창 우회는 금지.
    # 이전: ELEPHANT_TEST_PIT_SKIP=1 이 production shell에 노출 시
    # 시간창 가드 우회 가능 → 불변 원칙 3 (Backtest Mode B 격리) 위반.
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _mode_b_window() -> tuple[int, int]:
    try:
        cfg = config_load("risk_config.yaml", "mode_b_scheduler") or {}
        win = cfg.get("execution_window", {})
        return int(win.get("start_hour", 18)), int(win.get("end_hour", 22))
    except Exception:
        return 18, 22


def mode_b_only(func: _F) -> _F:
    """Mode B 전용 함수 가드 데코레이터.

    현재 환경변수 ELEPHANT_MODE != 'mode_b' 이면 RuntimeError 발생.
    Mode A 장중 경로에서 Mode B 전용 함수 호출을 원천 차단.

    사용 예:
        @mode_b_only
        def run_backtest(...):
            ...
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        current_mode = os.environ.get(_MODE_ENV_VAR, "")
        if current_mode != _MODE_B_VALUE:
            raise RuntimeError(
                f"Mode B 전용 함수 '{func.__qualname__}'가 "
                f"Mode A에서 호출됨. "
                f"ELEPHANT_MODE={current_mode!r} (필요값: {_MODE_B_VALUE!r})"
            )
        if not _is_test_context():
            start_hour, end_hour = _mode_b_window()
            now = datetime.now(_KST)
            if not (start_hour <= now.hour <= end_hour):
                raise RuntimeError(
                    f"Mode B 전용 함수 '{func.__qualname__}'가 "
                    f"Mode B 실행 창 밖에서 호출됨. "
                    f"now={now.isoformat()} window={start_hour:02d}:00-{end_hour:02d}:59 KST"
                )
        return func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]

"""Mode B 전용 가드 데코레이터. 불변 원칙 3: Backtest Agent Mode B 격리."""
from __future__ import annotations

import functools
import os
from collections.abc import Callable
from typing import Any, TypeVar

_F = TypeVar("_F", bound=Callable[..., Any])

_MODE_ENV_VAR = "ELEPHANT_MODE"
_MODE_B_VALUE = "mode_b"


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
        return func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]

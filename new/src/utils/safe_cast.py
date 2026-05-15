"""Small safe-cast helpers for external/LLM/operator inputs."""
from __future__ import annotations

import math
from typing import Any


def safe_bool(value: Any, default: bool = False) -> bool:
    """Interpret bool-like external values without Python truthiness traps."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "y", "1", "approve", "approved", "승인"}:
            return True
        if normalized in {
            "false",
            "no",
            "n",
            "0",
            "veto",
            "reject",
            "rejected",
            "거부",
            "없음",
            "아님",
        }:
            return False
    return default


def safe_float(
    value: Any,
    default: float = 0.0,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
    """Convert external numeric-ish values to finite floats with optional clamp."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    if min_value is not None:
        out = max(min_value, out)
    if max_value is not None:
        out = min(max_value, out)
    return out

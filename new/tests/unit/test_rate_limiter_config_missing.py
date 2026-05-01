"""RateLimiter config 누락 시 KeyError 검증."""
from __future__ import annotations

import pytest

from src.utils.rate_limiter import RateLimiter


def test_unknown_source_raises_keyerror():
    """존재하지 않는 source로 RateLimiter 생성 시 KeyError 발생.

    하드코딩 fallback 금지 원칙: config에 없으면 반드시 KeyError.
    """
    with pytest.raises(KeyError, match="rate_limits.nonexistent_source 섹션 누락"):
        RateLimiter("nonexistent_source")


def test_known_sources_do_not_raise():
    """risk_config.yaml에 정의된 소스들은 정상 생성된다."""
    valid_sources = [
        "kis_rest",
        "kis_ws",
        "dart",
        "krx_investor_flow",
        "naver",
        "ecos",
        "community",
        "us_market",
    ]
    for src in valid_sources:
        rl = RateLimiter(src)
        assert rl.source == src
        assert rl.capacity > 0
        assert rl.refill_rate > 0

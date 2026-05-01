"""RateLimiter Token bucket 알고리즘 단위 테스트."""
from __future__ import annotations

import time

import pytest

from src.utils.rate_limiter import RateLimiter, RateLimitExceeded


def test_acquire_within_capacity():
    """초기 버킷이 가득 찬 상태에서 burst 이내 요청은 모두 True."""
    rl = RateLimiter("kis_rest")  # capacity=50
    # 50번 연속 acquire
    for _ in range(rl.capacity):
        assert rl.acquire(1) is True


def test_acquire_beyond_capacity():
    """버킷 소진 후 추가 요청은 False."""
    rl = RateLimiter("dart")  # capacity=10
    # 버킷 소진
    for _ in range(rl.capacity):
        rl.acquire(1)
    # 토큰 없는 상태
    assert rl.acquire(1) is False


def test_refill_restores_tokens():
    """시간이 지나면 토큰이 보충된다."""
    rl = RateLimiter("ecos")  # capacity=5, refill_rate=2.0 req/s
    # 버킷 완전 소진
    for _ in range(rl.capacity):
        rl.acquire(1)
    assert rl.acquire(1) is False

    # 1초 대기 후 refill_rate(2.0) 이상 보충 기대
    time.sleep(1.1)
    assert rl.acquire(1) is True


def test_stats_returns_correct_keys():
    """stats()는 필수 키를 모두 포함한 dict를 반환한다."""
    rl = RateLimiter("naver")
    s = rl.stats()
    for key in ("source", "tokens", "capacity", "refill_rate", "last_refill_ago_sec"):
        assert key in s, f"stats()에 '{key}' 키 없음"
    assert s["source"] == "naver"
    assert s["capacity"] == rl.capacity


def test_wait_and_acquire_success():
    """버킷에 토큰이 있으면 wait_and_acquire가 즉시 반환된다."""
    rl = RateLimiter("krx_investor_flow")  # capacity=5
    start = time.monotonic()
    rl.wait_and_acquire(1)
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"토큰 있는 상태에서 {elapsed:.2f}s 대기: 너무 느림"


def test_wait_and_acquire_timeout():
    """버킷 소진 후 max_wait 초과 시 RateLimitExceeded 발생."""
    rl = RateLimiter("community")  # refill_rate=0.1 (매우 느림)
    # 버킷 소진 (capacity=3)
    for _ in range(rl.capacity):
        rl.acquire(1)
    with pytest.raises(RateLimitExceeded):
        rl.wait_and_acquire(tokens=1, max_wait_sec=0.2)


def test_initial_tokens_equal_capacity():
    """초기 토큰 수는 capacity(burst)와 같다."""
    rl = RateLimiter("us_market")  # capacity=2
    assert rl._tokens == float(rl.capacity)

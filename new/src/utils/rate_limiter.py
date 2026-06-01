"""Token bucket 기반 소스별 요청 속도 제한. risk_config.yaml rate_limits 섹션 경유."""
from __future__ import annotations

import threading
import time

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger

logger = get_logger("rate_limiter")


# 가드 2: process 단위 shared RateLimiter instance cache.
# 같은 source 이름은 항상 같은 instance 반환 → 여러 KISRestClient가 같은 token bucket 공유.
# Dependency injection 패턴 유지(rate_limiter 인자 명시 시 그것 사용)는 테스트 격리용으로 유지.
_SHARED_INSTANCES: dict[str, "RateLimiter"] = {}
_SHARED_INSTANCES_LOCK = threading.Lock()


def get_shared_rate_limiter(source: str) -> "RateLimiter":
    """Process 단위 shared RateLimiter instance를 반환한다.

    여러 thread / 여러 client가 같은 source로 호출해도 동일 instance를 공유한다.
    이로써 process-wide rate limit이 보장되어 KIS account-level rate 초과 위험을 차단.
    """
    instance = _SHARED_INSTANCES.get(source)
    if instance is not None:
        return instance
    with _SHARED_INSTANCES_LOCK:
        instance = _SHARED_INSTANCES.get(source)
        if instance is None:
            instance = RateLimiter(source)
            _SHARED_INSTANCES[source] = instance
        return instance


class RateLimitExceeded(Exception):
    """토큰 부족 + max_wait 초과 시 발생."""


class RateLimiter:
    """Token bucket rate limiter. 소스별 독립 bucket.

    risk_config.yaml rate_limits 섹션에서 req_per_sec / burst 로드.
    하드코딩 금지 원칙: 모든 수치는 config 경유.
    """

    source: str
    capacity: int        # burst (버킷 최대 용량)
    refill_rate: float   # req_per_sec (초당 토큰 보충량)
    _tokens: float
    _last_refill: float  # time.monotonic() 기준 초

    def __init__(self, source: str) -> None:
        """source (kis_rest, dart, krx, ...) 이름으로 config 조회.

        Raises:
            KeyError: rate_limits.{source} 섹션 누락
        """
        self.source = source

        # 하드코딩 fallback 금지: config에 없으면 KeyError
        rate_limits: dict = config_load("risk_config.yaml", "rate_limits")
        if source not in rate_limits:
            raise KeyError(f"rate_limits.{source} 섹션 누락. risk_config.yaml에 추가 필요.")

        cfg = rate_limits[source]
        self.capacity = int(cfg["burst"])
        self.refill_rate = float(cfg["req_per_sec"])

        # 버킷을 가득 채운 상태로 시작
        self._tokens = float(self.capacity)
        self._last_refill = time.monotonic()
        # 동시 acquire 시 토큰 차감 + refill을 원자화하기 위한 Lock.
        # 단일 thread 호출만 발생하면 락 contention 없어 비용 거의 0.
        self._lock = threading.Lock()

        logger.info(
            "RateLimiter 초기화: source=%s, capacity=%d, refill_rate=%.2f req/s",
            source,
            self.capacity,
            self.refill_rate,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self, tokens: int = 1) -> bool:
        """non-blocking. 토큰 있으면 차감 후 True. 없으면 False.

        Thread-safe: 토큰 read-modify-write 구간을 Lock으로 보호.
        """
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            logger.warning(
                "[rate_limiter] 토큰 부족: source=%s, 요청=%d, 잔여=%.2f",
                self.source,
                tokens,
                self._tokens,
            )
            return False

    def wait_and_acquire(self, tokens: int = 1, max_wait_sec: float = 10.0) -> None:
        """blocking. 토큰 생길 때까지 대기.

        Raises:
            RateLimitExceeded: max_wait_sec 초과
        """
        deadline = time.monotonic() + max_wait_sec
        while True:
            if self.acquire(tokens):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RateLimitExceeded(
                    f"{self.source}: {max_wait_sec}초 내 토큰 {tokens}개 확보 실패"
                )
            # 다음 토큰이 생기기까지 필요한 최소 대기 시간 계산 (read 시 lock 보호)
            with self._lock:
                needed = tokens - self._tokens
            wait = min(needed / self.refill_rate, remaining, 0.05)
            time.sleep(max(wait, 0.001))

    def stats(self) -> dict:
        """현재 상태 dict."""
        with self._lock:
            self._refill()
            return {
                "source": self.source,
                "tokens": round(self._tokens, 4),
                "capacity": self.capacity,
                "refill_rate": self.refill_rate,
                "last_refill_ago_sec": round(time.monotonic() - self._last_refill, 4),
            }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        """경과 시간만큼 토큰 보충. capacity 상한 cap.

        주의: 호출자가 반드시 self._lock을 보유한 상태에서 호출해야 한다.
        (acquire/stats 내부에서만 호출됨)
        """
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
        self._last_refill = now

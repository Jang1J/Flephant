"""Pub/Sub 브로커. MessagePool 위의 채널 라우팅 레이어.

v3 (2026-04-21, Sprint 2 S2-1): EventGateway 어댑터 역할 추가.

MessagePool 이 C4 5 operations 전체를 담당하므로,
PubSubBroker 는 아래 편의 기능만 추가한다:
  - handler 별 unsubscribe 지원
  - underlying MessagePool 노출 (dependency_activation, expire 직접 호출용)
  - EventGateway 가 handler → publish 흐름에서 PubSubBroker 를 단일 진입점으로 사용

채널명 SSOT: api_contracts.md message_taxonomy.publish_channels (15개).
하드코딩 금지 원칙: 채널 목록은 MessagePool._VALID_CHANNELS 에서 관리.
"""
from __future__ import annotations

from typing import Callable

from src.blackboard.message_pool import MessagePool
from src.utils.logger import get_logger

logger = get_logger("pubsub")


class PubSubBroker:
    """MessagePool 위의 얇은 route layer.

    주요 역할:
      1. publish / subscribe 위임 (MessagePool 경유)
      2. unsubscribe: handler 참조로 구독 해제
      3. pool() 노출: dependency_activation / expire 직접 접근

    MessagePool 은 constructor 주입 가능 (테스트 격리 지원).
    """

    def __init__(self, pool: MessagePool | None = None) -> None:
        self._pool: MessagePool = pool or MessagePool()
        # id(handler) → (channel, handler): unsubscribe 추적용
        self._handler_registry: dict[int, tuple[str, Callable]] = {}

    def publish(self, channel: str, message: dict) -> str:
        """채널에 메시지 publish. MessagePool.publish() 경유. message_id 반환."""
        return self._pool.publish(channel, message)

    def subscribe(
        self,
        channel: str,
        handler: Callable[[dict], None],
        filter_fn: Callable[[dict], bool] | None = None,
    ) -> None:
        """채널 구독 등록. MessagePool.subscribe() 경유.

        동일 handler 를 여러 채널에 구독 등록 가능하나,
        unsubscribe 는 가장 최근 (channel, handler) 쌍 기준 제거.
        """
        self._pool.subscribe(channel, handler, filter_fn)
        self._handler_registry[id(handler)] = (channel, handler)

    def unsubscribe(self, handler: Callable) -> bool:
        """handler 구독 해제. 성공 여부 반환.

        MessagePool._subscribers[channel] 에서 해당 handler 항목 제거.
        handler 가 등록된 적 없으면 False 반환 (에러 아님).
        """
        info = self._handler_registry.pop(id(handler), None)
        if info is None:
            logger.info(
                "[pubsub] unsubscribe: handler=%s 등록 이력 없음",
                getattr(handler, "__name__", repr(handler)),
            )
            return False

        channel, h = info
        # TODO(Sprint4): MessagePool.remove_subscriber() 공개 메서드로 리팩터
        subs = self._pool._subscribers.get(channel, [])
        self._pool._subscribers[channel] = [(hh, ff) for hh, ff in subs if hh is not h]
        logger.info(
            "[pubsub] unsubscribed: channel=%s handler=%s",
            channel, getattr(handler, "__name__", repr(handler)),
        )
        return True

    def pool(self) -> MessagePool:
        """underlying MessagePool 반환.

        dependency_activation 등록 / expire 직접 호출 / get_active_messages 조회에 사용.
        """
        return self._pool

"""PubSubBroker unit tests. MessagePool 위의 route layer 검증.

publish / subscribe / unsubscribe + shared pool 인스턴스 확인.
"""
from __future__ import annotations


from src.blackboard.message_pool import MessagePool
from src.blackboard.pubsub import PubSubBroker
from src.utils.time_utils import now_kst


def _valid_msg(**overrides) -> dict:
    """C4 message_schema 필수 9 필드 충족 기본 메시지."""
    base = {
        "content": "pubsub 테스트 메시지",
        "cause_by": "test_event",
        "sent_from": "test_agent",
        "priority": "normal",
        "confidence": 0.75,
        "reasoning": "테스트 추론",
        "scope": "005930",
        "action_type": "signal",
        "timestamp": now_kst().isoformat(),
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------
# 1. publish: MessagePool 경유 → message_id 반환
# ------------------------------------------------------------------

def test_pubsub_publish_routes_to_pool() -> None:
    broker = PubSubBroker()
    msg_id = broker.publish("news_signal", _valid_msg())

    assert msg_id.startswith("MSG-")
    assert broker.pool().pool_size() == 1


# ------------------------------------------------------------------
# 2. subscribe + publish: handler 수신 확인
# ------------------------------------------------------------------

def test_pubsub_subscribe_and_receive() -> None:
    broker = PubSubBroker()
    received: list[dict] = []

    broker.subscribe("news_signal", lambda m: received.append(m))
    broker.publish("news_signal", _valid_msg())

    assert len(received) == 1
    assert received[0]["content"] == "pubsub 테스트 메시지"


# ------------------------------------------------------------------
# 3. unsubscribe: handler 구독 해제 후 미수신
# ------------------------------------------------------------------

def test_pubsub_unsubscribe_removes_handler() -> None:
    broker = PubSubBroker()
    received: list[dict] = []

    def handler(m: dict) -> None:
        received.append(m)

    broker.subscribe("news_signal", handler)
    broker.unsubscribe(handler)
    broker.publish("news_signal", _valid_msg())

    assert len(received) == 0


# ------------------------------------------------------------------
# 4. unsubscribe: 미등록 handler → False 반환
# ------------------------------------------------------------------

def test_pubsub_unsubscribe_unknown_returns_false() -> None:
    # 등록 이력 없는 handler → False. S2-4 리팩터 시 "등록 후 재해제" 시나리오 보강 예정
    broker = PubSubBroker()
    result = broker.unsubscribe(lambda m: None)
    assert result is False


# ------------------------------------------------------------------
# 5. pool(): 외부 공유 MessagePool 주입 시 동일 인스턴스
# ------------------------------------------------------------------

def test_pubsub_wraps_shared_pool() -> None:
    shared_pool = MessagePool()
    broker = PubSubBroker(pool=shared_pool)

    assert broker.pool() is shared_pool


# ------------------------------------------------------------------
# 6. unsubscribe: 성공 시 True 반환
# ------------------------------------------------------------------

def test_pubsub_unsubscribe_success_returns_true() -> None:
    broker = PubSubBroker()

    def handler(m: dict) -> None:
        pass

    broker.subscribe("news_signal", handler)
    result = broker.unsubscribe(handler)
    assert result is True

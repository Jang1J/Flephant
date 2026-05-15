"""C4 SharedMessagePoolContract contract tests."""
from __future__ import annotations

from datetime import timedelta

from src.blackboard.message_pool import MessagePool
from src.utils.time_utils import now_kst


def _valid_msg(**overrides: object) -> dict:
    base = {
        "content": "C4 계약 테스트",
        "cause_by": "contract_test",
        "sent_from": "contract_test",
        "priority": "normal",
        "confidence": 0.8,
        "reasoning": "계약 필드 검증",
        "scope": "ticker:005930",
        "action_type": "signal",
        "timestamp": now_kst().isoformat(),
    }
    base.update(overrides)
    return base


def test_c04_publish_returns_id() -> None:
    """publish() returns a C4 message_id and stores the message."""
    pool = MessagePool()

    msg_id = pool.publish("news_signal", _valid_msg())

    assert msg_id.startswith("MSG-")
    assert pool.pool_size() == 1
    active = pool.get_active_messages("news_signal")
    assert len(active) == 1
    assert active[0]["message_id"] == msg_id


def test_c04_expire_removes_stale() -> None:
    """expire() removes messages older than expires_at."""
    pool = MessagePool()
    past = (now_kst() - timedelta(minutes=1)).isoformat()
    msg_id = pool.publish("news_signal", _valid_msg(expires_at=past))
    pool._messages[msg_id].message["expires_at"] = past

    removed = pool.expire(now=now_kst())

    assert removed == 1
    assert pool.pool_size() == 0


def test_c04_subscribe_filter() -> None:
    """Subscriber filters only deliver matched messages on the subscribed channel."""
    pool = MessagePool()
    received: list[dict] = []

    pool.subscribe(
        "news_signal",
        lambda message: received.append(message),
        filter_fn=lambda message: message.get("priority") == "urgent",
    )

    pool.publish("news_signal", _valid_msg(priority="normal"))
    pool.publish("news_signal", _valid_msg(priority="urgent"))

    assert len(received) == 1
    assert received[0]["priority"] == "urgent"

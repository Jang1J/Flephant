"""MessagePool unit tests. C4 SharedMessagePoolContract 검증.

5 operations 전부: publish / subscribe / ack / supersede / expire
message_schema 17 필드 validation, _VALID_CHANNELS 15개, action_types, priorities.
subscriber_filtering, at-least-once, dependency_activation, pool_overflow.
"""
from __future__ import annotations

import time
from datetime import timedelta

import pytest

from src.blackboard.message_pool import (
    MessagePool,
    _VALID_CHANNELS,
)
from src.utils.time_utils import now_kst


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

def _valid_msg(**overrides) -> dict:
    """C4 message_schema 필수 9 필드 충족 기본 메시지."""
    base = {
        "content": "테스트 메시지 내용",
        "cause_by": "test_event",
        "sent_from": "test_agent",
        "priority": "normal",
        "confidence": 0.8,
        "reasoning": "테스트 추론 근거",
        "scope": "005930",
        "action_type": "signal",
        "timestamp": now_kst().isoformat(),
    }
    base.update(overrides)
    return base


def _pool() -> MessagePool:
    """테스트용 MessagePool. config 기본값(TTL=300, max=10000)."""
    return MessagePool()


# ------------------------------------------------------------------
# 1. publish: 정상 게시 + message_id 생성 + 저장
# ------------------------------------------------------------------

def test_publish_generates_message_id_and_stores() -> None:
    pool = _pool()
    msg = _valid_msg()
    msg_id = pool.publish("news_signal", msg)

    assert msg_id.startswith("MSG-")
    assert pool.pool_size() == 1
    active = pool.get_active_messages("news_signal")
    assert len(active) == 1
    assert active[0]["message_id"] == msg_id


# ------------------------------------------------------------------
# 2. publish: 잘못된 채널 → ValueError INVALID_SCOPE
# ------------------------------------------------------------------

def test_publish_invalid_channel_raises() -> None:
    pool = _pool()
    with pytest.raises(ValueError, match="INVALID_SCOPE"):
        pool.publish("not_a_channel", _valid_msg())


# ------------------------------------------------------------------
# 3. publish: 필수 필드 누락 → ValueError MESSAGE_SCHEMA_INVALID
# ------------------------------------------------------------------

def test_publish_missing_required_field_raises() -> None:
    pool = _pool()
    msg = _valid_msg()
    del msg["content"]
    with pytest.raises(ValueError, match="MESSAGE_SCHEMA_INVALID"):
        pool.publish("news_signal", msg)


# ------------------------------------------------------------------
# 4. publish: 잘못된 priority → ValueError
# ------------------------------------------------------------------

def test_publish_invalid_priority_raises() -> None:
    pool = _pool()
    msg = _valid_msg(priority="super_urgent")
    with pytest.raises(ValueError, match="MESSAGE_SCHEMA_INVALID"):
        pool.publish("news_signal", msg)


# ------------------------------------------------------------------
# 5. publish: 잘못된 action_type → ValueError
# ------------------------------------------------------------------

def test_publish_invalid_action_type_raises() -> None:
    pool = _pool()
    msg = _valid_msg(action_type="buy_order")
    with pytest.raises(ValueError, match="MESSAGE_SCHEMA_INVALID"):
        pool.publish("news_signal", msg)


# ------------------------------------------------------------------
# 6. publish: expires_at 자동 계산 (ttl 경유)
# ------------------------------------------------------------------

def test_publish_expires_at_auto_computed_from_ttl() -> None:
    pool = _pool()
    msg = _valid_msg()
    msg["ttl"] = 60  # 60초 TTL
    pool.publish("news_signal", msg)

    assert "expires_at" in msg
    from datetime import datetime
    exp = datetime.fromisoformat(msg["expires_at"])
    diff = exp - now_kst()
    # 59 ~ 61초 사이
    assert timedelta(seconds=58) < diff < timedelta(seconds=62)


def test_publish_malformed_expires_at_raises() -> None:
    pool = _pool()
    msg = _valid_msg(expires_at="not-a-datetime")

    with pytest.raises(ValueError, match="MESSAGE_SCHEMA_INVALID: expires_at"):
        pool.publish("news_signal", msg)

    assert pool.pool_size() == 0


def test_publish_malformed_timestamp_raises() -> None:
    pool = _pool()
    msg = _valid_msg(timestamp="2026-99-99T99:99:99")

    with pytest.raises(ValueError, match="MESSAGE_SCHEMA_INVALID: timestamp"):
        pool.publish("news_signal", msg)

    assert pool.pool_size() == 0


def test_publish_valid_expires_at_still_publishes() -> None:
    pool = _pool()
    expires_at = (now_kst() + timedelta(minutes=5)).isoformat()
    msg = _valid_msg(expires_at=expires_at)

    msg_id = pool.publish("news_signal", msg)

    assert msg_id.startswith("MSG-")
    assert pool.pool_size() == 1
    assert msg["expires_at"] == expires_at


def test_publish_normalizes_malformed_confidence_and_ttl() -> None:
    pool = _pool()
    msg = _valid_msg(confidence="high", ttl="bad")

    pool.publish("news_signal", msg)

    assert msg["confidence"] == 0.5
    assert "expires_at" in msg


def test_publish_clamps_confidence_range() -> None:
    pool = _pool()
    high = _valid_msg(confidence=2.0)
    low = _valid_msg(confidence=-1.0)

    pool.publish("news_signal", high)
    pool.publish("risk_warning", low)

    assert high["confidence"] == 1.0
    assert low["confidence"] == 0.0


# ------------------------------------------------------------------
# 7. subscribe: 게시 시 handler 수신
# ------------------------------------------------------------------

def test_subscribe_receives_published_message() -> None:
    pool = _pool()
    received: list[dict] = []
    pool.subscribe("news_signal", lambda m: received.append(m))

    pool.publish("news_signal", _valid_msg())

    assert len(received) == 1
    assert received[0]["content"] == "테스트 메시지 내용"


# ------------------------------------------------------------------
# 8. subscribe: filter_fn 이 False 반환 시 handler 미호출
# ------------------------------------------------------------------

def test_subscribe_filter_fn_blocks_unmatched() -> None:
    pool = _pool()
    received: list[dict] = []

    def only_urgent(m: dict) -> bool:
        return m.get("priority") == "urgent"

    pool.subscribe("news_signal", lambda m: received.append(m), filter_fn=only_urgent)
    pool.publish("news_signal", _valid_msg(priority="normal"))

    assert len(received) == 0


# ------------------------------------------------------------------
# 9. subscribe: filter_fn 통과 시 수신
# ------------------------------------------------------------------

def test_subscribe_filter_fn_passes_matched() -> None:
    pool = _pool()
    received: list[dict] = []

    def only_urgent(m: dict) -> bool:
        return m.get("priority") == "urgent"

    pool.subscribe("news_signal", lambda m: received.append(m), filter_fn=only_urgent)
    pool.publish("news_signal", _valid_msg(priority="urgent"))

    assert len(received) == 1


# ------------------------------------------------------------------
# 10. subscribe: at-least-once. 동일 메시지 multi-handler
# ------------------------------------------------------------------

def test_subscribe_at_least_once_multiple_handlers() -> None:
    pool = _pool()
    calls_a: list[dict] = []
    calls_b: list[dict] = []
    pool.subscribe("news_signal", lambda m: calls_a.append(m))
    pool.subscribe("news_signal", lambda m: calls_b.append(m))

    pool.publish("news_signal", _valid_msg())

    assert len(calls_a) == 1
    assert len(calls_b) == 1


# ------------------------------------------------------------------
# 11. ack: 정상 ack
# ------------------------------------------------------------------

def test_ack_single_subscriber() -> None:
    pool = _pool()
    msg_id = pool.publish("news_signal", _valid_msg())
    pool.ack(msg_id, ack_by="fda_agent")

    entry = pool._messages[msg_id]
    assert entry.ack_count == 1
    assert "fda_agent" in entry.acked_by


# ------------------------------------------------------------------
# 12. ack: 같은 ack_by 중복 → 한 번만 count
# ------------------------------------------------------------------

def test_ack_deduplicates_same_ack_by() -> None:
    pool = _pool()
    msg_id = pool.publish("news_signal", _valid_msg())
    pool.ack(msg_id, ack_by="fda_agent")
    pool.ack(msg_id, ack_by="fda_agent")  # 중복

    entry = pool._messages[msg_id]
    assert entry.ack_count == 1


# ------------------------------------------------------------------
# 13. ack: 없는 message_id → ValueError ACK_TARGET_NOT_FOUND
# ------------------------------------------------------------------

def test_ack_unknown_id_raises() -> None:
    pool = _pool()
    with pytest.raises(ValueError, match="ACK_TARGET_NOT_FOUND"):
        pool.ack("MSG-99999999-9999-XXXXXXXX")


# ------------------------------------------------------------------
# 14. supersede: old 마킹 + new["supersedes"] 설정
# ------------------------------------------------------------------

def test_supersede_marks_old_superseded() -> None:
    pool = _pool()
    old_id = pool.publish("risk_warning", _valid_msg(action_type="alert"))
    new_id = pool.publish("risk_warning", _valid_msg(action_type="alert"))

    pool.supersede(old_id, new_id)

    old_entry = pool._messages[old_id]
    new_entry = pool._messages[new_id]
    assert old_entry.superseded_by == new_id
    assert new_entry.message.get("supersedes") == old_id


# ------------------------------------------------------------------
# 15. supersede: 없는 ID → ValueError SUPERSEDE_CONFLICT
# ------------------------------------------------------------------

def test_supersede_unknown_raises() -> None:
    pool = _pool()
    real_id = pool.publish("risk_warning", _valid_msg(action_type="alert"))

    with pytest.raises(ValueError, match="SUPERSEDE_CONFLICT"):
        pool.supersede("MISSING-ID", real_id)

    with pytest.raises(ValueError, match="SUPERSEDE_CONFLICT"):
        pool.supersede(real_id, "MISSING-ID")


# ------------------------------------------------------------------
# 16. expire: 만료 메시지 제거
# ------------------------------------------------------------------

def test_expire_removes_stale_messages() -> None:
    pool = _pool()
    # 이미 만료된 expires_at 직접 설정
    msg = _valid_msg()
    past = (now_kst() - timedelta(hours=1)).isoformat()
    msg["expires_at"] = past
    msg["ttl"] = 1
    msg_id = pool.publish("news_signal", msg)

    removed = pool.expire(now=now_kst())

    assert removed == 1
    assert pool.pool_size() == 0


# ------------------------------------------------------------------
# 17. expire: 미래 메시지는 유지
# ------------------------------------------------------------------

def test_expire_keeps_future_messages() -> None:
    pool = _pool()
    msg = _valid_msg()
    msg["ttl"] = 3600  # 1시간 TTL
    pool.publish("news_signal", msg)

    removed = pool.expire(now=now_kst())

    assert removed == 0
    assert pool.pool_size() == 1


# ------------------------------------------------------------------
# 18. dependency_activation: required 채널 모두 publish 시 callback
# ------------------------------------------------------------------

def test_dependency_activation_callback_when_all_channels_seen() -> None:
    pool = _pool()
    activated: list[dict] = []

    pool.register_dependency(
        "fda_cold_activate",
        {"news_signal", "risk_warning"},
        lambda m: activated.append(m),
    )

    # 첫 번째 채널만 publish → callback 미발동
    pool.publish("news_signal", _valid_msg())
    assert len(activated) == 0

    # 두 번째 채널 publish → callback 발동
    pool.publish("risk_warning", _valid_msg(action_type="alert"))
    assert len(activated) == 1
    assert "news_signal" in activated[0]
    assert "risk_warning" in activated[0]


# ------------------------------------------------------------------
# 19. dependency_activation: 재활성화 지원 (seen 리셋 후 재발동)
# ------------------------------------------------------------------

def test_dependency_activation_resets_after_trigger() -> None:
    pool = _pool()
    count: list[int] = [0]

    pool.register_dependency(
        "reuse_test",
        {"news_signal", "risk_warning"},
        lambda m: count.__setitem__(0, count[0] + 1),
    )

    pool.publish("news_signal", _valid_msg())
    pool.publish("risk_warning", _valid_msg(action_type="alert"))
    assert count[0] == 1

    # 두 번째 사이클
    pool.publish("news_signal", _valid_msg())
    pool.publish("risk_warning", _valid_msg(action_type="alert"))
    assert count[0] == 2


# ------------------------------------------------------------------
# 20. get_active_messages: superseded 제외
# ------------------------------------------------------------------

def test_get_active_messages_excludes_superseded() -> None:
    pool = _pool()
    old_id = pool.publish("risk_warning", _valid_msg(action_type="alert"))
    new_id = pool.publish("risk_warning", _valid_msg(action_type="alert"))

    pool.supersede(old_id, new_id)

    active = pool.get_active_messages("risk_warning")
    active_ids = [m["message_id"] for m in active]
    assert old_id not in active_ids
    assert new_id in active_ids


# ------------------------------------------------------------------
# 21. pool_overflow: expire 후 RuntimeError
# ------------------------------------------------------------------

def test_pool_overflow_forces_expire_then_raises() -> None:
    # max_pool_size=2 + default_ttl=1초 (즉시 만료) 설정
    cfg = {"message_pool": {"default_ttl_sec": 1, "max_pool_size": 2}}
    pool = MessagePool(config=cfg)

    # 2개 채운 뒤 즉시 만료 시뮬레이션
    msg1 = _valid_msg()
    msg1["ttl"] = 1
    id1 = pool.publish("news_signal", msg1)
    pool._messages[id1].message["expires_at"] = (
        now_kst() - timedelta(seconds=2)
    ).isoformat()

    msg2 = _valid_msg()
    msg2["ttl"] = 1
    id2 = pool.publish("risk_warning", msg2)
    pool._messages[id2].message["expires_at"] = (
        now_kst() - timedelta(seconds=2)
    ).isoformat()

    # pool size == 2 == max, publish 시 expire 호출 → 2개 제거 → 1개 추가 성공
    pool.publish("news_signal", _valid_msg())
    assert pool.pool_size() == 1


# ------------------------------------------------------------------
# 22. subscribe: 미등록 채널 → ValueError INVALID_SCOPE
# ------------------------------------------------------------------

def test_subscribe_invalid_channel_raises() -> None:
    pool = _pool()
    with pytest.raises(ValueError, match="INVALID_SCOPE"):
        pool.subscribe("invalid_channel", lambda m: None)


# ------------------------------------------------------------------
# 23. valid_channels: 15개 확인
# ------------------------------------------------------------------

def test_valid_channels_count() -> None:
    assert len(_VALID_CHANNELS) == 15


# ------------------------------------------------------------------
# 24. publish: risk_level None 허용
# ------------------------------------------------------------------

def test_publish_risk_level_none_allowed() -> None:
    pool = _pool()
    msg = _valid_msg(risk_level=None)
    msg_id = pool.publish("news_signal", msg)
    assert msg_id.startswith("MSG-")


# ------------------------------------------------------------------
# 25. publish: risk_level 잘못된 값 → ValueError
# ------------------------------------------------------------------

def test_publish_invalid_risk_level_raises() -> None:
    pool = _pool()
    msg = _valid_msg(risk_level="critical")
    with pytest.raises(ValueError, match="MESSAGE_SCHEMA_INVALID"):
        pool.publish("news_signal", msg)


# ------------------------------------------------------------------
# 26. ack: 만료 메시지 ack → ValueError MESSAGE_EXPIRED
# ------------------------------------------------------------------

def test_ack_expired_raises_message_expired() -> None:
    """ttl=1초 메시지 publish 후 1.5초 대기 → ack 시 MESSAGE_EXPIRED."""
    pool = _pool()
    msg = _valid_msg()
    msg["ttl"] = 1   # 1초 후 만료
    mid = pool.publish("news_signal", msg)
    time.sleep(1.5)   # 만료 대기
    with pytest.raises(ValueError, match="MESSAGE_EXPIRED"):
        pool.ack(mid, ack_by="test_subscriber")


# ------------------------------------------------------------------
# 27. dependency_activation: 같은 채널 두 번 publish → callback 없음
# ------------------------------------------------------------------

def test_dependency_activation_same_channel_twice_does_not_trigger() -> None:
    """같은 채널에만 두 번 publish → callback 호출 안 됨 (다른 required channel 미도달)."""
    pool = _pool()
    call_count = [0]
    pool.register_dependency(
        "t",
        {"news_signal", "risk_warning"},
        lambda msgs: call_count.__setitem__(0, call_count[0] + 1),
    )
    pool.publish("news_signal", _valid_msg())
    pool.publish("news_signal", _valid_msg())   # 두 번째: seen 변화 없음
    assert call_count[0] == 0, "risk_warning 미도달이므로 callback 없어야 함"

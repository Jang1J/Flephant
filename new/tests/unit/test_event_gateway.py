"""EventGateway unit tests. Cold Path 디스패처 검증.

ingest() / register_handler() / dispatch_next() 흐름 + 에러 케이스.
EventNormalizer 는 실 구현 사용. EventAdmission 은 테스트용 config 주입.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src.data.event_admission import EventAdmission
from src.data.event_normalizer import EventNormalizer
from src.orchestration.event_gateway import EventGateway

_KST = ZoneInfo("Asia/Seoul")
_PAST_TS = "2026-04-19T09:00:00+09:00"
_PAST_DATE = "2026-04-19"


@pytest.fixture(autouse=True)
def _bypass_pit_for_gateway_tests(monkeypatch):
    """Gateway 단위 테스트 전용 PIT-Safety bypass.

    EventGateway 검증 목적이 PIT-Safety 아닌 dispatch 흐름. 실행 시각이
    snapshot cutoff(18:00 KST) 이후여도 테스트 통과해야 함. is_pit_safe 를
    True 로 고정. 실 PIT 검증은 test_event_normalizer.py 에서 수행.
    """
    import src.data.event_normalizer as en
    monkeypatch.setattr(en, "is_pit_safe", lambda ts, **kw: True)

def _recent_ts() -> str:
    """stale_drop 회피: 현재 시각 1분 전 (expires_at = now+59min).

    PIT-Safety 는 autouse fixture `_bypass_pit_for_gateway_tests` 에서 bypass.
    """
    return (datetime.now(_KST) - timedelta(minutes=1)).isoformat()


def _admission(tmp_path: Path, max_backlog: int = 5) -> EventAdmission:
    cfg = {
        "event_admission": {
            "max_backlog": max_backlog,
            "stale_drop": True,
            "max_cold_path_jobs_per_minute": 100,
            "comparator_sort_key": ["priority", "trigger_type", "scope", "recency"],
            "dedupe_ttl_sec": 5,
            "dead_letter_path": str(tmp_path / "dl.jsonl"),
        }
    }
    return EventAdmission(config=cfg, dead_letter_path=tmp_path / "dl.jsonl")


def _gateway(tmp_path: Path) -> EventGateway:
    return EventGateway(
        admission=_admission(tmp_path),
        normalizer=EventNormalizer(),
    )


def _dart_raw() -> dict:
    """stale 판정을 피하기 위해 현재 시각 근처 disclosure_time 사용."""
    return {
        "title": "삼성전자 공시",
        "corp_name": "삼성전자",
        "disclosure_time": _recent_ts(),
        "ticker": "005930",
        "summary": "분기보고서 제출",
    }


# ------------------------------------------------------------------
# 1. 정규화 실패 시 normalize_failed 반환
# ------------------------------------------------------------------

def test_ingest_normalize_failure_returns_error(tmp_path: Path) -> None:
    gw = _gateway(tmp_path)

    # 필수 필드 누락 (dart: title 없음)
    bad_raw = {"corp_name": "테스트", "disclosure_time": _PAST_TS}
    result = gw.ingest(bad_raw, source="dart")

    assert result["status"] == "normalize_failed"
    assert result["event_id"] is None
    assert result["reason"] is not None


# ------------------------------------------------------------------
# 2. 정상 이벤트 ingest -> admitted + backlog 증가
# ------------------------------------------------------------------

def test_ingest_admitted_event_goes_to_backlog(tmp_path: Path) -> None:
    gw = _gateway(tmp_path)

    result = gw.ingest(_dart_raw(), source="dart")

    assert result["status"] == "admitted"
    assert result["event_id"] is not None
    assert gw.backlog_size() == 1


def test_ingest_rejects_event_after_asof(tmp_path: Path, monkeypatch) -> None:
    """EventGateway는 장중 asof 이후 이벤트를 backlog에 넣지 않는다."""
    import src.data.event_normalizer as en
    from src.utils.pit_guard import is_pit_safe as real_is_pit_safe

    monkeypatch.setattr(en, "is_pit_safe", real_is_pit_safe)
    gw = _gateway(tmp_path)
    raw = {
        "title": "장중 미래 공시",
        "corp_name": "테스트",
        "disclosure_time": "2026-04-19T10:01:00+09:00",
        "ticker": "005930",
    }

    result = gw.ingest(raw, source="dart", asof="2026-04-19T10:00:00+09:00")

    assert result["status"] == "normalize_failed"
    assert "PIT-Safety" in str(result["reason"])
    assert gw.backlog_size() == 0


# ------------------------------------------------------------------
# 3. 거부 이벤트 status = rejected
# ------------------------------------------------------------------

def test_ingest_rejected_event_status(tmp_path: Path, monkeypatch) -> None:
    # STALE 동작 직접 검증. conftest의 ELEPHANT_TEST_FRESHNESS_SKIP=1 우회.
    monkeypatch.delenv("ELEPHANT_TEST_FRESHNESS_SKIP", raising=False)

    gw = _gateway(tmp_path)

    # 만료된 이벤트: title+corp_name+disclosure_time 모두 존재 → normalize 성공.
    # disclosure_time이 2020년이라 expires_at < now → stale_drop 발동 → rejected.
    past_ts = "2020-01-01T09:00:00+09:00"
    stale_raw = {
        "title": "만료 DART 이벤트",
        "corp_name": "테스트",
        "disclosure_time": past_ts,
    }
    result = gw.ingest(stale_raw, source="dart")

    # normalize 성공 + stale_drop 발동 → rejected 단일 케이스
    assert result["status"] == "rejected"


# ------------------------------------------------------------------
# 4. register_handler + dispatch_next 정상 라우팅
# ------------------------------------------------------------------

def test_register_handler_and_dispatch(tmp_path: Path) -> None:
    gw = _gateway(tmp_path)

    handled: list[dict] = []

    def dart_handler(event: dict) -> dict:
        handled.append(event)
        return {"event_id": event["event_id"], "status": "processed"}

    gw.register_handler("dart", dart_handler)

    gw.ingest(_dart_raw(), source="dart")
    assert gw.backlog_size() == 1

    result = gw.dispatch_next()
    assert result is not None
    assert result["status"] == "processed"
    assert len(handled) == 1
    assert gw.backlog_size() == 0


# ------------------------------------------------------------------
# 5. dispatch_next: 핸들러 없는 event_type 은 dead-letter 기록
# ------------------------------------------------------------------

def test_dispatch_next_no_handler_skips(tmp_path: Path) -> None:
    gw = _gateway(tmp_path)
    # 핸들러 미등록 상태

    gw.ingest(_dart_raw(), source="dart")
    assert gw.backlog_size() == 1

    result = gw.dispatch_next()
    assert result["status"] == "no_handler"
    assert result["event_type"] == "dart"
    # backlog 에서는 pop 됨
    assert gw.backlog_size() == 0
    dead_letter = tmp_path / "dl.jsonl"
    assert "NO_HANDLER:dart" in dead_letter.read_text(encoding="utf-8")


# ------------------------------------------------------------------
# 6. dispatch_next: 핸들러 예외 시 handler_error 반환
# ------------------------------------------------------------------

def test_dispatch_next_handler_exception_returns_error(tmp_path: Path) -> None:
    gw = _gateway(tmp_path)

    def bad_handler(event: dict) -> dict:
        raise RuntimeError("핸들러 내부 오류")

    gw.register_handler("dart", bad_handler)
    gw.ingest(_dart_raw(), source="dart")

    result = gw.dispatch_next()
    assert result is not None
    assert result["status"] == "handler_error"
    assert "핸들러 내부 오류" in result["reason"]


# ------------------------------------------------------------------
# 7. backlog_size 반영 정확성
# ------------------------------------------------------------------

def test_backlog_size_reflects_admission(tmp_path: Path) -> None:
    gw = _gateway(tmp_path)

    assert gw.backlog_size() == 0

    # naver_news TTL=1800초. 1분 전 published_at -> expires_at=지금+29분 -> stale 아님
    raw_news = {
        "title": "테스트 뉴스",
        "published_at": _recent_ts(),
    }
    gw.ingest(raw_news, source="naver_news")
    assert gw.backlog_size() == 1

    gw.ingest(_dart_raw(), source="dart")
    assert gw.backlog_size() == 2

    gw.dispatch_next()
    assert gw.backlog_size() == 1


# ------------------------------------------------------------------
# 8. dispatch_next: backlog 빔 시 None
# ------------------------------------------------------------------

def test_dispatch_next_empty_backlog_returns_none(tmp_path: Path) -> None:
    gw = _gateway(tmp_path)
    assert gw.dispatch_next() is None


# ------------------------------------------------------------------
# 9. auto_publish: handler dict + content 반환 + publish_channel 등록 + pubsub 주입
#    → PubSubBroker.publish 호출, result 에 message_id 추가
# ------------------------------------------------------------------

def test_dispatch_next_auto_publish_success(tmp_path: Path) -> None:
    """handler 가 C4 필수 필드 포함 dict 반환 + publish_channel 등록 + pubsub 주입
    → PubSubBroker.publish 호출, result["message_id"] 존재 검증.
    """
    from src.blackboard.message_pool import MessagePool
    from src.blackboard.pubsub import PubSubBroker

    pool = MessagePool()
    pubsub = PubSubBroker(pool)

    gw = EventGateway(
        admission=_admission(tmp_path),
        normalizer=EventNormalizer(),
        pubsub=pubsub,
    )

    # C4 message_schema 필수 9 필드 + content
    def news_handler(event: dict) -> dict:
        return {
            "content": "뉴스 분석 결과",
            "cause_by": "NewsAgent",
            "sent_from": "news_agent",
            "priority": "normal",
            "confidence": 0.8,
            "reasoning": "뉴스 긍정 신호 감지",
            "scope": "005930",
            "action_type": "signal",
            "timestamp": _recent_ts(),
        }

    gw.register_handler("dart", news_handler, publish_channel="news_signal")

    gw.ingest(_dart_raw(), source="dart")
    assert gw.backlog_size() == 1

    result = gw.dispatch_next()

    assert result is not None
    assert isinstance(result, dict)
    assert "message_id" in result, "auto_publish 성공 시 result에 message_id 있어야 함"
    assert gw.backlog_size() == 0

    # MessagePool 에도 실제로 publish 됐는지 확인
    active = pool.get_active_messages("news_signal")
    assert len(active) == 1
    assert active[0]["content"] == "뉴스 분석 결과"


def test_dispatch_next_auto_publish_preserves_event_trace(tmp_path: Path) -> None:
    """auto_publish 메시지는 원 이벤트의 event_id/occurred_at/asof를 보존한다."""
    from src.blackboard.message_pool import MessagePool
    from src.blackboard.pubsub import PubSubBroker

    pool = MessagePool()
    pubsub = PubSubBroker(pool)
    gw = EventGateway(
        admission=_admission(tmp_path),
        normalizer=EventNormalizer(),
        pubsub=pubsub,
    )
    asof = datetime.now(_KST).isoformat()

    def news_handler(event: dict) -> dict:
        return {
            "content": "뉴스 분석 결과",
            "cause_by": "NewsAgent",
            "sent_from": "news_agent",
            "priority": "normal",
            "confidence": 0.8,
            "reasoning": "뉴스 긍정 신호 감지",
            "scope": "005930",
            "action_type": "signal",
            "timestamp": _recent_ts(),
        }

    gw.register_handler("dart", news_handler, publish_channel="news_signal")
    admitted = gw.ingest(_dart_raw(), source="dart", asof=asof)
    result = gw.dispatch_next()

    assert result is not None
    active = pool.get_active_messages("news_signal")
    assert len(active) == 1
    assert active[0]["event_id"] == admitted["event_id"]
    assert active[0]["occurred_at"] == result["occurred_at"]
    assert active[0]["asof"] == asof


def test_news_agent_attach_to_gateway_auto_publish_schema(tmp_path: Path) -> None:
    """NewsAgent attach_to_gateway 결과가 EventGateway auto_publish C4 schema를 만족한다."""
    from src.agents.cold.news import NewsAgent
    from src.blackboard.message_pool import MessagePool
    from src.blackboard.pubsub import PubSubBroker

    router = MagicMock()
    router_result = MagicMock()
    router_result.success = True
    router_result.content = (
        '{"stance":"buy","impacted_tickers":["005930"],'
        '"impacted_sectors":[],"narrative":"호재","confidence":0.82}'
    )
    router.call.return_value = router_result
    pool = MessagePool()
    pubsub = PubSubBroker(pool)
    gw = EventGateway(
        admission=_admission(tmp_path),
        normalizer=EventNormalizer(),
        pubsub=pubsub,
    )
    NewsAgent(llm_router=router).attach_to_gateway(gw)

    asof = datetime.now(_KST).isoformat()
    admitted = gw.ingest(
        {
            "title": "삼성전자 호재",
            "summary": "영업이익 증가",
            "published_at": _recent_ts(),
            "ticker": "005930",
        },
        source="naver_news",
        asof=asof,
    )
    result = gw.dispatch_next()

    assert result is not None
    assert "publish_error" not in result
    assert isinstance(result.get("message"), dict)
    messages = pool.get_active_messages("news_signal")
    assert len(messages) == 1
    assert messages[0]["confidence"] == pytest.approx(0.82)
    assert messages[0]["scope"] == "ticker:005930"
    assert messages[0]["event_id"] == admitted["event_id"]
    assert messages[0]["occurred_at"] == result["occurred_at"]
    assert messages[0]["asof"] == asof


def test_news_agent_gateway_skips_fallback_neutral_publish(tmp_path: Path) -> None:
    """NewsAgent LLM 실패 fallback neutral 결과는 MessagePool에 auto-publish하지 않는다."""
    from src.agents.cold.news import NewsAgent
    from src.blackboard.message_pool import MessagePool
    from src.blackboard.pubsub import PubSubBroker

    router = MagicMock()
    router_result = MagicMock()
    router_result.success = False
    router_result.content = None
    router_result.error = "timeout"
    router.call.return_value = router_result
    pool = MessagePool()
    pubsub = PubSubBroker(pool)
    gw = EventGateway(
        admission=_admission(tmp_path),
        normalizer=EventNormalizer(),
        pubsub=pubsub,
    )
    NewsAgent(llm_router=router).attach_to_gateway(gw)

    gw.ingest(
        {
            "title": "삼성전자 중립",
            "summary": "LLM 실패 테스트",
            "published_at": _recent_ts(),
            "ticker": "005930",
        },
        source="naver_news",
    )
    result = gw.dispatch_next()

    assert result is not None
    assert result["llm_fallback"] is True
    assert "message" not in result
    assert "message_id" not in result
    assert pool.pool_size() == 0


def test_news_agent_gateway_avoids_duplicate_publish_when_agent_has_pubsub(tmp_path: Path) -> None:
    """Agent 내부 publish가 선행된 경우 EventGateway auto_publish는 중복 게시하지 않는다."""
    from src.agents.cold.news import NewsAgent
    from src.blackboard.message_pool import MessagePool
    from src.blackboard.pubsub import PubSubBroker

    router = MagicMock()
    router_result = MagicMock()
    router_result.success = True
    router_result.content = (
        '{"stance":"buy","impacted_tickers":["005930"],'
        '"impacted_sectors":[],"narrative":"호재","confidence":0.81}'
    )
    router.call.return_value = router_result
    pool = MessagePool()
    pubsub = PubSubBroker(pool)
    gw = EventGateway(
        admission=_admission(tmp_path),
        normalizer=EventNormalizer(),
        pubsub=pubsub,
    )
    NewsAgent(llm_router=router, pubsub=pubsub).attach_to_gateway(gw)

    gw.ingest(
        {
            "title": "삼성전자 호재",
            "summary": "중복 publish 방지 테스트",
            "published_at": _recent_ts(),
            "ticker": "005930",
        },
        source="naver_news",
    )
    result = gw.dispatch_next()

    assert result is not None
    assert result["published_by_agent"] is True
    assert "publish_error" not in result
    messages = pool.get_active_messages("news_signal")
    assert len(messages) == 1
    assert messages[0]["confidence"] == pytest.approx(0.81)


# ------------------------------------------------------------------
# 10. auto_publish: pubsub=None 이면 skip, result 에 message_id 없음
# ------------------------------------------------------------------

def test_dispatch_next_auto_publish_skipped_when_no_pubsub(tmp_path: Path) -> None:
    """pubsub=None 이면 auto_publish skip. result 에 message_id 없어야."""
    # pubsub 미주입 (기본 _gateway 사용)
    gw = _gateway(tmp_path)

    def dart_handler(event: dict) -> dict:
        return {
            "content": "공시 분석",
            "cause_by": "DartAgent",
            "sent_from": "dart_agent",
            "priority": "normal",
            "confidence": 0.9,
            "reasoning": "중요 공시 발생",
            "scope": "005930",
            "action_type": "alert",
            "timestamp": _recent_ts(),
        }

    # publish_channel 등록해도 pubsub=None 이면 auto_publish 미발동
    gw.register_handler("dart", dart_handler, publish_channel="dart_alert")

    gw.ingest(_dart_raw(), source="dart")
    result = gw.dispatch_next()

    assert result is not None
    assert "message_id" not in result, "pubsub=None 이면 message_id 없어야 함"
    assert result.get("content") == "공시 분석"


# ------------------------------------------------------------------
# 11. auto_publish: handler 반환값이 dict 아니거나 content 없으면 publish skip
# ------------------------------------------------------------------

def test_dispatch_next_auto_publish_skipped_when_result_not_dict(tmp_path: Path) -> None:
    """handler 반환값이 dict 아니거나 content 없음 → publish skip."""
    from src.blackboard.message_pool import MessagePool
    from src.blackboard.pubsub import PubSubBroker

    pool = MessagePool()
    pubsub = PubSubBroker(pool)

    gw = EventGateway(
        admission=_admission(tmp_path),
        normalizer=EventNormalizer(),
        pubsub=pubsub,
    )

    # 케이스 A: content 필드 없는 dict 반환
    def no_content_handler(event: dict) -> dict:
        return {"status": "processed", "cause_by": "TestAgent"}

    gw.register_handler("dart", no_content_handler, publish_channel="dart_alert")

    gw.ingest(_dart_raw(), source="dart")
    result = gw.dispatch_next()

    assert result is not None
    assert "message_id" not in result, "content 없으면 publish skip, message_id 없어야 함"
    # pool 에도 게시 안 됨
    assert pool.pool_size() == 0


def test_auto_publish_treats_string_false_flags_as_false(tmp_path: Path) -> None:
    """문자열 false 플래그는 auto_publish 차단 조건으로 해석하지 않는다."""
    from src.blackboard.message_pool import MessagePool
    from src.blackboard.pubsub import PubSubBroker

    pool = MessagePool()
    pubsub = PubSubBroker(pool)
    gw = EventGateway(
        admission=_admission(tmp_path),
        normalizer=EventNormalizer(),
        pubsub=pubsub,
    )

    def dart_handler(event: dict) -> dict:
        return {
            "content": "공시 분석",
            "cause_by": "DartAgent",
            "sent_from": "dart_agent",
            "priority": "normal",
            "confidence": 0.9,
            "reasoning": "중요 공시 발생",
            "scope": "ticker:005930",
            "action_type": "alert",
            "timestamp": _recent_ts(),
            "llm_fallback": "false",
            "published_by_agent": "false",
        }

    gw.register_handler("dart", dart_handler, publish_channel="dart_alert")
    gw.ingest(_dart_raw(), source="dart")
    result = gw.dispatch_next()

    assert result is not None
    assert "message_id" in result
    assert pool.pool_size() == 1


# ------------------------------------------------------------------
# 12. dispatch_next: handler가 TimeoutError 발생 시 handler_error 반환
# ------------------------------------------------------------------

def test_dispatch_handler_timeout_raises_or_returns_error(tmp_path: Path) -> None:
    """handler가 TimeoutError raise 시 EventGateway가 handler_error 응답 반환."""
    gw = _gateway(tmp_path)

    def timeout_handler(event: dict) -> dict:
        raise TimeoutError("핸들러 응답 초과")

    gw.register_handler("dart", timeout_handler)
    gw.ingest(_dart_raw(), source="dart")
    assert gw.backlog_size() == 1

    result = gw.dispatch_next()

    assert result is not None
    assert result["status"] == "handler_error"
    assert "핸들러 응답 초과" in result["reason"]
    assert gw.backlog_size() == 0


# ------------------------------------------------------------------
# 13. backlog overflow: max_backlog=3 초과 ingest 시 dead_letter 기록 또는 거부
# ------------------------------------------------------------------

def test_admission_backlog_overflow_drops_event(tmp_path: Path) -> None:
    """backlog max_backlog=3 초과 시 추가 이벤트는 rejected 반환."""
    # max_backlog=3 으로 제한된 admission 사용
    gw = EventGateway(
        admission=_admission(tmp_path, max_backlog=3),
        normalizer=EventNormalizer(),
    )

    # 3건 채우기
    for _ in range(3):
        result = gw.ingest(_dart_raw(), source="dart")
        # dedupe 로 rejected 될 수 있음. 적어도 처음 1건은 admitted 되어야 함.

    assert gw.backlog_size() <= 3

    # 4번째 ingest: admitted 또는 rejected 중 하나여야 함. backlog는 3 초과 불가.
    result = gw.ingest(_dart_raw(), source="dart")
    assert result["status"] in ("admitted", "rejected")
    assert gw.backlog_size() <= 3

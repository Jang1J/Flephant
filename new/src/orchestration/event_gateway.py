"""Cold Path 이벤트 진입점. EventAdmission 필터 통과 후 에이전트 디스패치.

S2-0: 기본 ingest / register_handler / dispatch_next 구현.
S2-1: PubSubBroker 연동 + auto_publish 옵션 추가.
  - register_handler(event_type, handler, publish_channel=채널명) 으로
    handler 반환 dict 를 자동으로 MessagePool 에 publish.
  - publish_channel 은 api_contracts.md message_taxonomy.publish_channels 중 1개.
  - pubsub=None 이면 auto_publish 비활성 (기존 동작 유지).
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from src.data.event_admission import EventAdmission
from src.data.event_normalizer import EventNormalizer
from src.utils.logger import get_logger
from src.utils.safe_cast import safe_bool

logger = get_logger("event_gateway")
_KST = ZoneInfo("Asia/Seoul")

# PubSubBroker 는 optional dependency. import 실패 시 auto_publish 비활성
try:
    from src.blackboard.pubsub import PubSubBroker
    _PUBSUB_AVAILABLE = True
except ImportError:
    _PUBSUB_AVAILABLE = False
    PubSubBroker = None  # type: ignore[assignment,misc]


class EventGateway:
    """Cold Path 이벤트 디스패처.

    Pipeline:
      raw_event(source dict) -> EventNormalizer.normalize() -> EventAdmission.admit()
        -> (통과) backlog 대기 -> pop_next() -> 에이전트 핸들러 dispatch
        -> (선택) handler 반환값 MessagePool auto_publish
        -> (거부) dead_letter_log

    에이전트 핸들러:
      register_handler(event_type, handler, publish_channel=None)
      etype: "news" | "dart" | "macro" | "us_market" | "community" | "regime" | "investor_flow"

    auto_publish:
      pubsub 주입 + publish_channel 등록 + handler 가 {"content": ..., ...} 반환 시 자동 publish.
      handler 반환값이 None 또는 content 없으면 publish skip.

    Sprint 2 후속 S2-7/8/9 에서 News/Risk/Debate Agent 가 register_handler() 로 자기 event_type 등록.
    """

    def __init__(
        self,
        admission: EventAdmission | None = None,
        normalizer: EventNormalizer | None = None,
        pubsub: "PubSubBroker | None" = None,
    ) -> None:
        self._admission: EventAdmission = admission or EventAdmission()
        self._normalizer: EventNormalizer = normalizer or EventNormalizer()
        self._pubsub: "PubSubBroker | None" = pubsub
        self._handlers: dict[str, list[Callable[[dict], dict | None]]] = {}
        # event_type → handler별 publish_channel (auto_publish 매핑)
        self._auto_publish: dict[str, list[str | None]] = {}

    def register_handler(
        self,
        event_type: str,
        handler: Callable[[dict], dict | None],
        publish_channel: str | None = None,
    ) -> None:
        """에이전트 핸들러 등록.

        event_type: C2 EventNormalize event_type enum.
        publish_channel: 지정 시 handler 반환값을 MessagePool 에 auto_publish.
          api_contracts.md message_taxonomy.publish_channels 에서 1개 선택.
          pubsub 미주입 시 publish_channel 등록해도 auto_publish 실행 안 됨.
        """
        self._handlers.setdefault(event_type, []).append(handler)
        self._auto_publish.setdefault(event_type, []).append(publish_channel)
        logger.info(
            "[event_gateway] 핸들러 등록: event_type=%s publish_channel=%s",
            event_type, publish_channel,
        )

    def ingest(
        self,
        raw_event: dict,
        source: str,
        *,
        asof: datetime | str | None = None,
    ) -> dict:
        """raw 이벤트 수신 -> 정규화 -> admission -> backlog. 결과 요약 반환.

        Returns:
            {
                "event_id": str | None,
                "status": "admitted" | "rejected" | "normalize_failed",
                "reason": str | None
            }
        """
        try:
            effective_asof = asof if asof is not None else datetime.now(_KST)
            normalized = self._normalizer.normalize(raw_event, source, asof=effective_asof)
        except Exception as e:
            logger.warning("[event_gateway] 정규화 실패: source=%s error=%s", source, e)
            return {"event_id": None, "status": "normalize_failed", "reason": str(e)}

        event_id = normalized.get("event_id")
        admitted = self._admission.admit(normalized)

        if admitted:
            logger.debug("[event_gateway] 허용: event_id=%s", event_id)
            return {"event_id": event_id, "status": "admitted", "reason": None}
        else:
            logger.info("[event_gateway] 거부: event_id=%s", event_id)
            return {"event_id": event_id, "status": "rejected", "reason": "see dead_letter_log"}

    def dispatch_next(self) -> dict | None:
        """backlog 최상위 이벤트를 pop 후 등록된 핸들러로 라우팅.

        auto_publish:
          1. handler 반환값이 dict 이고
          2. result.get("content") 가 truthy 이고
          3. event_type 에 publish_channel 등록되어 있고
          4. pubsub 주입 상태

          → PubSubBroker.publish(channel, result) 호출.
          publish 실패는 result["publish_error"] 로 기록 (dispatch 자체는 계속).

        Returns:
          처리 결과 dict (핸들러 반환값 + 선택적 message_id), 또는 backlog 빔/핸들러 없음 시 None.
        """
        event = self._admission.pop_next()
        if event is None:
            return None

        etype = event.get("event_type")
        handlers = list(self._handlers.get(etype) or [])
        publish_channels = list(self._auto_publish.get(etype) or [])
        if not handlers:
            logger.warning("[event_gateway] 핸들러 없음: event_type=%s. skip.", etype)
            self._admission._write_dead_letter(event, f"NO_HANDLER:{etype}")
            return {
                "event_id": event.get("event_id"),
                "status": "no_handler",
                "event_type": etype,
            }

        results: list[dict | None] = []
        errors: list[dict] = []
        for idx, handler in enumerate(handlers):
            try:
                result = handler(event)
                logger.debug(
                    "[event_gateway] 디스패치 완료: event_id=%s event_type=%s handler_index=%d",
                    event.get("event_id"), etype, idx,
                )
            except Exception as e:
                event_id = event.get("event_id")
                logger.warning(
                    "[event_gateway] 핸들러 실패: event_id=%s handler_index=%d err=%s",
                    event_id, idx, e,
                )
                # dead_letter_log에 핸들러 오류 기록 (C11 format)
                self._admission._write_dead_letter(event, f"handler_error: {e}")
                error_result = {
                    "event_id": event_id,
                    "status": "handler_error",
                    "handler_index": idx,
                    "reason": str(e),
                }
                errors.append(error_result)
                results.append(error_result)
                continue

            publish_channel = publish_channels[idx] if idx < len(publish_channels) else None
            self._maybe_auto_publish(event, result, publish_channel)
            results.append(result)

        if len(results) == 1:
            return results[0]
        return {
            "event_id": event.get("event_id"),
            "status": "partial_error" if errors else "processed",
            "event_type": etype,
            "handler_count": len(handlers),
            "handler_results": results,
        }

    def _maybe_auto_publish(
        self,
        event: dict,
        result: dict | None,
        publish_channel: str | None,
    ) -> None:
        # auto_publish: handler 반환 dict + C4 message 존재 + channel 등록 + pubsub 주입.
        # Agent가 이미 직접 publish했거나 LLM fallback neutral 결과이면 중복/오염 방지를 위해 skip한다.
        publish_message = result.get("message") if isinstance(result, dict) else None
        if not isinstance(publish_message, dict):
            publish_message = result if isinstance(result, dict) else None
        if isinstance(publish_message, dict):
            for trace_key in ("event_id", "occurred_at", "asof", "supersedes"):
                value = event.get(trace_key)
                if value is not None and publish_message.get(trace_key) is None:
                    publish_message[trace_key] = value
                    if isinstance(result, dict) and result.get(trace_key) is None:
                        result[trace_key] = value
        if (
            self._pubsub is not None
            and publish_channel is not None
            and isinstance(result, dict)
            and isinstance(publish_message, dict)
            and publish_message.get("content")
            and not safe_bool(result.get("llm_fallback", False), default=False)
            and not safe_bool(result.get("published_by_agent", False), default=False)
            and "message_id" not in result
        ):
            try:
                msg_id = self._pubsub.publish(publish_channel, publish_message)
                result["message_id"] = msg_id
                logger.debug(
                    "[event_gateway] auto_publish: channel=%s message_id=%s",
                    publish_channel, msg_id,
                )
            except Exception as e:
                logger.warning(
                    "[event_gateway] auto_publish 실패: channel=%s err=%s",
                    publish_channel, e,
                )
                result["publish_error"] = str(e)

    def backlog_size(self) -> int:
        """현재 backlog 크기."""
        return self._admission.backlog_size()

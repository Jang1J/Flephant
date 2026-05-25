"""Kafka event producer for paper auto trading lifecycle events.

BE에서 요청한 7개 이벤트를 Kafka topic으로 발행:
  AUTO_TRADING_STARTED    — 자동매매 세션 시작
  DECISION_COMPLETED      — Hot Path 결정 완료 (매 cycle)
  PAPER_ORDER_SUBMITTED   — paper 주문 제출
  PAPER_ORDER_FILLED      — paper 주문 체결
  PAPER_ORDER_FAILED      — paper 주문 실패/거절
  AUTO_TRADING_STOPPED    — 자동매매 정상 종료
  AUTO_TRADING_FAILED     — 자동매매 비정상 종료

kafka-python 미설치 시 no-op (로깅만 수행).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.logger import get_logger

logger = get_logger("kafka_producer")
_KST = ZoneInfo("Asia/Seoul")

PAPER_TRADING_EVENTS: tuple[str, ...] = (
    "AUTO_TRADING_STARTED",
    "DECISION_COMPLETED",
    "PAPER_ORDER_SUBMITTED",
    "PAPER_ORDER_FILLED",
    "PAPER_ORDER_FAILED",
    "AUTO_TRADING_STOPPED",
    "AUTO_TRADING_FAILED",
)

_DEFAULT_TOPIC = "elephant-paper-trading-events"


class KafkaEventProducer:
    """Paper auto trading 이벤트를 Kafka로 발행.

    kafka-python 미설치 또는 broker 미연결 시 no-op (로깅만).
    bootstrap_servers는 환경변수 KAFKA_BOOTSTRAP_SERVERS에서 로드 (하드코딩 금지).
    """

    def __init__(
        self,
        bootstrap_servers: str | None = None,
        topic: str | None = None,
    ) -> None:
        self._topic = topic or os.environ.get(
            "KAFKA_TOPIC", _DEFAULT_TOPIC,
        )
        self._bootstrap_servers = bootstrap_servers or os.environ.get(
            "KAFKA_BOOTSTRAP_SERVERS", "localhost:9092",
        )
        self._producer: Any = None
        self._last_error = ""
        try:
            from kafka import KafkaProducer

            self._producer = KafkaProducer(
                bootstrap_servers=self._bootstrap_servers,
                value_serializer=lambda v: json.dumps(
                    v, ensure_ascii=False, default=str,
                ).encode("utf-8"),
                key_serializer=lambda k: k.encode("utf-8") if k else None,
            )
            logger.info(
                "[kafka] Producer 연결: %s topic=%s", self._bootstrap_servers, self._topic,
            )
        except ImportError:
            self._last_error = "kafka-python 미설치"
            logger.warning("[kafka] %s. 이벤트 로깅만 수행.", self._last_error)
        except Exception as e:
            self._last_error = str(e)
            logger.warning("[kafka] Producer 연결 실패: %s. 이벤트 로깅만 수행.", e)

    def emit(
        self,
        event_type: str,
        *,
        session_id: str = "",
        request_id: str = "",
        bundle_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        """이벤트 발행. Kafka 미연결 시 로깅만."""
        if event_type not in PAPER_TRADING_EVENTS:
            logger.warning("[kafka] 알 수 없는 event_type: %s. 발행 중단.", event_type)
            return

        event: dict[str, Any] = {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "session_id": session_id,
            "request_id": request_id,
            "bundle_id": bundle_id,
            "timestamp": datetime.now(_KST).isoformat(),
            "payload": dict(payload or {}),
        }

        if self._producer is not None:
            try:
                future = self._producer.send(
                    self._topic,
                    key=session_id or event_type,
                    value=event,
                )
                future.add_errback(
                    lambda exc: logger.error(
                        "[kafka] 비동기 발행 실패: %s — %s", event_type, exc,
                    )
                )
                logger.info(
                    "[kafka] 이벤트 발행: %s session=%s", event_type, session_id,
                )
            except Exception as e:
                logger.error("[kafka] 발행 실패: %s — %s", event_type, e)
        else:
            logger.info(
                "[kafka] (no-op) %s session=%s", event_type, session_id,
            )

    def status(self) -> dict[str, Any]:
        """Return producer readiness without exposing broker credentials."""
        return {
            "connected": self._producer is not None,
            "topic": self._topic,
            "bootstrap_servers_set": bool(str(self._bootstrap_servers).strip()),
            "last_error": self._last_error,
        }

    def flush(self) -> None:
        """미전송 이벤트 일괄 전송. 세션 종료 시 호출."""
        if self._producer is not None:
            try:
                self._producer.flush()
            except Exception as e:
                logger.warning("[kafka] flush 실패: %s", e)

    def close(self) -> None:
        """Producer 정리. flush 후 종료."""
        if self._producer is not None:
            try:
                self._producer.flush()
                self._producer.close()
            except Exception as e:
                logger.warning("[kafka] Producer 종료 실패: %s", e)

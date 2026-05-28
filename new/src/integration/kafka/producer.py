"""Kafka event producer for paper auto trading lifecycle events.

BE paper-auto 운영 이벤트를 Kafka topic으로 발행:
  AUTO_TRADING_START_REQUESTED — Start 요청 접수
  AUTO_TRADING_STARTED    — 자동매매 세션 시작
  DECISION_COMPLETED      — Hot Path 결정 완료 (매 cycle)
  PAPER_ORDER_SUBMITTED   — paper 주문 제출
  PAPER_ORDER_PARTIALLY_FILLED — paper 주문 부분 체결
  PAPER_ORDER_FILLED      — paper 주문 체결
  PAPER_ORDER_REJECTED    — paper 주문 broker reject
  PAPER_ORDER_VERIFICATION_FAILED — order-history 검증 실패
  PAPER_ORDER_FAILED      — paper 주문 실패/거절
  AUTO_TRADING_STOP_REQUESTED — Stop 요청 접수
  AUTO_TRADING_STOPPED    — 자동매매 정상 종료
  AUTO_TRADING_FAILED     — 자동매매 비정상 종료

kafka-python 미설치 시 no-op (로깅만 수행).
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.logger import get_logger
from src.utils.safe_cast import safe_int

logger = get_logger("kafka_producer")
_KST = ZoneInfo("Asia/Seoul")

PAPER_TRADING_EVENTS: tuple[str, ...] = (
    "AUTO_TRADING_START_REQUESTED",
    "AUTO_TRADING_STARTED",
    "DECISION_COMPLETED",
    "PAPER_ORDER_SUBMITTED",
    "PAPER_ORDER_PARTIALLY_FILLED",
    "PAPER_ORDER_FILLED",
    "PAPER_ORDER_REJECTED",
    "PAPER_ORDER_VERIFICATION_FAILED",
    "PAPER_ORDER_FAILED",
    "AUTO_TRADING_STOP_REQUESTED",
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
        required: bool | None = None,
    ) -> None:
        self._topic = topic or os.environ.get(
            "KAFKA_TOPIC", _DEFAULT_TOPIC,
        )
        self._bootstrap_servers = bootstrap_servers or os.environ.get(
            "KAFKA_BOOTSTRAP_SERVERS", "localhost:9092",
        )
        self._required = (
            str(os.environ.get("KAFKA_REQUIRED", "")).strip().lower()
            in {"1", "true", "yes", "on"}
            if required is None
            else bool(required)
        )
        self._producer: Any = None
        self._last_error = ""
        self._sequence_by_session: dict[str, int] = {}
        self._sequence_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending_futures: list[Any] = []
        self._delivery_timeout_sec = safe_int(
            os.environ.get("KAFKA_DELIVERY_TIMEOUT_SEC"),
            default=10,
            min_value=1,
            max_value=60,
        )
        try:
            from kafka import KafkaProducer

            self._producer = KafkaProducer(
                bootstrap_servers=self._bootstrap_servers,
                acks=os.environ.get("KAFKA_ACKS", "all"),
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
            if self._required:
                raise RuntimeError(self._last_error)
            logger.warning("[kafka] %s. 이벤트 로깅만 수행.", self._last_error)
        except Exception as e:
            self._last_error = str(e)
            if self._required:
                raise RuntimeError(f"Kafka producer required but unavailable: {e}") from e
            logger.warning("[kafka] Producer 연결 실패: %s. 이벤트 로깅만 수행.", e)

    def _next_sequence_no(self, session_id: str) -> int:
        sequence_lock = getattr(self, "_sequence_lock", None)
        if sequence_lock is None:
            sequence_lock = threading.Lock()
            self._sequence_lock = sequence_lock
        with sequence_lock:
            sequence_by_session = getattr(self, "_sequence_by_session", None)
            if not isinstance(sequence_by_session, dict):
                sequence_by_session = {}
                self._sequence_by_session = sequence_by_session
            sequence_key = session_id or "__global__"
            sequence_no = int(sequence_by_session.get(sequence_key, 0)) + 1
            sequence_by_session[sequence_key] = sequence_no
            return sequence_no

    def _remember_future(self, future: Any) -> None:
        pending_lock = getattr(self, "_pending_lock", None)
        if pending_lock is None:
            pending_lock = threading.Lock()
            self._pending_lock = pending_lock
        with pending_lock:
            pending_futures = getattr(self, "_pending_futures", None)
            if not isinstance(pending_futures, list):
                pending_futures = []
                self._pending_futures = pending_futures
            pending_futures.append(future)

    def _drain_pending_futures(self) -> list[Any]:
        pending_lock = getattr(self, "_pending_lock", None)
        if pending_lock is None:
            pending_lock = threading.Lock()
            self._pending_lock = pending_lock
        with pending_lock:
            pending_futures = list(getattr(self, "_pending_futures", []) or [])
            self._pending_futures = []
        return pending_futures

    def emit(
        self,
        event_type: str,
        *,
        session_id: str = "",
        request_id: str = "",
        bundle_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """이벤트 발행. Kafka 미연결 시 로깅만."""
        if event_type not in PAPER_TRADING_EVENTS:
            self._last_error = f"unknown_event_type:{event_type}"
            if getattr(self, "_required", False):
                raise ValueError(self._last_error)
            logger.warning("[kafka] 알 수 없는 event_type: %s. 발행 중단.", event_type)
            return None

        sequence_no = self._next_sequence_no(session_id)

        event: dict[str, Any] = {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "session_id": session_id,
            "request_id": request_id,
            "bundle_id": bundle_id,
            "event_key": session_id or event_type,
            "sequence_no": sequence_no,
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
                self._remember_future(future)
                logger.info(
                    "[kafka] 이벤트 발행: %s session=%s", event_type, session_id,
                )
                return event
            except Exception as e:
                self._last_error = str(e)
                logger.error("[kafka] 발행 실패: %s — %s", event_type, e)
                if getattr(self, "_required", False):
                    raise RuntimeError(
                        f"Kafka required emit failed: {event_type}: {e}"
                    ) from e
        else:
            if getattr(self, "_required", False):
                self._last_error = "kafka_required_but_not_connected"
                raise RuntimeError(self._last_error)
            logger.info(
                "[kafka] (no-op) %s session=%s", event_type, session_id,
            )
        return event

    def status(self) -> dict[str, Any]:
        """Return producer readiness without exposing broker credentials."""
        return {
            "connected": self._producer is not None,
            "topic": self._topic,
            "bootstrap_servers_set": bool(str(self._bootstrap_servers).strip()),
            "required": bool(getattr(self, "_required", False)),
            "last_error": self._last_error,
        }

    def flush(self) -> None:
        """미전송 이벤트 일괄 전송. 세션 종료 시 호출."""
        if self._producer is not None:
            try:
                self._producer.flush()
                timeout_sec = safe_int(
                    getattr(self, "_delivery_timeout_sec", 10),
                    default=10,
                    min_value=1,
                    max_value=60,
                )
                for future in self._drain_pending_futures():
                    if hasattr(future, "get"):
                        future.get(timeout=timeout_sec)
            except Exception as e:
                self._last_error = str(e)
                if getattr(self, "_required", False):
                    raise RuntimeError(f"Kafka required flush failed: {e}") from e
                logger.warning("[kafka] flush 실패: %s", e)

    def close(self) -> None:
        """Producer 정리. flush 후 종료."""
        if self._producer is not None:
            try:
                self.flush()
                self._producer.close()
            except Exception as e:
                self._last_error = str(e)
                if getattr(self, "_required", False):
                    raise RuntimeError(f"Kafka required close failed: {e}") from e
                logger.warning("[kafka] Producer 종료 실패: %s", e)

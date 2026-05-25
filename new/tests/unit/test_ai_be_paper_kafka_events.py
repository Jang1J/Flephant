"""Tests for the paper-auto Kafka event contract consumed by BE."""
from __future__ import annotations

import uuid

import src.integration.grpc.server as grpc_server
from src.integration.grpc.server import _PaperAutoSession, _paper_cycle_events
from src.integration.kafka.producer import KafkaEventProducer


class _RecordingKafka:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.flushed = False

    def emit(self, event_type: str, **kwargs) -> None:
        self.events.append((event_type, kwargs))

    def flush(self) -> None:
        self.flushed = True


def test_kafka_envelope_contains_ids_and_nested_payload() -> None:
    class _Future:
        def add_errback(self, _callback) -> None:
            return None

    class _Producer:
        value = None

        def send(self, _topic, *, key, value):
            assert key == "AI-SESSION"
            self.value = value
            return _Future()

    kafka_sender = _Producer()
    producer = KafkaEventProducer.__new__(KafkaEventProducer)
    producer._topic = "test-topic"
    producer._producer = kafka_sender

    producer.emit(
        "AUTO_TRADING_STARTED",
        session_id="AI-SESSION",
        request_id="BE-REQ-001",
        bundle_id="BUNDLE-1",
        payload={"cycles": 3},
    )

    uuid.UUID(kafka_sender.value["event_id"])
    assert kafka_sender.value["session_id"] == "AI-SESSION"
    assert kafka_sender.value["request_id"] == "BE-REQ-001"
    assert kafka_sender.value["bundle_id"] == "BUNDLE-1"
    assert kafka_sender.value["timestamp"]
    assert kafka_sender.value["payload"] == {"cycles": 3}
    assert "cycles" not in {
        key for key in kafka_sender.value
        if key != "payload"
    }


def test_paper_cycle_maps_submitted_and_failed_without_false_filled_event() -> None:
    result = {
        "status": "PASS",
        "hot_result": {
            "final_decision": {
                "decision_id": "DEC-1",
                "approved": True,
                "reason_code": "PASS",
                "order_deltas": [{"ticker": "005930"}, {"ticker": "000660"}],
            },
        },
        "execution": {
            "execution_report": {
                "order_plan_id": "OP-1",
                "fills": [{
                    "ticker": "5930",
                    "side": "buy",
                    "qty": 1,
                    "broker_status": "submitted",
                    "broker_order_id": "OD-1",
                    "broker_response": {
                        "price": 72500.0,
                        "order_type": "00",
                    },
                }],
                "rejections": [{
                    "ticker": 660,
                    "side": "buy",
                    "qty": 2,
                    "reason": "broker_rt_cd_1",
                    "broker_response": {"msg_cd": "EGW00001"},
                }],
            },
        },
    }

    decision, events = _paper_cycle_events(result, cycle=1)

    assert decision["decision_id"] == "DEC-1"
    assert decision["approved"] is True
    assert decision["reason_code"] == "PASS"
    assert decision["order_count"] == 2
    assert [event_type for event_type, _payload in events] == [
        "PAPER_ORDER_SUBMITTED",
        "PAPER_ORDER_FAILED",
    ]
    submitted = events[0][1]
    assert submitted["ticker"] == "005930"
    assert submitted["quantity"] == 1
    assert submitted["price"] == 72500.0
    assert submitted["order_type"] == "00"
    assert submitted["broker_order_id"] == "OD-1"
    assert submitted["paper_only"] is True
    assert submitted["execution_mode"] == "paper"
    assert submitted["kis_mode"] == "virtual"
    failed = events[1][1]
    assert failed["ticker"] == "000660"
    assert failed["error_code"] == "EGW00001"
    assert failed["reason"] == "broker_rt_cd_1"
    assert failed["price"] is None


def test_paper_cycle_emits_filled_when_order_history_confirms_fill() -> None:
    result = {
        "execution": {
            "execution_report": {
                "order_plan_id": "OP-1",
                "fills": [{
                    "ticker": "005930",
                    "side": "buy",
                    "qty": 1,
                    "broker_status": "submitted",
                    "broker_order_id": "OD-1",
                }],
            },
        },
        "order_history_verification": {
            "queries": [{
                "matched_orders": [{
                    "order_id": "OD-1",
                    "ticker": "5930",
                    "side": "buy",
                    "status": "filled",
                    "filled_qty": 1,
                    "avg_fill_price": 72450.0,
                }],
            }, {
                "matched_orders": [{
                    "order_id": "OD-1",
                    "ticker": "5930",
                    "side": "buy",
                    "status": "filled",
                    "filled_qty": 1,
                    "avg_fill_price": 72450.0,
                }],
            }],
        },
    }

    _decision, events = _paper_cycle_events(result, cycle=1)

    assert [event_type for event_type, _payload in events] == [
        "PAPER_ORDER_SUBMITTED",
        "PAPER_ORDER_FILLED",
    ]
    filled = events[1][1]
    assert filled["ticker"] == "005930"
    assert filled["filled_quantity"] == 1
    assert filled["filled_price"] == 72450.0
    assert filled["broker_order_id"] == "OD-1"


def test_start_event_carries_start_request_id(monkeypatch) -> None:
    kafka = _RecordingKafka()
    session = _PaperAutoSession(kafka=kafka)

    class _Thread:
        def start(self) -> None:
            return None

    monkeypatch.setattr(
        grpc_server.threading,
        "Thread",
        lambda **_kwargs: _Thread(),
    )

    session.start(
        request_id="BE-REQ-001",
        bundle_id="BUNDLE-1",
        cycles=3,
        interval_sec=10,
        tickers=["005930"],
        confirm_phrase="PAPER_ORDER_OK",
    )

    assert kafka.events[0][0] == "AUTO_TRADING_STARTED"
    assert kafka.events[0][1]["request_id"] == "BE-REQ-001"


def test_user_stop_is_published_as_stopped_with_start_request_id(monkeypatch) -> None:
    kafka = _RecordingKafka()
    session = _PaperAutoSession(kafka=kafka)
    session.session_id = "AI-SESSION"
    session.request_id = "BE-REQ-001"
    session.bundle_id = "BUNDLE-1"
    session.running = True
    session._stop_event.set()

    class _Trader:
        def run(self, **_kwargs) -> None:
            return None

    monkeypatch.setattr(
        grpc_server,
        "_make_grpc_paper_auto_trader",
        lambda **_kwargs: _Trader(),
    )

    session._run_loop(
        bundle_id="BUNDLE-1",
        cycles=3,
        interval_sec=10,
        tickers=["005930"],
        confirm_phrase="PAPER_ORDER_OK",
        root=None,
    )

    assert [event_type for event_type, _payload in kafka.events] == [
        "AUTO_TRADING_STOPPED",
    ]
    assert kafka.events[0][1]["request_id"] == "BE-REQ-001"
    assert kafka.events[0][1]["payload"]["stop_reason"] == "USER_REQUESTED"
    assert kafka.flushed is True
    assert session.running is False

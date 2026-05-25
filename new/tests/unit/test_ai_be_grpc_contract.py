"""AI-BE gRPC bridge contract tests."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.generate_ai_grpc_stubs import main as generate_ai_grpc_stubs
from src.integration.grpc.payloads import (
    build_ack_payload,
    build_health_payload,
    build_service_readiness_payload,
)

ROOT = Path(__file__).resolve().parents[3]
PROTO = ROOT / "new" / "proto" / "elephant_ai_bridge.proto"


def test_ai_be_proto_declares_grpc_replacements_for_http_bridge():
    text = PROTO.read_text(encoding="utf-8")

    assert 'syntax = "proto3";' in text
    assert "package elephant.ai.v1;" in text
    assert "service AiBeBridgeService" in text
    for rpc_name in [
        "HealthCheck",
        "GetServiceReadiness",
        "PublishPortfolioPatch",
        "PublishFinalDecision",
        "PublishExecutionFeedback",
        "PublishInternalMessage",
        "PublishAgentReport",
        "StartPaperAutoTrading",
        "StopPaperAutoTrading",
        "GetPaperAutoTradingStatus",
    ]:
        assert f"rpc {rpc_name}" in text


def test_paper_auto_status_proto_exposes_operability_fields():
    text = PROTO.read_text(encoding="utf-8")

    assert "message PaperAutoTradingStatusResponse" in text
    for field in [
        "stop_requested",
        "terminal_status",
        "stop_reason",
        "ended_at",
        "report_path",
        "kafka_connected",
        "kafka_topic",
        "kafka_last_error",
        "last_error",
    ]:
        assert field in text


def test_ai_be_proto_stubs_generate_after_paper_auto_contract_change(tmp_path):
    out_dir = tmp_path / "grpc_generated"

    assert generate_ai_grpc_stubs([
        "--proto", str(PROTO),
        "--output-dir", str(out_dir),
    ]) == 0
    generated = (out_dir / "elephant_ai_bridge_pb2.py").read_text(encoding="utf-8")
    assert "StartPaperAutoTrading" in generated
    assert "kafka_connected" in generated
    assert "last_error" in generated


def test_ai_be_proto_preserves_c8_c9_c10_core_fields():
    text = PROTO.read_text(encoding="utf-8")

    for message_name in [
        "PortfolioPatchEnvelope",
        "FinalDecisionEnvelope",
        "ExecutionFeedbackEnvelope",
    ]:
        assert f"message {message_name}" in text

    for field in [
        "portfolio_patch_id",
        "target_weights",
        "order_deltas",
        "decision_id",
        "reason_code",
        "risk_overrides",
        "final_decision_ref",
        "execution_mode",
        "live_enabled",
        "order_plan_id",
        "fills",
    ]:
        assert field in text


def test_health_payload_is_read_only_and_live_disabled(tmp_path):
    registry_dir = tmp_path / "artifacts" / "lgbm"
    registry_dir.mkdir(parents=True)
    (registry_dir / "registry.json").write_text(
        json.dumps({"active_version": None}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    payload = build_health_payload(request_id="REQ-1", root=tmp_path)

    assert payload["request_id"] == "REQ-1"
    assert payload["transport"] == "grpc"
    assert payload["status"] == "PASS"
    assert payload["live_trading_allowed"] is False
    assert payload["production_registry_mutated"] is False


def test_health_payload_does_not_label_existing_active_version_as_mutation(tmp_path):
    registry_dir = tmp_path / "artifacts" / "lgbm"
    registry_dir.mkdir(parents=True)
    (registry_dir / "registry.json").write_text(
        json.dumps({"active_version": "prod-existing"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    payload = build_health_payload(root=tmp_path)

    assert payload["production_active_version"] == "prod-existing"
    assert payload["production_registry_mutated"] is False


def test_service_readiness_payload_does_not_enable_order_actions(tmp_path):
    payload = build_service_readiness_payload(
        request_id="REQ-2",
        bundle_id="BUNDLE-TEST",
        include_details=True,
        root=tmp_path,
    )

    assert payload["request_id"] == "REQ-2"
    assert payload["bundle_id"] == "BUNDLE-TEST"
    assert payload["live_trading_allowed"] is False
    assert payload["safe_to_enable_order_actions"] is False
    assert payload["safe_to_enable_live_actions"] is False
    assert json.loads(payload["details_json"])["read_only"] is True


def test_ack_payload_is_transport_ack_not_live_permission():
    payload = build_ack_payload(
        request_id="REQ-3",
        idempotency_key="IDEMP-1",
    )

    assert payload["accepted"] is True
    assert payload["status"] == "ACK_READ_ONLY"
    assert payload["idempotency_key"] == "IDEMP-1"
    assert "no live trading" in payload["reason"]

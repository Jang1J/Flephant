"""AI-BE gRPC bridge contract tests."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.generate_ai_grpc_stubs import main as generate_ai_grpc_stubs
from src.integration.grpc.payloads import (
    build_ack_payload,
    build_health_payload,
    build_recommendations_payload,
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
        "GetRecommendations",
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
    generated_grpc = (out_dir / "elephant_ai_bridge_pb2_grpc.py").read_text(encoding="utf-8")
    assert "StartPaperAutoTrading" in generated
    assert "GetRecommendations" in generated_grpc
    assert "RecommendationItem" in generated
    assert "kafka_connected" in generated
    assert "last_error" in generated


def test_recommendation_proto_exposes_be_requested_fields_without_order_controls():
    text = PROTO.read_text(encoding="utf-8")

    assert "message GetRecommendationsRequest" in text
    assert "message RecommendationItem" in text
    assert "message GetRecommendationsResponse" in text
    for field in [
        "recommendation_id",
        "request_id",
        "stock_code",
        "ticker",
        "stock_name",
        "ranking",
        "score",
        "reason",
        "expected_return",
        "expected_return_available",
        "risk_level",
        "model_version",
        "bundle_id",
    ]:
        assert field in text
    recommendation_block = text.split("message RecommendationItem", 1)[1].split("}", 1)[0]
    for forbidden in ["target_weights", "order_deltas", "quantity", "side"]:
        assert forbidden not in recommendation_block


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


class _FakeRecommendationQuant:
    model_metadata = {
        "version": "model-v1",
        "bundle_id": "BUNDLE-TEST",
    }

    def __init__(self) -> None:
        self.bars: list[dict] = []

    def on_bar(self, bar: dict) -> None:
        self.bars.append(bar)

    def score_cross_section(self, tickers: list[str], *, asof: str) -> dict:
        return {
            "mode": "active",
            "ts": asof,
            "scores": {"005930": 0.42, "000660": 0.21},
            "confidences": {"005930": 0.09, "000660": 0.04},
            "n_tickers": len(tickers),
        }


class _StableBundleRecommendationQuant(_FakeRecommendationQuant):
    model_metadata = {
        "version": "model-v1",
        "bundle_id": "BUNDLE-20260521-POSTCLOSE",
    }


class _BlockedRecommendationQuant(_FakeRecommendationQuant):
    def score_cross_section(self, tickers: list[str], *, asof: str) -> dict:
        return {
            "mode": "blocked",
            "blocker": "required_feature_missing",
            "scores": {},
            "ts": asof,
        }


class _InvalidScoreRecommendationQuant(_FakeRecommendationQuant):
    def score_cross_section(self, tickers: list[str], *, asof: str) -> dict:
        return {
            "mode": "active",
            "ts": asof,
            "scores": {"005930": 0.42, "000660": float("nan")},
            "confidences": {"005930": 0.09, "000660": 0.04},
            "n_tickers": len(tickers),
        }


class _FakeMarketDataClient:
    def inquire_minute_bar(self, ticker: str, n_bars: int = 60) -> list[dict]:
        bars = []
        for idx in range(n_bars):
            hour = 9 + (idx // 60)
            minute = idx % 60
            bars.append({
                "ticker": ticker,
                "ts_close": f"2026-05-26T{hour:02d}:{minute:02d}:00+09:00",
                "open": 100.0 + idx,
                "high": 101.0 + idx,
                "low": 99.0 + idx,
                "close": 100.5 + idx,
                "volume": 1000 + idx,
            })
        return bars


class _PartialFailingMarketDataClient(_FakeMarketDataClient):
    def inquire_minute_bar(self, ticker: str, n_bars: int = 60) -> list[dict]:
        if ticker == "000660":
            raise RuntimeError("transient minute bar failure")
        return super().inquire_minute_bar(ticker, n_bars=n_bars)


def test_recommendations_payload_returns_read_only_ranked_items():
    payload = build_recommendations_payload(
        request_id="REQ-REC",
        bundle_id="BUNDLE-TEST",
        tickers=["005930", "000660"],
        top_k=1,
        include_diagnostics=True,
        quant_agent=_FakeRecommendationQuant(),
        market_data_client=_FakeMarketDataClient(),
    )

    assert payload["status"] == "PASS"
    assert payload["request_id"] == "REQ-REC"
    assert payload["bundle_id"] == "BUNDLE-TEST"
    assert payload["model_version"] == "model-v1"
    assert payload["live_trading_allowed"] is False
    assert payload["registry_mutated"] is False
    assert len(payload["recommendations"]) == 1
    item = payload["recommendations"][0]
    assert item["recommendation_id"] == "REQ-REC-1-005930"
    assert item["stock_code"] == "005930"
    assert item["ticker"] == "005930"
    assert item["stock_name"] == "삼성전자"
    assert item["ranking"] == 1
    assert item["score"] == 0.42
    assert item["risk_level"] == "low"
    assert item["expected_return"] == 0.0
    assert item["expected_return_available"] is False
    for forbidden in ["target_weights", "order_deltas", "quantity", "side"]:
        assert forbidden not in item


def test_recommendations_payload_treats_string_ticker_as_single_ticker():
    payload = build_recommendations_payload(
        request_id="REQ-STRING-TICKER",
        bundle_id="BUNDLE-TEST",
        tickers="005930",
        top_k=1,
        quant_agent=_FakeRecommendationQuant(),
        market_data_client=_FakeMarketDataClient(),
    )

    assert payload["status"] == "PASS"
    assert [item["ticker"] for item in payload["recommendations"]] == ["005930"]


def test_recommendations_payload_uses_stable_default_bundle_when_request_empty():
    payload = build_recommendations_payload(
        request_id="REQ-DEFAULT-BUNDLE",
        tickers=["005930"],
        top_k=1,
        quant_agent=_StableBundleRecommendationQuant(),
        market_data_client=_FakeMarketDataClient(),
    )

    assert payload["status"] == "PASS"
    assert payload["bundle_id"] == "BUNDLE-20260521-POSTCLOSE"
    assert payload["recommendations"][0]["bundle_id"] == "BUNDLE-20260521-POSTCLOSE"


def test_recommendations_payload_keeps_partial_bar_failures_as_diagnostics():
    payload = build_recommendations_payload(
        request_id="REQ-PARTIAL-BARS",
        bundle_id="BUNDLE-TEST",
        tickers=["005930", "000660"],
        include_diagnostics=True,
        quant_agent=_FakeRecommendationQuant(),
        market_data_client=_PartialFailingMarketDataClient(),
    )

    assert payload["status"] == "PASS"
    assert payload["recommendations"]
    diagnostics = json.loads(payload["diagnostics_json"])
    assert "000660" in diagnostics["bar_errors"]


def test_recommendations_payload_keeps_partial_invalid_scores_as_diagnostics():
    payload = build_recommendations_payload(
        request_id="REQ-PARTIAL-SCORES",
        bundle_id="BUNDLE-TEST",
        tickers=["005930", "000660"],
        include_diagnostics=True,
        quant_agent=_InvalidScoreRecommendationQuant(),
        market_data_client=_FakeMarketDataClient(),
    )

    assert payload["status"] == "PASS"
    assert [item["ticker"] for item in payload["recommendations"]] == ["005930"]
    diagnostics = json.loads(payload["diagnostics_json"])
    assert diagnostics["invalid_score_tickers"] == ["000660"]


def test_recommendations_payload_blocks_when_quant_is_not_active():
    payload = build_recommendations_payload(
        request_id="REQ-BLOCKED",
        bundle_id="BUNDLE-TEST",
        tickers=["005930", "000660"],
        include_diagnostics=True,
        quant_agent=_BlockedRecommendationQuant(),
        market_data_client=_FakeMarketDataClient(),
    )

    assert payload["status"] == "BLOCKED"
    assert payload["reason"] == "required_feature_missing"
    assert payload["recommendations"] == []
    assert payload["live_trading_allowed"] is False
    assert json.loads(payload["diagnostics_json"])["registry_mutated"] is False


def test_recommendations_payload_rejects_out_of_universe_ticker():
    payload = build_recommendations_payload(
        request_id="REQ-BAD-TICKER",
        bundle_id="BUNDLE-TEST",
        tickers=["999999"],
        include_diagnostics=True,
        quant_agent=_FakeRecommendationQuant(),
        market_data_client=_FakeMarketDataClient(),
    )

    assert payload["status"] == "BLOCKED"
    assert payload["reason"] == "invalid_or_out_of_universe_ticker"
    assert payload["recommendations"] == []


def test_ack_payload_is_transport_ack_not_live_permission():
    payload = build_ack_payload(
        request_id="REQ-3",
        idempotency_key="IDEMP-1",
    )

    assert payload["accepted"] is True
    assert payload["status"] == "ACK_READ_ONLY"
    assert payload["idempotency_key"] == "IDEMP-1"
    assert "no live trading" in payload["reason"]

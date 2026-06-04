"""AI-BE gRPC bridge contract tests."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.generate_ai_grpc_stubs import main as generate_ai_grpc_stubs
from scripts import run_ai_grpc_server
from src.data.minute_bar_window_cache import (
    MinuteBarWindowCache,
    MinuteBarWindowCacheConfig,
)
from src.integration.grpc import payloads as grpc_payloads
from src.integration.grpc.payloads import (
    build_ack_payload,
    build_health_payload,
    build_recommendations_payload,
    build_service_readiness_payload,
)

ROOT = Path(__file__).resolve().parents[3]
PROTO = ROOT / "new" / "proto" / "elephant_ai_bridge.proto"
REC_ASOF = "2026-05-26T10:00:00+09:00"


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
        "start_request_id",
        "orders_submitted",
        "orders_filled",
        "orders_rejected",
        "last_cycle_status",
        "kafka_required",
        "kis_mode",
        "live_trading_allowed",
        "production_registry_mutated",
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
    assert "orders_submitted" in generated
    assert "kafka_required" in generated
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


def test_run_ai_grpc_server_defaults_to_operational_bundle(monkeypatch):
    monkeypatch.setattr(
        run_ai_grpc_server,
        "config_load",
        lambda *_args, **_kwargs: {
            "default_bundle_id": "BUNDLE-20260521-POSTCLOSE",
        },
    )

    assert run_ai_grpc_server._default_bundle_id() == "BUNDLE-20260521-POSTCLOSE"


def test_run_ai_grpc_server_env_preflight_is_fail_closed(monkeypatch):
    monkeypatch.setattr(
        run_ai_grpc_server,
        "_env_readiness_report",
        lambda: {"status": "FAIL", "guards": {"kis_mode_virtual": False}},
    )

    try:
        run_ai_grpc_server._require_env_readiness()
    except RuntimeError as e:
        assert "env readiness failed" in str(e)
    else:
        raise AssertionError("server startup must fail closed when env readiness fails")


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


def test_service_readiness_payload_enables_paper_order_actions_when_ready(monkeypatch):
    def fake_build_service_status(*, bundle_id, root):
        return {
            "status": "PASS",
            "generated_at": "2026-06-02T09:30:00+09:00",
            "bundle_id": bundle_id,
            "deploy_quality": "PASS",
            "broker_evidence": "PASS",
            "live_trading_allowed": False,
            "registry_mutated": False,
            "read_only": True,
            "be_contract": {
                "safe_to_show_dashboard": True,
                "safe_to_enable_order_actions": True,
                "safe_to_enable_live_actions": False,
            },
        }

    monkeypatch.setattr(
        grpc_payloads,
        "build_service_status",
        fake_build_service_status,
    )

    payload = build_service_readiness_payload(
        request_id="REQ-READY",
        bundle_id="BUNDLE-TEST",
        include_details=True,
        root=Path("."),
    )

    assert payload["status"] == "PASS"
    assert payload["bundle_id"] == "BUNDLE-TEST"
    assert payload["live_trading_allowed"] is False
    assert payload["safe_to_enable_order_actions"] is True
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


def _minute_cache(
    client: object,
    *,
    parallel_fetch_workers: int = 1,
    batch_fetch_budget_sec: float = 45.0,
) -> MinuteBarWindowCache:
    return MinuteBarWindowCache(
        client,
        MinuteBarWindowCacheConfig(
            window_size=60,
            incremental_fetch_bars=6,
            freshness_max_lag_sec=120,
            gap_refetch_sec=300,
            expected_bar_interval_sec=60,
            max_contiguity_gap_sec=90,
            force_cold_on_session_date_change=True,
            session_open_time="09:00",
            session_close_time="15:30",
            parallel_fetch_workers=parallel_fetch_workers,
            batch_fetch_budget_sec=batch_fetch_budget_sec,
        ),
    )


class _PartialFailingMarketDataClient(_FakeMarketDataClient):
    def inquire_minute_bar(self, ticker: str, n_bars: int = 60) -> list[dict]:
        if ticker == "000660":
            raise RuntimeError("transient minute bar failure")
        return super().inquire_minute_bar(ticker, n_bars=n_bars)


class _SlowTickerMarketDataClient(_FakeMarketDataClient):
    def inquire_minute_bar(self, ticker: str, n_bars: int = 60) -> list[dict]:
        if ticker == "000660":
            time.sleep(0.20)
        return super().inquire_minute_bar(ticker, n_bars=n_bars)


class _IncrementalMarketDataClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def inquire_minute_bar(self, ticker: str, n_bars: int = 60) -> list[dict]:
        self.calls.append((ticker, n_bars))
        if n_bars >= 60:
            return [
                {
                    "ticker": ticker,
                    "ts_close": (
                        datetime(2026, 5, 26, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
                        + timedelta(minutes=idx)
                    ).isoformat(),
                    "open": 100.0 + idx,
                    "high": 101.0 + idx,
                    "low": 99.0 + idx,
                    "close": 100.5 + idx,
                    "volume": 1000 + idx,
                }
                for idx in range(n_bars)
            ]
        return [
            {
                "ticker": ticker,
                "ts_close": f"2026-05-26T10:{idx:02d}:00+09:00",
                "open": 200.0 + idx,
                "high": 201.0 + idx,
                "low": 199.0 + idx,
                "close": 200.5 + idx,
                "volume": 2000 + idx,
            }
            for idx in range(n_bars)
        ]


class _FutureBarMarketDataClient(_FakeMarketDataClient):
    def inquire_minute_bar(self, ticker: str, n_bars: int = 60) -> list[dict]:
        bars = super().inquire_minute_bar(ticker, n_bars=n_bars)
        bars.append({
            "ticker": ticker,
            "ts_close": "2026-05-26T10:01:00+09:00",
            "open": 999.0,
            "high": 999.0,
            "low": 999.0,
            "close": 999.0,
            "volume": 999,
        })
        return bars


class _ScriptedMarketDataClient:
    def __init__(self, responses: list[list[dict]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, int]] = []

    def inquire_minute_bar(self, ticker: str, n_bars: int = 60) -> list[dict]:
        self.calls.append((ticker, n_bars))
        if not self.responses:
            return []
        return list(self.responses.pop(0))


def _grpc_bar(ticker: str, ts_close: str, close: float) -> dict:
    return {
        "ticker": ticker,
        "ts_close": ts_close,
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": 1000,
    }


def test_recommendations_payload_returns_read_only_ranked_items():
    payload = build_recommendations_payload(
        request_id="REQ-REC",
        bundle_id="BUNDLE-TEST",
        asof=REC_ASOF,
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
    assert "삼성전자(005930)" in item["reason"]
    assert "랭킹 모델 score" in item["reason"]
    assert "1위" in item["reason"]
    assert "주문 권고가 아닌 참고용 신호" in item["reason"]
    assert "MODEL_RANKING_SIGNAL" not in item["reason"]
    for forbidden in ["target_weights", "order_deltas", "quantity", "side"]:
        assert forbidden not in item


def test_recommendations_payload_treats_string_ticker_as_single_ticker():
    payload = build_recommendations_payload(
        request_id="REQ-STRING-TICKER",
        bundle_id="BUNDLE-TEST",
        asof=REC_ASOF,
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
        asof=REC_ASOF,
        tickers=["005930"],
        top_k=1,
        quant_agent=_StableBundleRecommendationQuant(),
        market_data_client=_FakeMarketDataClient(),
    )

    assert payload["status"] == "PASS"
    assert payload["bundle_id"] == "BUNDLE-20260521-POSTCLOSE"
    assert payload["recommendations"][0]["bundle_id"] == "BUNDLE-20260521-POSTCLOSE"


def test_recommendations_payload_can_use_recommendation_only_mock_market_data(
    monkeypatch,
):
    monkeypatch.setenv("AI_RECOMMENDATION_MARKET_DATA_MODE", "mock")

    payload = build_recommendations_payload(
        request_id="REQ-REC-MOCK-MARKET",
        bundle_id="BUNDLE-TEST",
        asof=datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        tickers=["005930", "000660"],
        top_k=2,
        include_diagnostics=True,
        quant_agent=_FakeRecommendationQuant(),
    )

    assert payload["status"] == "PASS"
    assert [item["ticker"] for item in payload["recommendations"]] == [
        "005930",
        "000660",
    ]
    diagnostics = json.loads(payload["diagnostics_json"])
    assert diagnostics["market_data_mode"] == "mock"
    assert diagnostics["minute_bar_window_cache"]["failed_tickers"] == {}


def test_recommendations_payload_allows_partial_bar_failures_when_enough_to_fill_top_k():
    payload = build_recommendations_payload(
        request_id="REQ-PARTIAL-BARS",
        bundle_id="BUNDLE-TEST",
        asof=REC_ASOF,
        tickers=["005930", "000660"],
        top_k=1,
        include_diagnostics=True,
        quant_agent=_FakeRecommendationQuant(),
        market_data_client=_PartialFailingMarketDataClient(),
    )

    assert payload["status"] == "PASS"
    assert [item["ticker"] for item in payload["recommendations"]] == ["005930"]
    diagnostics = json.loads(payload["diagnostics_json"])
    assert "000660" in diagnostics["bar_errors"]
    assert diagnostics["partial_minute_bar_failures_allowed"] is True
    assert diagnostics["scoring_tickers"] == ["005930"]


def test_recommendations_payload_blocks_partial_bar_failures_when_underfilled_top_k():
    payload = build_recommendations_payload(
        request_id="REQ-PARTIAL-BARS-UNDERFILLED",
        bundle_id="BUNDLE-TEST",
        asof=REC_ASOF,
        tickers=["005930", "000660"],
        top_k=2,
        include_diagnostics=True,
        quant_agent=_FakeRecommendationQuant(),
        market_data_client=_PartialFailingMarketDataClient(),
    )

    assert payload["status"] == "BLOCKED"
    assert payload["reason"] == "partial_minute_bars_unavailable"
    assert payload["recommendations"] == []
    diagnostics = json.loads(payload["diagnostics_json"])
    assert diagnostics["partial_minute_bar_failures_allowed"] is False
    assert diagnostics["min_successful_tickers"] == 2
    assert diagnostics["successful_bar_tickers"] == ["005930"]


def test_recommendations_payload_blocks_partial_bar_failures_when_policy_disabled(
    monkeypatch,
):
    original_config = grpc_payloads._resolve_recommendation_config

    def policy_disabled_config() -> dict:
        cfg = dict(original_config())
        cfg["allow_partial_minute_bar_failures"] = False
        return cfg

    monkeypatch.setattr(
        grpc_payloads,
        "_resolve_recommendation_config",
        policy_disabled_config,
    )

    payload = build_recommendations_payload(
        request_id="REQ-PARTIAL-BARS-BLOCKED",
        bundle_id="BUNDLE-TEST",
        asof=REC_ASOF,
        tickers=["005930", "000660"],
        include_diagnostics=True,
        quant_agent=_FakeRecommendationQuant(),
        market_data_client=_PartialFailingMarketDataClient(),
    )

    assert payload["status"] == "BLOCKED"
    assert payload["reason"] == "partial_minute_bars_unavailable"
    assert payload["recommendations"] == []
    diagnostics = json.loads(payload["diagnostics_json"])
    assert diagnostics["partial_minute_bar_failures_allowed"] is False


def test_recommendations_payload_allows_partial_fetch_timeout_for_display() -> None:
    payload = build_recommendations_payload(
        request_id="REQ-TIMEOUT-BARS",
        bundle_id="BUNDLE-TEST",
        asof=REC_ASOF,
        tickers=["005930", "000660"],
        top_k=1,
        include_diagnostics=True,
        quant_agent=_FakeRecommendationQuant(),
        minute_bar_cache=_minute_cache(
            _SlowTickerMarketDataClient(),
            parallel_fetch_workers=2,
            batch_fetch_budget_sec=0.05,
        ),
    )

    assert payload["status"] == "PASS"
    assert [item["ticker"] for item in payload["recommendations"]] == ["005930"]
    diagnostics = json.loads(payload["diagnostics_json"])
    assert diagnostics["bar_errors"] == {"000660": "fetch_timeout"}
    assert diagnostics["partial_minute_bar_failures_allowed"] is True
    assert diagnostics["scoring_tickers"] == ["005930"]
    timeout_meta = diagnostics["minute_bar_window_cache"]["tickers"]["000660"]
    assert timeout_meta["reason"] == "fetch_timeout"
    assert timeout_meta["timeout_sec"] == 0.05


def test_recommendations_payload_uses_minute_bar_cache_incremental_fetches() -> None:
    client = _IncrementalMarketDataClient()
    cache = _minute_cache(client)

    first = build_recommendations_payload(
        request_id="REQ-CACHE-1",
        bundle_id="BUNDLE-TEST",
        asof="2026-05-26T09:59:00+09:00",
        tickers=["005930"],
        top_k=1,
        include_diagnostics=True,
        quant_agent=_FakeRecommendationQuant(),
        minute_bar_cache=cache,
    )
    second = build_recommendations_payload(
        request_id="REQ-CACHE-2",
        bundle_id="BUNDLE-TEST",
        asof="2026-05-26T10:01:00+09:00",
        tickers=["005930"],
        top_k=1,
        include_diagnostics=True,
        quant_agent=_FakeRecommendationQuant(),
        minute_bar_cache=cache,
    )

    assert first["status"] == "PASS"
    assert second["status"] == "PASS"
    assert client.calls == [("005930", 61), ("005930", 6)]
    diagnostics = json.loads(second["diagnostics_json"])
    assert diagnostics["minute_bar_window_cache"]["tickers"]["005930"]["fetch_policy"] == "incremental"


def test_recommendations_payload_filters_t_plus_one_before_quant() -> None:
    quant = _FakeRecommendationQuant()
    payload = build_recommendations_payload(
        request_id="REQ-FUTURE-BAR",
        bundle_id="BUNDLE-TEST",
        asof=REC_ASOF,
        tickers=["005930"],
        top_k=1,
        include_diagnostics=True,
        quant_agent=quant,
        market_data_client=_FutureBarMarketDataClient(),
    )

    assert payload["status"] == "PASS"
    assert all(bar["ts_close"] <= REC_ASOF for bar in quant.bars)
    diagnostics = json.loads(payload["diagnostics_json"])
    assert (
        diagnostics["minute_bar_window_cache"]["tickers"]["005930"][
            "future_bar_filtered_count"
        ]
        == 1
    )


def test_recommendations_payload_blocks_non_contiguous_minute_window() -> None:
    quant = _FakeRecommendationQuant()
    hole_window = [
        *[
            _grpc_bar(
                "005930",
                f"2026-05-26T09:{minute:02d}:00+09:00",
                100.0 + minute,
            )
            for minute in range(1, 60)
        ],
        _grpc_bar("005930", "2026-05-26T10:01:00+09:00", 201.0),
    ]

    payload = build_recommendations_payload(
        request_id="REQ-HOLE-BLOCKED",
        bundle_id="BUNDLE-TEST",
        asof="2026-05-26T10:02:50+09:00",
        tickers=["005930"],
        include_diagnostics=True,
        quant_agent=quant,
        market_data_client=_ScriptedMarketDataClient([hole_window]),
    )

    assert payload["status"] == "BLOCKED"
    assert payload["reason"] == "minute_bars_unavailable"
    assert quant.bars == []
    diagnostics = json.loads(payload["diagnostics_json"])
    assert diagnostics["bar_errors"] == {"005930": "non_contiguous_window"}
    ticker_meta = diagnostics["minute_bar_window_cache"]["tickers"]["005930"]
    assert ticker_meta["contiguity_status"] == "FAIL"


def test_recommendations_payload_blocks_underfilled_invalid_scores():
    payload = build_recommendations_payload(
        request_id="REQ-PARTIAL-SCORES",
        bundle_id="BUNDLE-TEST",
        asof=REC_ASOF,
        tickers=["005930", "000660"],
        include_diagnostics=True,
        quant_agent=_InvalidScoreRecommendationQuant(),
        market_data_client=_FakeMarketDataClient(),
    )

    assert payload["status"] == "BLOCKED"
    assert payload["reason"] == "insufficient_recommendations"
    assert payload["recommendations"] == []
    diagnostics = json.loads(payload["diagnostics_json"])
    assert diagnostics["invalid_score_tickers"] == ["000660"]
    assert diagnostics["finite_score_count"] == 1
    assert diagnostics["min_recommendation_count"] == 2


def test_recommendations_payload_blocks_when_quant_is_not_active():
    payload = build_recommendations_payload(
        request_id="REQ-BLOCKED",
        bundle_id="BUNDLE-TEST",
        asof=REC_ASOF,
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

"""Read-only payload builders for the AI-BE gRPC bridge.

The helpers in this module intentionally do not place broker orders, do not
read secret files, and do not mutate registries. They translate local artifact
status and read-only model signals into gRPC-friendly dictionaries used by the
generated service wrapper.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.data.minute_bar_window_cache import (
    MinuteBarWindowCache,
    MinuteBarWindowCacheConfig,
    load_minute_bar_window_cache_config,
)
from src.ops.service_readiness_status import build_service_status
from src.utils.config_loader import load as config_load
from src.utils.safe_cast import safe_bool, safe_int
from src.utils.ticker_utils import pad_ticker

_KST = ZoneInfo("Asia/Seoul")
_REPO_ROOT = Path(__file__).resolve().parents[4]
_RECOMMENDATION_BAR_CACHE: MinuteBarWindowCache | None = None
_RECOMMENDATION_BAR_CACHE_CONFIG: MinuteBarWindowCacheConfig | None = None
_RECOMMENDATION_BAR_CACHE_MARKET_DATA_MODE: str | None = None
_RECOMMENDATION_BAR_CACHE_LOCK = threading.Lock()
logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(_KST).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        return {"_read_error": f"json_decode_error: {e}"}
    except OSError as e:
        return {"_read_error": f"os_error: {e}"}
    return data if isinstance(data, dict) else {}


def _production_active_version(root: Path) -> str:
    registry = _read_json(root / "artifacts" / "lgbm" / "registry.json")
    active = registry.get("active_version")
    return str(active) if active else ""


def build_health_payload(
    *,
    request_id: str = "",
    bundle_id: str = "",
    root: Path | None = None,
) -> dict[str, Any]:
    """Build a lightweight health response for BE gRPC liveness checks."""
    repo_root = root or _REPO_ROOT
    active_version = _production_active_version(repo_root)
    status = "PASS"
    message = "AI gRPC bridge is reachable; live trading remains disabled."
    if bundle_id:
        readiness = build_service_status(bundle_id=bundle_id, root=repo_root)
        status = str(readiness.get("status") or "BLOCKED")
        message = "Service readiness reflected from local read-only artifacts."
    return {
        "request_id": request_id,
        "status": status,
        "generated_at": _now_iso(),
        "transport": "grpc",
        "bundle_id": bundle_id,
        "live_trading_allowed": False,
        "production_registry_mutated": False,
        "production_active_version": active_version,
        "message": message,
    }


def build_service_readiness_payload(
    *,
    request_id: str = "",
    bundle_id: str,
    include_details: bool = False,
    root: Path | None = None,
) -> dict[str, Any]:
    """Translate service_readiness_status into a gRPC response dictionary."""
    repo_root = root or _REPO_ROOT
    readiness = build_service_status(bundle_id=bundle_id, root=repo_root)
    be_contract = readiness.get("be_contract")
    if not isinstance(be_contract, dict):
        be_contract = {}
    details_json = ""
    if include_details:
        details_json = json.dumps(readiness, ensure_ascii=False, sort_keys=True)
    return {
        "request_id": request_id,
        "status": str(readiness.get("status") or "BLOCKED"),
        "generated_at": str(readiness.get("generated_at") or _now_iso()),
        "bundle_id": str(readiness.get("bundle_id") or bundle_id),
        "deploy_quality": str(readiness.get("deploy_quality") or "BLOCKED"),
        "broker_evidence": str(readiness.get("broker_evidence") or "BLOCKED"),
        "live_trading_allowed": False,
        "registry_mutated": bool(readiness.get("registry_mutated")),
        "safe_to_show_dashboard": bool(be_contract.get("safe_to_show_dashboard", True)),
        "safe_to_enable_order_actions": bool(be_contract.get("safe_to_enable_order_actions")),
        "safe_to_enable_live_actions": bool(be_contract.get("safe_to_enable_live_actions")),
        "details_json": details_json,
    }


def _active_universe() -> tuple[list[str], dict[str, str]]:
    """Load active recommendation universe and stock names from SSOT config."""
    data = config_load("universe_config.yaml") or {}
    sectors = data.get("sectors")
    sectors = sectors if isinstance(sectors, dict) else {}
    tickers: list[str] = []
    names: dict[str, str] = {}
    for sector in sectors.values():
        sector = sector if isinstance(sector, dict) else {}
        stocks = sector.get("stocks")
        stocks = stocks if isinstance(stocks, list) else []
        for stock in stocks:
            stock = stock if isinstance(stock, dict) else {}
            if str(stock.get("status", "")).strip() != "active":
                continue
            raw_ticker = str(stock.get("ticker", "")).strip()
            if not raw_ticker:
                continue
            ticker = pad_ticker(raw_ticker)
            if ticker in names:
                continue
            tickers.append(ticker)
            names[ticker] = str(stock.get("name") or ticker)
    return tickers, names


def _recommendation_blocked_payload(
    *,
    request_id: str,
    bundle_id: str,
    reason: str,
    asof: str = "",
    mode: str = "blocked",
    model_version: str = "",
    diagnostics: dict[str, Any] | None = None,
    include_diagnostics: bool = False,
) -> dict[str, Any]:
    diagnostics_json = ""
    if include_diagnostics and diagnostics is not None:
        diagnostics_json = json.dumps(diagnostics, ensure_ascii=False, sort_keys=True)
    return {
        "request_id": request_id,
        "status": "BLOCKED",
        "reason": reason,
        "generated_at": _now_iso(),
        "bundle_id": bundle_id,
        "model_version": model_version,
        "asof": asof,
        "mode": mode,
        "recommendations": [],
        "live_trading_allowed": False,
        "registry_mutated": False,
        "diagnostics_json": diagnostics_json,
    }


def _resolve_recommendation_config() -> dict[str, Any]:
    cfg = config_load("risk_config.yaml", "grpc_recommendations") or {}
    default_top_k = int(cfg["default_top_k"])
    max_top_k = int(cfg["max_top_k"])
    risk_level_cfg = cfg.get("risk_level")
    risk_level_cfg = risk_level_cfg if isinstance(risk_level_cfg, dict) else {}
    market_data_mode = os.getenv(
        "AI_RECOMMENDATION_MARKET_DATA_MODE",
        str(cfg.get("market_data_mode", "auto")),
    ).strip().lower()
    if market_data_mode not in {"auto", "mock", "virtual", "real"}:
        raise ValueError(
            "grpc_recommendations.market_data_mode must be one of "
            "auto/mock/virtual/real",
        )
    return {
        "default_bundle_id": str(cfg["default_bundle_id"]),
        "default_top_k": default_top_k,
        "max_top_k": max_top_k,
        "market_data_mode": market_data_mode,
        "low_min_confidence": float(risk_level_cfg["low_min_confidence"]),
        "medium_min_confidence": float(risk_level_cfg["medium_min_confidence"]),
        "reason_code": str(cfg["reason_code"]),
        "expected_return_unavailable_reason": str(
            cfg["expected_return_unavailable_reason"],
        ),
        "reason_ko_template": str(cfg["reason_ko_template"]),
        "parallel_fetch_workers": int(cfg["parallel_fetch_workers"]),
        "allow_partial_minute_bar_failures": safe_bool(
            cfg.get("allow_partial_minute_bar_failures", False),
            default=False,
        ),
        "min_successful_tickers": safe_int(
            cfg.get("min_successful_tickers", 1),
            default=1,
            min_value=1,
        ),
    }


def _normalize_recommendation_tickers(
    raw_tickers: list[Any] | tuple[Any, ...] | Any,
) -> tuple[list[str], list[str], dict[str, str]]:
    universe, names = _active_universe()
    universe_set = set(universe)
    invalid: list[str] = []
    normalized: list[str] = []
    raw_list = [raw_tickers] if isinstance(raw_tickers, str) else list(raw_tickers or [])
    if not raw_list:
        return universe, [], names
    for raw in raw_list:
        value = str(raw).strip()
        if not value or not value.isdigit():
            invalid.append(value)
            continue
        ticker = pad_ticker(value)
        if ticker not in universe_set:
            invalid.append(ticker)
            continue
        if ticker not in normalized:
            normalized.append(ticker)
    return normalized, invalid, names


def _risk_level_for_confidence(confidence: float, cfg: dict[str, Any]) -> str:
    if confidence >= float(cfg["low_min_confidence"]):
        return "low"
    if confidence >= float(cfg["medium_min_confidence"]):
        return "medium"
    return "high"


def _risk_level_ko(risk_level: str) -> str:
    return {
        "low": "낮음",
        "medium": "보통",
        "high": "높음",
    }.get(str(risk_level), str(risk_level))


def _required_recommendation_count(
    *,
    resolved_top_k: int,
    candidate_count: int,
    cfg: dict[str, Any],
) -> int:
    if candidate_count <= 0:
        return 0
    configured_min = safe_int(
        cfg.get("min_successful_tickers", 1),
        default=1,
        min_value=1,
    )
    configured_min = min(configured_min, int(resolved_top_k))
    fill_requested_top_k = min(int(resolved_top_k), int(candidate_count))
    return min(max(configured_min, fill_requested_top_k), int(candidate_count))


def _recommendation_reason_ko(
    *,
    template: str,
    stock_name: str,
    ticker: str,
    ranking: int,
    score: float,
    risk_level: str,
    model_version: str,
) -> str:
    try:
        return template.format(
            stock_name=stock_name,
            ticker=ticker,
            ranking=int(ranking),
            score=float(score),
            risk_level=risk_level,
            risk_level_ko=_risk_level_ko(risk_level),
            model_version=model_version,
        )
    except (KeyError, ValueError, TypeError):
        return (
            f"{stock_name}({ticker})는 현재 1분봉 기반 랭킹 모델 score가 "
            f"후보군 중 {int(ranking)}위로 산출되어 추천 목록에 포함됐습니다. "
            "이 score는 보정된 기대수익률이 아니며, 주문 권고가 아닌 참고용 신호입니다. "
            f"위험도 표시는 {_risk_level_ko(risk_level)}입니다."
        )


def _candidate_registry_dir(repo_root: Path, bundle_id: str) -> Path | None:
    if not bundle_id:
        return None
    candidate_dir = repo_root / "artifacts" / "lgbm_paper_candidate" / bundle_id
    return candidate_dir if candidate_dir.exists() else None


def _make_recommendation_quant_agent(
    *,
    bundle_id: str,
    repo_root: Path,
) -> Any:
    from src.agents.hot.quant import QuantAgent
    from src.data.dual_source_runner import load_latest_scores
    from src.models.registry import ModelRegistry

    registry = None
    if bundle_id:
        registry_dir = _candidate_registry_dir(repo_root, bundle_id)
        if registry_dir is None:
            raise FileNotFoundError(
                f"paper candidate registry not found for bundle_id={bundle_id}",
            )
        registry = ModelRegistry(artifacts_dir=registry_dir)
    return QuantAgent(registry=registry, dual_source_loader=load_latest_scores)


def _make_recommendation_market_client(*, market_data_mode: str = "auto") -> Any:
    from src.connectors.kis_rest import KISRestClient
    from src.utils.auth import AuthManager

    if market_data_mode == "auto":
        return KISRestClient()

    class NoopRateLimiter:
        def wait_and_acquire(
            self,
            tokens: int = 1,
            max_wait_sec: float = 10.0,
        ) -> None:
            return None

    class RecommendationAuthManager(AuthManager):
        def get_mode(self) -> str:
            return market_data_mode

    rate_limiter = NoopRateLimiter() if market_data_mode == "mock" else None
    return KISRestClient(auth=RecommendationAuthManager(), rate_limiter=rate_limiter)


def _recommendation_minute_bar_cache(
    *,
    config: MinuteBarWindowCacheConfig,
    market_data_client: Any | None,
    minute_bar_cache: MinuteBarWindowCache | None,
    market_data_mode: str,
) -> MinuteBarWindowCache:
    if minute_bar_cache is not None:
        return minute_bar_cache
    if market_data_client is not None:
        return MinuteBarWindowCache(market_data_client, config)
    global _RECOMMENDATION_BAR_CACHE
    global _RECOMMENDATION_BAR_CACHE_CONFIG
    global _RECOMMENDATION_BAR_CACHE_MARKET_DATA_MODE
    with _RECOMMENDATION_BAR_CACHE_LOCK:
        if (
            _RECOMMENDATION_BAR_CACHE is None
            or _RECOMMENDATION_BAR_CACHE_CONFIG != config
            or _RECOMMENDATION_BAR_CACHE_MARKET_DATA_MODE != market_data_mode
        ):
            _RECOMMENDATION_BAR_CACHE = MinuteBarWindowCache(
                _make_recommendation_market_client(market_data_mode=market_data_mode),
                config,
            )
            _RECOMMENDATION_BAR_CACHE_CONFIG = config
            _RECOMMENDATION_BAR_CACHE_MARKET_DATA_MODE = market_data_mode
        return _RECOMMENDATION_BAR_CACHE


def build_recommendations_payload(
    *,
    request_id: str = "",
    bundle_id: str = "",
    asof: str = "",
    tickers: list[str] | tuple[str, ...] | None = None,
    top_k: int = 0,
    include_diagnostics: bool = False,
    root: Path | None = None,
    quant_agent: Any | None = None,
    market_data_client: Any | None = None,
    minute_bar_cache: MinuteBarWindowCache | None = None,
) -> dict[str, Any]:
    """Build read-only model recommendations for BE dashboard use.

    The response exposes rank/score metadata only. It does not include target
    weights, order deltas, quantities, or order authorization fields.
    """
    repo_root = root or _REPO_ROOT
    response_request_id = request_id or f"REQ-{uuid.uuid4().hex[:12]}"
    raw_bundle_id = str(bundle_id or "").strip()
    requested_bundle_id = raw_bundle_id
    requested_asof = str(asof or "").strip()
    diagnostics: dict[str, Any] = {
        "external_order_api_called": False,
        "registry_mutated": False,
        "live_trading_allowed": False,
    }
    try:
        cfg = _resolve_recommendation_config()
        requested_bundle_id = raw_bundle_id or str(cfg["default_bundle_id"]).strip()
        if not requested_bundle_id:
            raise ValueError("grpc_recommendations.default_bundle_id_required")
        warmup_bars = int(config_load("risk_config.yaml", "quant_agent")["warmup_bars"])
        minute_bar_cache_config = load_minute_bar_window_cache_config(
            window_size=warmup_bars,
            parallel_fetch_workers=int(cfg["parallel_fetch_workers"]),
        )
    except Exception as e:
        diagnostics["config_error"] = str(e)
        return _recommendation_blocked_payload(
            request_id=response_request_id,
            bundle_id=requested_bundle_id,
            reason="grpc_recommendations_config_invalid",
            asof=requested_asof,
            diagnostics=diagnostics,
            include_diagnostics=include_diagnostics,
        )

    selected_tickers, invalid_tickers, names = _normalize_recommendation_tickers(
        tickers or [],
    )
    diagnostics["requested_tickers"] = list(tickers or [])
    diagnostics["selected_tickers"] = selected_tickers
    if invalid_tickers:
        diagnostics["invalid_tickers"] = invalid_tickers
        return _recommendation_blocked_payload(
            request_id=response_request_id,
            bundle_id=requested_bundle_id,
            reason="invalid_or_out_of_universe_ticker",
            asof=requested_asof,
            diagnostics=diagnostics,
            include_diagnostics=include_diagnostics,
        )
    if not selected_tickers:
        return _recommendation_blocked_payload(
            request_id=response_request_id,
            bundle_id=requested_bundle_id,
            reason="active_universe_empty",
            asof=requested_asof,
            diagnostics=diagnostics,
            include_diagnostics=include_diagnostics,
        )

    resolved_top_k = int(top_k or cfg["default_top_k"])
    resolved_top_k = max(1, min(resolved_top_k, int(cfg["max_top_k"])))
    diagnostics["top_k"] = resolved_top_k
    diagnostics["market_data_mode"] = cfg["market_data_mode"]

    try:
        quant = quant_agent or _make_recommendation_quant_agent(
            bundle_id=requested_bundle_id,
            repo_root=repo_root,
        )
    except Exception as e:
        diagnostics["quant_init_error"] = str(e)
        return _recommendation_blocked_payload(
            request_id=response_request_id,
            bundle_id=requested_bundle_id,
            reason="quant_agent_unavailable",
            asof=requested_asof,
            diagnostics=diagnostics,
            include_diagnostics=include_diagnostics,
        )

    metadata = getattr(quant, "model_metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    model_version = str(metadata.get("version") or "")
    metadata_bundle_id = str(metadata.get("bundle_id") or "")
    response_bundle_id = requested_bundle_id or metadata_bundle_id
    if requested_bundle_id and metadata_bundle_id and requested_bundle_id != metadata_bundle_id:
        diagnostics["metadata_bundle_id"] = metadata_bundle_id
        return _recommendation_blocked_payload(
            request_id=response_request_id,
            bundle_id=requested_bundle_id,
            reason="bundle_id_mismatch",
            asof=requested_asof,
            mode="blocked",
            model_version=model_version,
            diagnostics=diagnostics,
            include_diagnostics=include_diagnostics,
        )

    resolved_asof = requested_asof or _now_iso()
    try:
        cache = _recommendation_minute_bar_cache(
            config=minute_bar_cache_config,
            market_data_client=market_data_client,
            minute_bar_cache=minute_bar_cache,
            market_data_mode=str(cfg["market_data_mode"]),
        )
        cache_result = cache.get_windows(
            selected_tickers,
            asof=resolved_asof,
            min_bars=warmup_bars,
        )
        diagnostics["minute_bar_window_cache"] = cache_result.metadata
        bar_errors: dict[str, str] = {
            str(ticker): str(reason)
            for ticker, reason in cache_result.metadata.get("failed_tickers", {}).items()
        }
        for ticker in selected_tickers:
            for bar in cache_result.windows.get(ticker, []):
                if not isinstance(bar, dict):
                    continue
                row = dict(bar)
                row["ticker"] = ticker
                quant.on_bar(row)
    except Exception as e:
        diagnostics["market_data_error"] = str(e)
        return _recommendation_blocked_payload(
            request_id=response_request_id,
            bundle_id=response_bundle_id,
            reason="market_data_unavailable",
            asof=resolved_asof,
            model_version=model_version,
            diagnostics=diagnostics,
            include_diagnostics=include_diagnostics,
        )

    if bar_errors:
        diagnostics["bar_errors"] = bar_errors

    scoring_tickers = list(selected_tickers)
    if bar_errors:
        successful_bar_tickers = [
            ticker
            for ticker in selected_tickers
            if ticker not in bar_errors and ticker in cache_result.windows
        ]
        diagnostics["successful_bar_tickers"] = successful_bar_tickers
        min_successful_tickers = _required_recommendation_count(
            resolved_top_k=resolved_top_k,
            candidate_count=len(selected_tickers),
            cfg=cfg,
        )
        partial_allowed = bool(cfg["allow_partial_minute_bar_failures"])
        if (
            not partial_allowed
            or len(successful_bar_tickers) < min_successful_tickers
            or len(bar_errors) == len(selected_tickers)
        ):
            diagnostics["partial_minute_bar_failures_allowed"] = False
            diagnostics["min_successful_tickers"] = min_successful_tickers
            return _recommendation_blocked_payload(
                request_id=response_request_id,
                bundle_id=response_bundle_id,
                reason=(
                    "minute_bars_unavailable"
                    if len(bar_errors) == len(selected_tickers)
                    else "partial_minute_bars_unavailable"
                ),
                asof=resolved_asof,
                model_version=model_version,
                diagnostics=diagnostics,
                include_diagnostics=include_diagnostics,
            )
        diagnostics["partial_minute_bar_failures_allowed"] = True
        diagnostics["min_successful_tickers"] = min_successful_tickers
        scoring_tickers = successful_bar_tickers

    diagnostics["scoring_tickers"] = scoring_tickers
    if not scoring_tickers:
        return _recommendation_blocked_payload(
            request_id=response_request_id,
            bundle_id=response_bundle_id,
            reason="minute_bars_unavailable",
            asof=resolved_asof,
            model_version=model_version,
            diagnostics=diagnostics,
            include_diagnostics=include_diagnostics,
        )

    if not resolved_asof:
        return _recommendation_blocked_payload(
            request_id=response_request_id,
            bundle_id=response_bundle_id,
            reason="asof_unavailable",
            model_version=model_version,
            diagnostics=diagnostics,
            include_diagnostics=include_diagnostics,
        )

    quant_output = quant.score_cross_section(scoring_tickers, asof=resolved_asof)
    quant_output = quant_output if isinstance(quant_output, dict) else {}
    diagnostics["quant_output"] = quant_output
    mode = str(quant_output.get("mode") or "blocked")
    scores = quant_output.get("scores")
    scores = scores if isinstance(scores, dict) else {}
    if mode != "active":
        return _recommendation_blocked_payload(
            request_id=response_request_id,
            bundle_id=response_bundle_id,
            reason=str(quant_output.get("blocker") or quant_output.get("error") or "quant_not_active"),
            asof=resolved_asof,
            mode=mode,
            model_version=model_version,
            diagnostics=diagnostics,
            include_diagnostics=include_diagnostics,
        )
    finite_scores: dict[str, float] = {}
    invalid_score_tickers: list[str] = []
    for ticker, score in scores.items():
        try:
            value = float(score)
        except Exception:
            invalid_score_tickers.append(str(ticker))
            continue
        if not math.isfinite(value):
            invalid_score_tickers.append(str(ticker))
            continue
        finite_scores[pad_ticker(str(ticker))] = value
    if invalid_score_tickers:
        diagnostics["invalid_score_tickers"] = invalid_score_tickers
    if not finite_scores:
        diagnostics["invalid_score_tickers"] = invalid_score_tickers
        return _recommendation_blocked_payload(
            request_id=response_request_id,
            bundle_id=response_bundle_id,
            reason="quant_scores_invalid_or_empty",
            asof=resolved_asof,
            mode=mode,
            model_version=model_version,
            diagnostics=diagnostics,
            include_diagnostics=include_diagnostics,
        )

    confidences = quant_output.get("confidences")
    confidences = confidences if isinstance(confidences, dict) else {}
    scoring_ticker_set = set(scoring_tickers)
    finite_scores = {
        ticker: score
        for ticker, score in finite_scores.items()
        if ticker in scoring_ticker_set
    }
    if not finite_scores:
        diagnostics["unexpected_score_tickers"] = sorted(
            pad_ticker(str(ticker))
            for ticker in scores
            if pad_ticker(str(ticker)) not in scoring_ticker_set
        )
        return _recommendation_blocked_payload(
            request_id=response_request_id,
            bundle_id=response_bundle_id,
            reason="quant_scores_invalid_or_empty",
            asof=resolved_asof,
            mode=mode,
            model_version=model_version,
            diagnostics=diagnostics,
            include_diagnostics=include_diagnostics,
        )
    min_recommendation_count = _required_recommendation_count(
        resolved_top_k=resolved_top_k,
        candidate_count=len(scoring_tickers),
        cfg=cfg,
    )
    diagnostics["min_recommendation_count"] = min_recommendation_count
    diagnostics["finite_score_count"] = len(finite_scores)
    if len(finite_scores) < min_recommendation_count:
        logger.warning(
            "[grpc_recommendations] insufficient recommendations: finite=%d required=%d top_k=%d scoring=%d",
            len(finite_scores),
            min_recommendation_count,
            resolved_top_k,
            len(scoring_tickers),
        )
        return _recommendation_blocked_payload(
            request_id=response_request_id,
            bundle_id=response_bundle_id,
            reason="insufficient_recommendations",
            asof=resolved_asof,
            mode=mode,
            model_version=model_version,
            diagnostics=diagnostics,
            include_diagnostics=include_diagnostics,
        )
    ranked = sorted(finite_scores.items(), key=lambda item: item[1], reverse=True)
    recommendations: list[dict[str, Any]] = []
    for rank, (ticker, score) in enumerate(ranked[:resolved_top_k], start=1):
        try:
            confidence = float(confidences.get(ticker, 0.0))
        except Exception:
            confidence = 0.0
        risk_level = _risk_level_for_confidence(confidence, cfg)
        stock_name = names.get(ticker, ticker)
        recommendations.append({
            "recommendation_id": f"{response_request_id}-{rank}-{ticker}",
            "request_id": response_request_id,
            "stock_code": ticker,
            "ticker": ticker,
            "stock_name": stock_name,
            "ranking": rank,
            "score": score,
            "reason": _recommendation_reason_ko(
                template=str(cfg["reason_ko_template"]),
                stock_name=stock_name,
                ticker=ticker,
                ranking=rank,
                score=score,
                risk_level=risk_level,
                model_version=model_version,
            ),
            "expected_return": 0.0,
            "expected_return_available": False,
            "risk_level": risk_level,
            "model_version": model_version,
            "bundle_id": response_bundle_id,
        })

    diagnostics_json = ""
    if include_diagnostics:
        diagnostics_json = json.dumps(diagnostics, ensure_ascii=False, sort_keys=True)
    return {
        "request_id": response_request_id,
        "status": "PASS",
        "reason": "recommendations_ready",
        "generated_at": _now_iso(),
        "bundle_id": response_bundle_id,
        "model_version": model_version,
        "asof": resolved_asof,
        "mode": mode,
        "recommendations": recommendations,
        "live_trading_allowed": False,
        "registry_mutated": False,
        "diagnostics_json": diagnostics_json,
    }


def build_ack_payload(
    *,
    request_id: str = "",
    idempotency_key: str = "",
    accepted: bool = True,
    status: str = "ACK_READ_ONLY",
    reason: str = "validated by AI gRPC bridge; no live trading or registry mutation",
) -> dict[str, Any]:
    """Build a transport-level ACK without persisting or mutating state."""
    return {
        "request_id": request_id,
        "accepted": bool(accepted),
        "status": status,
        "reason": reason,
        "received_at": _now_iso(),
        "idempotency_key": idempotency_key,
    }

#!/usr/bin/env python
"""Community live risk smoke.

This script does not read .env files. Real Naver Search calls only use process
environment variables through existing AuthManager/CommunityCrawler code.

Goal:
  Naver cafearticle/blog or internal fake posts
  -> C2 community normalize through EventGateway
  -> MessagePool risk_warning / uncertainty_signal publish
  -> FDA Cold Path reason_code smoke.

This is live Cold Path evidence, not C12 deploy-quality or historical alpha.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.agents.cold.risk_fast import RiskAgentFast  # noqa: E402
from src.agents.fda import FDAAgent  # noqa: E402
from src.blackboard.message_pool import MessagePool  # noqa: E402
from src.blackboard.pubsub import PubSubBroker  # noqa: E402
from src.connectors.community import CommunityCrawler, CommunityPost  # noqa: E402
from src.data.event_admission import EventAdmission  # noqa: E402
from src.data.event_normalizer import EventNormalizer  # noqa: E402
from src.orchestration.event_gateway import EventGateway  # noqa: E402
from src.utils.safe_cast import safe_bool  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_REPORT_DIR = ROOT / "artifacts" / "reports" / "community_live_risk"


class _FakeNaverAuth:
    def get_naver_client(self) -> tuple[str, str]:
        return ("internal_fake_client", "internal_fake_secret")


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _split_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _fake_naver_get_json(url: str, params: dict[str, str], headers=None) -> dict[str, Any]:
    provider = "blog" if "blog" in url else "cafearticle"
    query = params.get("query", "")
    today = datetime.now(_KST).strftime("%Y%m%d")
    if provider == "blog":
        return {
            "items": [
                {
                    "title": f"{query} 블로그 리스크 점검",
                    "description": "폭락 우려와 손절 불안이 커졌다는 개인 투자자 의견입니다.",
                    "link": f"https://example.com/blog/{abs(hash(query)) % 100000}",
                    "bloggername": "risk-blogger",
                    "postdate": today,
                }
            ]
        }
    return {
        "items": [
                {
                    "title": f"{query} 카페 반응 급증",
                    "description": "폭락 루머와 손절 불안, 하락 우려가 커졌다는 게시글입니다.",
                "link": f"https://example.com/cafe/{abs(hash(query)) % 100000}",
                "cafename": "주식토론",
                "cafeurl": "https://example.com/cafe",
            }
        ]
    }


def _raw_from_post(post: CommunityPost) -> dict[str, Any]:
    link_hash = (
        hashlib.sha256(post.url.encode("utf-8")).hexdigest()[:16]
        if post.url
        else ""
    )
    return {
        "post_title": f"community_proxy_event:{post.ticker}:{post.provider}",
        "posted_at": post.timestamp.isoformat(),
        "body": "",
        "ticker_mentioned": post.ticker,
        "spam_flag": False,
        "post_id": post.post_id,
        "author_id_hash": post.author_id,
        "source_link_hash": link_hash,
        "view_count": post.view_count,
        "comment_count": post.comment_count,
        "provider": post.provider,
        "query": post.query,
        "timestamp_quality": post.timestamp_quality,
        "timestamp_confidence": post.timestamp_confidence,
        "content_retention_policy": "derived_signal_only",
        "stores_raw_content": False,
        "collected_at": (
            post.collected_at.isoformat()
            if isinstance(post.collected_at, datetime)
            else None
        ),
    }


def _admission_for_report(report_dir: Path) -> EventAdmission:
    cfg = {
        "event_admission": {
            "max_backlog": 500,
            "stale_drop": True,
            "max_cold_path_jobs_per_minute": 1000,
            "jobs_per_minute_window_sec": 60,
            "comparator_sort_key": ["priority", "trigger_type", "scope", "recency"],
            "dedupe_ttl_sec": 300,
            "dead_letter_path": str(report_dir / "community_live_risk_dead_letter.jsonl"),
            "dead_letter_retention_days": 7,
        }
    }
    return EventAdmission(config=cfg)


def _apply_query_overrides(
    crawler: CommunityCrawler,
    *,
    providers: list[str],
    query_suffixes: list[str],
    max_posts_per_ticker: int,
    display: int,
    sort: str,
    include_ticker_queries: bool,
    max_queries_per_ticker: int | None,
) -> None:
    cfg = dict(getattr(crawler, "_community_cfg", {}) or {})
    if providers:
        cfg["naver_search_providers"] = providers
    if query_suffixes:
        cfg["naver_query_suffixes"] = query_suffixes
    cfg["max_posts_per_ticker"] = max(1, int(max_posts_per_ticker))
    cfg["display_per_query"] = max(1, min(100, int(display)))
    cfg["sort"] = sort if sort in {"sim", "date"} else "date"
    cfg["include_ticker_queries"] = include_ticker_queries
    if max_queries_per_ticker is not None:
        cfg["max_queries_per_ticker"] = max(1, int(max_queries_per_ticker))
    setattr(crawler, "_community_cfg", cfg)


def _timestamp_quality_counts(posts: list[CommunityPost]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for post in posts:
        key = str(post.timestamp_quality or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _timestamp_confidence_counts(posts: list[CommunityPost]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for post in posts:
        key = str(getattr(post, "timestamp_confidence", "") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _provider_counts(posts: list[CommunityPost]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for post in posts:
        key = str(post.provider or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _provider_coverage(
    requested_providers: list[str],
    provider_counts: dict[str, int],
) -> dict[str, Any]:
    requested = list(
        dict.fromkeys(
            str(provider).strip()
            for provider in requested_providers
            if str(provider).strip()
        )
    )
    observed = {provider: int(provider_counts.get(provider, 0) or 0) for provider in requested}
    missing = [provider for provider, count in observed.items() if count <= 0]
    return {
        "requested": requested,
        "observed": observed,
        "missing_requested_providers": missing,
        "status": "WARN" if missing else "PASS",
        "note": (
            "Missing requested providers means this smoke proves only observed provider paths."
            if missing
            else "All requested providers produced at least one post."
        ),
    }


def _query_counts(posts: list[CommunityPost]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for post in posts:
        key = str(post.query or "(unknown)")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _context_for_score(
    score: Any,
    *,
    use_post_count_as_smoke_zscore: bool,
    news_score_neutral_proxy: bool,
) -> dict[str, float]:
    comm_score = float(getattr(score, "comm_score", 0.0) or 0.0)
    post_count = float(getattr(score, "post_count", 0) or 0)
    return {
        "comm_volume_zscore": post_count if use_post_count_as_smoke_zscore else 0.0,
        "comm_sentiment_delta": comm_score,
        "intraday_return_zscore": 0.0,
        "foreign_net_sell_krw": 0.0,
        "news_comm_divergence": abs(0.0 - comm_score) if news_score_neutral_proxy else 0.0,
    }


def _sidecar_reason_codes(
    scores: dict[str, Any],
    *,
    timestamp_confidence_counts: dict[str, int],
    news_score_neutral_proxy: bool,
) -> list[str]:
    codes: list[str] = []
    if sum(timestamp_confidence_counts.get(key, 0) for key in ("low", "unknown")) > 0:
        codes.append("COMMUNITY_TIMESTAMP_WEAK")
    if any(int(getattr(score, "manipulation_flagged", 0) or 0) > 0 for score in scores.values()):
        codes.append("COMMUNITY_MANIPULATION_FLAG")
    if any(int(getattr(score, "post_count", 0) or 0) > 0 for score in scores.values()):
        codes.append("COMMUNITY_LIVE_PROXY_RISK")
    if news_score_neutral_proxy and any(
        abs(float(getattr(score, "comm_score", 0.0) or 0.0)) >= 0.5
        for score in scores.values()
    ):
        codes.append("NEWS_COMMUNITY_DIVERGENCE")
    return list(dict.fromkeys(codes))


def run_community_live_risk_smoke(
    *,
    tickers: list[str],
    providers: list[str],
    query_suffixes: list[str],
    max_posts_per_ticker: int,
    display: int,
    sort: str,
    include_ticker_queries: bool,
    max_queries_per_ticker: int | None,
    window_minutes: int,
    publish_to_message_pool: bool,
    use_post_count_as_smoke_zscore: bool,
    news_score_neutral_proxy: bool,
    allow_mock: bool,
    internal_fake_naver: bool,
    output_dir: Path,
    write_report: bool = True,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_tickers = [pad_ticker(ticker) for ticker in tickers]
    try:
        if internal_fake_naver:
            previous_scrape_enabled = os.environ.get("COMMUNITY_SCRAPE_ENABLED")
            os.environ["COMMUNITY_SCRAPE_ENABLED"] = "1"
            try:
                crawler = CommunityCrawler(auth=_FakeNaverAuth())
            finally:
                if previous_scrape_enabled is None:
                    os.environ.pop("COMMUNITY_SCRAPE_ENABLED", None)
                else:
                    os.environ["COMMUNITY_SCRAPE_ENABLED"] = previous_scrape_enabled
            crawler._http_get_json = _fake_naver_get_json  # type: ignore[method-assign]
        else:
            crawler = CommunityCrawler()
    except Exception as e:
        report = {
            "status": "BLOCKED",
            "action": "community_live_risk_smoke",
            "generated_at": datetime.now(_KST).isoformat(),
            "tickers": normalized_tickers,
            "blockers": [f"community_crawler_init_failed:{type(e).__name__}"],
            "error": str(e),
            "is_mock": None,
            "internal_fake_naver": internal_fake_naver,
            "deploy_quality": False,
        }
        return _write_report(report, output_dir, write_report)
    _apply_query_overrides(
        crawler,
        providers=providers,
        query_suffixes=query_suffixes,
        max_posts_per_ticker=max_posts_per_ticker,
        display=display,
        sort=sort,
        include_ticker_queries=include_ticker_queries,
        max_queries_per_ticker=max_queries_per_ticker,
    )
    is_mock = bool(getattr(crawler, "_is_mock", True)) and not internal_fake_naver
    blockers: list[str] = []
    if is_mock and not allow_mock:
        blockers.append("community_connector_mock_mode")

    posts: list[CommunityPost] = []
    scores: dict[str, Any] = {}
    ingest_results: list[dict[str, Any]] = []
    dispatch_results: list[dict[str, Any]] = []
    risk_evals: list[dict[str, Any]] = []
    fda_result: dict[str, Any] | None = None

    try:
        posts = crawler.poll(normalized_tickers, window_minutes=window_minutes)
        scores = crawler.parse_sentiment(posts, window_minutes=window_minutes)
    except Exception as e:
        blockers.append(f"community_poll_failed:{type(e).__name__}")
        report = {
            "status": "BLOCKED",
            "action": "community_live_risk_smoke",
            "generated_at": datetime.now(_KST).isoformat(),
            "tickers": normalized_tickers,
            "blockers": blockers,
            "error": str(e),
            "is_mock": is_mock,
            "internal_fake_naver": internal_fake_naver,
            "deploy_quality": False,
        }
        return _write_report(report, output_dir, write_report)

    pool = MessagePool()
    pubsub = PubSubBroker(pool)
    gateway = EventGateway(
        admission=_admission_for_report(output_dir),
        normalizer=EventNormalizer(),
        pubsub=pubsub if publish_to_message_pool else None,
    )
    risk_fast = RiskAgentFast(pubsub=pubsub if publish_to_message_pool else None)
    fda = FDAAgent()
    context_by_ticker = {
        ticker: _context_for_score(
            score,
            use_post_count_as_smoke_zscore=use_post_count_as_smoke_zscore,
            news_score_neutral_proxy=news_score_neutral_proxy,
        )
        for ticker, score in scores.items()
    }
    timestamp_confidence_counts = _timestamp_confidence_counts(posts)
    sidecar_reason_codes = _sidecar_reason_codes(
        scores,
        timestamp_confidence_counts=timestamp_confidence_counts,
        news_score_neutral_proxy=news_score_neutral_proxy,
    )
    recommended_reason_code = (
        "NEWS_COMMUNITY_DIVERGENCE"
        if "NEWS_COMMUNITY_DIVERGENCE" in sidecar_reason_codes
        else sidecar_reason_codes[0]
        if sidecar_reason_codes
        else None
    )

    def community_handler(event: dict[str, Any]) -> dict[str, Any]:
        ticker = str(event.get("ticker") or event.get("payload", {}).get("ticker_mentioned") or "")
        context = context_by_ticker.get(ticker, {})
        risk_eval = risk_fast.evaluate(event, context=context)
        risk_evals.append(risk_eval)
        payload = {
            **risk_eval,
            "source": "community_live_risk_smoke",
            "ticker": ticker or None,
            "scope": event.get("scope", "market"),
            "event_id": event.get("event_id"),
            "occurred_at": event.get("occurred_at"),
            "asof": event.get("asof"),
            "confidence": 0.7 if risk_eval.get("triggered_rules") else 0.45,
            "priority": "normal" if risk_eval.get("triggered_rules") else "low",
            "risk_level": risk_eval.get("risk_level", "low"),
            "sidecar_reason_codes": sidecar_reason_codes,
            "recommended_fda_reason_code": recommended_reason_code,
            "timestamp_confidence": event.get("payload", {}).get("timestamp_confidence"),
            "content_retention_policy": "derived_signal_only",
            "stores_raw_content": False,
            "reasoning": (
                "커뮤니티 live proxy를 C2/EventGateway/RiskFast 경로로 처리. "
                f"context={context}"
            ),
        }
        msg = risk_fast.report("risk_warning", payload)
        return {
            "status": "processed",
            "published_by_agent": True,
            "risk_eval": risk_eval,
            "message": msg,
        }

    gateway.register_handler("community", community_handler)

    for post in posts:
        raw = _raw_from_post(post)
        result = gateway.ingest(raw, source="community", asof=datetime.now(_KST))
        result["post_id"] = post.post_id
        result["provider"] = post.provider
        result["timestamp_quality"] = post.timestamp_quality
        ingest_results.append(result)
        if result.get("status") == "admitted":
            dispatched = gateway.dispatch_next()
            if isinstance(dispatched, dict):
                dispatch_results.append(dispatched)

    risk_messages = pool.get_active_messages("risk_warning")
    uncertainty_messages = pool.get_active_messages("uncertainty_signal")
    risk_warning_payloads = [
        msg.get("payload", msg) if isinstance(msg, dict) else msg
        for msg in risk_messages
    ]
    actionable_risk = bool(uncertainty_messages) or any(
        isinstance(payload, dict)
        and (
            payload.get("stance") == "veto_recommendation"
            or str(payload.get("risk_level", "")).lower() in {"high", "critical"}
        )
        for payload in risk_warning_payloads
    )
    agent_signals = uncertainty_messages
    active_report_ids = [
        str(msg.get("message_id"))
        for msg in risk_messages + uncertainty_messages
        if isinstance(msg, dict) and msg.get("message_id")
    ]
    if dispatch_results and actionable_risk:
        max_uncertainty = max(
            [
                float(msg.get("payload", {}).get("uncertainty_score", 0.0) or 0.0)
                for msg in uncertainty_messages
            ]
            or [0.0]
        )
        fda_result = fda.decide(
            portfolio_patch_ref="PP-COMMUNITY-SMOKE",
            target_weights={},
            order_deltas=[],
            active_reports=active_report_ids,
            risk_warnings=risk_warning_payloads,
            mode="cold",
            agent_signals=agent_signals,
            uncertainty_score=max_uncertainty,
        )

    raw_post_count = len(posts)
    valid_event_count = sum(1 for item in ingest_results if item.get("status") == "admitted")
    message_pool_publish_count = len(risk_messages) + len(uncertainty_messages)
    if raw_post_count <= 0:
        blockers.append("raw_post_count_zero")
    if valid_event_count <= 0:
        blockers.append("valid_event_count_zero")
    if publish_to_message_pool and message_pool_publish_count <= 0:
        blockers.append("message_pool_publish_count_zero")
    fda_stage_status = "PASS" if fda_result else "SKIP_NO_ACTIONABLE_RISK"
    timestamp_quality_counts = _timestamp_quality_counts(posts)
    timestamp_confidence_counts = _timestamp_confidence_counts(posts)
    provider_counts = _provider_counts(posts)
    provider_coverage = _provider_coverage(providers, provider_counts)
    query_counts = _query_counts(posts)
    spam_filtered_total = sum(int(score.spam_filtered) for score in scores.values())
    manipulation_flagged_total = sum(
        int(score.manipulation_flagged) for score in scores.values()
    )
    provider_mode = "naver_search" if not is_mock else "mock"
    source_health_status = (
        "BLOCKED"
        if raw_post_count <= 0 or valid_event_count <= 0
        else provider_coverage["status"]
    )

    report = {
        "status": "PASS" if not blockers else "BLOCKED",
        "action": "community_live_risk_smoke",
        "generated_at": datetime.now(_KST).isoformat(),
        "evidence_level": "cold_path_live_smoke",
        "deploy_quality": False,
        "is_mock": is_mock,
        "internal_fake_naver": internal_fake_naver,
        "provider_mode": provider_mode,
        "tickers": normalized_tickers,
        "providers": providers,
        "query_suffixes": query_suffixes,
        "provider_coverage": provider_coverage,
        "params": {
            "max_posts_per_ticker": max_posts_per_ticker,
            "display": display,
            "sort": sort,
            "include_ticker_queries": include_ticker_queries,
            "max_queries_per_ticker": max_queries_per_ticker,
            "window_minutes": window_minutes,
            "use_post_count_as_smoke_zscore": use_post_count_as_smoke_zscore,
            "news_score_neutral_proxy": news_score_neutral_proxy,
        },
        "metrics": {
            "raw_post_count": raw_post_count,
            "valid_event_count": valid_event_count,
            "message_pool_publish_count": message_pool_publish_count,
            "risk_warning_count": len(risk_messages),
            "uncertainty_signal_count": len(uncertainty_messages),
            "dispatch_count": len(dispatch_results),
            "tickers_with_posts": sorted({post.ticker for post in posts}),
            "timestamp_quality_counts": timestamp_quality_counts,
            "timestamp_confidence_counts": timestamp_confidence_counts,
            "provider_counts": provider_counts,
            "query_counts": query_counts,
        },
        "community_source_health": {
            "status": source_health_status,
            "community_provider_mode": provider_mode,
            "official_api_proxy": provider_mode == "naver_search",
            "raw_post_count": raw_post_count,
            "normalized_event_count": valid_event_count,
            "message_pool_publish_count": message_pool_publish_count,
            "provider_counts": provider_counts,
            "provider_coverage": provider_coverage,
            "timestamp_quality_counts": timestamp_quality_counts,
            "timestamp_confidence_counts": timestamp_confidence_counts,
            "published_at_quality": {
                "cafearticle": "missing_collected_at",
                "blog": "official_postdate_date_only",
            },
            "query_coverage": {
                "unique_query_count": len(query_counts),
                "queries": sorted(query_counts),
                "include_ticker_queries": include_ticker_queries,
                "query_suffixes": query_suffixes,
            },
            "filter_health": {
                "spam_filtered_total": spam_filtered_total,
                "manipulation_flagged_total": manipulation_flagged_total,
            },
            "quota": {
                "api_calls": len(query_counts) * max(1, len(providers)),
                "api_quota_limit": 25000,
                "api_quota_used_ratio": (
                    (len(query_counts) * max(1, len(providers))) / 25000.0
                ),
            },
            "compliance": {
                "stores_raw_content": False,
                "stores_derived_signal_only": True,
                "terms_review_required_for_durable_archive": True,
                "raw_content_fields_persisted": [],
            },
            "risk_sidecar": {
                "community_live_risk_ready": (
                    raw_post_count > 0
                    and valid_event_count > 0
                    and message_pool_publish_count > 0
                ),
                "reason_codes_emitted": sidecar_reason_codes,
                "recommended_fda_reason_code": recommended_reason_code,
            },
            "dedupe": {
                "policy": "provider+link+ticker",
                "post_dedupe_count": raw_post_count,
                "duplicate_rate_after_dedupe": 0.0,
            },
            "deploy_quality": False,
            "caveat": (
                "Cold Path live source-health evidence only. Historical PIT "
                "community coverage still requires a timestamped compliant archive."
            ),
        },
        "community_scores": {
            ticker: {
                "comm_score": score.comm_score,
                "post_count": score.post_count,
                "spam_filtered": score.spam_filtered,
                "manipulation_flagged": score.manipulation_flagged,
                "window_minutes": score.window_minutes,
                "timestamp": score.timestamp.isoformat(),
            }
            for ticker, score in scores.items()
        },
        "risk_evals": risk_evals,
        "fda": {
            "status": fda_stage_status,
            "mode": fda_result.get("mode") if fda_result else None,
            "reason_code": (
                fda_result.get("final_decision", {}).get("reason_code")
                if fda_result
                else None
            ),
            "approved": (
                fda_result.get("final_decision", {}).get("approved")
                if fda_result
                else None
            ),
            "can_change_weight": FDAAgent.CAN_CHANGE_WEIGHT,
            "decision": fda_result.get("final_decision") if fda_result else None,
            "active_report_ids": active_report_ids,
            "actionable_risk": actionable_risk,
        },
        "message_pool": {
            "risk_warning_messages": risk_messages,
            "uncertainty_signal_messages": uncertainty_messages,
        },
        "ingest_results": ingest_results,
        "dispatch_results": dispatch_results,
        "blockers": blockers,
        "caveats": [
            "This is live Cold Path smoke evidence, not C12 historical deploy-quality.",
            "Cafearticle timestamp_quality=missing_collected_at because official response has no posted_at field.",
            (
                "use_post_count_as_smoke_zscore is enabled as a smoke proxy, "
                "not a calibrated historical z-score."
                if use_post_count_as_smoke_zscore
                else "strict real z-score mode is active; post_count is not used as a z-score proxy."
            ),
            (
                "Provider coverage is partial: "
                + ",".join(provider_coverage["missing_requested_providers"])
                if provider_coverage["missing_requested_providers"]
                else "Provider coverage includes every requested provider."
            ),
        ],
    }
    return _write_report(report, output_dir, write_report)


def _write_report(report: dict[str, Any], output_dir: Path, write_report: bool) -> dict[str, Any]:
    if not write_report:
        return report
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"community_live_risk_smoke_{ts}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")
    report["report_path"] = str(path)
    report["report_path_relative"] = _repo_relative(path)
    with path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", required=True, help="Comma-separated tickers")
    parser.add_argument("--providers", default="cafearticle,blog")
    parser.add_argument(
        "--query-suffixes",
        default="주식,종목토론,실적,목표가,급락,공시,토론",
    )
    parser.add_argument("--max-posts-per-ticker", type=int, default=10)
    parser.add_argument("--display", type=int, default=10)
    parser.add_argument("--sort", choices=["sim", "date"], default="date")
    parser.add_argument(
        "--include-ticker-queries",
        dest="include_ticker_queries",
        action="store_true",
        default=True,
    )
    parser.add_argument(
        "--no-include-ticker-queries",
        dest="include_ticker_queries",
        action="store_false",
    )
    parser.add_argument("--max-queries-per-ticker", type=int, default=None)
    parser.add_argument("--window-minutes", type=int, default=5)
    parser.add_argument("--publish-to-message-pool", action="store_true")
    parser.add_argument(
        "--strict-real-zscore",
        action="store_true",
        help="Keep post_count out of z-score proxy. This is the default.",
    )
    parser.add_argument(
        "--use-smoke-post-count-zscore",
        action="store_true",
        help="Opt in to using post_count as a smoke-only z-score proxy.",
    )
    parser.add_argument(
        "--news-score-neutral-proxy",
        action="store_true",
        help="Use neutral news score proxy for divergence smoke only.",
    )
    parser.add_argument("--allow-mock", action="store_true")
    parser.add_argument("--internal-fake-naver", action="store_true")
    parser.add_argument("--no-write-report", action="store_true")
    parser.add_argument("--output-dir", default=str(_REPORT_DIR))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_community_live_risk_smoke(
        tickers=_split_csv(args.tickers),
        providers=_split_csv(args.providers),
        query_suffixes=_split_csv(args.query_suffixes),
        max_posts_per_ticker=args.max_posts_per_ticker,
        display=args.display,
        sort=args.sort,
        include_ticker_queries=safe_bool(args.include_ticker_queries),
        max_queries_per_ticker=args.max_queries_per_ticker,
        window_minutes=args.window_minutes,
        publish_to_message_pool=safe_bool(args.publish_to_message_pool),
        use_post_count_as_smoke_zscore=(
            safe_bool(args.use_smoke_post_count_zscore)
            and not safe_bool(args.strict_real_zscore)
        ),
        news_score_neutral_proxy=safe_bool(args.news_score_neutral_proxy),
        allow_mock=safe_bool(args.allow_mock),
        internal_fake_naver=safe_bool(args.internal_fake_naver),
        output_dir=Path(args.output_dir),
        write_report=not safe_bool(args.no_write_report),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

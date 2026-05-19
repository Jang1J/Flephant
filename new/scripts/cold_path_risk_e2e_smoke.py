#!/usr/bin/env python
"""Cold Path news/community/DataLab risk E2E smoke.

This script does not read .env and does not call external APIs. It consumes
previous real evidence reports and proves the non-live risk path:

  community/DataLab/source-scope evidence
  -> Dual-Source risk context
  -> RiskFast
  -> RiskSlow
  -> Debate
  -> FDA cold decision

The stress branch is an admission/control proof, not a claim that the latest
real community sample itself produced a veto.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.agents.cold.debate import DebateAgent  # noqa: E402
from src.agents.cold.risk_fast import RiskAgentFast  # noqa: E402
from src.agents.cold.risk_slow import RiskAgentSlow  # noqa: E402
from src.agents.fda import FDAAgent  # noqa: E402
from src.blackboard.message_pool import MessagePool  # noqa: E402
from src.blackboard.pubsub import PubSubBroker  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_REPORT_DIR = ROOT / "artifacts" / "reports" / "cold_path_risk_e2e"
_COMMUNITY_DIR = ROOT / "artifacts" / "reports" / "community_live_risk"
_DATALAB_DIR = ROOT / "artifacts" / "reports" / "naver_datalab_attention"
_SOURCE_SCOPE_DIR = ROOT / "artifacts" / "reports" / "source_scope_summary"


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be object: {path}")
    return payload


def _latest_json(directory: Path, pattern: str) -> Path | None:
    candidates = sorted(directory.glob(pattern))
    return candidates[-1] if candidates else None


def _load_report(path: str | None, directory: Path, pattern: str) -> tuple[Path | None, dict[str, Any]]:
    resolved = Path(path) if path else _latest_json(directory, pattern)
    if resolved is None:
        return None, {}
    return resolved, _read_json(resolved)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _attention_stats(datalab_report: dict[str, Any], ticker: str) -> dict[str, Any]:
    rows = [
        row for row in datalab_report.get("rows", [])
        if pad_ticker(str(row.get("ticker", ""))) == ticker
    ]
    rows = sorted(rows, key=lambda row: str(row.get("period", "")))
    ratios = [_safe_float(row.get("ratio")) for row in rows]
    latest_ratio = ratios[-1] if ratios else 0.0
    peak_ratio = max(ratios) if ratios else 0.0
    if len(ratios) >= 3:
        baseline = ratios[:-1] or ratios
        mean = sum(baseline) / len(baseline)
        var = sum((value - mean) ** 2 for value in baseline) / len(baseline)
        std = math.sqrt(var)
        latest_z = (latest_ratio - mean) / std if std > 1e-12 else 0.0
        peak_z = (peak_ratio - mean) / std if std > 1e-12 else 0.0
    else:
        latest_z = 0.0
        peak_z = 0.0
    return {
        "ticker": ticker,
        "row_count": len(rows),
        "latest_ratio": round(latest_ratio, 6),
        "peak_ratio": round(peak_ratio, 6),
        "latest_z": round(latest_z, 6),
        "peak_z": round(peak_z, 6),
        "ratio_is_relative": bool(datalab_report.get("ratio_is_relative")),
    }


def _community_score(community_report: dict[str, Any], ticker: str) -> dict[str, Any]:
    scores = community_report.get("community_scores")
    item = scores.get(ticker, {}) if isinstance(scores, dict) else {}
    return {
        "ticker": ticker,
        "comm_score": _safe_float(item.get("comm_score")),
        "post_count": int(_safe_float(item.get("post_count"), 0.0)),
        "timestamp": item.get("timestamp"),
    }


def _community_ready(report: dict[str, Any]) -> bool:
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    return (
        report.get("status") == "PASS"
        and report.get("is_mock") is False
        and report.get("internal_fake_naver") is False
        and int(metrics.get("valid_event_count", 0) or 0) > 0
        and int(metrics.get("message_pool_publish_count", 0) or 0) > 0
    )


def _datalab_ready(report: dict[str, Any]) -> bool:
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    return (
        report.get("status") == "PASS"
        and report.get("is_mock") is False
        and report.get("internal_fake_naver") is False
        and int(metrics.get("attention_ratio_rows", 0) or 0) > 0
        and report.get("ratio_is_relative") is True
    )


def _event(ticker: str, community_report: dict[str, Any]) -> dict[str, Any]:
    generated_at = str(community_report.get("generated_at") or datetime.now(_KST).isoformat())
    return {
        "event_id": f"COLD-RISK-E2E-{datetime.now(_KST).strftime('%Y%m%d%H%M%S')}",
        "event_type": "community",
        "ticker": ticker,
        "scope": f"ticker:{ticker}",
        "occurred_at": generated_at,
        "asof": datetime.now(_KST).isoformat(),
        "priority": "normal",
        "payload": {
            "ticker": ticker,
            "title": "community/DataLab risk sidecar E2E",
            "summary": "뉴스-커뮤니티 divergence와 DataLab attention proxy를 Risk Agent 체인에 투입",
            "source": "cold_path_risk_e2e_smoke",
        },
    }


class _DeterministicColdRouter:
    """No-network router for deterministic Cold Path control evidence."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def call(
        self,
        prompt: str,
        mode: str,
        caller: str,
        structured_schema: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        self.calls.append({
            "mode": mode,
            "caller": caller,
            "structured_schema": bool(structured_schema),
        })
        if mode != "cold":
            return SimpleNamespace(
                success=False,
                model_used="deterministic-cold-smoke",
                content="",
                latency_ms=0.0,
                error="non_cold_mode_forbidden",
            )
        if caller == "risk_agent":
            content = {
                "stance": "veto_recommendation",
                "risk_level": "high",
                "regime_signal": False,
                "affected_tickers": ["005930"],
                "narrative": (
                    "커뮤니티 부정 반응과 검색 관심도 급등이 뉴스 방향과 충돌해 "
                    "신규 주문 보류가 필요합니다."
                ),
            }
        elif caller == "debate_agent":
            pairs = self._parse_pairs(prompt)
            content = {
                "results": [
                    {
                        "pair": [left, right],
                        "winner": left,
                        "confidence": 0.72,
                        "reasoning": "리스크 충돌 상황에서는 기존 상위 후보를 보수적으로 유지",
                    }
                    for left, right in pairs
                ]
            }
        elif caller == "fda_cold_path":
            content = {
                "approved": False,
                "reason_code": "NEWS_COMMUNITY_DIVERGENCE",
                "veto_reason": "뉴스-커뮤니티 divergence와 커뮤니티 리스크가 동시에 감지됨",
                "confidence": 0.86,
            }
        else:
            content = {"status": "neutral"}
        return SimpleNamespace(
            success=True,
            model_used="deterministic-cold-smoke",
            content=json.dumps(content, ensure_ascii=False),
            latency_ms=1.0,
            error=None,
        )

    @staticmethod
    def _parse_pairs(prompt: str) -> list[tuple[str, str]]:
        match = re.search(r"비교 pair 목록:\s*(\[.*?\])\s*\n\n", prompt, flags=re.S)
        if not match:
            return [("005930", "000660")]
        try:
            raw_pairs = json.loads(match.group(1))
        except json.JSONDecodeError:
            return [("005930", "000660")]
        pairs: list[tuple[str, str]] = []
        for item in raw_pairs:
            if isinstance(item, list) and len(item) == 2:
                pairs.append((pad_ticker(str(item[0])), pad_ticker(str(item[1]))))
        return pairs or [("005930", "000660")]


def _signal(agent: str, channel: str, payload: dict[str, Any], asof: str) -> dict[str, Any]:
    return {
        "agent": agent,
        "channel": channel,
        "payload": payload,
        "ts": asof,
        "asof": asof,
    }


def _publish_final_decision(
    *,
    pubsub: PubSubBroker,
    fda: FDAAgent,
    decision: dict[str, Any],
) -> str:
    message = fda.publish("final_decision", decision["final_decision"])
    return pubsub.publish("final_decision", message)


def run_cold_path_risk_e2e_smoke(
    *,
    ticker: str,
    community_report_path: str | None = None,
    datalab_report_path: str | None = None,
    source_scope_path: str | None = None,
    output_dir: Path = _REPORT_DIR,
    write_report: bool = True,
    stress_divergence: float = 0.72,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    ticker = pad_ticker(ticker)

    community_path, community_report = _load_report(
        community_report_path, _COMMUNITY_DIR, "community_live_risk_smoke_*.json"
    )
    datalab_path, datalab_report = _load_report(
        datalab_report_path, _DATALAB_DIR, "naver_datalab_attention_smoke_*.json"
    )
    source_scope_report_path, source_scope_report = _load_report(
        source_scope_path, _SOURCE_SCOPE_DIR, "source_scope_summary_*.json"
    )

    blockers: list[str] = []
    if not community_report:
        blockers.append("community_report_missing")
    elif not _community_ready(community_report):
        blockers.append("community_live_proxy_not_ready")
    if not datalab_report:
        blockers.append("datalab_report_missing")
    elif not _datalab_ready(datalab_report):
        blockers.append("datalab_attention_not_ready")
    if not source_scope_report:
        blockers.append("source_scope_report_missing")
    elif source_scope_report.get("status") != "PASS":
        blockers.append("source_scope_not_pass")

    event = _event(ticker, community_report)
    comm = _community_score(community_report, ticker)
    attention = _attention_stats(datalab_report, ticker)

    real_context = {
        "comm_volume_zscore": 0.0,
        "comm_sentiment_delta": comm["comm_score"],
        "intraday_return_zscore": 0.0,
        "foreign_net_sell_krw": 0.0,
        "news_comm_divergence": 0.0,
    }
    stress_context = {
        "comm_volume_zscore": max(3.0, abs(_safe_float(attention.get("peak_z")))),
        "comm_sentiment_delta": -0.65 if comm["comm_score"] <= 0 else 0.65,
        "intraday_return_zscore": 0.0,
        "foreign_net_sell_krw": 0.0,
        "news_comm_divergence": stress_divergence,
    }

    pool = MessagePool()
    pubsub = PubSubBroker(pool)
    router = _DeterministicColdRouter()
    fast = RiskAgentFast(pubsub=pubsub)
    slow = RiskAgentSlow(llm_router=router, pubsub=pubsub, memory_root=output_dir / "_agent_memory")
    debate = DebateAgent(llm_router=router, pubsub=pubsub, memory_root=output_dir / "_agent_memory")
    fda = FDAAgent(llm_router=router)

    real_fast_eval = fast.evaluate(event, context=real_context)
    stress_fast_eval = fast.evaluate(event, context=stress_context)
    stress_fast_payload = {
        **stress_fast_eval,
        "source": "cold_path_risk_e2e_smoke",
        "ticker": ticker,
        "scope": f"ticker:{ticker}",
        "event_id": event["event_id"],
        "occurred_at": event["occurred_at"],
        "asof": event["asof"],
        "confidence": max(0.7, stress_divergence),
        "priority": "urgent" if stress_fast_eval.get("stance") == "veto_recommendation" else "normal",
        "sidecar_reason_codes": [
            "NEWS_COMMUNITY_DIVERGENCE",
            "COMMUNITY_LIVE_PROXY_RISK",
        ],
        "recommended_fda_reason_code": "NEWS_COMMUNITY_DIVERGENCE",
        "reasoning": (
            "실수급 community/DataLab evidence 기반 stress admission: "
            f"context={stress_context}"
        ),
    }
    stress_fast_msg = fast.report("risk_warning", stress_fast_payload)

    slow_msg = slow.analyze(event, fast_eval=stress_fast_eval)
    risk_payload = (
        slow_msg.get("payload", {}) if isinstance(slow_msg, dict) else stress_fast_payload
    )
    risk_payload = {
        **risk_payload,
        "sidecar_reason_codes": [
            "NEWS_COMMUNITY_DIVERGENCE",
            "COMMUNITY_LIVE_PROXY_RISK",
        ],
        "recommended_fda_reason_code": "NEWS_COMMUNITY_DIVERGENCE",
    }

    quant_candidates = [ticker, "000660", "042700"]
    signals = [
        _signal(
            "quant_agent",
            "quant_signal",
            {
                "stance": "buy",
                "top10_candidates": quant_candidates,
                "confidence": 0.74,
            },
            event["asof"],
        ),
        _signal(
            "news_agent",
            "news_signal",
            {
                "stance": "sell",
                "ticker": ticker,
                "confidence": 0.71,
                "reasoning": "뉴스 방향과 커뮤니티 반응이 충돌",
            },
            event["asof"],
        ),
        _signal("risk_slow", "risk_warning", risk_payload, event["asof"]),
    ]
    uncertainty_signals = pool.get_active_messages("uncertainty_signal")
    signals.extend(
        _signal(
            "risk_fast",
            "uncertainty_signal",
            msg.get("payload", {}),
            event["asof"],
        )
        for msg in uncertainty_signals
    )

    debate_result = debate.run_debate(signals, candidates=quant_candidates)
    fda_decision = fda.decide(
        portfolio_patch_ref="PATCH-COLD-RISK-E2E",
        target_weights={ticker: 0.0},
        order_deltas=[{"ticker": ticker, "side": "buy", "qty": 1}],
        mode="cold",
        risk_warnings=[stress_fast_payload, risk_payload],
        debate_result=debate_result,
        agent_signals=signals,
        active_reports=[
            msg.get("message_id")
            for msg in pool.get_active_messages()
            if msg.get("message_id")
        ],
        uncertainty_score=stress_fast_eval.get("uncertainty_score", 0.0),
    )
    final_decision_message_id = _publish_final_decision(
        pubsub=pubsub,
        fda=fda,
        decision=fda_decision,
    )

    checks = {
        "community_real_ready": _community_ready(community_report),
        "datalab_real_ready": _datalab_ready(datalab_report),
        "source_scope_pass": source_scope_report.get("status") == "PASS",
        "risk_fast_real_executed": bool(real_fast_eval),
        "risk_fast_stress_triggered": bool(stress_fast_eval.get("triggered_rules")),
        "uncertainty_signal_published": len(uncertainty_signals) > 0,
        "risk_slow_executed": slow_msg is not None,
        "debate_conflict_detected": bool(debate_result.get("conflict_detected")),
        "fda_vetoed_by_risk": fda_decision["final_decision"]["approved"] is False,
        "fda_reason_dual_source": (
            fda_decision["final_decision"]["reason_code"]
            == "NEWS_COMMUNITY_DIVERGENCE"
        ),
        "fda_can_change_weight_false": FDAAgent.CAN_CHANGE_WEIGHT is False,
        "live_trading_enabled": False,
        "registry_mutated": False,
        "external_api_called": False,
    }
    expected = {
        "community_real_ready": True,
        "datalab_real_ready": True,
        "source_scope_pass": True,
        "risk_fast_real_executed": True,
        "risk_fast_stress_triggered": True,
        "uncertainty_signal_published": True,
        "risk_slow_executed": True,
        "debate_conflict_detected": True,
        "fda_vetoed_by_risk": True,
        "fda_reason_dual_source": True,
        "fda_can_change_weight_false": True,
        "live_trading_enabled": False,
        "registry_mutated": False,
        "external_api_called": False,
    }
    failed = [
        key for key, expected_value in expected.items()
        if checks.get(key) is not expected_value
    ]
    blockers.extend(f"check_failed:{key}" for key in failed)

    report = {
        "status": "PASS" if not blockers else "BLOCKED",
        "action": "cold_path_risk_e2e_smoke",
        "generated_at": datetime.now(_KST).isoformat(),
        "evidence_level": "non_live_risk_agent_chain_e2e",
        "deploy_quality": False,
        "live_trading_enabled": False,
        "registry_mutated": False,
        "external_api_called": False,
        "ticker": ticker,
        "input_reports": {
            "community_live_risk": _repo_relative(community_path) if community_path else None,
            "naver_datalab_attention": _repo_relative(datalab_path) if datalab_path else None,
            "source_scope_summary": (
                _repo_relative(source_scope_report_path)
                if source_scope_report_path else None
            ),
        },
        "source_evidence": {
            "community": {
                "status": community_report.get("status"),
                "is_mock": community_report.get("is_mock"),
                "internal_fake_naver": community_report.get("internal_fake_naver"),
                "metrics": community_report.get("metrics", {}),
                "provider_coverage": community_report.get("provider_coverage", {}),
                "community_score": comm,
            },
            "datalab": {
                "status": datalab_report.get("status"),
                "is_mock": datalab_report.get("is_mock"),
                "internal_fake_naver": datalab_report.get("internal_fake_naver"),
                "ratio_is_relative": datalab_report.get("ratio_is_relative"),
                "metrics": datalab_report.get("metrics", {}),
                "attention_stats": attention,
            },
            "source_scope": {
                "status": source_scope_report.get("status"),
                "dual_source_history": source_scope_report.get("dual_source_history", {}),
                "cold_path": source_scope_report.get("cold_path", {}),
                "caveats": source_scope_report.get("caveats", []),
            },
        },
        "risk_context": {
            "real_sample_context": real_context,
            "stress_admission_context": stress_context,
            "stress_is_control_evidence": True,
            "stress_claim_scope": (
                "Proves RiskFast/RiskSlow/Debate/FDA behavior when "
                "news-community divergence crosses SSOT thresholds; it is not a "
                "claim that the latest real sample itself vetoed."
            ),
        },
        "agent_results": {
            "risk_fast_real": real_fast_eval,
            "risk_fast_stress": stress_fast_eval,
            "risk_fast_stress_message_id": stress_fast_msg.get("message_id"),
            "risk_slow": slow_msg,
            "debate": debate_result,
            "fda": fda_decision,
            "final_decision_message_id": final_decision_message_id,
            "router_calls": router.calls,
            "message_pool_counts": {
                channel: len(pool.get_active_messages(channel))
                for channel in [
                    "risk_warning",
                    "uncertainty_signal",
                    "veto_recommendation",
                    "debate_resolution",
                    "pairwise_ranking",
                    "final_decision",
                ]
            },
        },
        "checks": checks,
        "blockers": blockers,
        "caveats": [
            "Historical community alpha remains unproven because historical community_event_count is zero.",
            "DataLab is a relative attention proxy, not sentiment text.",
            "Cafearticle live timestamp confidence can remain low; this path is Cold Path risk sidecar evidence.",
            "Stress admission branch is deterministic control evidence, not real live veto evidence.",
        ],
    }
    return _write_report(report, output_dir, write_report)


def _write_report(report: dict[str, Any], output_dir: Path, write_report: bool) -> dict[str, Any]:
    if not write_report:
        return report
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"cold_path_risk_e2e_smoke_{ts}.json"
    report["report_path"] = str(path)
    report["report_path_relative"] = _repo_relative(path)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="005930")
    parser.add_argument("--community-report")
    parser.add_argument("--datalab-report")
    parser.add_argument("--source-scope-report")
    parser.add_argument("--stress-divergence", type=float, default=0.72)
    parser.add_argument("--output-dir", default=str(_REPORT_DIR))
    parser.add_argument("--no-write-report", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_cold_path_risk_e2e_smoke(
        ticker=args.ticker,
        community_report_path=args.community_report,
        datalab_report_path=args.datalab_report,
        source_scope_path=args.source_scope_report,
        output_dir=Path(args.output_dir),
        write_report=not args.no_write_report,
        stress_divergence=args.stress_divergence,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

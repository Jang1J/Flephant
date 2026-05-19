#!/usr/bin/env python3
"""Real LLM API smoke for Kanana/GPT routing.

This script never reads .env files and never stores raw prompt/response text.
Credentials are consumed only from the current process environment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from src.orchestration.llm_router import LLMModel, LLMRouter, LLMCallResult
from src.utils.time_utils import now_kst


REPORT_DIR = Path("artifacts/reports/llm_real_api_smoke")


SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
        "summary": {"type": "string"},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": ["risk_level", "summary", "risk_flags", "confidence"],
}


PROMPT = (
    "다음 한국어 시장 이벤트를 KOSPI 자동매매 Cold Path 리스크 관점에서 "
    "JSON으로 요약하라. 주문 생성이나 비중 변경 지시는 금지한다.\n"
    "이벤트: 삼성전자 반도체 수출 회복 기대감은 긍정적이나, "
    "커뮤니티에서는 단기 급등 과열과 차익실현 우려가 함께 관측된다."
)


@contextmanager
def _without_env(keys: list[str]) -> Iterator[None]:
    saved = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _content_digest(content: str | None) -> str | None:
    if content is None:
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def _parse_quality(content: str | None) -> dict[str, Any]:
    if not content:
        return {
            "json_parse_ok": False,
            "required_fields_present": False,
            "summary_len": 0,
            "risk_flags_count": 0,
            "error": "empty_content",
        }
    try:
        parsed = json.loads(content)
    except Exception as e:
        return {
            "json_parse_ok": False,
            "required_fields_present": False,
            "summary_len": 0,
            "risk_flags_count": 0,
            "error": f"{type(e).__name__}: {e}",
        }
    required = set(SUMMARY_SCHEMA["required"])
    present = required.issubset(parsed.keys()) if isinstance(parsed, dict) else False
    return {
        "json_parse_ok": True,
        "required_fields_present": present,
        "summary_len": len(str(parsed.get("summary", ""))) if isinstance(parsed, dict) else 0,
        "risk_flags_count": len(parsed.get("risk_flags", [])) if isinstance(parsed, dict) else 0,
        "risk_level": parsed.get("risk_level") if isinstance(parsed, dict) else None,
        "confidence_type": type(parsed.get("confidence")).__name__ if isinstance(parsed, dict) else None,
    }


def _summarize_result(label: str, result: LLMCallResult) -> dict[str, Any]:
    quality = _parse_quality(result.content)
    return {
        "label": label,
        "success": result.success,
        "model_used": result.model_used,
        "fallback_used": result.fallback_used,
        "latency_ms": result.latency_ms,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "content_len": len(result.content or ""),
        "content_sha256_12": _content_digest(result.content),
        "error_type": type(result.error).__name__ if result.error else None,
        "error_present": bool(result.error),
        "circuit_state": result.circuit_state,
        "quality": quality,
    }


def _hot_guard_result() -> dict[str, Any]:
    router = LLMRouter()
    try:
        router.call("should not call", mode="hot", caller="quant_agent")
    except RuntimeError as e:
        return {
            "label": "hot_path_guard",
            "success": "HOT_PATH_LLM_FORBIDDEN" in str(e),
            "error_contains": "HOT_PATH_LLM_FORBIDDEN" in str(e),
        }
    return {"label": "hot_path_guard", "success": False, "error_contains": False}


def run_smoke() -> dict[str, Any]:
    env_presence = {
        "KANANA_API_KEY": bool(os.environ.get("KANANA_API_KEY")),
        "KANANA_API_URL": bool(os.environ.get("KANANA_API_URL")),
        "KANANA_BASE_URL": bool(os.environ.get("KANANA_BASE_URL")),
        "KANANA_EFFECTIVE_BASE_URL": bool(
            os.environ.get("KANANA_API_URL") or os.environ.get("KANANA_BASE_URL")
        ),
        "OPENAI_API_KEY": bool(os.environ.get("OPENAI_API_KEY")),
    }

    results: list[dict[str, Any]] = []

    router = LLMRouter()
    kanana = router.call(
        PROMPT,
        mode="cold",
        caller="news_agent",
        structured_schema=SUMMARY_SCHEMA,
    )
    results.append(_summarize_result("cold_kanana_primary", kanana))

    with _without_env(["KANANA_API_KEY"]):
        fallback_router = LLMRouter()
        fallback = fallback_router.call(
            PROMPT,
            mode="cold",
            caller="news_agent",
            structured_schema=SUMMARY_SCHEMA,
        )
    results.append(_summarize_result("cold_gpt_fallback_without_kanana_key", fallback))

    direct_router = LLMRouter()
    direct = direct_router._call_gpt4o(  # provider smoke only; public Mode B guard tested separately.
        PROMPT,
        caller="backtest_reasoning",
        schema=SUMMARY_SCHEMA,
        is_fallback=False,
    )
    results.append(_summarize_result("gpt4o_provider_direct_schema_smoke", direct))
    results.append(_hot_guard_result())

    blockers: list[str] = []
    expected = {
        "cold_kanana_primary": (LLMModel.KANANA_O.value, False),
        "cold_gpt_fallback_without_kanana_key": (LLMModel.GPT_4O.value, True),
        "gpt4o_provider_direct_schema_smoke": (LLMModel.GPT_4O.value, False),
    }
    by_label = {item["label"]: item for item in results}
    for label, (model, fallback_used) in expected.items():
        item = by_label.get(label, {})
        if not item.get("success"):
            blockers.append(f"{label}:call_failed")
            continue
        if item.get("model_used") != model:
            blockers.append(f"{label}:unexpected_model:{item.get('model_used')}")
        if bool(item.get("fallback_used")) != fallback_used:
            blockers.append(f"{label}:unexpected_fallback:{item.get('fallback_used')}")
        quality = item.get("quality", {})
        if not quality.get("json_parse_ok"):
            blockers.append(f"{label}:json_parse_failed")
        if not quality.get("required_fields_present"):
            blockers.append(f"{label}:required_fields_missing")
        if int(quality.get("summary_len") or 0) <= 0:
            blockers.append(f"{label}:empty_summary")
    if not by_label.get("hot_path_guard", {}).get("success"):
        blockers.append("hot_path_guard_failed")

    status = "PASS" if not blockers else "BLOCKED"
    generated_at = now_kst().isoformat()
    return {
        "status": status,
        "action": "llm_real_api_smoke",
        "generated_at": generated_at,
        "stores_prompt_or_response_content": False,
        "env_presence": env_presence,
        "results": results,
        "blockers": blockers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-write-report", action="store_true")
    parser.add_argument("--output-dir", default=str(REPORT_DIR))
    args = parser.parse_args()

    report = run_smoke()
    if not args.no_write_report:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = now_kst().strftime("%Y%m%d_%H%M%S")
        path = output_dir / f"llm_real_api_smoke_{ts}.json"
        report["report_path"] = str(path.resolve())
        report["report_path_relative"] = str(path)
        with path.open("w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

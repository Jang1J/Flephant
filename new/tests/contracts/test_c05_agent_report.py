"""C5 AgentReportContract contract tests."""
from __future__ import annotations

import pytest

from src.agents._base import AgentBase
from src.agents.cold.news import NewsAgent


class _NoopRouter:
    def call(self, *args, **kwargs):  # pragma: no cover - report() tests do not call LLM.
        raise AssertionError("LLM must not be called by report()")


def test_c05_valid_report_types() -> None:
    """C5 report_types are distinct from C4 publish channels."""
    assert AgentBase.VALID_REPORT_TYPES == {
        "news_signal",
        "risk_warning",
        "quant_signal",
        "investor_flow_alert",
        "theme_score",
    }


def test_c05_news_report_payload_matches_contract() -> None:
    """NewsAgent C5 payload preserves C4 propagation confidence/scope."""
    agent = NewsAgent(llm_router=_NoopRouter())

    report = agent.report(
        "news_signal",
        {
            "stance": "buy",
            "impacted_tickers": ["005930"],
            "impacted_sectors": ["반도체"],
            "narrative": "실적 개선 기대",
            "confidence": 0.91,
            "scope": "ticker:005930",
        },
    )

    assert report["report_type"] == "news_signal"
    assert report["payload"] == {
        "stance": "buy",
        "confidence": 0.91,
        "scope": "ticker:005930",
        "impacted_tickers": ["005930"],
        "impacted_sectors": ["반도체"],
        "narrative": "실적 개선 기대",
    }


def test_c05_invalid_type_rejected() -> None:
    """C5: C4 publish-only channels must not be accepted as report_type."""
    agent = NewsAgent(llm_router=_NoopRouter())

    with pytest.raises(ValueError, match="report_type"):
        agent.report(
            "dart_alert",
            {
                "stance": "neutral",
                "impacted_tickers": [],
                "impacted_sectors": [],
                "narrative": "공시 이벤트",
            },
        )

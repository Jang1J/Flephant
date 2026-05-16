"""C3A DualSourceScoreContract contract tests."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.data.dual_source_scorer import DualSourceScorer

_KST = ZoneInfo("Asia/Seoul")


@pytest.fixture
def scorer() -> DualSourceScorer:
    import src.data.dual_source_scorer as mod

    mod._FINBERT_AVAILABLE = False
    return DualSourceScorer()


def _score(scorer: DualSourceScorer) -> dict:
    snapshot_ts = datetime.now(_KST).replace(hour=18, minute=0, second=0, microsecond=0)
    data_ts = datetime.now(_KST).replace(hour=7, minute=50, second=0, microsecond=0)
    return scorer.score(
        ticker="5930",
        news_texts=["삼성전자 실적 호조 상한가 기대"],
        comm_texts_t1=["떡상 기대되는 종목 매수 좋아요"],
        comm_texts_t2=["상한가 기대되는 종목입니다"],
        current_volume=120.0,
        historical_volumes=[80.0, 90.0, 85.0, 88.0],
        data_ts=data_ts,
        snapshot_ts=snapshot_ts,
    )


def test_c03a_scores_include_contract_fields(scorer: DualSourceScorer) -> None:
    """C3A output.scores 항목은 SSOT 8개 필드를 포함한다."""
    result = _score(scorer)

    assert set(result) >= {
        "ticker",
        "asof",
        "news_score_t",
        "comm_score_t_1",
        "comm_score_t_2",
        "news_comm_divergence",
        "community_noise_multiplier",
        "source_notes",
    }
    assert result["ticker"] == "005930"


def test_c03a_score_numeric_ranges(scorer: DualSourceScorer) -> None:
    """C3A numeric fields stay in their documented ranges."""
    result = _score(scorer)

    assert -1.0 <= result["news_score_t"] <= 1.0
    assert -1.0 <= result["comm_score_t_1"] <= 1.0
    assert -1.0 <= result["comm_score_t_2"] <= 1.0
    assert 0.0 <= result["news_comm_divergence"] <= 2.0
    assert 0.0 <= result["community_noise_multiplier"] <= 1.0

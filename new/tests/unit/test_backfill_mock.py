"""Backfill unit tests. Sprint 1-0 S1-0 Batch A."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

_KST = ZoneInfo("Asia/Seoul")


def _set_mock_env(monkeypatch):
    monkeypatch.setenv("KIS_MODE", "mock")
    monkeypatch.setenv("KIS_MOCK_SEED", "42")


# ------------------------------------------------------------------ #
# test_fetch_1m_bars_mock_mode
# ------------------------------------------------------------------ #


def test_fetch_1m_bars_mock_mode(monkeypatch, tmp_path):
    """KIS Mock에서 fake OHLCV 생성 검증. 장중 시간 필터 확인."""
    _set_mock_env(monkeypatch)
    from src.data.backfill import Backfill
    from src.connectors.kis_rest import KISRestClient

    bf = Backfill(
        kis_client=KISRestClient(),
        output_dir=tmp_path,
    )
    # 오늘보다 과거 날짜 사용 (PIT-Safety 통과)
    yesterday = (datetime.now(_KST) - timedelta(days=1)).strftime("%Y%m%d")
    two_days_ago = (datetime.now(_KST) - timedelta(days=2)).strftime("%Y%m%d")

    bars = bf.fetch_1m_bars("005930", two_days_ago, yesterday)

    # 결과 검증
    assert isinstance(bars, list)
    if bars:
        bar = bars[0]
        assert "ticker" in bar
        assert bar["ticker"] == "005930"
        assert "ts_close" in bar
        assert "open" in bar
        assert "close" in bar
        assert "volume" in bar


# ------------------------------------------------------------------ #
# test_save_parquet_creates_file
# ------------------------------------------------------------------ #


def test_save_parquet_creates_file(monkeypatch, tmp_path):
    """parquet 또는 JSONL 저장 경로 확인."""
    _set_mock_env(monkeypatch)
    from src.data.backfill import Backfill

    bf = Backfill(output_dir=tmp_path)
    bars = [
        {
            "ticker": "005930",
            "ts_close": "2026-04-17T09:01:00+09:00",
            "open": 70000,
            "high": 70500,
            "low": 69800,
            "close": 70200,
            "volume": 5000,
        }
    ]
    out_path = bf.save_parquet("005930", bars, "20260417")

    assert out_path.exists()
    # parquet 또는 jsonl 둘 중 하나
    assert out_path.suffix in {".parquet", ".jsonl"}


# ------------------------------------------------------------------ #
# test_pit_violation_future_date
# ------------------------------------------------------------------ #


def test_pit_violation_future_date(monkeypatch):
    """미래 end_date → PITViolationError 발생."""
    _set_mock_env(monkeypatch)
    from src.data.backfill import Backfill, PITViolationError

    bf = Backfill()
    future_date = (datetime.now(_KST) + timedelta(days=30)).strftime("%Y%m%d")
    start_date = (datetime.now(_KST) - timedelta(days=1)).strftime("%Y%m%d")

    with pytest.raises(PITViolationError):
        bf.fetch_1m_bars("005930", start_date, future_date)


# ------------------------------------------------------------------ #
# test_backfill_universe_multiple_tickers
# ------------------------------------------------------------------ #


def test_backfill_universe_multiple_tickers(monkeypatch, tmp_path):
    """3 종목 일괄 수집 결과 dict 검증."""
    _set_mock_env(monkeypatch)
    from src.data.backfill import Backfill
    from src.connectors.kis_rest import KISRestClient

    bf = Backfill(kis_client=KISRestClient(), output_dir=tmp_path)
    tickers = ["005930", "000660", "042700"]
    two_days_ago = (datetime.now(_KST) - timedelta(days=2)).strftime("%Y%m%d")
    yesterday = (datetime.now(_KST) - timedelta(days=1)).strftime("%Y%m%d")

    result = bf.backfill_universe(tickers, two_days_ago, yesterday)

    assert isinstance(result, dict)
    assert len(result) == 3
    for ticker in ["005930", "000660", "042700"]:
        assert ticker in result
        assert isinstance(result[ticker], int)
        assert result[ticker] >= 0


# ------------------------------------------------------------------ #
# test_start_after_end_raises
# ------------------------------------------------------------------ #


def test_start_after_end_raises(monkeypatch):
    """start_date > end_date → BackfillError."""
    _set_mock_env(monkeypatch)
    from src.data.backfill import Backfill, BackfillError

    bf = Backfill()
    yesterday = (datetime.now(_KST) - timedelta(days=1)).strftime("%Y%m%d")
    two_days_ago = (datetime.now(_KST) - timedelta(days=2)).strftime("%Y%m%d")

    with pytest.raises(BackfillError):
        bf.fetch_1m_bars("005930", yesterday, two_days_ago)

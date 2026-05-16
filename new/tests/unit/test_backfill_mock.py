"""Backfill unit tests. Sprint 1-0 S1-0 Batch A."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_KST = ZoneInfo("Asia/Seoul")


def _set_mock_env(monkeypatch):
    monkeypatch.setenv("KIS_MODE", "mock")
    monkeypatch.setenv("KIS_MOCK_SEED", "42")


def _sample_bars() -> list[dict]:
    return [
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


def _sample_bars_many(n: int, yyyymmdd: str = "20260417") -> list[dict]:
    day = datetime.strptime(yyyymmdd, "%Y%m%d").date()
    bars: list[dict] = []
    for i in range(n):
        hour = 9 + (i // 60)
        minute = i % 60
        ts = datetime.combine(day, datetime.min.time(), tzinfo=_KST).replace(
            hour=hour,
            minute=minute,
        )
        bars.append({
            "ticker": "005930",
            "ts_close": ts.isoformat(),
            "open": 70000 + i,
            "high": 70100 + i,
            "low": 69900 + i,
            "close": 70050 + i,
            "volume": 5000 + i,
        })
    return bars


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
    bars = _sample_bars()
    out_path = bf.save_parquet("005930", bars, "20260417")

    assert out_path.exists()
    # parquet 또는 jsonl 둘 중 하나
    assert out_path.suffix in {".parquet", ".jsonl"}


def test_save_parquet_removes_stale_jsonl(monkeypatch, tmp_path):
    """parquet 저장 성공 시 동일 날짜 stale JSONL 제거."""
    pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    _set_mock_env(monkeypatch)
    from src.data.backfill import Backfill

    stale_jsonl = tmp_path / "005930" / "bars_1m_20260417.jsonl"
    stale_jsonl.parent.mkdir(parents=True, exist_ok=True)
    stale_jsonl.write_text(
        json.dumps({"stale": True}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    bf = Backfill(output_dir=tmp_path)
    out_path = bf.save_parquet("005930", _sample_bars(), "20260417")

    assert out_path.suffix == ".parquet"
    assert out_path.exists()
    assert not stale_jsonl.exists()


def test_save_jsonl_fallback_removes_stale_parquet(monkeypatch, tmp_path):
    """JSONL fallback 저장 성공 시 stale parquet 제거."""
    _set_mock_env(monkeypatch)
    from src.data import backfill as backfill_module
    from src.data.backfill import Backfill

    stale_parquet = tmp_path / "005930" / "bars_1m_20260417.parquet"
    stale_parquet.parent.mkdir(parents=True, exist_ok=True)
    stale_parquet.write_bytes(b"stale parquet placeholder")
    monkeypatch.setattr(backfill_module, "_has_pyarrow", lambda: False)

    bf = Backfill(output_dir=tmp_path)
    out_path = bf.save_parquet("005930", _sample_bars(), "20260417")

    assert out_path.suffix == ".jsonl"
    assert out_path.exists()
    assert not stale_parquet.exists()


def test_save_jsonl_preserves_existing_complete_artifact_on_short_refetch(
    monkeypatch,
    tmp_path,
):
    """partial refetch가 기존 완성 JSONL artifact를 덮어쓰지 않는다."""
    _set_mock_env(monkeypatch)
    from src.data import backfill as backfill_module
    from src.data.backfill import Backfill

    monkeypatch.setattr(backfill_module, "_has_pyarrow", lambda: False)
    bf = Backfill(output_dir=tmp_path)
    original = bf.save_parquet("005930", _sample_bars_many(381), "20260417")

    out_path = bf.save_parquet("005930", _sample_bars_many(100), "20260417")

    assert out_path == original
    assert original.exists()
    with original.open("r", encoding="utf-8") as fh:
        assert sum(1 for line in fh if line.strip()) == 381


def test_save_parquet_preserves_existing_complete_artifact_on_short_refetch(
    monkeypatch,
    tmp_path,
):
    """partial refetch가 기존 완성 parquet artifact를 덮어쓰지 않는다."""
    pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    _set_mock_env(monkeypatch)
    import pandas as pd
    from src.data.backfill import Backfill

    bf = Backfill(output_dir=tmp_path)
    original = bf.save_parquet("005930", _sample_bars_many(381), "20260417")

    out_path = bf.save_parquet("005930", _sample_bars_many(100), "20260417")

    assert out_path == original
    assert len(pd.read_parquet(original)) == 381


def test_parquet_atomic_replace_failure_preserves_existing(monkeypatch, tmp_path):
    """parquet replace 실패 시 기존 parquet을 보존하고 temp 파일을 제거."""
    pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    _set_mock_env(monkeypatch)
    from src.data import backfill as backfill_module
    from src.data.backfill import Backfill, BackfillError

    save_dir = tmp_path / "005930"
    save_dir.mkdir(parents=True, exist_ok=True)
    existing = save_dir / "bars_1m_20260417.parquet"
    existing.write_bytes(b"old parquet bytes")

    def fail_replace(temp_path: Path, target_path: Path) -> None:
        raise OSError(f"forced replace failure for {target_path.name}")

    monkeypatch.setattr(backfill_module, "_atomic_replace", fail_replace)

    bf = Backfill(output_dir=tmp_path)
    with pytest.raises(BackfillError):
        bf.save_parquet("005930", _sample_bars(), "20260417")

    assert existing.read_bytes() == b"old parquet bytes"
    assert list(save_dir.glob(".*.tmp.parquet")) == []


def test_jsonl_atomic_replace_failure_preserves_existing(monkeypatch, tmp_path):
    """replace 실패 시 기존 JSONL을 보존하고 temp 파일을 제거."""
    _set_mock_env(monkeypatch)
    from src.data import backfill as backfill_module
    from src.data.backfill import Backfill, BackfillError

    save_dir = tmp_path / "005930"
    save_dir.mkdir(parents=True, exist_ok=True)
    existing = save_dir / "bars_1m_20260417.jsonl"
    existing.write_text(
        json.dumps({"ticker": "005930", "close": 1}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    def fail_replace(temp_path: Path, target_path: Path) -> None:
        raise OSError(f"forced replace failure for {target_path.name}")

    monkeypatch.setattr(backfill_module, "_atomic_replace", fail_replace)

    bf = Backfill(output_dir=tmp_path)
    with pytest.raises(BackfillError):
        bf._save_jsonl(save_dir, "005930", _sample_bars(), "20260417")

    assert existing.read_text(encoding="utf-8") == (
        json.dumps({"ticker": "005930", "close": 1}, ensure_ascii=False) + "\n"
    )
    assert list(save_dir.glob(".*.tmp.jsonl")) == []


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


def test_fetch_single_day_filters_non_target_real_bars(monkeypatch, tmp_path):
    """실 KIS 응답에 전일 bar가 섞여도 요청일 장중 bar만 남긴다."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    from src.data.backfill import Backfill

    class FakeKISClient:
        def inquire_minute_bar(self, ticker: str, n_bars: int = 390, date: str | None = None):
            return [
                {
                    "ticker": ticker,
                    "ts_close": "2026-05-07T15:29:00+09:00",
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": 1,
                    "_mode": "virtual",
                },
                {
                    "ticker": ticker,
                    "ts_close": "2026-05-08T08:59:00+09:00",
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": 1,
                    "_mode": "virtual",
                },
                {
                    "ticker": ticker,
                    "ts_close": "2026-05-08T09:00:00+09:00",
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": 1,
                    "_mode": "virtual",
                },
            ]

    bf = Backfill(kis_client=FakeKISClient(), output_dir=tmp_path)
    bars = bf._fetch_single_day("005930", "20260508")
    assert len(bars) == 1
    assert bars[0]["ts_close"] == "2026-05-08T09:00:00+09:00"

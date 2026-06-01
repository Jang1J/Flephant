from __future__ import annotations

from datetime import datetime, timedelta
import threading
import time
from typing import Any
from zoneinfo import ZoneInfo

from src.data.minute_bar_window_cache import (
    MinuteBarWindowCache,
    MinuteBarWindowCacheConfig,
)

_KST = ZoneInfo("Asia/Seoul")


def _bar(ticker: str, ts: datetime, close: float) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "ts_close": ts.isoformat(),
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": 1000,
    }


def _bars(ticker: str, start: datetime, count: int, *, close_base: float = 100.0) -> list[dict[str, Any]]:
    return [
        _bar(ticker, start + timedelta(minutes=idx), close_base + idx)
        for idx in range(count)
    ]


def _config(
    *,
    window_size: int = 60,
    incremental_fetch_bars: int = 6,
    freshness_max_lag_sec: int = 120,
    gap_refetch_sec: int = 300,
    expected_bar_interval_sec: int = 60,
    max_contiguity_gap_sec: int = 60,
    parallel_fetch_workers: int = 1,
) -> MinuteBarWindowCacheConfig:
    return MinuteBarWindowCacheConfig(
        window_size=window_size,
        incremental_fetch_bars=incremental_fetch_bars,
        freshness_max_lag_sec=freshness_max_lag_sec,
        gap_refetch_sec=gap_refetch_sec,
        expected_bar_interval_sec=expected_bar_interval_sec,
        max_contiguity_gap_sec=max_contiguity_gap_sec,
        force_cold_on_session_date_change=True,
        parallel_fetch_workers=parallel_fetch_workers,
    )


class ScriptedMinuteClient:
    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, int]] = []

    def inquire_minute_bar(self, ticker: str, n_bars: int = 60) -> list[dict[str, Any]]:
        self.calls.append((ticker, n_bars))
        if not self.responses:
            return []
        return list(self.responses.pop(0))


class ConcurrentMinuteClient:
    def __init__(self, responses: dict[str, list[dict[str, Any]]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, int]] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def inquire_minute_bar(self, ticker: str, n_bars: int = 60) -> list[dict[str, Any]]:
        with self._lock:
            self.calls.append((ticker, n_bars))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.02)
            return list(self.responses.get(ticker, []))
        finally:
            with self._lock:
                self.active -= 1


def test_cold_fetches_warmup_then_incremental_tail_only() -> None:
    start = datetime(2026, 6, 1, 9, 0, tzinfo=_KST)
    client = ScriptedMinuteClient([
        _bars("005930", start, 61),
        _bars("005930", start + timedelta(minutes=56), 6, close_base=156.0),
    ])
    cache = MinuteBarWindowCache(client, _config())

    first = cache.get_windows(["5930"], asof="2026-06-01T09:59:00+09:00", min_bars=60)
    second = cache.get_windows(["005930"], asof="2026-06-01T10:01:00+09:00", min_bars=60)

    assert first.status == "PASS"
    assert second.status == "PASS"
    assert client.calls == [("005930", 61), ("005930", 6)]
    assert first.metadata["tickers"]["005930"]["future_bar_filtered_count"] == 1
    assert first.metadata["tickers"]["005930"]["returned_rows"] == 60
    assert len(second.windows["005930"]) == 60
    assert second.windows["005930"][0]["ts_close"] == "2026-06-01T09:02:00+09:00"
    assert second.windows["005930"][-1]["ts_close"] == "2026-06-01T10:01:00+09:00"
    assert second.windows["005930"][-1]["change"] == 1.0
    assert second.metadata["tickers"]["005930"]["fetch_policy"] == "incremental"


def test_parallel_workers_fetch_ticker_windows_without_changing_ordered_output() -> None:
    start = datetime(2026, 6, 1, 9, 0, tzinfo=_KST)
    client = ConcurrentMinuteClient({
        "005930": _bars("005930", start, 61, close_base=100.0),
        "000660": _bars("000660", start, 61, close_base=200.0),
        "035420": _bars("035420", start, 61, close_base=300.0),
    })
    cache = MinuteBarWindowCache(client, _config(parallel_fetch_workers=3))

    result = cache.get_windows(
        ["005930", "000660", "035420"],
        asof="2026-06-01T09:59:00+09:00",
        min_bars=60,
    )

    assert result.status == "PASS"
    assert client.max_active > 1
    assert set(client.calls) == {
        ("005930", 61),
        ("000660", 61),
        ("035420", 61),
    }
    assert list(result.windows) == ["005930", "000660", "035420"]
    assert result.metadata["parallel_fetch_workers"] == 3


def test_current_forming_minute_is_filtered_when_asof_has_seconds() -> None:
    start = datetime(2026, 6, 1, 9, 0, tzinfo=_KST)
    client = ScriptedMinuteClient([
        _bars("005930", start, 61),
    ])
    cache = MinuteBarWindowCache(client, _config())

    result = cache.get_windows(
        ["005930"],
        asof="2026-06-01T09:59:30+09:00",
        min_bars=1,
    )

    assert result.status == "PASS"
    ticker_meta = result.metadata["tickers"]["005930"]
    assert ticker_meta["forming_bar_filtered_count"] == 1
    assert ticker_meta["future_bar_filtered_count"] == 1
    assert result.windows["005930"][-1]["ts_close"] == "2026-06-01T09:58:00+09:00"


def test_config_tune_covers_188_second_cycle_gap_with_incremental_fetch() -> None:
    start = datetime(2026, 6, 1, 9, 0, tzinfo=_KST)
    client = ScriptedMinuteClient([
        _bars("005930", start, 61),
        _bars("005930", start + timedelta(minutes=57), 6, close_base=157.0),
    ])
    cache = MinuteBarWindowCache(client, _config())

    first = cache.get_windows(["005930"], asof="2026-06-01T09:59:00+09:00", min_bars=60)
    second = cache.get_windows(["005930"], asof="2026-06-01T10:02:08+09:00", min_bars=60)

    assert first.status == "PASS"
    assert second.status == "PASS"
    assert client.calls == [("005930", 61), ("005930", 6)]
    ticker_meta = second.metadata["tickers"]["005930"]
    assert ticker_meta["fetch_policy"] == "incremental"
    assert ticker_meta["effective_fetch_policy"] == "incremental"
    assert ticker_meta["fetch_n"] == 6
    assert ticker_meta["contiguity_status"] == "PASS"
    assert ticker_meta["freshness_status"] == "PASS"
    assert ticker_meta["latest_ts"] == "2026-06-01T10:01:00+09:00"
    assert ticker_meta["forming_bar_filtered_count"] == 1
    assert second.windows["005930"][-1]["ts_close"] == "2026-06-01T10:01:00+09:00"


def test_filters_t_plus_one_before_cache_and_score_window() -> None:
    start = datetime(2026, 6, 1, 9, 0, tzinfo=_KST)
    client = ScriptedMinuteClient([
        [
            *_bars("005930", start, 60),
            _bar("005930", datetime(2026, 6, 1, 10, 0, tzinfo=_KST), 999.0),
        ],
    ])
    cache = MinuteBarWindowCache(client, _config(gap_refetch_sec=300))

    result = cache.get_windows(["005930"], asof="2026-06-01T09:59:00+09:00", min_bars=60)

    assert result.status == "PASS"
    assert client.calls == [("005930", 61)]
    returned_ts = [row["ts_close"] for row in result.windows["005930"]]
    assert "2026-06-01T10:00:00+09:00" not in returned_ts
    assert result.metadata["tickers"]["005930"]["future_bar_filtered_count"] == 1


def test_incremental_window_matches_fresh_full_fetch_reference() -> None:
    start = datetime(2026, 6, 1, 9, 0, tzinfo=_KST)
    raw = _bars("005930", start, 62)
    cache_client = ScriptedMinuteClient([
        raw[:60],
        raw[60:],
    ])
    reference_client = ScriptedMinuteClient([
        raw[2:],
    ])
    cache = MinuteBarWindowCache(cache_client, _config())
    reference = MinuteBarWindowCache(reference_client, _config())

    cache.get_windows(["005930"], asof="2026-06-01T09:59:00+09:00", min_bars=60)
    incremental = cache.get_windows(
        ["005930"],
        asof="2026-06-01T10:01:00+09:00",
        min_bars=60,
    )
    full = reference.get_windows(
        ["005930"],
        asof="2026-06-01T10:01:00+09:00",
        min_bars=60,
    )

    assert incremental.status == "PASS"
    assert full.status == "PASS"
    assert [
        (row["ts_close"], row["close"], row["change"])
        for row in incremental.windows["005930"]
    ] == [
        (row["ts_close"], row["close"], row["change"])
        for row in full.windows["005930"]
    ]


def test_incremental_hole_triggers_cold_retry_then_fails_if_hole_remains() -> None:
    start = datetime(2026, 6, 1, 9, 0, tzinfo=_KST)
    warmup = _bars("005930", start, 60)
    broker_drop_tail = [_bar("005930", datetime(2026, 6, 1, 10, 1, tzinfo=_KST), 161.0)]
    cold_still_missing = [
        *_bars("005930", start + timedelta(minutes=1), 59, close_base=101.0),
        _bar("005930", datetime(2026, 6, 1, 10, 1, tzinfo=_KST), 161.0),
    ]
    client = ScriptedMinuteClient([warmup, broker_drop_tail, cold_still_missing])
    cache = MinuteBarWindowCache(client, _config(gap_refetch_sec=300))

    cache.get_windows(["005930"], asof="2026-06-01T09:59:00+09:00", min_bars=60)
    result = cache.get_windows(["005930"], asof="2026-06-01T10:02:50+09:00", min_bars=60)

    assert result.status == "FAIL"
    assert result.windows == {}
    assert client.calls == [("005930", 61), ("005930", 6), ("005930", 61)]
    ticker_meta = result.metadata["tickers"]["005930"]
    assert ticker_meta["cold_retry_after_hole"] is True
    assert ticker_meta["fetch_policy"] == "cold_retry_after_hole"
    assert ticker_meta["reason"] == "non_contiguous_window"
    assert ticker_meta["contiguity_gap_count"] == 1
    assert result.metadata["failed_tickers"] == {"005930": "non_contiguous_window"}


def test_direct_cold_non_contiguous_window_fails_closed() -> None:
    start = datetime(2026, 6, 1, 9, 0, tzinfo=_KST)
    hole_window = [
        *_bars("005930", start + timedelta(minutes=1), 59, close_base=101.0),
        _bar("005930", datetime(2026, 6, 1, 10, 1, tzinfo=_KST), 161.0),
    ]
    client = ScriptedMinuteClient([hole_window])
    cache = MinuteBarWindowCache(client, _config(gap_refetch_sec=180))

    result = cache.get_windows(["005930"], asof="2026-06-01T10:02:50+09:00", min_bars=60)

    assert result.status == "FAIL"
    assert result.windows == {}
    assert client.calls == [("005930", 61)]
    assert result.metadata["failed_tickers"] == {"005930": "non_contiguous_window"}


def test_cold_cross_date_response_fails_closed_without_seeding_cache() -> None:
    day1_tail = _bars(
        "005930",
        datetime(2026, 6, 1, 14, 2, tzinfo=_KST),
        58,
        close_base=100.0,
    )
    day2_head = _bars(
        "005930",
        datetime(2026, 6, 2, 9, 0, tzinfo=_KST),
        2,
        close_base=200.0,
    )
    day2_retry = _bars(
        "005930",
        datetime(2026, 6, 2, 9, 0, tzinfo=_KST),
        3,
        close_base=200.0,
    )
    client = ScriptedMinuteClient([day1_tail + day2_head, day2_retry])
    cache = MinuteBarWindowCache(client, _config())

    first = cache.get_windows(["005930"], asof="2026-06-02T09:01:00+09:00", min_bars=1)
    second = cache.get_windows(["005930"], asof="2026-06-02T09:02:00+09:00", min_bars=1)

    assert first.status == "FAIL"
    assert first.windows == {}
    assert first.metadata["failed_tickers"] == {"005930": "non_contiguous_window"}
    assert second.status == "PASS"
    assert client.calls == [("005930", 61), ("005930", 61)]
    ticker_meta = second.metadata["tickers"]["005930"]
    assert ticker_meta["fetch_policy"] == "cold"
    assert ticker_meta["cached_rows_before"] == 0
    assert second.windows["005930"][0]["ts_close"] == "2026-06-02T09:00:00+09:00"
    assert second.windows["005930"][-1]["ts_close"] == "2026-06-02T09:02:00+09:00"


def test_incremental_hole_recovers_when_cold_retry_is_contiguous() -> None:
    start = datetime(2026, 6, 1, 9, 0, tzinfo=_KST)
    raw = _bars("005930", start, 62)
    broker_drop_tail = [_bar("005930", datetime(2026, 6, 1, 10, 1, tzinfo=_KST), 161.0)]
    client = ScriptedMinuteClient([
        raw[:60],
        broker_drop_tail,
        raw[2:],
    ])
    cache = MinuteBarWindowCache(client, _config())

    cache.get_windows(["005930"], asof="2026-06-01T09:59:00+09:00", min_bars=60)
    result = cache.get_windows(["005930"], asof="2026-06-01T10:02:50+09:00", min_bars=60)

    assert result.status == "PASS"
    assert client.calls == [("005930", 61), ("005930", 6), ("005930", 61)]
    assert result.windows["005930"][0]["ts_close"] == "2026-06-01T09:02:00+09:00"
    assert result.windows["005930"][-1]["ts_close"] == "2026-06-01T10:01:00+09:00"
    ticker_meta = result.metadata["tickers"]["005930"]
    assert ticker_meta["cold_retry_attempted"] is True
    assert ticker_meta["fetch_policy"] == "cold_retry_after_hole"
    assert ticker_meta["contiguity_status"] == "PASS"


def test_stale_incremental_window_fails_closed_without_serving_cache() -> None:
    start = datetime(2026, 6, 1, 9, 0, tzinfo=_KST)
    client = ScriptedMinuteClient([
        _bars("005930", start, 60),
        [],
    ])
    cache = MinuteBarWindowCache(
        client,
        _config(freshness_max_lag_sec=60, gap_refetch_sec=999),
    )

    assert cache.get_windows(["005930"], asof="2026-06-01T09:59:00+09:00", min_bars=60).status == "PASS"
    result = cache.get_windows(["005930"], asof="2026-06-01T10:05:00+09:00", min_bars=60)

    assert result.status == "FAIL"
    assert result.windows == {}
    assert result.metadata["failed_tickers"] == {"005930": "stale_latest_bar"}


def test_gap_refetch_forces_cold_without_incremental_tail() -> None:
    start = datetime(2026, 6, 1, 9, 0, tzinfo=_KST)
    client = ScriptedMinuteClient([
        _bars("005930", start, 60),
        _bars("005930", start + timedelta(minutes=5), 60, close_base=105.0),
    ])
    cache = MinuteBarWindowCache(client, _config(gap_refetch_sec=180))

    cache.get_windows(["005930"], asof="2026-06-01T09:59:00+09:00", min_bars=60)
    result = cache.get_windows(["005930"], asof="2026-06-01T10:04:00+09:00", min_bars=60)

    assert result.status == "PASS"
    assert client.calls == [("005930", 61), ("005930", 61)]
    assert result.metadata["tickers"]["005930"]["fetch_policy"] == "cold_gap_refetch"


def test_gap_refetch_cold_does_not_merge_old_cache_to_satisfy_min_bars() -> None:
    start = datetime(2026, 6, 1, 9, 0, tzinfo=_KST)
    client = ScriptedMinuteClient([
        _bars("005930", start, 60),
        _bars("005930", start + timedelta(minutes=44), 20, close_base=144.0),
    ])
    cache = MinuteBarWindowCache(client, _config(gap_refetch_sec=180))

    cache.get_windows(["005930"], asof="2026-06-01T09:59:00+09:00", min_bars=60)
    result = cache.get_windows(["005930"], asof="2026-06-01T10:04:00+09:00", min_bars=60)

    assert result.status == "FAIL"
    assert client.calls == [("005930", 61), ("005930", 61)]
    assert result.windows == {}
    assert result.metadata["failed_tickers"] == {"005930": "insufficient_bars"}


def test_per_ticker_stale_failure_isolated_as_partial() -> None:
    start = datetime(2026, 6, 1, 9, 0, tzinfo=_KST)
    client = ScriptedMinuteClient([
        _bars("005930", start, 60),
        _bars("000660", start - timedelta(minutes=10), 60),
    ])
    cache = MinuteBarWindowCache(client, _config(freshness_max_lag_sec=120))

    result = cache.get_windows(
        ["005930", "000660"],
        asof="2026-06-01T09:59:00+09:00",
        min_bars=60,
    )

    assert result.status == "PARTIAL"
    assert set(result.windows) == {"005930"}
    assert result.metadata["failed_tickers"] == {"000660": "stale_latest_bar"}


def test_session_date_boundary_forces_cold_refetch() -> None:
    day1 = datetime(2026, 6, 1, 9, 0, tzinfo=_KST)
    day2 = datetime(2026, 6, 2, 9, 0, tzinfo=_KST)
    client = ScriptedMinuteClient([
        _bars("005930", day1, 60),
        _bars("005930", day2, 60),
    ])
    cache = MinuteBarWindowCache(client, _config())

    cache.get_windows(["005930"], asof="2026-06-01T09:59:00+09:00", min_bars=60)
    result = cache.get_windows(["005930"], asof="2026-06-02T09:59:00+09:00", min_bars=60)

    assert result.status == "PASS"
    assert client.calls == [("005930", 61), ("005930", 61)]
    assert result.metadata["tickers"]["005930"]["fetch_policy"] == "cold_session_boundary"


def test_asof_rollback_forces_cold_refetch_without_serving_future_cache() -> None:
    start = datetime(2026, 6, 1, 9, 0, tzinfo=_KST)
    later_window = _bars("005930", start + timedelta(minutes=1), 60, close_base=101.0)
    earlier_window = _bars("005930", start, 60, close_base=100.0)
    client = ScriptedMinuteClient([later_window, earlier_window])
    cache = MinuteBarWindowCache(client, _config(gap_refetch_sec=300))

    first = cache.get_windows(
        ["005930"],
        asof="2026-06-01T10:00:00+09:00",
        min_bars=60,
    )
    rollback = cache.get_windows(
        ["005930"],
        asof="2026-06-01T09:59:00+09:00",
        min_bars=60,
    )

    assert first.status == "PASS"
    assert rollback.status == "PASS"
    assert client.calls == [("005930", 61), ("005930", 61)]
    assert rollback.metadata["tickers"]["005930"]["fetch_policy"] == "cold_asof_rollback"
    returned_ts = [row["ts_close"] for row in rollback.windows["005930"]]
    assert "2026-06-01T10:00:00+09:00" not in returned_ts
    assert rollback.windows["005930"][-1]["ts_close"] == "2026-06-01T09:59:00+09:00"

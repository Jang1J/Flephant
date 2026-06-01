"""PIT-safe rolling cache for KIS one-minute bar windows."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
import threading
import time
from typing import Any, Callable, Literal, Protocol, Sequence
from zoneinfo import ZoneInfo

from src.utils.config_loader import load as config_load
from src.utils.safe_cast import safe_bool, safe_float, safe_int
from src.utils.ticker_utils import pad_ticker

_KST = ZoneInfo("Asia/Seoul")


class MinuteBarClient(Protocol):
    def inquire_minute_bar(self, ticker: str, n_bars: int = 60) -> list[dict[str, Any]]:
        """Return normalized ascending minute bars for ``ticker``."""


@dataclass(frozen=True)
class MinuteBarWindowCacheConfig:
    window_size: int
    incremental_fetch_bars: int
    freshness_max_lag_sec: int
    gap_refetch_sec: int
    expected_bar_interval_sec: int
    max_contiguity_gap_sec: int
    force_cold_on_session_date_change: bool
    parallel_fetch_workers: int = 1
    batch_fetch_budget_sec: float = 45.0


@dataclass(frozen=True)
class MinuteBarWindowResult:
    status: Literal["PASS", "PARTIAL", "FAIL"]
    reason: str
    asof: str
    windows: dict[str, list[dict[str, Any]]]
    metadata: dict[str, Any]


def load_minute_bar_window_cache_config(
    *,
    window_size: int,
    parallel_fetch_workers: int | None = None,
) -> MinuteBarWindowCacheConfig:
    """Load cache policy from risk_config.yaml without embedding magic values."""
    cfg = config_load("risk_config.yaml", "minute_bar_window_cache") or {}
    required_keys = [
        "incremental_fetch_bars",
        "freshness_max_lag_sec",
        "gap_refetch_sec",
        "expected_bar_interval_sec",
        "max_contiguity_gap_sec",
        "force_cold_on_session_date_change",
    ]
    missing = [key for key in required_keys if key not in cfg]
    if missing:
        raise ValueError(f"minute_bar_window_cache missing required keys: {missing}")
    incremental_fetch_bars = safe_int(
        cfg["incremental_fetch_bars"],
        default=1,
        min_value=1,
    )
    freshness_max_lag_sec = safe_int(
        cfg["freshness_max_lag_sec"],
        default=0,
        min_value=0,
    )
    gap_refetch_sec = safe_int(
        cfg["gap_refetch_sec"],
        default=freshness_max_lag_sec,
        min_value=0,
    )
    expected_bar_interval_sec = safe_int(
        cfg["expected_bar_interval_sec"],
        default=60,
        min_value=1,
    )
    max_contiguity_gap_sec = safe_int(
        cfg["max_contiguity_gap_sec"],
        default=expected_bar_interval_sec,
        min_value=expected_bar_interval_sec,
    )
    return MinuteBarWindowCacheConfig(
        window_size=safe_int(window_size, default=1, min_value=1),
        incremental_fetch_bars=incremental_fetch_bars,
        freshness_max_lag_sec=freshness_max_lag_sec,
        gap_refetch_sec=gap_refetch_sec,
        expected_bar_interval_sec=expected_bar_interval_sec,
        max_contiguity_gap_sec=max_contiguity_gap_sec,
        force_cold_on_session_date_change=safe_bool(
            cfg["force_cold_on_session_date_change"],
            default=True,
        ),
        parallel_fetch_workers=safe_int(
            parallel_fetch_workers,
            default=1,
            min_value=1,
        ),
        batch_fetch_budget_sec=safe_float(
            cfg.get("batch_fetch_budget_sec", 45.0),
            default=45.0,
            min_value=0.1,
        ),
    )


class MinuteBarWindowCache:
    """Cache recent per-ticker minute-bar windows and fetch only incremental tails."""

    def __init__(
        self,
        client: MinuteBarClient,
        config: MinuteBarWindowCacheConfig,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self.config = config
        self._now = now_fn or (lambda: datetime.now(_KST))
        self._windows: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self._batch_lock = threading.Lock()

    def clear(self, ticker: str | None = None) -> None:
        with self._lock:
            if ticker is None:
                self._windows.clear()
                return
            self._windows.pop(pad_ticker(str(ticker)), None)

    def get_windows(
        self,
        tickers: Sequence[str],
        *,
        asof: str | datetime,
        min_bars: int | None = None,
    ) -> MinuteBarWindowResult:
        asof_ts = self._parse_ts(asof)
        if asof_ts is None:
            metadata = self._base_metadata(asof=str(asof or ""))
            metadata["failed_tickers"] = {"*": "invalid_asof"}
            return MinuteBarWindowResult(
                status="FAIL",
                reason="invalid_asof",
                asof=str(asof or ""),
                windows={},
                metadata=metadata,
            )

        requested = self._normalize_tickers(tickers)
        metadata = self._base_metadata(asof=asof_ts.isoformat())
        windows: dict[str, list[dict[str, Any]]] = {}
        failed: dict[str, str] = {}

        with self._batch_lock:
            ticker_results = self._fetch_update_windows(
                requested,
                asof_ts=asof_ts,
                min_bars=min_bars,
            )
        for ticker in requested:
            ticker_window, ticker_meta, reason = ticker_results.get(
                ticker,
                ([], {"reason": "not_evaluated"}, "not_evaluated"),
            )
            metadata["tickers"][ticker] = ticker_meta
            if reason != "ok":
                failed[ticker] = reason
                continue
            windows[ticker] = ticker_window

        metadata["failed_tickers"] = failed
        if not requested:
            status: Literal["PASS", "PARTIAL", "FAIL"] = "FAIL"
            reason = "no_tickers"
        elif not failed:
            status = "PASS"
            reason = "ok"
        elif windows:
            status = "PARTIAL"
            reason = "partial_minute_bar_window"
        else:
            status = "FAIL"
            reason = "minute_bar_window_unavailable"
        metadata["status"] = status
        metadata["reason"] = reason
        return MinuteBarWindowResult(
            status=status,
            reason=reason,
            asof=asof_ts.isoformat(),
            windows=windows,
            metadata=metadata,
        )

    def _fetch_update_windows(
        self,
        tickers: list[str],
        *,
        asof_ts: datetime,
        min_bars: int | None,
    ) -> dict[str, tuple[list[dict[str, Any]], dict[str, Any], str]]:
        if not tickers:
            return {}
        workers = max(1, min(int(self.config.parallel_fetch_workers), len(tickers)))
        if workers == 1:
            return {
                ticker: self._fetch_update_window(
                    ticker=ticker,
                    asof_ts=asof_ts,
                    min_bars=min_bars,
                )
                for ticker in tickers
            }

        results: dict[str, tuple[list[dict[str, Any]], dict[str, Any], str]] = {}
        budget_sec = float(self.config.batch_fetch_budget_sec)
        deadline_monotonic = time.monotonic() + budget_sec
        pool = ThreadPoolExecutor(max_workers=workers)
        future_to_ticker = {
            pool.submit(
                self._fetch_update_window,
                ticker=ticker,
                asof_ts=asof_ts,
                min_bars=min_bars,
                deadline_monotonic=deadline_monotonic,
            ): ticker
            for ticker in tickers
        }
        pending = set(future_to_ticker)
        try:
            for future in as_completed(future_to_ticker, timeout=budget_sec):
                pending.discard(future)
                ticker = future_to_ticker[future]
                try:
                    results[ticker] = future.result()
                except Exception as e:
                    results[ticker] = (
                        [],
                        self._failure_meta(
                            reason="fetch_error",
                            error=str(e),
                        ),
                        "fetch_error",
                    )
        except TimeoutError:
            pass
        finally:
            for future in pending:
                ticker = future_to_ticker[future]
                future.cancel()
                results[ticker] = (
                    [],
                    self._failure_meta(
                        reason="fetch_timeout",
                        timeout_sec=budget_sec,
                    ),
                    "fetch_timeout",
                )
            pool.shutdown(wait=False, cancel_futures=True)
        return results

    def _fetch_update_window(
        self,
        *,
        ticker: str,
        asof_ts: datetime,
        min_bars: int | None,
        deadline_monotonic: float | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
        with self._lock:
            cached = list(self._windows.get(ticker, []))
        cached_latest = self._latest_valid_ts(cached)
        fetch_policy = self._fetch_policy(cached, cached_latest, asof_ts)
        fetch_n = (
            self._cold_fetch_bars()
            if fetch_policy != "incremental"
            else self.config.incremental_fetch_bars
        )
        meta: dict[str, Any] = {
            "fetch_policy": fetch_policy,
            "initial_fetch_policy": fetch_policy,
            "effective_fetch_policy": fetch_policy,
            "fetch_n": int(fetch_n),
            "cached_rows_before": len(cached),
            "fetched_rows": 0,
            "accepted_rows": 0,
            "returned_rows": 0,
            "latest_ts": None,
            "latest_age_sec": None,
            "freshness_status": "UNKNOWN",
            "future_bar_filtered_count": 0,
            "future_bar_filtered_rows": [],
            "invalid_rows": [],
            "duplicate_replaced_count": 0,
            "insufficient_bars": 0,
            "contiguity_status": "UNKNOWN",
            "contiguity_gap_count": 0,
            "contiguity_gaps": [],
            "cold_retry_after_hole": False,
            "cold_retry_attempted": False,
            "reason": "ok",
        }

        try:
            fetched = self._client.inquire_minute_bar(ticker, n_bars=fetch_n)
        except Exception as e:
            meta["reason"] = "fetch_error"
            meta["error"] = str(e)
            return [], meta, "fetch_error"
        if self._deadline_expired(deadline_monotonic):
            meta["reason"] = "fetch_timeout"
            meta["timeout_sec"] = float(self.config.batch_fetch_budget_sec)
            return [], meta, "fetch_timeout"
        fetched = fetched if isinstance(fetched, list) else []
        meta["fetched_rows"] = len(fetched)

        accepted, filter_meta = self._pit_clean_rows(ticker, fetched, asof_ts)
        meta.update(filter_meta)
        meta["accepted_rows"] = len(accepted)

        merge_input = accepted if fetch_policy != "incremental" else [*cached, *accepted]
        merged, duplicate_replaced_count = self._dedupe_sort_cap(merge_input)
        meta["duplicate_replaced_count"] = duplicate_replaced_count

        self._update_window_health_metadata(merged, asof_ts, meta)
        reason = self._window_failure_reason(merged, meta, min_bars)
        if reason == "non_contiguous_window" and fetch_policy == "incremental":
            meta["cold_retry_after_hole"] = True
            meta["cold_retry_attempted"] = True
            meta["initial_contiguity_gaps"] = list(meta.get("contiguity_gaps", []))
            retry_reason = self._cold_retry_after_hole(
                ticker=ticker,
                asof_ts=asof_ts,
                min_bars=min_bars,
                meta=meta,
                deadline_monotonic=deadline_monotonic,
            )
            with self._lock:
                merged = list(self._windows.get(ticker, []))
            reason = retry_reason
        elif reason == "ok":
            if self._deadline_expired(deadline_monotonic):
                meta["reason"] = "fetch_timeout"
                meta["timeout_sec"] = float(self.config.batch_fetch_budget_sec)
                return [], meta, "fetch_timeout"
            with self._lock:
                self._windows[ticker] = merged
        meta["reason"] = reason
        meta["returned_rows"] = len(merged) if reason == "ok" else 0
        if reason != "ok":
            return [], meta, reason
        return list(merged), meta, "ok"

    def _fetch_policy(
        self,
        cached: list[dict[str, Any]],
        cached_latest: datetime | None,
        asof_ts: datetime,
    ) -> str:
        if len(cached) < self.config.window_size or cached_latest is None:
            return "cold"
        if (
            self.config.force_cold_on_session_date_change
            and cached_latest.date() != asof_ts.date()
        ):
            return "cold_session_boundary"
        if cached_latest > asof_ts:
            return "cold_asof_rollback"
        if (asof_ts - cached_latest) > timedelta(seconds=self.config.gap_refetch_sec):
            return "cold_gap_refetch"
        return "incremental"

    def _cold_fetch_bars(self) -> int:
        # KIS can include the currently forming minute in the latest page. Fetch
        # one extra bar so PIT filtering can drop that row and still leave a full
        # scoring window.
        return int(self.config.window_size) + 1

    def _window_failure_reason(
        self,
        window: list[dict[str, Any]],
        meta: dict[str, Any],
        min_bars: int | None,
    ) -> str:
        if meta.get("invalid_rows"):
            return "invalid_ts"
        if not window:
            return "no_valid_bars"
        if meta.get("freshness_status") != "PASS":
            return "stale_latest_bar"
        if meta.get("contiguity_status") != "PASS":
            return "non_contiguous_window"
        if min_bars is not None and len(window) < int(min_bars):
            meta["insufficient_bars"] = int(min_bars) - len(window)
            return "insufficient_bars"
        return "ok"

    def _pit_clean_rows(
        self,
        ticker: str,
        rows: list[Any],
        asof_ts: datetime,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        accepted: list[dict[str, Any]] = []
        future_rows: list[dict[str, str]] = []
        invalid_rows: list[dict[str, str]] = []
        forming_rows: list[dict[str, str]] = []
        forming_cutoff = asof_ts.replace(second=0, microsecond=0)
        filter_forming_minute = (
            asof_ts.second > 0 or asof_ts.microsecond > 0
        )
        for row in rows:
            if not isinstance(row, dict):
                invalid_rows.append({"ticker": ticker, "ts_close": "non_dict_bar"})
                continue
            normalized = dict(row)
            normalized["ticker"] = pad_ticker(str(normalized.get("ticker") or ticker))
            raw_ts = str(normalized.get("ts_close") or normalized.get("ts") or "").strip()
            normalized["ts_close"] = raw_ts
            parsed = self._parse_ts(raw_ts)
            if parsed is None:
                invalid_rows.append({"ticker": ticker, "ts_close": raw_ts})
                continue
            if parsed > asof_ts:
                future_rows.append({"ticker": ticker, "ts_close": raw_ts})
                continue
            if (
                filter_forming_minute
                and parsed.date() == asof_ts.date()
                and parsed >= forming_cutoff
            ):
                forming_rows.append({"ticker": ticker, "ts_close": raw_ts})
                continue
            accepted.append(normalized)
        return accepted, {
            "future_bar_filtered_count": len(future_rows),
            "future_bar_filtered_rows": future_rows[:10],
            "forming_bar_filtered_count": len(forming_rows),
            "forming_bar_filtered_rows": forming_rows[:10],
            "invalid_rows": invalid_rows[:10],
        }

    def _dedupe_sort_cap(
        self,
        rows: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        by_key: dict[tuple[str, str], dict[str, Any]] = {}
        duplicate_replaced_count = 0
        for row in rows:
            ticker = pad_ticker(str(row.get("ticker") or ""))
            ts_close = str(row.get("ts_close") or "")
            if ticker == "000000" or not ts_close or self._parse_ts(ts_close) is None:
                continue
            normalized = dict(row)
            normalized["ticker"] = ticker
            key = (ticker, ts_close)
            if key in by_key:
                duplicate_replaced_count += 1
            by_key[key] = normalized
        ordered = sorted(
            by_key.values(),
            key=lambda item: self._parse_ts(item.get("ts_close")) or datetime.min.replace(tzinfo=_KST),
        )
        capped = ordered[-self.config.window_size :]
        self._recompute_change(capped)
        return capped, duplicate_replaced_count

    def _cold_retry_after_hole(
        self,
        *,
        ticker: str,
        asof_ts: datetime,
        min_bars: int | None,
        meta: dict[str, Any],
        deadline_monotonic: float | None,
    ) -> str:
        meta["fetch_policy"] = "cold_retry_after_hole"
        meta["effective_fetch_policy"] = "cold_retry_after_hole"
        meta["fetch_n"] = int(self._cold_fetch_bars())
        if self._deadline_expired(deadline_monotonic):
            meta["reason"] = "fetch_timeout"
            meta["timeout_sec"] = float(self.config.batch_fetch_budget_sec)
            return "fetch_timeout"
        try:
            fetched = self._client.inquire_minute_bar(
                ticker,
                n_bars=self._cold_fetch_bars(),
            )
        except Exception as e:
            meta["reason"] = "fetch_error"
            meta["error"] = str(e)
            with self._lock:
                self._windows[ticker] = []
            return "fetch_error"
        if self._deadline_expired(deadline_monotonic):
            meta["reason"] = "fetch_timeout"
            meta["timeout_sec"] = float(self.config.batch_fetch_budget_sec)
            return "fetch_timeout"
        fetched = fetched if isinstance(fetched, list) else []
        meta["fetched_rows"] = len(fetched)
        accepted, filter_meta = self._pit_clean_rows(ticker, fetched, asof_ts)
        meta.update(filter_meta)
        meta["accepted_rows"] = len(accepted)
        merged, duplicate_replaced_count = self._dedupe_sort_cap(accepted)
        meta["duplicate_replaced_count"] = duplicate_replaced_count
        self._update_window_health_metadata(merged, asof_ts, meta)
        reason = self._window_failure_reason(merged, meta, min_bars)
        if reason == "ok":
            if self._deadline_expired(deadline_monotonic):
                meta["reason"] = "fetch_timeout"
                meta["timeout_sec"] = float(self.config.batch_fetch_budget_sec)
                return "fetch_timeout"
            with self._lock:
                self._windows[ticker] = merged
        return reason

    @staticmethod
    def _deadline_expired(deadline_monotonic: float | None) -> bool:
        return deadline_monotonic is not None and time.monotonic() >= deadline_monotonic

    def _failure_meta(
        self,
        *,
        reason: str,
        error: str | None = None,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "reason": reason,
            "fetch_policy": "unknown",
            "effective_fetch_policy": "unknown",
            "fetch_n": 0,
            "cached_rows_before": 0,
            "fetched_rows": 0,
            "accepted_rows": 0,
            "returned_rows": 0,
        }
        if error is not None:
            meta["error"] = error
        if timeout_sec is not None:
            meta["timeout_sec"] = float(timeout_sec)
        return meta

    def _update_window_health_metadata(
        self,
        window: list[dict[str, Any]],
        asof_ts: datetime,
        meta: dict[str, Any],
    ) -> None:
        latest_ts = self._latest_valid_ts(window)
        if latest_ts is not None:
            latest_age_sec = max(0.0, (asof_ts - latest_ts).total_seconds())
            meta["latest_ts"] = latest_ts.isoformat()
            meta["latest_age_sec"] = latest_age_sec
            meta["freshness_status"] = (
                "PASS"
                if latest_age_sec <= self.config.freshness_max_lag_sec
                else "FAIL"
            )
        else:
            meta["latest_ts"] = None
            meta["latest_age_sec"] = None
            meta["freshness_status"] = "FAIL"

        gaps = self._contiguity_gaps(window)
        meta["contiguity_gap_count"] = len(gaps)
        meta["contiguity_gaps"] = gaps[:10]
        meta["contiguity_status"] = "PASS" if not gaps else "FAIL"

    def _contiguity_gaps(self, window: list[dict[str, Any]]) -> list[dict[str, Any]]:
        parsed = [
            ts
            for ts in (self._parse_ts(row.get("ts_close")) for row in window)
            if ts is not None
        ]
        parsed = sorted(set(parsed))
        gaps: list[dict[str, Any]] = []
        for prev_ts, next_ts in zip(parsed, parsed[1:]):
            gap_sec = (next_ts - prev_ts).total_seconds()
            if gap_sec <= self.config.max_contiguity_gap_sec:
                continue
            gaps.append({
                "prev_ts": prev_ts.isoformat(),
                "next_ts": next_ts.isoformat(),
                "gap_sec": gap_sec,
                "missing_bars": max(
                    0,
                    int(gap_sec // self.config.expected_bar_interval_sec) - 1,
                ),
                "expected_bar_interval_sec": int(self.config.expected_bar_interval_sec),
                "max_contiguity_gap_sec": int(self.config.max_contiguity_gap_sec),
            })
        return gaps

    @staticmethod
    def _recompute_change(rows: list[dict[str, Any]]) -> None:
        prev_close: float | None = None
        for row in rows:
            close = safe_float(row.get("close"), default=0.0)
            row["change"] = 0.0 if prev_close is None else float(close - prev_close)
            prev_close = close

    def _latest_valid_ts(self, rows: list[dict[str, Any]]) -> datetime | None:
        parsed = [
            ts
            for ts in (self._parse_ts(row.get("ts_close")) for row in rows)
            if ts is not None
        ]
        return max(parsed) if parsed else None

    def _base_metadata(self, *, asof: str) -> dict[str, Any]:
        return {
            "status": "FAIL",
            "reason": "not_evaluated",
            "asof": asof,
            "window_size": int(self.config.window_size),
            "cold_fetch_bars": int(self._cold_fetch_bars()),
            "incremental_fetch_bars": int(self.config.incremental_fetch_bars),
            "freshness_max_lag_sec": int(self.config.freshness_max_lag_sec),
            "gap_refetch_sec": int(self.config.gap_refetch_sec),
            "expected_bar_interval_sec": int(self.config.expected_bar_interval_sec),
            "max_contiguity_gap_sec": int(self.config.max_contiguity_gap_sec),
            "parallel_fetch_workers": int(self.config.parallel_fetch_workers),
            "batch_fetch_budget_sec": float(self.config.batch_fetch_budget_sec),
            "force_cold_on_session_date_change": bool(
                self.config.force_cold_on_session_date_change
            ),
            "tickers": {},
            "failed_tickers": {},
        }

    @staticmethod
    def _normalize_tickers(tickers: Sequence[str]) -> list[str]:
        normalized: list[str] = []
        for raw in tickers:
            ticker = pad_ticker(str(raw))
            if ticker == "000000" or ticker in normalized:
                continue
            normalized.append(ticker)
        return normalized

    @staticmethod
    def _parse_ts(raw: Any) -> datetime | None:
        if raw is None:
            return None
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        if ts.tzinfo is None:
            return ts.replace(tzinfo=_KST)
        return ts.astimezone(_KST)

"""S1-0 Batch B WalkForwardSplitter unit tests."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.models.splitter import WalkForwardSplitter


def _make_panel(n_days: int, n_tickers: int = 4, bars_per_day: int = 390):
    """합성 panel: n_days × n_tickers × bars_per_day. MultiIndex (ticker, ts_close)."""
    rows = []
    base_date = pd.Timestamp("2026-01-01 09:00:00+09:00")
    for d in range(n_days):
        day_start = base_date + pd.Timedelta(days=d)
        for b in range(bars_per_day):
            ts = day_start + pd.Timedelta(minutes=b)
            for t in range(n_tickers):
                ticker = f"00000{t}"
                rows.append({
                    "ticker": ticker,
                    "ts_close": ts,
                    "close": 100.0 + float(b),
                    "feat_x": float(b),
                })
    df = pd.DataFrame(rows)
    df = df.sort_values(["ts_close", "ticker"]).reset_index(drop=True)
    return df.set_index(["ticker", "ts_close"])


def test_init_loads_config() -> None:
    s = WalkForwardSplitter()
    assert s.n_splits == 8
    assert s.train_window_days == 60
    assert s.test_window_days == 20
    assert s.step_days == 20
    assert s.trading_minutes == 390
    assert s.purge_bars == 60
    assert s.embargo_bars == 78


def test_split_insufficient_days() -> None:
    panel = _make_panel(n_days=50, n_tickers=2, bars_per_day=10)
    # 요구: train 60 + test 20 = 80일. 실제 50일 → 0 fold.
    s = WalkForwardSplitter()
    folds = list(s.split(panel))
    assert folds == []


def test_split_one_fold_small_values() -> None:
    panel = _make_panel(n_days=4, n_tickers=2, bars_per_day=10)

    s = WalkForwardSplitter()
    # config 덮어쓰기로 테스트용 값 조정
    s.train_window_days = 3
    s.test_window_days = 1
    s.step_days = 1
    s.n_splits = 1
    s.purge_bars = 2
    s.embargo_bars = 1

    folds = list(s.split(panel))
    assert len(folds) == 1
    train_idx, test_idx = folds[0]
    assert len(train_idx) > 0
    assert len(test_idx) > 0


def test_split_multiple_folds_no_overlap() -> None:
    panel = _make_panel(n_days=10, n_tickers=2, bars_per_day=20)
    s = WalkForwardSplitter()
    s.train_window_days = 4
    s.test_window_days = 2
    s.step_days = 2
    s.n_splits = 3
    s.purge_bars = 0
    s.embargo_bars = 0

    folds = list(s.split(panel))
    assert len(folds) == 3
    for train_idx, test_idx in folds:
        assert len(train_idx) > 0
        assert len(test_idx) > 0
        assert len(np.intersect1d(train_idx, test_idx)) == 0


def test_split_empty_panel_raises() -> None:
    s = WalkForwardSplitter()
    with pytest.raises(ValueError):
        list(s.split(pd.DataFrame()))


def test_split_purge_applied() -> None:
    panel = _make_panel(n_days=6, n_tickers=2, bars_per_day=10)
    s = WalkForwardSplitter()
    s.train_window_days = 3
    s.test_window_days = 2
    s.step_days = 2
    s.n_splits = 2
    s.purge_bars = 5
    s.embargo_bars = 0

    folds = list(s.split(panel))
    assert len(folds) >= 1
    train_idx, _ = folds[0]

    # 원래 train 사이즈 3*10*2 = 60 rows. purge 5 ts → 25 ts × 2 = 50 rows.
    assert len(train_idx) < 60


def test_split_embargo_applied() -> None:
    panel = _make_panel(n_days=6, n_tickers=2, bars_per_day=10)
    s = WalkForwardSplitter()
    s.train_window_days = 3
    s.test_window_days = 2
    s.step_days = 2
    s.n_splits = 2
    s.purge_bars = 0
    s.embargo_bars = 5

    folds = list(s.split(panel))
    assert len(folds) >= 1
    _, test_idx = folds[0]

    # 원래 test 2*10*2 = 40 rows. embargo 5 ts → 15 ts × 2 = 30 rows.
    assert len(test_idx) < 40


def test_split_skips_when_purge_or_embargo_consumes_entire_window(caplog) -> None:
    panel = _make_panel(n_days=2, n_tickers=1, bars_per_day=2)
    s = WalkForwardSplitter()
    s.train_window_days = 1
    s.test_window_days = 1
    s.step_days = 1
    s.n_splits = 1
    s.purge_bars = 2
    s.embargo_bars = 2

    with caplog.at_level("WARNING", logger="splitter"):
        folds = list(s.split(panel))

    assert folds == []
    assert "purge/embargo 후 empty. skip." in caplog.text

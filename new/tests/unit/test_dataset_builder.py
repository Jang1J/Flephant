"""S1-0 Batch B DatasetBuilder unit tests.

검증 항목:
  1. 초기화 + risk_config 로드
  2. 날짜 파싱 helper (_parse_yyyymmdd, _extract_file_date)
  3. rolling feature 계산 (_rolling_trend, _rolling_robust_z)
  4. label 생성 (_generate_labels): horizon=5 drop 규칙
  5. cross-sectional rank + relevance grade
  6. _to_relevance grade 분할
  7. _load_ticker_bars (jsonl/parquet 양쪽)
  8. build_training_frame end-to-end
  9. PIT-Safety guard (NaN/inf 감지)
 10. 빈 ticker 디렉토리 skip
 11. 그룹 크기 부족 시 drop

pandas/pyarrow 환경 전제. init.sh에서 버전 확인됨.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.data.dataset_builder import (
    DatasetBuildError,
    DatasetBuilder,
    LabelLeakageError,
    _extract_file_date,
    _parse_yyyymmdd,
)


# ====================================================================== #
# fixtures
# ====================================================================== #


@pytest.fixture
def builder(tmp_path: Path) -> DatasetBuilder:
    return DatasetBuilder(artifacts_dir=tmp_path)


def _write_jsonl_day(
    base_dir: Path,
    ticker: str,
    yyyymmdd: str,
    n_bars: int = 390,
    start_price: float = 50000.0,
    seed: int = 0,
) -> Path:
    """가짜 1분봉 jsonl 파일 생성. 현실적 long-term trend 포함."""
    rng = np.random.default_rng(seed)
    ticker_dir = base_dir / ticker
    ticker_dir.mkdir(parents=True, exist_ok=True)
    out_path = ticker_dir / f"bars_1m_{yyyymmdd}.jsonl"

    price = start_price
    # 09:00 ~ 15:30 = 390분. ISO 8601 KST.
    records = []
    for i in range(n_bars):
        delta = rng.normal(0, 50)
        open_p = price
        close_p = max(1.0, price + delta)
        high_p = max(open_p, close_p) + abs(rng.normal(0, 30))
        low_p = max(1.0, min(open_p, close_p) - abs(rng.normal(0, 30)))
        volume = max(1, int(rng.integers(500, 10000)))
        hour = 9 + (i // 60)
        minute = i % 60
        ts = f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}T{hour:02d}:{minute:02d}:00+09:00"
        records.append(
            {
                "ticker": ticker,
                "ts_close": ts,
                "open": round(open_p, 2),
                "high": round(high_p, 2),
                "low": round(low_p, 2),
                "close": round(close_p, 2),
                "volume": volume,
            }
        )
        price = close_p

    with out_path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return out_path


def _write_parquet_day(
    base_dir: Path,
    ticker: str,
    yyyymmdd: str,
    n_bars: int = 120,
    start_price: float = 60000.0,
    seed: int = 1,
) -> Path:
    """parquet fixture용. JSONL과 동일한 스키마."""
    rng = np.random.default_rng(seed)
    ticker_dir = base_dir / ticker
    ticker_dir.mkdir(parents=True, exist_ok=True)
    out_path = ticker_dir / f"bars_1m_{yyyymmdd}.parquet"

    price = start_price
    rows = []
    for i in range(n_bars):
        close_p = max(1.0, price + rng.normal(0, 40))
        hour = 9 + (i // 60)
        minute = i % 60
        ts = f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}T{hour:02d}:{minute:02d}:00+09:00"
        rows.append(
            {
                "ticker": ticker,
                "ts_close": ts,
                "open": price,
                "high": max(price, close_p) + 10.0,
                "low": min(price, close_p) - 10.0,
                "close": close_p,
                "volume": 1000.0 + float(i),
            }
        )
        price = close_p
    pd.DataFrame(rows).to_parquet(out_path, index=False)
    return out_path


# ====================================================================== #
# 1. 초기화
# ====================================================================== #


def test_init_loads_config(builder: DatasetBuilder) -> None:
    # Public property 경유 (Part C-2 캡슐화 복원)
    assert builder.horizon_bars == 5
    assert builder.target_col == "label_5m_ret"
    assert builder.multi_scale_windows == [1, 5, 30, 60]
    # 내부 상태 검증 (테스트 전용, private OK)
    assert builder._rank_col == "cs_rank"
    assert builder._leakage_guard is True
    assert builder._drop_last_n == 5
    assert builder._n_relevance_grades == 4
    assert 1.0 < builder._mad_constant < 2.0
    assert builder._outlier_cap_z > 0


# ====================================================================== #
# 2. 날짜 파싱 helper
# ====================================================================== #


def test_parse_yyyymmdd() -> None:
    assert _parse_yyyymmdd("20260420") == date(2026, 4, 20)


def test_parse_yyyymmdd_invalid() -> None:
    with pytest.raises(ValueError):
        _parse_yyyymmdd("2026-04-20")


def test_extract_file_date_parquet() -> None:
    p = Path("/tmp/005930/bars_1m_20260420.parquet")
    assert _extract_file_date(p) == date(2026, 4, 20)


def test_extract_file_date_jsonl() -> None:
    p = Path("/tmp/005930/bars_1m_20260420.jsonl")
    assert _extract_file_date(p) == date(2026, 4, 20)


def test_extract_file_date_invalid_name() -> None:
    p = Path("/tmp/005930/garbage_file.parquet")
    assert _extract_file_date(p) is None


# ====================================================================== #
# 3. rolling feature 계산
# ====================================================================== #


def test_rolling_trend_increasing(builder: DatasetBuilder) -> None:
    closes = np.linspace(100, 200, 60)
    trend = builder._rolling_trend(closes, window=60)
    # 마지막 값은 slope>0 → trend>0
    assert not np.isnan(trend[-1])
    assert trend[-1] > 0


def test_rolling_trend_insufficient(builder: DatasetBuilder) -> None:
    closes = np.array([100.0, 101.0, 102.0])
    trend = builder._rolling_trend(closes, window=60)
    assert all(np.isnan(trend))


def test_rolling_robust_z_constant(builder: DatasetBuilder) -> None:
    closes = np.ones(70) * 100.0
    z = builder._rolling_robust_z(closes, window=60)
    # 모든 값이 같으면 MAD=0, denom epsilon fallback. z=0에 가까움.
    valid = z[~np.isnan(z)]
    assert len(valid) > 0
    assert np.all(np.abs(valid) < 1e-3)


def test_rolling_robust_z_outlier_cap(builder: DatasetBuilder) -> None:
    closes = np.concatenate([np.ones(60) * 100.0, [1e9]])
    z = builder._rolling_robust_z(closes, window=60)
    # 마지막 값은 극단 outlier → clip 됨
    assert z[-1] == pytest.approx(builder._outlier_cap_z, rel=1e-6)


# ====================================================================== #
# 4. label 생성 (horizon=5)
# ====================================================================== #


def test_generate_labels_drops_last_n(builder: DatasetBuilder) -> None:
    df = pd.DataFrame(
        {
            "ticker": ["005930"] * 20,
            "ts_close": pd.date_range("2026-04-20 09:00:00+09:00", periods=20, freq="1min"),
            "open": np.arange(100.0, 120.0),
            "high": np.arange(100.0, 120.0) + 1,
            "low": np.arange(100.0, 120.0) - 1,
            "close": np.arange(100.0, 120.0),
            "volume": np.ones(20) * 1000,
        }
    )
    out = builder._generate_labels(df)
    # drop_last_n_bars = 5이므로 20 - 5 = 15 행
    assert len(out) == 15
    # label 없는 행 없음
    assert not out["label_5m_ret"].isna().any()
    # label = close_{t+5} / close_t - 1, 예: close=100, close_5=105 → 0.05
    assert out["label_5m_ret"].iloc[0] == pytest.approx(5.0 / 100.0)


def test_generate_labels_too_short(builder: DatasetBuilder) -> None:
    df = pd.DataFrame(
        {
            "ticker": ["005930"] * 3,
            "ts_close": pd.date_range("2026-04-20 09:00:00+09:00", periods=3, freq="1min"),
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [100.0, 101.0, 102.0],
            "volume": [1000.0, 1000.0, 1000.0],
        }
    )
    out = builder._generate_labels(df)
    # n=3 < horizon=5 → 전부 drop 후 empty
    assert out.empty


# ====================================================================== #
# 5. cross-sectional rank + relevance
# ====================================================================== #


def test_to_relevance_ranges(builder: DatasetBuilder) -> None:
    s = pd.Series([0.0, 0.24, 0.25, 0.5, 0.75, 1.0], name="cs_rank")
    grades = builder._to_relevance(s, n_grades=4)
    # 경계 포함 0/1/2/3
    vals = grades.to_numpy()
    assert int(vals[0]) == 0
    assert int(vals[-1]) == 3
    assert set(np.unique(vals[~np.isnan(vals)]).astype(int)).issubset({0, 1, 2, 3})


def test_cross_sectional_rank_basic(builder: DatasetBuilder) -> None:
    # 4 종목 × 1 timestamp. label 다양하게.
    df = pd.DataFrame(
        {
            "ticker": ["000001", "000002", "000003", "000004"],
            "ts_close": [pd.Timestamp("2026-04-20 10:00:00+09:00")] * 4,
            "open": [100.0] * 4,
            "high": [101.0] * 4,
            "low": [99.0] * 4,
            "close": [100.0] * 4,
            "volume": [1000.0] * 4,
            "label_5m_ret": [0.01, 0.03, 0.02, 0.04],
        }
    )
    out = builder._cross_sectional_rank(df)
    assert not out.empty
    # relevance 0~3 분포 확인
    rel = out["relevance"].to_numpy()
    assert sorted(rel.astype(int).tolist()) == [0, 1, 2, 3]


def test_cross_sectional_group_too_small(builder: DatasetBuilder) -> None:
    # 3 종목만 (n_grades=4 미만) → 전부 drop
    df = pd.DataFrame(
        {
            "ticker": ["000001", "000002", "000003"],
            "ts_close": [pd.Timestamp("2026-04-20 10:00:00+09:00")] * 3,
            "open": [100.0] * 3,
            "high": [101.0] * 3,
            "low": [99.0] * 3,
            "close": [100.0] * 3,
            "volume": [1000.0] * 3,
            "label_5m_ret": [0.01, 0.02, 0.03],
        }
    )
    out = builder._cross_sectional_rank(df)
    assert out.empty


# ====================================================================== #
# 6. _load_ticker_bars (JSONL + parquet)
# ====================================================================== #


def test_load_ticker_bars_jsonl(tmp_path: Path) -> None:
    _write_jsonl_day(tmp_path, "005930", "20260420", n_bars=100, seed=42)
    b = DatasetBuilder(artifacts_dir=tmp_path)
    df = b._load_ticker_bars("005930", "20260420", "20260420")
    assert df is not None
    assert len(df) == 100
    assert df["ticker"].iloc[0] == "005930"
    # ts_close tz-aware KST
    assert str(df["ts_close"].dt.tz).startswith("Asia/Seoul") or str(
        df["ts_close"].dt.tz
    ) == "Asia/Seoul"


def test_load_ticker_bars_parquet(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow", reason="parquet I/O는 pyarrow 필요")
    _write_parquet_day(tmp_path, "000660", "20260420", n_bars=80, seed=7)
    b = DatasetBuilder(artifacts_dir=tmp_path)
    df = b._load_ticker_bars("000660", "20260420", "20260420")
    assert df is not None
    assert len(df) == 80


def test_load_ticker_bars_date_range_filter(tmp_path: Path) -> None:
    _write_jsonl_day(tmp_path, "005930", "20260418", n_bars=50, seed=1)
    _write_jsonl_day(tmp_path, "005930", "20260419", n_bars=50, seed=2)
    _write_jsonl_day(tmp_path, "005930", "20260420", n_bars=50, seed=3)
    b = DatasetBuilder(artifacts_dir=tmp_path)
    # 중간 1일만 요청
    df = b._load_ticker_bars("005930", "20260419", "20260419")
    assert df is not None
    assert len(df) == 50


def test_load_ticker_bars_missing_dir(tmp_path: Path) -> None:
    b = DatasetBuilder(artifacts_dir=tmp_path)
    assert b._load_ticker_bars("999999", "20260420", "20260420") is None


# ====================================================================== #
# 7. build_training_frame end-to-end
# ====================================================================== #


def test_build_training_frame_happy_path(tmp_path: Path) -> None:
    # 4 종목 × 2일, 각 120 bars
    for i, tk in enumerate(["005930", "000660", "035420", "051910"]):
        for j, d in enumerate(["20260418", "20260419"]):
            _write_jsonl_day(tmp_path, tk, d, n_bars=120, start_price=50000 + i * 1000, seed=i * 10 + j)

    b = DatasetBuilder(artifacts_dir=tmp_path)
    panel = b.build_training_frame(
        tickers=["005930", "000660", "035420", "051910"],
        start_date="20260418",
        end_date="20260419",
    )

    # MultiIndex (ticker, ts_close)
    assert panel.index.names == ["ticker", "ts_close"]
    # 기본 컬럼 전부 있음
    for col in (
        "open", "high", "low", "close", "volume",
        "feat_1m_close_robust_z", "feat_5m_ret", "feat_30m_vol", "feat_60m_trend",
        "label_5m_ret", "cs_rank", "relevance",
    ):
        assert col in panel.columns, f"missing column: {col}"

    # label NaN 없음 (drop + leakage guard)
    assert not panel["label_5m_ret"].isna().any()

    # relevance는 정수형 grade (0~3 float)
    unique_rel = set(panel["relevance"].astype(int).unique().tolist())
    assert unique_rel.issubset({0, 1, 2, 3})


def test_build_training_frame_no_data_raises(tmp_path: Path) -> None:
    b = DatasetBuilder(artifacts_dir=tmp_path)
    with pytest.raises(DatasetBuildError):
        b.build_training_frame(
            tickers=["005930", "000660"],
            start_date="20260418",
            end_date="20260419",
        )


# ====================================================================== #
# 8. PIT-Safety guard
# ====================================================================== #


def test_leakage_guard_nan_raises(builder: DatasetBuilder) -> None:
    panel = pd.DataFrame(
        {
            "label_5m_ret": [0.01, np.nan, 0.03, 0.04],
            "cs_rank": [0.25, 0.5, 0.75, 1.0],
            "relevance": [0, 1, 2, 3],
        },
        index=pd.MultiIndex.from_tuples(
            [
                ("005930", pd.Timestamp("2026-04-20 10:00+09:00")),
                ("005930", pd.Timestamp("2026-04-20 10:01+09:00")),
                ("005930", pd.Timestamp("2026-04-20 10:02+09:00")),
                ("005930", pd.Timestamp("2026-04-20 10:03+09:00")),
            ],
            names=["ticker", "ts_close"],
        ),
    )
    with pytest.raises(LabelLeakageError):
        builder._assert_no_leakage(panel)


def test_leakage_guard_inf_raises(builder: DatasetBuilder) -> None:
    panel = pd.DataFrame(
        {
            "label_5m_ret": [0.01, np.inf, 0.03, 0.04],
            "cs_rank": [0.25, 0.5, 0.75, 1.0],
            "relevance": [0, 1, 2, 3],
        },
        index=pd.MultiIndex.from_tuples(
            [
                ("005930", pd.Timestamp("2026-04-20 10:00+09:00")),
                ("005930", pd.Timestamp("2026-04-20 10:01+09:00")),
                ("005930", pd.Timestamp("2026-04-20 10:02+09:00")),
                ("005930", pd.Timestamp("2026-04-20 10:03+09:00")),
            ],
            names=["ticker", "ts_close"],
        ),
    )
    with pytest.raises(LabelLeakageError):
        builder._assert_no_leakage(panel)


def test_leakage_guard_extreme_raises(builder: DatasetBuilder) -> None:
    """label |r| > 1.0 극단값 = 100% 수익률 감지 (Part A W7 추가)."""
    panel = pd.DataFrame(
        {
            "label_5m_ret": [0.01, 2.5, 0.02, 0.03],   # 2.5 = +250%: corrupt/Mock drift
            "cs_rank": [0.25, 0.5, 0.75, 1.0],
            "relevance": [0, 1, 2, 3],
        },
        index=pd.MultiIndex.from_tuples(
            [
                ("005930", pd.Timestamp("2026-04-20 10:00+09:00")),
                ("005930", pd.Timestamp("2026-04-20 10:01+09:00")),
                ("005930", pd.Timestamp("2026-04-20 10:02+09:00")),
                ("005930", pd.Timestamp("2026-04-20 10:03+09:00")),
            ],
            names=["ticker", "ts_close"],
        ),
    )
    with pytest.raises(LabelLeakageError, match="극단값"):
        builder._assert_no_leakage(panel)


# ====================================================================== #
# R1-W4: Dual-Source join 벡터화 결과 일치 검증
# ====================================================================== #


def test_dual_source_join_vectorized_result(tmp_path: Path) -> None:
    """벡터화된 _join_dual_source_features: 점수 값이 정확히 join되는지 확인.

    - 2종목 × 1일 더미 panel 생성
    - mock load_latest_scores: 005930에만 scores 제공
    - join 후 005930 행은 news_score_t = 0.5, 000660 행은 0.0 (default)
    """
    from unittest.mock import patch

    from src.data.dataset_builder import DUAL_SOURCE_FEATURES

    b = DatasetBuilder(artifacts_dir=tmp_path)
    # 강제로 enabled_for_lgbm 활성화
    b._ds_enabled_for_lgbm = True

    # 더미 panel (MultiIndex ticker × ts_close)
    ts = pd.date_range("2026-04-20 09:00:00+09:00", periods=4, freq="1min")
    rows = {
        "open": [100.0] * 8, "high": [101.0] * 8,
        "low": [99.0] * 8, "close": [100.0] * 8,
        "volume": [1000.0] * 8,
        "label_5m_ret": [0.01] * 8, "cs_rank": [0.5] * 8, "relevance": [1.0] * 8,
    }
    idx = pd.MultiIndex.from_tuples(
        [("005930", t) for t in ts] + [("000660", t) for t in ts],
        names=["ticker", "ts_close"],
    )
    panel = pd.DataFrame(rows, index=idx)

    # mock: 005930에 news_score_t=0.5 제공, 000660 없음
    mock_scores = [
        {
            "ticker": "005930",
            "news_score_t": 0.5,
            "comm_score_t_1": 0.3,
            "comm_score_t_2": 0.1,
            "news_comm_divergence": 0.2,
            "community_noise_multiplier": 0.9,
        }
    ]

    with patch("src.data.dataset_builder.load_latest_scores", return_value=mock_scores):
        result = b._join_dual_source_features(panel, "20260420", "20260420")

    # 005930 행: news_score_t = 0.5
    sel_005930 = result.loc["005930", "news_score_t"].to_numpy()
    assert all(abs(v - 0.5) < 1e-6 for v in sel_005930), f"005930 news_score_t 불일치: {sel_005930}"

    # 000660 행: news_score_t = 0.0 (default)
    sel_000660 = result.loc["000660", "news_score_t"].to_numpy()
    assert all(abs(v) < 1e-9 for v in sel_000660), f"000660 news_score_t 불일치: {sel_000660}"


def test_dual_source_join_vectorized_no_scores(tmp_path: Path) -> None:
    """load_latest_scores가 빈 리스트 반환 시 기본값 0.0 유지."""
    from unittest.mock import patch

    b = DatasetBuilder(artifacts_dir=tmp_path)
    b._ds_enabled_for_lgbm = True

    ts = pd.date_range("2026-04-20 09:00:00+09:00", periods=2, freq="1min")
    rows = {
        "open": [100.0] * 2, "high": [101.0] * 2,
        "low": [99.0] * 2, "close": [100.0] * 2,
        "volume": [1000.0] * 2,
        "label_5m_ret": [0.01] * 2, "cs_rank": [0.5] * 2, "relevance": [1.0] * 2,
    }
    idx = pd.MultiIndex.from_tuples([("005930", t) for t in ts], names=["ticker", "ts_close"])
    panel = pd.DataFrame(rows, index=idx)

    with patch("src.data.dataset_builder.load_latest_scores", return_value=[]):
        result = b._join_dual_source_features(panel, "20260420", "20260420")

    from src.data.dataset_builder import DUAL_SOURCE_FEATURES
    for feat in DUAL_SOURCE_FEATURES:
        assert feat in result.columns
        vals = result[feat].to_numpy()
        assert all(abs(v) < 1e-9 for v in vals), f"{feat}: 0.0 기본값 불일치 {vals}"

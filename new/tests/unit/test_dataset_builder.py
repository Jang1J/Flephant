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

import src.data.dataset_builder as dataset_builder_module
from src.data.dataset_builder import (
    DatasetBuildError,
    DatasetBuilder,
    LabelLeakageError,
    _extract_file_date,
    _parse_yyyymmdd,
)
from src.data.dual_source_runner import DEFAULT_DUAL_SOURCE_ARTIFACT_DIR
from src.data.exogenous_feature_store import DEFAULT_EXOGENOUS_ARTIFACT_DIR


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


def test_feature_artifact_defaults_use_repo_root() -> None:
    """Dual-Source/exogenous feature artifacts는 root artifacts/를 SSOT로 쓴다."""
    repo_root = Path(__file__).resolve().parents[3]

    assert DEFAULT_DUAL_SOURCE_ARTIFACT_DIR == repo_root / "artifacts" / "dual_source"
    assert DEFAULT_EXOGENOUS_ARTIFACT_DIR == repo_root / "artifacts" / "exogenous"


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


def test_init_treats_string_false_feature_flags_as_false(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """label/feature 설정의 문자열 false가 학습 feature gate를 켜지 않도록 방어."""
    original_config_load = dataset_builder_module.config_load

    def _fake_config_load(file_name: str, section: str):
        cfg = original_config_load(file_name, section)
        if isinstance(cfg, dict):
            cfg = dict(cfg)
        if section == "label":
            cfg["leakage_guard"] = "false"
        elif section == "dual_source":
            cfg["enabled_for_lgbm"] = "false"
        elif section == "exogenous_features":
            cfg["enabled_for_lgbm"] = "false"
        return cfg

    monkeypatch.setattr(dataset_builder_module, "config_load", _fake_config_load)

    b = DatasetBuilder(
        artifacts_dir=tmp_path,
        allow_synthetic_fallback="false",  # type: ignore[arg-type]
    )

    assert b._allow_synthetic_fallback is False
    assert b._leakage_guard is False
    assert b._ds_enabled_for_lgbm is False
    assert b._exog_enabled_for_lgbm is False


def test_join_exogenous_features_reads_daily_artifact(
    builder: DatasetBuilder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exogenous artifact가 있으면 neutral default 대신 날짜/ticker 값을 join한다."""
    exog_dir = tmp_path / "exogenous"
    exog_dir.mkdir()
    payload = {
        "batch_date": "2026-05-08",
        "snapshot_ts": "2026-05-08T08:30:00+09:00",
        "source_stats": {
            "input_mode": "real",
            "provider_availability": {
                "us_market_real": True,
                "ecos_real": True,
                "kis_investor_real": True,
            },
            "us_market_source": "yfinance",
        },
        "features": {
            "us_sp500_change": 0.012,
            "us_vix": 18.5,
        },
        "per_ticker": {
            "005930": {
                "foreign_net_buy": 1200000.0,
            }
        },
    }
    (exog_dir / "20260508.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        dataset_builder_module,
        "DEFAULT_EXOGENOUS_ARTIFACT_DIR",
        exog_dir,
    )

    frame = pd.DataFrame(
        {
            "ticker": ["005930", "000660"],
            "ts_close": pd.to_datetime([
                "2026-05-08T09:00:00+09:00",
                "2026-05-08T09:00:00+09:00",
            ]),
            "close": [70000.0, 120000.0],
        }
    ).set_index(["ticker", "ts_close"])

    joined = builder._join_exogenous_features(frame)

    assert joined.loc[("005930", frame.index[0][1]), "us_sp500_change"] == pytest.approx(0.012)
    assert joined.loc[("000660", frame.index[1][1]), "us_vix"] == pytest.approx(18.5)
    assert joined.loc[("005930", frame.index[0][1]), "foreign_net_buy"] == pytest.approx(1200000.0)
    assert joined.loc[("000660", frame.index[1][1]), "foreign_net_buy"] == pytest.approx(0.0)
    stats = joined.attrs["exogenous_join_stats"]
    assert stats["dates_found"] == 1
    assert stats["rows_non_neutral"] == 2


def test_join_exogenous_features_rejects_rehearsal_artifact(
    builder: DatasetBuilder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """deploy-quality feature join은 rehearsal exogenous artifact를 거부한다."""
    exog_dir = tmp_path / "exogenous"
    exog_dir.mkdir()
    payload = {
        "batch_date": "2026-05-08",
        "snapshot_ts": "2026-05-08T08:30:00+09:00",
        "source_stats": {"input_mode": "real", "neutral_rehearsal_file": True},
        "features": {"us_sp500_change": 0.012},
    }
    (exog_dir / "20260508.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        dataset_builder_module,
        "DEFAULT_EXOGENOUS_ARTIFACT_DIR",
        exog_dir,
    )

    frame = pd.DataFrame(
        {
            "ticker": ["005930"],
            "ts_close": pd.to_datetime(["2026-05-08T09:00:00+09:00"]),
            "close": [70000.0],
        }
    ).set_index(["ticker", "ts_close"])

    with pytest.raises(DatasetBuildError, match="exogenous_neutral_rehearsal_artifact"):
        builder._join_exogenous_features(frame)


def test_join_exogenous_features_rejects_missing_provenance(
    builder: DatasetBuilder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exogenous artifact가 source_stats 없이 있으면 학습 join을 중단한다."""
    exog_dir = tmp_path / "exogenous"
    exog_dir.mkdir()
    payload = {
        "batch_date": "2026-05-08",
        "snapshot_ts": "2026-05-08T08:30:00+09:00",
        "features": {"us_sp500_change": 0.012},
    }
    (exog_dir / "20260508.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        dataset_builder_module,
        "DEFAULT_EXOGENOUS_ARTIFACT_DIR",
        exog_dir,
    )

    frame = pd.DataFrame(
        {
            "ticker": ["005930"],
            "ts_close": pd.to_datetime(["2026-05-08T09:00:00+09:00"]),
            "close": [70000.0],
        }
    ).set_index(["ticker", "ts_close"])

    with pytest.raises(DatasetBuildError, match="exogenous_provenance_missing"):
        builder._join_exogenous_features(frame)


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


def test_generate_labels_adds_cost_aware_auxiliary_labels(
    builder: DatasetBuilder,
) -> None:
    df = pd.DataFrame(
        {
            "ticker": ["005930"] * 10,
            "ts_close": pd.date_range("2026-04-20 09:00:00+09:00", periods=10, freq="1min"),
            "open": np.arange(100.0, 110.0),
            "high": np.arange(100.0, 110.0) + 1,
            "low": np.arange(100.0, 110.0) - 1,
            "close": np.arange(100.0, 110.0),
            "volume": np.ones(10) * 1000,
        }
    )

    out = builder._generate_labels(df)

    assert "label_5m_net_bps" in out.columns
    assert "label_5m_net_ret" in out.columns
    assert "label_5m_tradeable" in out.columns
    assert "label_session_close_ret" in out.columns
    assert "label_session_close_net_bps" in out.columns
    assert "label_session_close_net_ret" in out.columns
    assert "label_session_close_tradeable" in out.columns

    total_cost_bps = builder._label_total_cost_bps
    min_expected_net_bps = builder._label_min_expected_net_bps

    assert out["label_5m_net_bps"].iloc[0] == pytest.approx(
        (105.0 / 100.0 - 1.0) * 10_000.0 - total_cost_bps
    )
    assert out["label_session_close_ret"].iloc[0] == pytest.approx(
        109.0 / 100.0 - 1.0
    )
    assert out["label_session_close_net_bps"].iloc[0] == pytest.approx(
        (109.0 / 100.0 - 1.0) * 10_000.0 - total_cost_bps
    )
    assert out["label_5m_net_ret"].iloc[0] == pytest.approx(
        out["label_5m_net_bps"].iloc[0] / 10_000.0
    )
    assert out["label_session_close_net_ret"].iloc[0] == pytest.approx(
        out["label_session_close_net_bps"].iloc[0] / 10_000.0
    )
    assert int(out["label_5m_tradeable"].iloc[0]) == int(
        out["label_5m_net_bps"].iloc[0] >= min_expected_net_bps
    )


def test_generate_labels_materializes_cost_aware_horizon_candidates(
    builder: DatasetBuilder,
) -> None:
    df = pd.DataFrame(
        {
            "ticker": ["005930"] * 80,
            "ts_close": pd.date_range("2026-04-20 09:00:00+09:00", periods=80, freq="1min"),
            "open": np.arange(100.0, 180.0),
            "high": np.arange(100.0, 180.0) + 1,
            "low": np.arange(100.0, 180.0) - 1,
            "close": np.arange(100.0, 180.0),
            "volume": np.ones(80) * 1000,
        }
    )

    out = builder._generate_labels(df)

    assert "label_30m_net_ret" in out.columns
    assert "label_60m_net_ret" in out.columns
    assert "label_30m_tradeable" in out.columns
    assert "label_60m_tradeable" in out.columns
    assert out["label_30m_net_ret"].iloc[0] == pytest.approx(
        ((130.0 / 100.0 - 1.0) * 10_000.0 - builder._label_total_cost_bps) / 10_000.0
    )
    assert out["label_60m_net_ret"].iloc[0] == pytest.approx(
        ((160.0 / 100.0 - 1.0) * 10_000.0 - builder._label_total_cost_bps) / 10_000.0
    )


def test_generate_labels_materializes_service_policy_min_holding_horizon(
    builder: DatasetBuilder,
) -> None:
    df = pd.DataFrame(
        {
            "ticker": ["005930"] * 220,
            "ts_close": pd.date_range("2026-04-20 09:00:00+09:00", periods=220, freq="1min"),
            "open": np.arange(100.0, 320.0),
            "high": np.arange(100.0, 320.0) + 1,
            "low": np.arange(100.0, 320.0) - 1,
            "close": np.arange(100.0, 320.0),
            "volume": np.ones(220) * 1000,
        }
    )

    out = builder._generate_labels(df)

    assert "label_195m_net_ret" in out.columns
    assert "label_195m_tradeable" in out.columns
    assert out["label_195m_net_ret"].iloc[0] == pytest.approx(
        ((295.0 / 100.0 - 1.0) * 10_000.0 - builder._label_total_cost_bps) / 10_000.0
    )


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


def test_generate_labels_never_crosses_trading_day(builder: DatasetBuilder) -> None:
    ts_day1 = pd.date_range("2026-04-20 15:24:00+09:00", periods=7, freq="1min")
    ts_day2 = pd.date_range("2026-04-21 09:00:00+09:00", periods=7, freq="1min")
    closes = list(np.arange(100.0, 107.0)) + list(np.arange(200.0, 207.0))
    df = pd.DataFrame(
        {
            "ticker": ["005930"] * 14,
            "ts_close": list(ts_day1) + list(ts_day2),
            "open": closes,
            "high": [value + 1.0 for value in closes],
            "low": [value - 1.0 for value in closes],
            "close": closes,
            "volume": np.ones(14) * 1000,
        }
    )

    out = builder._generate_labels(df)
    out_dates = out["ts_close"].dt.date

    assert len(out) == 4
    assert int((out_dates == date(2026, 4, 20)).sum()) == 2
    assert int((out_dates == date(2026, 4, 21)).sum()) == 2
    day1_labels = out.loc[out_dates == date(2026, 4, 20), "label_5m_ret"].to_numpy()
    assert day1_labels[0] == pytest.approx(105.0 / 100.0 - 1.0)
    assert day1_labels[1] == pytest.approx(106.0 / 101.0 - 1.0)
    assert day1_labels.max() < 0.06
    day1_session_close = out.loc[
        out_dates == date(2026, 4, 20), "label_session_close_ret"
    ].to_numpy()
    assert day1_session_close[0] == pytest.approx(106.0 / 100.0 - 1.0)
    assert day1_session_close.max() < 0.07


def test_generate_labels_drops_short_irregular_session(builder: DatasetBuilder) -> None:
    ts_day1 = pd.date_range("2026-04-20 15:27:00+09:00", periods=4, freq="1min")
    ts_day2 = pd.date_range("2026-04-21 09:00:00+09:00", periods=8, freq="1min")
    closes = list(np.arange(100.0, 104.0)) + list(np.arange(200.0, 208.0))
    df = pd.DataFrame(
        {
            "ticker": ["005930"] * 12,
            "ts_close": list(ts_day1) + list(ts_day2),
            "open": closes,
            "high": [value + 1.0 for value in closes],
            "low": [value - 1.0 for value in closes],
            "close": closes,
            "volume": np.ones(12) * 1000,
        }
    )

    out = builder._generate_labels(df)

    assert len(out) == 3
    assert set(out["ts_close"].dt.date) == {date(2026, 4, 21)}
    assert out["label_5m_ret"].iloc[0] == pytest.approx(205.0 / 200.0 - 1.0)


def test_generate_labels_groups_by_ticker_and_localizes_naive_kst(
    builder: DatasetBuilder,
) -> None:
    ts = list(pd.date_range("2026-04-20 09:00:00", periods=6, freq="1min"))
    df = pd.DataFrame(
        {
            "ticker": ["005930"] * 6 + ["000660"] * 6,
            "ts_close": ts + ts,
            "open": [100.0] * 6 + [1000.0] * 6,
            "high": [106.0] * 6 + [1006.0] * 6,
            "low": [99.0] * 6 + [999.0] * 6,
            "close": list(np.arange(100.0, 106.0)) + list(np.arange(1000.0, 1006.0)),
            "volume": np.ones(12) * 1000,
        }
    )

    out = builder._generate_labels(df)
    by_ticker = dict(zip(out["ticker"], out["label_5m_ret"], strict=True))

    assert out["ts_close"].dt.tz is not None
    assert by_ticker["005930"] == pytest.approx(105.0 / 100.0 - 1.0)
    assert by_ticker["000660"] == pytest.approx(1005.0 / 1000.0 - 1.0)


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


def test_relabel_panel_for_cost_aware_target(builder: DatasetBuilder) -> None:
    df = pd.DataFrame(
        {
            "ticker": ["000001", "000002", "000003", "000004"],
            "ts_close": [pd.Timestamp("2026-04-20 10:00:00+09:00")] * 4,
            "open": [100.0] * 4,
            "high": [101.0] * 4,
            "low": [99.0] * 4,
            "close": [100.0] * 4,
            "volume": [1000.0] * 4,
            "label_5m_ret": [0.04, 0.03, 0.02, 0.01],
            "label_session_close_net_ret": [0.01, 0.02, 0.03, 0.04],
        }
    )
    panel = builder._cross_sectional_rank(df)

    relabeled = builder.relabel_panel_for_target(
        panel,
        "label_session_close_net_ret",
    )

    assert relabeled.loc[("000001", pd.Timestamp("2026-04-20 10:00:00+09:00")), "relevance"] == 0
    assert relabeled.loc[("000004", pd.Timestamp("2026-04-20 10:00:00+09:00")), "relevance"] == 3


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
            "snapshot_ts": "2026-04-20T08:30:00+09:00",
            "source_stats": {"input_mode": "archived_raw_events"},
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


def test_dual_source_join_reads_injected_artifact_dir(tmp_path: Path) -> None:
    """DatasetBuilder는 주입된 root artifact dir에서 Dual-Source JSON을 읽는다."""
    ds_dir = tmp_path / "dual_source"
    ds_dir.mkdir()
    (ds_dir / "20260420.json").write_text(
        json.dumps(
            {
                "batch_date": "2026-04-20",
                "snapshot_ts": "2026-04-20T08:30:00+09:00",
                "source_stats": {"input_mode": "archived_raw_events"},
                "scores": [
                    {
                        "ticker": "005930",
                        "news_score_t": 0.5,
                        "comm_score_t_1": 0.3,
                        "comm_score_t_2": 0.1,
                        "news_comm_divergence": 0.2,
                        "community_noise_multiplier": 0.9,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    b = DatasetBuilder(
        artifacts_dir=tmp_path / "data",
        dual_source_artifact_dir=ds_dir,
    )
    ts = pd.date_range("2026-04-20 09:00:00+09:00", periods=2, freq="1min")
    idx = pd.MultiIndex.from_tuples(
        [("005930", t) for t in ts] + [("000660", t) for t in ts],
        names=["ticker", "ts_close"],
    )
    panel = pd.DataFrame(
        {
            "open": [100.0] * 4,
            "high": [101.0] * 4,
            "low": [99.0] * 4,
            "close": [100.0] * 4,
            "volume": [1000.0] * 4,
            "label_5m_ret": [0.01] * 4,
            "cs_rank": [0.5] * 4,
            "relevance": [1.0] * 4,
        },
        index=idx,
    )

    result = b._join_dual_source_features(panel, "20260420", "20260420")

    assert result.attrs["dual_source_join_stats"]["artifact_dir"] == str(ds_dir)
    assert result.attrs["dual_source_join_stats"]["dates_found"] == 1
    assert result.attrs["dual_source_join_stats"]["rows_non_neutral"] == 2
    assert result.loc[("005930", ts[0]), "news_score_t"] == pytest.approx(0.5)
    assert result.loc[("000660", ts[0]), "community_noise_multiplier"] == pytest.approx(1.0)


def test_dual_source_join_rejects_future_snapshot(tmp_path: Path) -> None:
    """Dual-Source artifact snapshot이 장중 이후면 학습 join을 중단한다."""
    from unittest.mock import patch

    b = DatasetBuilder(artifacts_dir=tmp_path)
    b._ds_enabled_for_lgbm = True
    ts = pd.date_range("2026-04-20 09:00:00+09:00", periods=2, freq="1min")
    idx = pd.MultiIndex.from_tuples([("005930", t) for t in ts], names=["ticker", "ts_close"])
    panel = pd.DataFrame(
        {
            "open": [100.0] * 2,
            "high": [101.0] * 2,
            "low": [99.0] * 2,
            "close": [100.0] * 2,
            "volume": [1000.0] * 2,
            "label_5m_ret": [0.01] * 2,
            "cs_rank": [0.5] * 2,
            "relevance": [1.0] * 2,
        },
        index=idx,
    )
    mock_scores = [{
        "ticker": "005930",
        "snapshot_ts": "2026-04-20T10:00:00+09:00",
        "source_stats": {"input_mode": "archived_raw_events"},
        "news_score_t": 0.5,
        "comm_score_t_1": 0.3,
        "comm_score_t_2": 0.1,
        "news_comm_divergence": 0.2,
        "community_noise_multiplier": 0.9,
    }]

    with patch("src.data.dataset_builder.load_latest_scores", return_value=mock_scores):
        with pytest.raises(DatasetBuildError, match="dual_source_snapshot_after_market_open"):
            b._join_dual_source_features(panel, "20260420", "20260420")


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

    from src.data.dataset_builder import DUAL_SOURCE_DEFAULTS, DUAL_SOURCE_FEATURES
    for feat in DUAL_SOURCE_FEATURES:
        assert feat in result.columns
        vals = result[feat].to_numpy()
        expected = DUAL_SOURCE_DEFAULTS[feat]
        assert all(abs(v - expected) < 1e-9 for v in vals), (
            f"{feat}: neutral default 불일치 {vals}"
        )


def test_dual_source_join_missing_rows_use_multiplier_neutral(tmp_path: Path) -> None:
    """partial Dual-Source 파일에서 미매칭 행은 multiplier=1.0 neutral 유지."""
    from unittest.mock import patch

    b = DatasetBuilder(artifacts_dir=tmp_path)
    b._ds_enabled_for_lgbm = True

    ts = pd.date_range("2026-04-20 09:00:00+09:00", periods=2, freq="1min")
    rows = {
        "open": [100.0] * 4, "high": [101.0] * 4,
        "low": [99.0] * 4, "close": [100.0] * 4,
        "volume": [1000.0] * 4,
        "label_5m_ret": [0.01] * 4, "cs_rank": [0.5] * 4, "relevance": [1.0] * 4,
    }
    idx = pd.MultiIndex.from_tuples(
        [("005930", t) for t in ts] + [("000660", t) for t in ts],
        names=["ticker", "ts_close"],
    )
    panel = pd.DataFrame(rows, index=idx)

    mock_scores = [{
        "ticker": "005930",
        "snapshot_ts": "2026-04-20T08:30:00+09:00",
        "source_stats": {"input_mode": "archived_raw_events"},
        "news_score_t": 0.0,
        "comm_score_t_1": 0.0,
        "comm_score_t_2": 0.0,
        "news_comm_divergence": 0.0,
        "community_noise_multiplier": 1.0,
    }]

    with patch("src.data.dataset_builder.load_latest_scores", return_value=mock_scores):
        result = b._join_dual_source_features(panel, "20260420", "20260420")

    missing_rows = result.loc["000660"]
    assert (missing_rows["community_noise_multiplier"] == 1.0).all()
    assert result.attrs["dual_source_join_stats"]["rows_non_neutral"] == 0


def test_dual_source_join_rejects_missing_provenance(tmp_path: Path) -> None:
    """Dual-Source score가 provenance 없이 들어오면 학습 join을 중단한다."""
    from unittest.mock import patch

    b = DatasetBuilder(artifacts_dir=tmp_path)
    b._ds_enabled_for_lgbm = True
    ts = pd.date_range("2026-04-20 09:00:00+09:00", periods=1, freq="1min")
    idx = pd.MultiIndex.from_tuples([("005930", ts[0])], names=["ticker", "ts_close"])
    panel = pd.DataFrame(
        {
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.0],
            "volume": [1000.0],
            "label_5m_ret": [0.01],
            "cs_rank": [0.5],
            "relevance": [1.0],
        },
        index=idx,
    )
    mock_scores = [{
        "ticker": "005930",
        "snapshot_ts": "2026-04-20T08:30:00+09:00",
        "news_score_t": 0.0,
        "comm_score_t_1": 0.0,
        "comm_score_t_2": 0.0,
        "news_comm_divergence": 0.0,
        "community_noise_multiplier": 1.0,
    }]

    with patch("src.data.dataset_builder.load_latest_scores", return_value=mock_scores):
        with pytest.raises(DatasetBuildError, match="dual_source_provenance_missing"):
            b._join_dual_source_features(panel, "20260420", "20260420")

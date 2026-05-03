"""S4-2 Dual-Source Ablation unit tests.

검증 항목:
  1. DatasetBuilder: dual_source 5피처 join 정확히 수행 (mock load_latest_scores)
  2. DatasetBuilder: enabled_for_lgbm=False 시 5피처 미포함 (rollback 경로)
  3. _load_feature_cols: enabled_for_lgbm=True 시 9피처 반환, False 시 4피처 반환
  4. PerformanceAnalyzer ablation_components에 dual_source 등록 확인
  5. Ablation 리포트 형식 검증 (key 구조 + verdict 값)
  6. DatasetBuilder join: 점수 파일 없으면 기본값 0.0 유지
  7. run_dual_source_ablation: _compute_delta / _verdict 함수 단위 검증
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.data.dataset_builder import DUAL_SOURCE_FEATURES, DatasetBuilder
from src.mode_b.validation_tools import PerformanceAnalyzer, _VALID_ABLATION_COMPONENTS
from src.models.lgbm_trainer import _load_feature_cols


# ====================================================================== #
# Fixtures
# ====================================================================== #


@pytest.fixture
def builder_ds_enabled(tmp_path: Path) -> DatasetBuilder:
    """dual_source enabled_for_lgbm=True DatasetBuilder."""
    b = DatasetBuilder(artifacts_dir=tmp_path)
    b._ds_enabled_for_lgbm = True
    return b


@pytest.fixture
def builder_ds_disabled(tmp_path: Path) -> DatasetBuilder:
    """dual_source enabled_for_lgbm=False DatasetBuilder."""
    b = DatasetBuilder(artifacts_dir=tmp_path)
    b._ds_enabled_for_lgbm = False
    return b


def _make_mock_panel(ticker: str = "005930", n_days: int = 3) -> pd.DataFrame:
    """테스트용 MultiIndex (ticker, ts_close) 패널 생성."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    _KST = ZoneInfo("Asia/Seoul")
    base = datetime(2026, 1, 2, 9, 0, tzinfo=_KST)
    rows = []
    for d in range(n_days):
        ts = base + timedelta(days=d, minutes=0)
        rows.append({
            "ticker": ticker,
            "ts_close": ts,
            "feat_1m_close_robust_z": float(d),
            "feat_5m_ret": 0.001 * d,
            "feat_30m_vol": 0.01,
            "feat_60m_trend": 0.0,
        })

    df = pd.DataFrame(rows)
    df = df.set_index(["ticker", "ts_close"])
    return df


# ====================================================================== #
# Test 1: 5피처 join 정확히 수행
# ====================================================================== #


def test_join_dual_source_features_applies_scores(builder_ds_enabled: DatasetBuilder) -> None:
    """load_latest_scores mock으로 join 검증. 5피처 컬럼 값이 mock 값과 일치해야 함."""
    panel = _make_mock_panel("005930", n_days=2)

    mock_scores = [
        {
            "ticker": "005930",
            "news_score_t": 0.7,
            "comm_score_t_1": 0.3,
            "comm_score_t_2": -0.1,
            "news_comm_divergence": 0.4,
            "community_noise_multiplier": 0.8,
        }
    ]

    with patch("src.data.dataset_builder.load_latest_scores", return_value=mock_scores):
        result = builder_ds_enabled._join_dual_source_features(panel, "20260102", "20260104")

    # 5피처 컬럼 존재 확인
    for feat in DUAL_SOURCE_FEATURES:
        assert feat in result.columns, f"컬럼 없음: {feat}"

    # 값 확인: news_score_t = 0.7
    assert result["news_score_t"].iloc[0] == pytest.approx(0.7), (
        "news_score_t join 실패"
    )
    assert result["comm_score_t_1"].iloc[0] == pytest.approx(0.3)
    assert result["news_comm_divergence"].iloc[0] == pytest.approx(0.4)
    assert result["community_noise_multiplier"].iloc[0] == pytest.approx(0.8)


# ====================================================================== #
# Test 2: enabled_for_lgbm=False 시 5피처 미포함 (rollback)
# ====================================================================== #


def test_join_dual_source_features_skipped_when_disabled(
    builder_ds_disabled: DatasetBuilder,
    tmp_path: Path,
) -> None:
    """enabled_for_lgbm=False 시 build_training_frame이 5피처 join 경로를 타지 않음."""
    # 간단한 jsonl 데이터 생성 (최소 5 ticker × 5 bar)
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    _KST = ZoneInfo("Asia/Seoul")
    tickers = [f"00{i:04d}" for i in range(4)]
    base = datetime(2026, 1, 2, 9, 0, tzinfo=_KST)

    for t in tickers:
        ticker_dir = tmp_path / t
        ticker_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        for bar_idx in range(80):
            ts = base + timedelta(minutes=bar_idx)
            rows.append({
                "ticker": t,
                "ts_close": ts.isoformat(),
                "open": 50000.0,
                "high": 50100.0,
                "low": 49900.0,
                "close": 50000.0 + bar_idx,
                "volume": 1000.0,
            })
        out = ticker_dir / "bars_1m_20260102.jsonl"
        with out.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    with patch("src.data.dataset_builder.load_latest_scores", return_value=[]) as mock_load:
        panel = builder_ds_disabled.build_training_frame(
            tickers=tickers,
            start_date="20260102",
            end_date="20260104",
        )
        # enabled_for_lgbm=False면 load_latest_scores 호출 없음
        mock_load.assert_not_called()

    for feat in DUAL_SOURCE_FEATURES:
        assert feat not in panel.columns, f"disabled인데 컬럼 존재: {feat}"


# ====================================================================== #
# Test 3: _load_feature_cols 피처 수 검증
# ====================================================================== #


def test_load_feature_cols_with_dual_source_enabled() -> None:
    """enabled_for_lgbm=True 시 9피처 반환 (base 4 + dual_source 5)."""
    with patch(
        "src.models.lgbm_trainer.config_load",
        side_effect=_mock_config_load_enabled,
    ):
        cols = _load_feature_cols()

    assert len(cols) == 9, f"9피처 기대, 실제: {len(cols)} — {cols}"
    for feat in DUAL_SOURCE_FEATURES:
        assert feat in cols, f"Dual-Source 피처 없음: {feat}"


def test_load_feature_cols_without_dual_source() -> None:
    """enabled_for_lgbm=False 시 4피처만 반환."""
    with patch(
        "src.models.lgbm_trainer.config_load",
        side_effect=_mock_config_load_disabled,
    ):
        cols = _load_feature_cols()

    assert len(cols) == 4, f"4피처 기대, 실제: {len(cols)} — {cols}"
    for feat in DUAL_SOURCE_FEATURES:
        assert feat not in cols, f"Dual-Source 피처가 있어선 안 됨: {feat}"


# ====================================================================== #
# Test 4: PerformanceAnalyzer ablation_components에 dual_source 등록
# ====================================================================== #


def test_performance_analyzer_valid_ablation_components_includes_dual_source() -> None:
    """_VALID_ABLATION_COMPONENTS에 dual_source 포함 확인 (C13 SSOT)."""
    assert "dual_source" in _VALID_ABLATION_COMPONENTS, (
        "C13 _VALID_ABLATION_COMPONENTS에 dual_source 없음. "
        "validation_tools.py 수정 필요."
    )


def test_performance_analyzer_dual_source_ablation_runs() -> None:
    """PerformanceAnalyzer.analyze에 dual_source 전달 시 정상 실행."""
    pa = PerformanceAnalyzer(seed=42)

    regime_labels = [
        {"date": "2026-01-02", "regime": "bull"},
        {"date": "2026-01-03", "regime": "sideways"},
        {"date": "2026-01-04", "regime": "bear"},
    ]

    result = pa.analyze(
        backtest_run_id="BT-20260102-abcd1234",
        baseline_run_id="BT-20260101-efgh5678",
        regime_labels=regime_labels,
        ablation_components=["dual_source"],
        caller="BacktestAgent",
    )

    ablation_list = result.get("ablation", [])
    assert len(ablation_list) == 1
    assert ablation_list[0]["component"] == "dual_source"
    assert "delta_sharpe" in ablation_list[0]
    assert "delta_mdd" in ablation_list[0]
    assert "significance" in ablation_list[0]


# ====================================================================== #
# Test 5: Ablation 리포트 형식 검증
# ====================================================================== #


def test_ablation_report_schema(tmp_path: Path) -> None:
    """run_dual_source_ablation._compute_delta + _verdict 반환 스키마 확인."""
    from src.jobs.run_dual_source_ablation import _compute_delta, _verdict

    baseline = {
        "ic": 0.043, "rank_ic": 0.051,
        "sharpe": 1.21, "mdd": -0.085, "ndcg5": 0.62,
    }
    with_ds = {
        "ic": 0.058, "rank_ic": 0.067,
        "sharpe": 1.45, "mdd": -0.078, "ndcg5": 0.71,
    }

    delta = _compute_delta(baseline, with_ds)

    # delta 키 확인
    for key in ("ic", "rank_ic", "sharpe", "mdd", "ndcg5"):
        assert key in delta, f"delta에 키 없음: {key}"

    # delta 형식: "+X.XXXX" 또는 "-X.XXXX"
    assert delta["sharpe"].startswith("+") or delta["sharpe"].startswith("-"), (
        f"delta.sharpe 형식 오류: {delta['sharpe']}"
    )

    # verdict 검증
    v = _verdict(delta, threshold_sharpe=0.1)
    assert v == "improvement", f"delta_sharpe=+0.24이면 improvement. 실제: {v}"


def test_verdict_regression() -> None:
    """delta_sharpe 음수 → regression."""
    from src.jobs.run_dual_source_ablation import _compute_delta, _verdict

    baseline = {"ic": 0.05, "rank_ic": 0.06, "sharpe": 1.5, "mdd": -0.08, "ndcg5": 0.65}
    with_ds = {"ic": 0.04, "rank_ic": 0.05, "sharpe": 1.2, "mdd": -0.10, "ndcg5": 0.60}
    delta = _compute_delta(baseline, with_ds)
    v = _verdict(delta, threshold_sharpe=0.1)
    assert v == "regression", f"delta_sharpe 음수면 regression. 실제: {v}"


def test_verdict_neutral() -> None:
    """delta_sharpe 미미 → neutral."""
    from src.jobs.run_dual_source_ablation import _compute_delta, _verdict

    baseline = {"ic": 0.05, "rank_ic": 0.06, "sharpe": 1.5, "mdd": -0.08, "ndcg5": 0.65}
    with_ds = {"ic": 0.05, "rank_ic": 0.06, "sharpe": 1.52, "mdd": -0.08, "ndcg5": 0.65}
    delta = _compute_delta(baseline, with_ds)
    v = _verdict(delta, threshold_sharpe=0.1)
    assert v == "neutral", f"delta_sharpe=+0.02이면 neutral. 실제: {v}"


# ====================================================================== #
# Test 6: 점수 파일 없으면 기본값 0.0
# ====================================================================== #


def test_join_dual_source_features_missing_file_uses_default(
    builder_ds_enabled: DatasetBuilder,
) -> None:
    """load_latest_scores 빈 리스트 반환 시 5피처 기본값 0.0."""
    panel = _make_mock_panel("005930", n_days=2)

    with patch("src.data.dataset_builder.load_latest_scores", return_value=[]):
        result = builder_ds_enabled._join_dual_source_features(panel, "20260102", "20260103")

    for feat in DUAL_SOURCE_FEATURES:
        assert feat in result.columns
        assert (result[feat] == 0.0).all(), f"{feat} 기본값 0.0 아님: {result[feat].values}"


# ====================================================================== #
# 내부 헬퍼: mock config_load
# ====================================================================== #


def _base_preprocessor_cfg() -> dict:
    return {
        "feature_cols": [
            "feat_1m_close_robust_z",
            "feat_5m_ret",
            "feat_30m_vol",
            "feat_60m_trend",
        ],
        "dual_source_feature_cols": list(DUAL_SOURCE_FEATURES),
        "multi_scale_windows": [1, 5, 30, 60],
        "mad_constant": 1.4826,
        "outlier_cap_z": 5.0,
        "rolling_min_periods": 5,
    }


def _mock_config_load_enabled(key: str, section: str | None = None) -> Any:
    """enabled_for_lgbm=True mock."""
    if section == "preprocessor":
        return _base_preprocessor_cfg()
    if section == "dual_source":
        return {"enabled_for_lgbm": True}
    return {}


def _mock_config_load_disabled(key: str, section: str | None = None) -> Any:
    """enabled_for_lgbm=False mock."""
    if section == "preprocessor":
        return _base_preprocessor_cfg()
    if section == "dual_source":
        return {"enabled_for_lgbm": False}
    return {}

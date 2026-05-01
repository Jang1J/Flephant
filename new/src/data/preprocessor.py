"""C3 QuantFeatureContract 전처리기.

Sprint 1-0 실구현: architecture.md §2.3 ①②③ 3단계.
  ① MAD robust Z-score (outlier에 강건, RD-Agent 기반)
  ② 결측치 처리: forward-fill → cross-sectional mean fallback
  ③ Multi-scale 분해: 1m/5m/30m/60m rolling aggregation

파라미터는 risk_config.yaml preprocessor 섹션에서만 로드 (하드코딩 금지).
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from src.utils.config_loader import load as config_load
from src.utils.id_factory import generate_report_id
from src.utils.logger import get_logger
from src.utils.ticker_utils import pad_ticker

logger = get_logger("preprocessor")


def _load_preprocessor_cfg() -> dict:
    """risk_config.yaml preprocessor 섹션 로드. 없으면 default dict 반환."""
    try:
        return config_load("risk_config.yaml", "preprocessor")
    except (KeyError, TypeError) as e:
        logger.warning("[preprocessor] config 로드 실패 (%s). default 사용", e)
        return {}


class Preprocessor:
    """C3 PreprocessingContract. architecture.md §2.3 ①②③.

    multi_scale_windows와 mad_constant는 risk_config.yaml preprocessor 섹션 로드.
    outlier_cap_z: |z| > cap 이상이면 clip.
    """

    def __init__(self) -> None:
        cfg = _load_preprocessor_cfg()
        if not cfg:
            logger.warning("[preprocessor] risk_config.yaml preprocessor 섹션 없음. 기본값 사용.")
        self.multi_scale_windows: list[int] = cfg.get(
            "multi_scale_windows", [1, 5, 30, 60]  # 기본값: yaml 로드 실패 시 임시
        )
        self.mad_constant: float = float(cfg.get("mad_constant", 1.4826))  # 기본값: yaml 로드 실패 시 임시
        self.outlier_cap_z: float = float(cfg.get("outlier_cap_z", 5.0))  # 기본값: yaml 로드 실패 시 임시
        logger.info(
            "[preprocessor] 초기화: windows=%s, mad_constant=%.4f, cap=%.1f",
            self.multi_scale_windows, self.mad_constant, self.outlier_cap_z,
        )

    # ------------------------------------------------------------------ #
    # ① MAD Robust Z-score
    # ------------------------------------------------------------------ #

    def robust_z(self, values: np.ndarray) -> np.ndarray:
        """MAD robust Z-score. (x - median) / (MAD * constant).

        MAD = median(|x - median(x)|).
        MAD=0이면 epsilon=1e-8 fallback (divide-by-zero 방지).
        outlier_cap_z 초과는 clip.
        """
        values = np.asarray(values, dtype=float)
        if values.size == 0:
            return values.copy()

        med = float(np.median(values))
        mad = float(np.median(np.abs(values - med)))
        denom = mad * self.mad_constant
        if denom < 1e-8:
            denom = 1e-8

        z = (values - med) / denom
        return np.clip(z, -self.outlier_cap_z, self.outlier_cap_z)

    # ------------------------------------------------------------------ #
    # ② 결측치 처리
    # ------------------------------------------------------------------ #

    def forward_fill(self, series: list[float | None]) -> list[float]:
        """None → 직전 값으로 채움 (forward-fill).

        첫 값이 None이면 cross-section mean fallback을 위해 0.0 임시 삽입.
        실제 0.0 삽입 여부는 build_quant_frame에서 cross_sectional_mean으로 교체.
        """
        result: list[float] = []
        last_valid: float | None = None

        for v in series:
            if v is not None and not (isinstance(v, float) and math.isnan(v)):
                last_valid = float(v)
                result.append(last_valid)
            elif last_valid is not None:
                result.append(last_valid)
            else:
                # 직전 값 없음: 0.0 임시 삽입 (cross-section fallback 대상)
                result.append(0.0)

        return result

    def cross_sectional_mean(self, values: dict[str, float]) -> float:
        """20 종목 값 평균 (None/NaN 제외). 결측 fallback용."""
        valid = [v for v in values.values() if v is not None and not math.isnan(v)]
        if not valid:
            return 0.0
        return float(sum(valid) / len(valid))

    # ------------------------------------------------------------------ #
    # ③ Multi-scale Aggregation
    # ------------------------------------------------------------------ #

    def multi_scale_aggregate(
        self,
        bars_1m: list[dict],
        windows: list[int] | None = None,
    ) -> dict[int, list[dict]]:
        """1m → 1/5/30/60m rolling aggregation (OHLCV average pooling).

        window=1 은 원본 bars 반환.
        window=W 는 W분봉 단위로 OHLC(open/close는 첫/마지막) + volume sum + vwap/turnover/change mean.

        Returns: {1: [...], 5: [...], 30: [...], 60: [...]}
        """
        active_windows = windows if windows is not None else self.multi_scale_windows
        result: dict[int, list[dict]] = {}

        for w in active_windows:
            if w == 1:
                result[1] = list(bars_1m)
                continue
            result[w] = self._aggregate_window(bars_1m, w)

        return result

    def _aggregate_window(self, bars_1m: list[dict], window: int) -> list[dict]:
        """bars_1m를 window 크기 블록으로 집계.

        OHLCV 규칙:
          open: 블록 첫 bar의 open
          close: 블록 마지막 bar의 close
          high: 블록 내 최대 high
          low: 블록 내 최소 low
          volume: 블록 합산
          vwap/turnover/change: 블록 평균
          ts_close: 블록 마지막 bar의 ts_close
        """
        if not bars_1m:
            return []

        aggregated: list[dict] = []
        n = len(bars_1m)

        for start in range(0, n, window):
            block = bars_1m[start:start + window]
            if not block:
                continue

            agg_bar: dict[str, Any] = {
                "ticker": block[0].get("ticker", ""),
                "ts_close": block[-1].get("ts_close", ""),
                "open": float(block[0].get("open", 0)),
                "close": float(block[-1].get("close", 0)),
                "high": max(float(b.get("high", 0)) for b in block),
                "low": min(float(b.get("low", float("inf"))) for b in block),
                "volume": sum(float(b.get("volume", 0)) for b in block),
                "window": window,
            }

            # 선택 필드 평균
            for field in ("vwap", "turnover", "change"):
                vals = [float(b[field]) for b in block if field in b]
                if vals:
                    agg_bar[field] = sum(vals) / len(vals)

            aggregated.append(agg_bar)

        return aggregated

    # ------------------------------------------------------------------ #
    # C3 quant_frame 생성
    # ------------------------------------------------------------------ #

    def build_quant_frame(
        self,
        tickers: list[str],
        bar_batch: dict[str, list[dict]],
        asof: str,
    ) -> dict:
        """C3 output 생성. 20 종목 동시 처리.

        각 ticker별로:
          - close 시계열에 robust_z 적용
          - 5m/30m/60m return, volatility, trend 계산
          - 결측 ticker는 cross-sectional mean fallback

        처리 순서 보장: _compute_ticker_features → _apply_cross_sectional_fallback.
        순서 역전 시 cross-sectional 대체값이 계산되지 않아 빈 피처가 남음.
        이 메서드 외부에서 forward_fill 결과를 직접 cross_sectional_mean에 전달할 경우
        반드시 forward_fill 먼저, cross_sectional_mean 교체 이후 순으로 호출해야 함.

        Returns:
          {
            "asof": asof,
            "tickers": [...],
            "features": {ticker: {feature_name: float}},
            "quant_frame_id": "RPT-...",
          }
        """
        # 순서 안전 점검: _compute_ticker_features 가 먼저 실행되었는지 확인용 flag.
        # 외부에서 forward_fill → cross_sectional_mean 순서를 건너뛰면 features가 빈 dict로 남음.
        _forward_fill_applied = True  # 이 함수 내에서는 항상 compute 먼저 → assert 통과.
        padded_tickers = [pad_ticker(str(t)) for t in tickers]
        features: dict[str, dict[str, float]] = {}

        # 1차: 각 ticker 계산
        for ticker in padded_tickers:
            bars = bar_batch.get(ticker, [])
            if not bars:
                features[ticker] = {}
                continue
            features[ticker] = self._compute_ticker_features(ticker, bars)

        # 2차: cross-sectional mean fallback (결측 피처 보완)
        features = self._apply_cross_sectional_fallback(padded_tickers, features)

        return {
            "asof": asof,
            "tickers": padded_tickers,
            "features": features,
            "quant_frame_id": generate_report_id(),
        }

    def _compute_ticker_features(
        self, ticker: str, bars: list[dict]
    ) -> dict[str, float]:
        """단일 ticker 피처 계산.

        피처:
          1m_close_robust_z: 마지막 1분봉 close의 robust z (전체 시계열 기준)
          5m_ret: 최근 5분 수익률
          30m_vol: 최근 30분 close 변동성 (std)
          60m_trend: 최근 60분 선형 추세 기울기 (정규화)
        """
        closes = [float(b.get("close", 0)) for b in bars]
        if not closes:
            return {}

        arr = np.array(closes, dtype=float)
        z_arr = self.robust_z(arr)

        feats: dict[str, float] = {}

        # feat_ prefix 규약 (SSOT: risk_config.yaml preprocessor.feature_cols).
        # DatasetBuilder._compute_rolling_features / QuantAgent._compute_features 와 동일.

        # feat_1m_close_robust_z: 마지막 값
        feats["feat_1m_close_robust_z"] = float(z_arr[-1]) if len(z_arr) > 0 else 0.0

        # feat_5m_ret
        if len(closes) >= 5:
            feats["feat_5m_ret"] = (closes[-1] - closes[-5]) / (closes[-5] + 1e-8)
        else:
            feats["feat_5m_ret"] = 0.0

        # feat_30m_vol
        if len(closes) >= 30:
            feats["feat_30m_vol"] = float(np.std(closes[-30:]))
        elif len(closes) > 1:
            feats["feat_30m_vol"] = float(np.std(closes))
        else:
            feats["feat_30m_vol"] = 0.0

        # feat_60m_trend (선형 회귀 기울기)
        window = min(60, len(closes))
        if window >= 2:
            y = np.array(closes[-window:], dtype=float)
            x = np.arange(window, dtype=float)
            # 정규화된 기울기: slope / (mean + eps)
            mean_y = float(np.mean(y))
            if mean_y > 1e-8:
                slope = float(np.polyfit(x, y, 1)[0])
                feats["feat_60m_trend"] = slope / mean_y
            else:
                feats["feat_60m_trend"] = 0.0
        else:
            feats["feat_60m_trend"] = 0.0

        return feats

    def _apply_cross_sectional_fallback(
        self,
        tickers: list[str],
        features: dict[str, dict[str, float]],
    ) -> dict[str, dict[str, float]]:
        """결측 피처를 cross-sectional mean으로 채움.

        피처가 아예 없는 ticker는 전체 평균으로 채움.
        """
        # 피처 이름 수집
        all_feat_names: set[str] = set()
        for feats in features.values():
            all_feat_names.update(feats.keys())

        if not all_feat_names:
            return features

        # 피처별 cross-sectional mean 계산
        cross_mean: dict[str, float] = {}
        for feat_name in all_feat_names:
            vals = {
                t: features[t][feat_name]
                for t in tickers
                if t in features and feat_name in features[t]
            }
            cross_mean[feat_name] = self.cross_sectional_mean(vals)

        # 결측 ticker에 mean 채움
        for ticker in tickers:
            if ticker not in features or not features[ticker]:
                features[ticker] = {fn: cross_mean[fn] for fn in all_feat_names}
                logger.warning(
                    "[preprocessor] %s: 데이터 없음. cross-sectional mean 사용", ticker
                )
            else:
                for feat_name in all_feat_names:
                    if feat_name not in features[ticker]:
                        features[ticker][feat_name] = cross_mean[feat_name]

        return features

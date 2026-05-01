"""Hot Path Quant Agent (S1-1).

C1 MinuteBar 수신 → BarBuffer 저장 → cross-sectional ranking score 생성.
C5 publish: quant_signal / quant_alert / anomaly_detected.

불변 원칙:
  - Hot Path <100ms (동기 LLM 호출 금지)
  - 모든 수치는 risk_config.yaml quant_agent + preprocessor 섹션 로드 (하드코딩 금지)
  - 종목코드 pad_ticker (6자리 zero-padded)

아키텍처 포지션 (architecture.md L83, L373):
  "Quant Agent = 숫자 세계와 텍스트 세계의 다리. 숫자 이상 감지 시 에이전트를 깨운다."
  Risk Fast Agent와 역할 분리: 숫자 anomaly 전담 vs 커뮤니티/수급 rule 전담.

Feature 이름 규약:
  DatasetBuilder/LightGBM 훈련과 동일한 feat_ prefix 사용 (feat_1m_close_robust_z,
  feat_5m_ret, feat_30m_vol, feat_60m_trend). Preprocessor(hot path 1-shot용)는 다른 이름
  규약을 쓰므로 여기서 직접 rolling feature 계산 (S1-0 preprocessor와 중복 최소화).
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any

import numpy as np

from src.agents._base import AgentBase
from src.data.bar_buffer import BarBuffer
from src.models.registry import (
    ModelRegistry,
    PklLoadFailedError,
    RegistryCorruptedError,
    VersionNotFoundError,
)
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.ticker_utils import pad_ticker

logger = get_logger("quant_agent")


class QuantAgent(AgentBase):
    """Hot Path 퀀트 시그널 + 이상 탐지.

    사용 흐름 (Hot Runner 1분 루프):
        1. 매 bar 수신 시 `on_bar(bar)` → BarBuffer 저장 (가벼움)
        2. 매 분 cross-sectional 시점에 `score_cross_section(tickers, asof)` 호출
        3. 동시에 `detect_anomalies(tickers)` 호출 → Cold Path escalation 트리거
        4. 레이턴시 관찰: `latency_percentiles()`

    모델 미로드 시 passive mode. 학습 전에도 on_bar / anomaly 동작은 정상.
    """

    # QuantAgent publishes 3종 (api_contracts.md L53-67 publish_channels 중 QuantAgent 항목).
    # architecture.md L464 QuantAgent.publishes와 1:1 일치. AgentBase 2단계 검증(옵션 C).
    ALLOWED_PUBLISH_CHANNELS = frozenset(
        {"quant_signal", "quant_alert", "anomaly_detected"}
    )

    def __init__(
        self,
        registry: ModelRegistry | None = None,
        bar_buffer: BarBuffer | None = None,
    ) -> None:
        # --- 설정 로드 (yaml 경유) ---
        qa_cfg = config_load("risk_config.yaml", "quant_agent")
        pp_cfg = config_load("risk_config.yaml", "preprocessor")

        self._warmup_bars: int = int(qa_cfg["warmup_bars"])
        self._anomaly_zscore_threshold: float = float(
            qa_cfg["anomaly_zscore_threshold"]
        )
        self._latency_window: int = int(qa_cfg["latency_window"])
        self._latency_p95_target_ms: float = float(qa_cfg["latency_p95_target_ms"])

        self._multi_scale_windows: list[int] = list(pp_cfg["multi_scale_windows"])
        self._mad_constant: float = float(pp_cfg["mad_constant"])
        self._outlier_cap_z: float = float(pp_cfg["outlier_cap_z"])
        self._feature_cols: list[str] = list(pp_cfg["feature_cols"])

        # --- 의존 컴포넌트 ---
        self._registry = registry or ModelRegistry()
        self._bar_buffer = bar_buffer or BarBuffer()

        # --- 모델 로드 (실패 시 passive mode) ---
        self._booster: Any = None
        self._model_metadata: dict[str, Any] | None = None
        self._try_load_model()

        # --- 레이턴시 측정 ---
        self._latency_records: deque[float] = deque(maxlen=self._latency_window)

        logger.info(
            "[quant_agent] 초기화 완료. model_loaded=%s, warmup=%d, "
            "anomaly_z=%.1f, latency_window=%d",
            self.has_model, self._warmup_bars,
            self._anomaly_zscore_threshold, self._latency_window,
        )

    # ================================================================== #
    # Public properties
    # ================================================================== #

    @property
    def has_model(self) -> bool:
        """booster 로드 성공 여부."""
        return self._booster is not None

    @property
    def model_metadata(self) -> dict[str, Any] | None:
        """로드된 모델 메타데이터 (version, feature_cols, metrics, ...)."""
        return self._model_metadata

    # ================================================================== #
    # Public API
    # ================================================================== #

    def on_bar(self, bar: dict[str, Any]) -> None:
        """단일 1분봉 수신 → BarBuffer.push.

        BarBuffer가 필수 필드 검증 수행 (누락 시 ValueError 전파).
        Hot Path 최경량 (<1ms).
        """
        self._bar_buffer.push(bar)

    def score_cross_section(
        self,
        tickers: list[str],
        asof: str,
    ) -> dict[str, Any]:
        """Cross-sectional ranking score 생성.

        매 분 Hot Runner가 호출. 20 종목 한꺼번에 처리.
        warmup 부족 종목은 자동 제외.
        booster 없으면 passive mode (scores 비어있음, mode=passive).

        Returns:
            {
              "tickers": [padded ticker list, valid만],
              "scores": {ticker: ranking_score},
              "ts": asof,
              "mode": "active" | "passive" | "warmup",
              "latency_ms": float,
              "n_tickers": int,
            }
        """
        t0 = time.perf_counter()
        padded_all = [pad_ticker(str(t)) for t in tickers]
        asof_str = str(asof)

        if not self.has_model:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self._latency_records.append(elapsed_ms)
            return {
                "tickers": [],
                "scores": {},
                "ts": asof_str,
                "mode": "passive",
                "latency_ms": elapsed_ms,
                "n_tickers": 0,
            }

        # BarBuffer batch load
        max_window = int(self._multi_scale_windows[-1])
        bars_batch = self._bar_buffer.get_batch(padded_all, max_window)

        feature_matrix: list[list[float]] = []
        valid_tickers: list[str] = []

        for ticker in padded_all:
            bars = bars_batch.get(ticker, [])
            if len(bars) < self._warmup_bars:
                continue
            feats = self._compute_features(bars)
            if feats is None:
                continue
            try:
                feature_vec = [float(feats[c]) for c in self._feature_cols]
            except KeyError as e:
                logger.warning(
                    "[quant_agent] %s feature 누락: %s. skip", ticker, e
                )
                continue
            feature_matrix.append(feature_vec)
            valid_tickers.append(ticker)

        if not valid_tickers:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self._latency_records.append(elapsed_ms)
            return {
                "tickers": [],
                "scores": {},
                "ts": asof_str,
                "mode": "warmup",
                "latency_ms": elapsed_ms,
                "n_tickers": 0,
            }

        X = np.asarray(feature_matrix, dtype=float)
        preds = self._booster.predict(X)
        scores = {t: float(s) for t, s in zip(valid_tickers, preds)}

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self._latency_records.append(elapsed_ms)

        if elapsed_ms > self._latency_p95_target_ms:
            logger.warning(
                "[quant_agent] 레이턴시 초과: %.2fms > %.1fms SLA "
                "(tickers=%d)",
                elapsed_ms, self._latency_p95_target_ms, len(valid_tickers),
            )

        return {
            "tickers": valid_tickers,
            "scores": scores,
            "ts": asof_str,
            "mode": "active",
            "latency_ms": elapsed_ms,
            "n_tickers": len(valid_tickers),
        }

    def detect_anomalies(
        self,
        tickers: list[str],
        asof: str,
    ) -> list[dict[str, Any]]:
        """Cross-sectional anomaly 탐지.

        현재 1종: intraday_drop (1min return z-score < -threshold, rolling 60 bars).
        architecture.md trigger_catalog intraday_drop_anomaly 대응.

        Returns: list of {ticker, anomaly_type, z_score, ts}
        """
        padded_all = [pad_ticker(str(t)) for t in tickers]
        max_window = int(self._multi_scale_windows[-1])
        out: list[dict[str, Any]] = []

        for ticker in padded_all:
            bars = self._bar_buffer.get_latest(ticker, max_window)
            if len(bars) < self._warmup_bars:
                continue
            is_anom, z = self._is_intraday_drop_anomaly(bars)
            if is_anom:
                out.append({
                    "ticker": ticker,
                    "anomaly_type": "intraday_drop",
                    "z_score": z,
                    "ts": bars[-1].get("ts_close", str(asof)),
                })
        return out

    def latency_percentiles(self) -> dict[str, float]:
        """p50 / p95 / p99 ms (최근 latency_window 기록 기준)."""
        if len(self._latency_records) == 0:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "n": 0}
        arr = np.asarray(list(self._latency_records), dtype=float)
        return {
            "p50": float(np.percentile(arr, 50)),
            "p95": float(np.percentile(arr, 95)),
            "p99": float(np.percentile(arr, 99)),
            "n": int(arr.size),
        }

    def report(self, report_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """QuantAgent 산출물 생성. publish channel 명 기준으로 검증.

        QuantAgent의 "report"는 publish_channels에 대응하는 구조화된 산출물.
        (C5 report_types 5종에 quant_signal만 포함되지만, quant_alert / anomaly_detected도
        같은 report() API로 처리한다. ALLOWED_PUBLISH_CHANNELS로 검증.)
        """
        if report_type not in self.ALLOWED_PUBLISH_CHANNELS:
            raise ValueError(
                f"QuantAgent.report: invalid report_type={report_type}. "
                f"허용: {sorted(self.ALLOWED_PUBLISH_CHANNELS)}"
            )
        return {
            "report_type": report_type,
            "agent": "QuantAgent",
            "payload": payload,
        }

    def reload_model(self) -> bool:
        """모델 재로드 (S3-6 야간 재학습 후 다음 날 아침 호출).

        Returns: 성공 True, 실패 False.
        """
        return self._try_load_model()

    # ================================================================== #
    # Internal: 모델 로드
    # ================================================================== #

    def _try_load_model(self) -> bool:
        try:
            booster, metadata = self._registry.load_latest()
        except (VersionNotFoundError, PklLoadFailedError, RegistryCorruptedError) as e:
            logger.warning(
                "[quant_agent] 모델 로드 실패 (%s). passive mode로 전환.", e
            )
            self._booster = None
            self._model_metadata = None
            return False

        self._booster = booster
        self._model_metadata = metadata
        logger.info(
            "[quant_agent] 모델 로드: version=%s, train_end=%s, feature_cols=%d",
            metadata.get("version"), metadata.get("train_end"),
            len(metadata.get("feature_cols", [])),
        )
        return True

    # ================================================================== #
    # Internal: Feature 계산 (DatasetBuilder rolling feature 동일 규약)
    # ================================================================== #

    def _compute_features(self, bars: list[dict[str, Any]]) -> dict[str, float] | None:
        """단일 ticker 최근 60 bars → 4 피처 dict (feat_ prefix).

        DatasetBuilder._compute_rolling_features와 동일한 수식.
        단건 계산 목적이므로 numpy only (pandas 미사용).

        PIT-Safety 경계: feat_1m_close_robust_z와 feat_60m_trend는 현재 bar(t) 포함
        block으로 median/slope 계산. 자기 참조 편향 있으나 DatasetBuilder 훈련과
        동일한 규약을 유지해야 feature distribution mismatch 없음 (설계 의도).
        """
        if len(bars) < self._warmup_bars:
            return None

        close_values = [float(b["close"]) for b in bars if "close" in b]
        n_missing = len(bars) - len(close_values)
        if n_missing > 0:
            logger.warning(
                "[quant_agent] %d개 bar에 'close' 필드 누락. 필터 후 n=%d",
                n_missing, len(close_values),
            )
        closes = np.array(close_values, dtype=float)
        if len(closes) < self._warmup_bars:
            return None

        _, w5, w30, w60 = self._multi_scale_windows
        last = float(closes[-1])

        # feat_5m_ret (5분 전 close 대비 return)
        if len(closes) > w5 and closes[-w5 - 1] > 1e-8:
            feat_5m_ret = last / float(closes[-w5 - 1]) - 1.0
        else:
            feat_5m_ret = 0.0

        # feat_30m_vol (rolling std / rolling mean, last w30 window)
        if len(closes) >= w30:
            last_w30 = closes[-w30:]
            mean_30 = float(last_w30.mean())
            if mean_30 > 1e-8:
                feat_30m_vol = float(last_w30.std(ddof=0) / mean_30)
            else:
                feat_30m_vol = 0.0
        else:
            feat_30m_vol = 0.0

        # feat_60m_trend (linear slope / mean, last w60 window)
        window_len = min(w60, len(closes))
        if window_len >= 2:
            last_window = closes[-window_len:]
            x = np.arange(window_len, dtype=float)
            x_mean = x.mean()
            x_var = float(np.sum((x - x_mean) ** 2))
            y_mean = float(last_window.mean())
            if x_var > 1e-12 and y_mean > 1e-8:
                cov = float(np.sum((x - x_mean) * (last_window - y_mean)))
                slope = cov / x_var
                feat_60m_trend = slope / y_mean
            else:
                feat_60m_trend = 0.0
        else:
            feat_60m_trend = 0.0

        # feat_1m_close_robust_z (MAD robust Z wrt last w60 window)
        window_len_z = min(w60, len(closes))
        block = closes[-window_len_z:]
        med = float(np.median(block))
        mad = float(np.median(np.abs(block - med)))
        denom = mad * self._mad_constant
        if denom < 1e-8:
            denom = 1e-8
        z_raw = (last - med) / denom
        feat_1m_close_robust_z = float(
            np.clip(z_raw, -self._outlier_cap_z, self._outlier_cap_z)
        )

        return {
            "feat_1m_close_robust_z": feat_1m_close_robust_z,
            "feat_5m_ret": feat_5m_ret,
            "feat_30m_vol": feat_30m_vol,
            "feat_60m_trend": feat_60m_trend,
        }

    # ================================================================== #
    # Internal: Anomaly detection
    # ================================================================== #

    def _is_intraday_drop_anomaly(
        self,
        bars: list[dict[str, Any]],
    ) -> tuple[bool, float]:
        """1min return z-score < -threshold → anomaly.

        rolling 60 bars 내 return 분포 기준. architecture.md trigger_catalog 대응.
        Returns: (is_anomaly, z_score).
        """
        if len(bars) < self._warmup_bars:
            return False, 0.0

        closes = np.array([float(b["close"]) for b in bars], dtype=float)
        returns = np.diff(closes) / np.where(closes[:-1] > 1e-8, closes[:-1], 1e-8)
        if len(returns) < 2:
            return False, 0.0

        # 최신 return 제외한 나머지 기준 mean/std (current bar를 outlier 판정)
        hist = returns[:-1]
        if len(hist) < 2:
            return False, 0.0
        mu = float(hist.mean())
        sigma = float(hist.std(ddof=0))
        if sigma < 1e-8:
            return False, 0.0

        latest_ret = float(returns[-1])
        z = (latest_ret - mu) / sigma
        return (z < -self._anomaly_zscore_threshold), z

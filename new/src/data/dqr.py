"""Data Quality Report. 누락률 / 지연 / outlier 비율 추적."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from src.dqr.dqr_runner import _compute_outlier_rate
from src.utils.config_loader import load as config_load


class DataQualityReport:
    """단일 1분봉 묶음 품질 리포트 생성기.

    추적 항목:
      missing_rate: 종목별 1분봉 누락률.
      latency_ms: 수신 지연 (실제 bar ts vs 수신 ts).
      outlier_ratio: MAD 기준 이상치 비율.

    결과를 ops/monitor.py에 전달하여 SLA 위반 감지.
    임계값 하드코딩 금지. config_loader.load('risk_config.yaml', 'dqr') 경유.
    """

    def __init__(self, connector_name: str = "kis_rest") -> None:
        self._connector_name = connector_name
        self._cfg = config_load("risk_config.yaml", "dqr") or {}
        expected = self._cfg.get("expected_bars", {})
        self._expected_per_day = int(expected.get(connector_name, 0))
        self._outlier_z_threshold = float(self._cfg.get("outlier_z_threshold", 5.0))

    def report(self, bars: list[dict[str, Any]]) -> dict[str, Any]:
        """bars 목록 분석. DQR 딕셔너리 반환."""
        clean_bars = [b for b in bars if isinstance(b, dict)]
        tickers = {str(b.get("ticker", "")).zfill(6) for b in clean_bars if b.get("ticker")}
        dates = {
            str(b.get("date") or str(b.get("ts_close", ""))[:10]).replace("-", "")
            for b in clean_bars
            if b.get("date") or b.get("ts_close")
        }
        expected_count = self._expected_count(tickers, dates, len(clean_bars))
        actual_count = len(clean_bars)
        missing_rate = 0.0
        if expected_count > 0:
            missing_rate = max(expected_count - actual_count, 0) / expected_count

        closes = [self._to_float(b.get("close")) for b in clean_bars]
        closes = [v for v in closes if v is not None]
        latencies = [self._latency_ms(b) for b in clean_bars]
        latencies = [v for v in latencies if v is not None]
        outlier_rate_pct = _compute_outlier_rate(
            closes,
            threshold=self._outlier_z_threshold,
        )
        return {
            "connector": self._connector_name,
            "expected_count": expected_count,
            "actual_count": actual_count,
            "missing_rate": round(missing_rate, 6),
            "missing_rate_pct": round(missing_rate * 100, 4),
            "latency_ms": round(sum(latencies) / len(latencies), 4) if latencies else 0.0,
            "outlier_ratio": round(outlier_rate_pct / 100, 6),
            "outlier_rate_pct": outlier_rate_pct,
            "ticker_count": len(tickers),
            "date_count": len(dates),
        }

    def _expected_count(self, tickers: set[str], dates: set[str], actual_count: int) -> int:
        if self._expected_per_day <= 0:
            return actual_count
        if not tickers or not dates:
            return actual_count
        return self._expected_per_day * len(tickers) * len(dates)

    @staticmethod
    def _to_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _latency_ms(bar: dict[str, Any]) -> float | None:
        ts_close = bar.get("ts_close")
        received_at = bar.get("received_at")
        if not ts_close or not received_at:
            return None
        try:
            close_dt = datetime.fromisoformat(str(ts_close))
            recv_dt = datetime.fromisoformat(str(received_at))
            return max((recv_dt - close_dt).total_seconds() * 1000.0, 0.0)
        except (TypeError, ValueError):
            return None

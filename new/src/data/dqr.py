"""Data Quality Report. 누락률 / 지연 / outlier 비율 추적."""
from __future__ import annotations


class DataQualityReport:
    """데이터 품질 리포트 생성기.

    추적 항목:
      missing_rate: 종목별 1분봉 누락률.
      latency_ms: 수신 지연 (실제 bar ts vs 수신 ts).
      outlier_ratio: MAD 기준 이상치 비율.

    결과를 ops/monitor.py에 전달하여 SLA 위반 감지.
    임계값 하드코딩 금지. config_loader.load('risk_config.yaml', 'dqr') 경유.
    Sprint 1 구현 예정.
    """

    def report(self, bars: list[dict]) -> dict:
        """bars 목록 분석. DQR 딕셔너리 반환."""
        raise NotImplementedError("Sprint 1 구현 예정")

"""S4-5 DQR Runner: 일별 커넥터 데이터 품질 자동 측정.

Mode B 18:00 KST 이후 실행. 8개 커넥터별 5가지 지표 측정:
  missing_rate_pct: 기대 데이터 대비 누락 비율 (%)
  delay_minutes: 수집-적재 지연 (분)
  outlier_rate_pct: MAD robust-z |z|>5 이상치 비율 (%)
  error_rate_pct: HTTP 4xx/5xx + retry 에러율 (%)
  rate_limit_hit: rate_limiter 한도 도달 횟수

임계값은 risk_config.yaml dqr 섹션에서 로드 (불변 원칙 5).
PIT-Safety: snapshot_ts 18:00 KST 이후에만 실행 (불변 원칙 1).
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.pit_guard import PITViolationError

logger = get_logger("dqr")
_KST = ZoneInfo("Asia/Seoul")

# 8개 커넥터 이름 (BaseConnector 상속 클래스명 기준)
CONNECTOR_NAMES: tuple[str, ...] = (
    "kis_rest",
    "kis_ws",
    "krx_rest",
    "dart_rest",
    "naver_rest",
    "community",
    "ecos_rest",
    "us_market",
)


def _check_pit_guard(now: datetime | None = None) -> None:
    """실행 시점이 18:00 KST 이후인지 확인.

    Args:
        now: 현재 시각 (테스트 주입용). None이면 datetime.now(KST) 사용.

    Raises:
        PITViolationError: 18:00 KST 이전 실행 시도.
    """
    cfg = config_load("risk_config.yaml", "pit_safety")
    snapshot_hour: int = int(cfg.get("snapshot_hour", 18))
    current = now or datetime.now(_KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_KST)
    cutoff = datetime.combine(current.date(), time(snapshot_hour, 0, 0), tzinfo=_KST)
    if current < cutoff:
        raise PITViolationError(
            f"[dqr] PIT-Safety 위반: DQR은 {snapshot_hour}:00 KST 이후에만 실행 가능. "
            f"현재={current.isoformat()}, 기준={cutoff.isoformat()}"
        )


def _robust_z(values: list[float]) -> list[float]:
    """MAD 기반 robust Z-score. 이상치 탐지용.

    z_i = (x_i - median) / (1.4826 * MAD)
    MAD = 0 케이스 처리:
      - max-min > 0 (단조 데이터가 아닌데 MAD=0): std 기반 fallback z.
        std > 0이면 (v - median) / std 반환.
        std = 0이지만 값이 median과 다른 것이 있으면 inf 반환 (데이터 이상).
      - max-min = 0 (완전 상수 시계열): 모두 0.0 반환.
    """
    if not values:
        return []
    med = statistics.median(values)
    deviations = [abs(v - med) for v in values]
    mad = statistics.median(deviations)
    if mad == 0.0:
        if max(values) - min(values) > 0:
            # 단조 데이터가 아닌데 MAD=0 → std 기반 fallback
            std = statistics.stdev(values) if len(values) > 1 else 0.0
            if std > 0:
                return [(v - med) / std for v in values]
            # std=0이지만 min != max → 데이터 이상
            return [float("inf") if v != med else 0.0 for v in values]
        # 완전 상수 시계열
        return [0.0] * len(values)
    scale = 1.4826 * mad
    return [(v - med) / scale for v in values]


def _compute_outlier_rate(values: list[float], threshold: float = 5.0) -> float:
    """MAD robust-z |z| > threshold 비율 (%) 계산.

    Args:
        values: 수치 시계열 (빈 리스트면 0.0 반환).
        threshold: z-score 임계값. 기본 5.0 (C1 MinuteBar 8 features 기준).

    Returns:
        이상치 비율 (%). 0.0~100.0.
    """
    if not values:
        return 0.0
    zscores = _robust_z(values)
    outlier_count = sum(1 for z in zscores if abs(z) > threshold)
    return round(outlier_count / len(values) * 100, 4)


class ConnectorStats:
    """단일 커넥터의 품질 측정 결과 컨테이너.

    DQRRunner.measure_connector()가 이 객체를 반환.
    run_daily()가 모든 커넥터를 순회하여 dict로 변환.
    """

    def __init__(
        self,
        connector_name: str,
        expected_count: int,
        actual_count: int,
        delay_samples: list[float],
        numeric_samples: list[float],
        error_count: int,
        total_requests: int,
        rate_limit_hit: int,
        outlier_z_threshold: float = 5.0,
    ) -> None:
        self.connector_name = connector_name
        self.expected_count = max(expected_count, 1)
        self.actual_count = actual_count
        self.delay_samples = delay_samples
        self.numeric_samples = numeric_samples
        self.error_count = error_count
        self.total_requests = max(total_requests, 1)
        self.rate_limit_hit = rate_limit_hit
        # W1 yaml화: outlier_z_threshold는 DQRRunner가 risk_config.yaml에서 로드 후 주입.
        self._outlier_z_threshold: float = outlier_z_threshold

    @property
    def missing_rate_pct(self) -> float:
        """기대 대비 누락률 (%). actual < expected 비율."""
        missed = max(self.expected_count - self.actual_count, 0)
        return round(missed / self.expected_count * 100, 4)

    @property
    def delay_minutes(self) -> float:
        """delay_samples 평균 (분). 샘플 없으면 0.0."""
        if not self.delay_samples:
            return 0.0
        return round(statistics.mean(self.delay_samples), 4)

    @property
    def outlier_rate_pct(self) -> float:
        """MAD robust-z |z|>_outlier_z_threshold 이상치 비율 (%).

        threshold는 risk_config.yaml dqr.outlier_z_threshold에서 DQRRunner가 로드 후 주입.
        """
        return _compute_outlier_rate(self.numeric_samples, threshold=self._outlier_z_threshold)

    @property
    def error_rate_pct(self) -> float:
        """에러(4xx/5xx + retry 소진) 비율 (%)."""
        return round(self.error_count / self.total_requests * 100, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "connector": self.connector_name,
            "expected_count": self.expected_count,
            "actual_count": self.actual_count,
            "missing_rate_pct": self.missing_rate_pct,
            "delay_minutes": self.delay_minutes,
            "outlier_rate_pct": self.outlier_rate_pct,
            "error_rate_pct": self.error_rate_pct,
            "rate_limit_hit": self.rate_limit_hit,
        }


class DQRRunner:
    """S4-5 DQR 일별 자동화 실행기.

    run_daily(date): 8개 커넥터 품질 지표 측정 → 리포트 dict.
    save_report(): artifacts/dqr/dqr_YYYYMMDD.json 저장.
    check_alerts(): 임계값 초과 항목 alert list 반환.
    append_ops_alert(): artifacts/ops_alerts.jsonl append.

    불변 원칙 5: 모든 임계값은 risk_config.yaml dqr 섹션에서 로드.
    불변 원칙 1: _check_pit_guard()로 18:00 KST 이후 실행 강제.
    """

    def __init__(
        self,
        connectors: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        """
        Args:
            connectors: {connector_name: connector_instance} dict.
                        None이면 각 커넥터를 동적 import 시도 (stub 반환).
            now: 현재 시각 (테스트 주입용). None이면 datetime.now(KST).
        """
        self._connectors: dict[str, Any] = connectors or {}
        self._now = now
        self._cfg: dict[str, Any] = self._load_config()
        output_dir_str: str = self._cfg.get("output_dir", "artifacts/dqr")
        self._output_dir = Path(output_dir_str)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        ops_alert_path_str: str = self._cfg.get(
            "ops_alert_path", "artifacts/ops_alerts.jsonl"
        )
        self._ops_alert_path = Path(ops_alert_path_str)
        self._ops_alert_path.parent.mkdir(parents=True, exist_ok=True)
        # W1 yaml화: outlier_z_threshold를 risk_config.yaml dqr 섹션에서 로드 (불변 원칙 5).
        self._outlier_z_threshold: float = float(
            self._cfg.get("outlier_z_threshold", 5.0)
        )
        logger.info("[dqr] DQRRunner 초기화 완료. output_dir=%s outlier_z_threshold=%.1f",
                    self._output_dir, self._outlier_z_threshold)

    # ================================================================== #
    # Public API
    # ================================================================== #

    def run_daily(
        self,
        date: str | None = None,
        skip_pit_guard: bool = False,
    ) -> dict[str, Any]:
        """date 일자의 8 커넥터 DQR 측정. 리포트 dict 반환.

        Args:
            date: YYYY-MM-DD 형식. None이면 오늘.
            skip_pit_guard: True면 PIT-Safety 시간 체크 건너뜀 (테스트 전용).

        Returns:
            {
              "date": "2026-05-02",
              "generated_at": "2026-05-02T18:01:00+09:00",
              "connectors": { connector_name: {...stats...} },
              "alerts": [ {alert dict}, ... ],
              "summary": { "total_connectors": 8, "alert_count": N }
            }

        PIT-Safety: skip_pit_guard=False(기본) 시 18:00 KST 이후만 허용.
        """
        if not skip_pit_guard:
            _check_pit_guard(self._now)

        current = self._now or datetime.now(_KST)
        if current.tzinfo is None:
            current = current.replace(tzinfo=_KST)
        date_str = date or current.strftime("%Y-%m-%d")

        logger.info("[dqr] 일별 DQR 시작: date=%s", date_str)

        connectors_report: dict[str, Any] = {}
        for name in CONNECTOR_NAMES:
            try:
                stats = self.measure_connector(name, date_str)
                connectors_report[name] = stats.to_dict()
                logger.info(
                    "[dqr] %s 측정 완료: missing=%.2f%% delay=%.2fmin outlier=%.2f%% error=%.2f%% rl_hit=%d",
                    name,
                    stats.missing_rate_pct,
                    stats.delay_minutes,
                    stats.outlier_rate_pct,
                    stats.error_rate_pct,
                    stats.rate_limit_hit,
                )
            except Exception as e:
                logger.error("[dqr] %s 측정 실패: %s", name, e)
                connectors_report[name] = {
                    "connector": name,
                    "error": str(e),
                    "missing_rate_pct": 100.0,
                    "delay_minutes": 0.0,
                    "outlier_rate_pct": 0.0,
                    "error_rate_pct": 100.0,
                    "rate_limit_hit": 0,
                }

        report: dict[str, Any] = {
            "date": date_str,
            "generated_at": current.isoformat(),
            "connectors": connectors_report,
            "alerts": [],
            "summary": {
                "total_connectors": len(CONNECTOR_NAMES),
                "alert_count": 0,
            },
        }

        alerts = self.check_alerts(report)
        report["alerts"] = alerts
        report["summary"]["alert_count"] = len(alerts)

        # ops alert 파일 기록
        for alert in alerts:
            self.append_ops_alert(alert)

        logger.info(
            "[dqr] 일별 DQR 완료: date=%s connectors=%d alerts=%d",
            date_str,
            len(CONNECTOR_NAMES),
            len(alerts),
        )
        return report

    def measure_connector(self, name: str, date: str) -> ConnectorStats:
        """단일 커넥터 품질 측정. ConnectorStats 반환.

        커넥터 인스턴스가 주입된 경우: _collect_stats_from_connector() 호출.
        미주입 시: stub ConnectorStats (실제 운영에서는 주입 필수).

        Args:
            name: 커넥터 이름 (CONNECTOR_NAMES 중 하나).
            date: YYYY-MM-DD.
        """
        connector = self._connectors.get(name)
        if connector is not None:
            return self._collect_stats_from_connector(name, connector, date)
        # stub: 커넥터 미주입 시 (단위 테스트 또는 초기 smoke)
        logger.warning("[dqr] %s 커넥터 미주입. stub stats 반환.", name)
        return ConnectorStats(
            connector_name=name,
            expected_count=1,
            actual_count=1,
            delay_samples=[0.0],
            numeric_samples=[0.0],
            error_count=0,
            total_requests=1,
            rate_limit_hit=0,
            outlier_z_threshold=self._outlier_z_threshold,
        )

    def _collect_stats_from_connector(
        self,
        name: str,
        connector: Any,
        date: str,
    ) -> ConnectorStats:
        """커넥터 인스턴스에서 DQR 지표 수집.

        커넥터가 get_dqr_stats(date) 메서드를 구현하면 해당 결과 사용.
        미구현 시 BaseConnector 공통 속성에서 추론:
          - connector._request_count: 총 요청 수
          - connector._error_count: 에러 수
          - connector._rate_limit_hit_count: rate limit 도달 횟수
          - connector._bars: 수집된 bar 리스트 (kis_rest/kis_ws용)
          - connector._delay_samples: 지연 샘플 리스트 (분 단위)
          - connector._numeric_samples: 수치 샘플 (이상치 탐지용)
        """
        # 커넥터가 get_dqr_stats()를 구현한 경우 우선 사용
        if hasattr(connector, "get_dqr_stats") and callable(connector.get_dqr_stats):
            try:
                raw = connector.get_dqr_stats(date)
                return ConnectorStats(
                    connector_name=name,
                    expected_count=int(raw.get("expected_count", 1)),
                    actual_count=int(raw.get("actual_count", 0)),
                    delay_samples=list(raw.get("delay_samples", [])),
                    numeric_samples=list(raw.get("numeric_samples", [])),
                    error_count=int(raw.get("error_count", 0)),
                    total_requests=int(raw.get("total_requests", 1)),
                    rate_limit_hit=int(raw.get("rate_limit_hit", 0)),
                    outlier_z_threshold=self._outlier_z_threshold,
                )
            except Exception as e:
                logger.warning(
                    "[dqr] %s.get_dqr_stats() 실패, 공통 속성으로 폴백: %s", name, e
                )

        # 공통 속성 폴백
        total_requests = int(getattr(connector, "_request_count", 1))
        error_count = int(getattr(connector, "_error_count", 0))
        rate_limit_hit = int(getattr(connector, "_rate_limit_hit_count", 0))
        delay_samples = list(getattr(connector, "_delay_samples", [0.0]))
        numeric_samples = list(getattr(connector, "_numeric_samples", [0.0]))

        # 1분봉 커넥터 기대치: 장중 09:00~15:30 = 390분
        expected_bars = self._cfg.get("expected_bars", {}).get(name, 1)
        bars = getattr(connector, "_bars", None)
        actual_count = len(bars) if bars is not None else total_requests

        return ConnectorStats(
            connector_name=name,
            expected_count=int(expected_bars),
            actual_count=actual_count,
            delay_samples=delay_samples,
            numeric_samples=numeric_samples,
            error_count=error_count,
            total_requests=max(total_requests, 1),
            rate_limit_hit=rate_limit_hit,
            outlier_z_threshold=self._outlier_z_threshold,
        )

    def check_alerts(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        """리포트에서 임계값 초과 항목 추출. alert list 반환.

        critical=True 커넥터가 임계값 초과 시 severity=CRITICAL.
        critical=False 커넥터는 severity=WARNING.

        Args:
            report: run_daily() 반환 리포트 dict.

        Returns:
            alert dict list. 각 항목:
              {
                "ts": ISO8601,
                "severity": "CRITICAL" | "WARNING",
                "connector": str,
                "metric": str,
                "value": float,
                "threshold": float,
              }
        """
        thresholds: dict[str, float] = self._cfg.get("thresholds", {})
        per_connector_cfg: dict[str, Any] = self._cfg.get("per_connector", {})
        generated_at: str = report.get("generated_at", datetime.now(_KST).isoformat())

        metric_keys = [
            ("missing_rate_pct", "missing_rate_pct"),
            ("delay_minutes", "delay_minutes"),
            ("outlier_rate_pct", "outlier_rate_pct"),
            ("error_rate_pct", "error_rate_pct"),
        ]

        alerts: list[dict[str, Any]] = []
        for name, connector_report in report.get("connectors", {}).items():
            if "error" in connector_report:
                # 측정 자체가 실패한 커넥터는 무조건 CRITICAL alert
                conn_cfg = per_connector_cfg.get(name, {})
                severity = "CRITICAL" if conn_cfg.get("critical", False) else "WARNING"
                alerts.append({
                    "ts": generated_at,
                    "severity": severity,
                    "connector": name,
                    "metric": "measurement_error",
                    "value": 100.0,
                    "threshold": 0.0,
                    "detail": connector_report.get("error", "unknown"),
                })
                continue

            conn_cfg = per_connector_cfg.get(name, {})
            is_critical = conn_cfg.get("critical", False)

            for report_key, threshold_key in metric_keys:
                value = float(connector_report.get(report_key, 0.0))
                threshold = float(thresholds.get(threshold_key, float("inf")))
                if value > threshold:
                    severity = "CRITICAL" if is_critical else "WARNING"
                    alerts.append({
                        "ts": generated_at,
                        "severity": severity,
                        "connector": name,
                        "metric": report_key,
                        "value": value,
                        "threshold": threshold,
                    })
                    logger.error(
                        "[dqr] ALERT %s: %s %s=%.4f > threshold=%.4f",
                        severity,
                        name,
                        report_key,
                        value,
                        threshold,
                    )

            # rate_limit_hit: 1 이상이면 INFO alert (임계값 없음, 항상 기록)
            rl_hit = int(connector_report.get("rate_limit_hit", 0))
            if rl_hit > 0:
                alerts.append({
                    "ts": generated_at,
                    "severity": "INFO",
                    "connector": name,
                    "metric": "rate_limit_hit",
                    "value": float(rl_hit),
                    "threshold": 0.0,
                })
                logger.warning("[dqr] %s rate_limit_hit=%d", name, rl_hit)

        return alerts

    def save_report(self, report: dict[str, Any], output_path: Path | None = None) -> Path:
        """리포트를 JSON 파일로 저장.

        Args:
            report: run_daily() 반환 dict.
            output_path: 저장 경로. None이면 artifacts/dqr/dqr_YYYYMMDD.json.

        Returns:
            실제 저장된 Path.
        """
        if output_path is None:
            date_str = report.get("date", datetime.now(_KST).strftime("%Y-%m-%d"))
            filename = f"dqr_{date_str.replace('-', '')}.json"
            output_path = self._output_dir / filename

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        logger.info("[dqr] 리포트 저장 완료: %s", output_path)
        return output_path

    def append_ops_alert(self, alert: dict[str, Any]) -> None:
        """ops alert를 artifacts/ops_alerts.jsonl에 append.

        Args:
            alert: check_alerts()가 반환한 단건 alert dict.
        """
        with self._ops_alert_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(alert, ensure_ascii=False) + "\n")

    # ================================================================== #
    # Internal helpers
    # ================================================================== #

    def _load_config(self) -> dict[str, Any]:
        """risk_config.yaml dqr 섹션 로드.

        섹션 미존재 시 기본값 사용 (불변 원칙 5: 임계값은 yaml SSOT).
        """
        try:
            cfg = config_load("risk_config.yaml", "dqr")
            logger.info("[dqr] risk_config.yaml dqr 섹션 로드 완료")
            return cfg if isinstance(cfg, dict) else {}
        except (KeyError, Exception) as e:
            logger.warning("[dqr] dqr 섹션 로드 실패, 기본값 사용: %s", e)
            return {
                "enabled": True,
                "output_dir": "artifacts/dqr",
                "ops_alert_path": "artifacts/ops_alerts.jsonl",
                "thresholds": {
                    "missing_rate_pct": 5.0,
                    "delay_minutes": 10.0,
                    "outlier_rate_pct": 2.0,
                    "error_rate_pct": 1.0,
                },
                "per_connector": {
                    name: {"critical": name in ("kis_rest", "kis_ws")}
                    for name in CONNECTOR_NAMES
                },
                "expected_bars": {
                    "kis_rest": 390,
                    "kis_ws": 390,
                    "krx_rest": 1,
                    "dart_rest": 390,
                    "naver_rest": 390,
                    "community": 78,
                    "ecos_rest": 1,
                    "us_market": 1,
                },
            }

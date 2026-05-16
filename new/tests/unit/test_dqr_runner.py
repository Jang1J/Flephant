"""S4-5 DQR Runner 단위 테스트.

테스트 5종:
  1. DQR 측정 정확성 (mock connector stats)
  2. 임계값 초과 alert 생성 확인
  3. 임계값 미달 alert 미생성 확인
  4. JSON 저장 형식 검증
  5. critical=True 커넥터 CRITICAL severity 검증

PIT-Safety: skip_pit_guard=True 사용 (시간 의존 제거).
하드코딩 금지: 임계값은 DQRRunner._load_config() 경유 (risk_config.yaml).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.dqr.dqr_runner import (
    ConnectorStats,
    DQRRunner,
    PITViolationError,
    _check_pit_guard,
    _compute_outlier_rate,
    _robust_z,
)

_KST = ZoneInfo("Asia/Seoul")
_TEST_DATE = "2026-05-02"


# ======================================================================
# Helper: mock connector with known stats
# ======================================================================

class MockConnector:
    """DQRRunner._collect_stats_from_connector() 테스트용 mock.

    get_dqr_stats(date) 구현: 고정값 반환.
    """

    def __init__(
        self,
        expected_count: int = 390,
        actual_count: int = 380,
        delay_samples: list[float] | None = None,
        numeric_samples: list[float] | None = None,
        error_count: int = 2,
        total_requests: int = 200,
        rate_limit_hit: int = 0,
    ) -> None:
        self._expected_count = expected_count
        self._actual_count = actual_count
        self._delay_samples = delay_samples or [1.0, 2.0, 1.5]
        self._numeric_samples = numeric_samples or [1.0, 2.0, 3.0]
        self._error_count = error_count
        self._total_requests = total_requests
        self._rate_limit_hit = rate_limit_hit

    def get_dqr_stats(self, date: str) -> dict:  # noqa: ARG002
        return {
            "expected_count": self._expected_count,
            "actual_count": self._actual_count,
            "delay_samples": self._delay_samples,
            "numeric_samples": self._numeric_samples,
            "error_count": self._error_count,
            "total_requests": self._total_requests,
            "rate_limit_hit": self._rate_limit_hit,
        }


def _make_runner(connectors: dict | None = None, tmp_path: Path | None = None) -> DQRRunner:
    """테스트용 DQRRunner. tmp_path로 output_dir 오버라이드."""
    runner = DQRRunner(connectors=connectors)
    if tmp_path:
        runner._output_dir = tmp_path / "dqr"
        runner._output_dir.mkdir(parents=True, exist_ok=True)
        runner._ops_alert_path = tmp_path / "ops_alerts.jsonl"
    return runner


# ======================================================================
# Test 1: DQR 측정 정확성
# ======================================================================

class TestDQRMeasurement:
    """ConnectorStats 계산 및 DQRRunner.measure_connector() 정확성."""

    def test_missing_rate_calculation(self):
        """390개 기대, 380개 수집 → missing_rate = 10/390 * 100 ≈ 2.564%."""
        stats = ConnectorStats(
            connector_name="kis_rest",
            expected_count=390,
            actual_count=380,
            delay_samples=[1.0],
            numeric_samples=[1.0],
            error_count=0,
            total_requests=390,
            rate_limit_hit=0,
        )
        expected_rate = round(10 / 390 * 100, 4)
        assert abs(stats.missing_rate_pct - expected_rate) < 0.001

    def test_delay_average(self):
        """delay_samples 평균 확인."""
        stats = ConnectorStats(
            connector_name="naver_rest",
            expected_count=10,
            actual_count=10,
            delay_samples=[2.0, 4.0, 6.0],
            numeric_samples=[1.0],
            error_count=0,
            total_requests=10,
            rate_limit_hit=0,
        )
        assert abs(stats.delay_minutes - 4.0) < 0.001

    def test_outlier_rate(self):
        """outlier_rate 계산: 값 분포가 있을 때 극단값 비율 탐지.

        samples: [1,2,3,4,5,6,7,8,9, 1000]. median=5, MAD=4, scale=4*1.4826=5.93.
        1000의 z = (1000-5)/5.93 ≈ 167.8 >> 5.0 → outlier 1개 → 10%.
        """
        normal = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
        extreme = [1000.0]
        stats = ConnectorStats(
            connector_name="kis_ws",
            expected_count=10,
            actual_count=10,
            delay_samples=[0.0],
            numeric_samples=normal + extreme,
            error_count=0,
            total_requests=10,
            rate_limit_hit=0,
        )
        assert stats.outlier_rate_pct == 10.0

    def test_error_rate_calculation(self):
        """error_count=2, total_requests=200 → error_rate = 1.0%."""
        stats = ConnectorStats(
            connector_name="dart_rest",
            expected_count=200,
            actual_count=198,
            delay_samples=[0.5],
            numeric_samples=[1.0],
            error_count=2,
            total_requests=200,
            rate_limit_hit=0,
        )
        assert abs(stats.error_rate_pct - 1.0) < 0.001

    def test_measure_connector_with_mock(self):
        """MockConnector 주입 시 get_dqr_stats() 결과가 ConnectorStats에 반영."""
        mock = MockConnector(
            expected_count=390,
            actual_count=380,
            delay_samples=[3.0],
            numeric_samples=[1.0, 2.0],
            error_count=0,
            total_requests=100,
            rate_limit_hit=0,
        )
        runner = _make_runner(connectors={"kis_rest": mock})
        stats = runner.measure_connector("kis_rest", _TEST_DATE)
        assert stats.expected_count == 390
        assert stats.actual_count == 380
        assert abs(stats.delay_minutes - 3.0) < 0.001

    def test_measure_connector_stub_when_no_connector(self):
        """커넥터 미주입 시 stub ConnectorStats (missing_rate=0.0, 에러 없음)."""
        runner = _make_runner(connectors={})
        stats = runner.measure_connector("ecos_rest", _TEST_DATE)
        assert stats.missing_rate_pct == 0.0
        assert stats.error_rate_pct == 0.0

    def test_robust_z_constant_series(self):
        """상수 시계열 (MAD=0) → 모든 z=0."""
        zscores = _robust_z([5.0, 5.0, 5.0, 5.0])
        assert all(z == 0.0 for z in zscores)

    def test_compute_outlier_rate_empty(self):
        """빈 리스트 → 0.0."""
        assert _compute_outlier_rate([]) == 0.0

    def test_run_daily_returns_all_connectors(self):
        """run_daily() 결과에 8개 커넥터 모두 포함."""
        runner = _make_runner()
        report = runner.run_daily(date=_TEST_DATE, skip_pit_guard=True)
        assert "connectors" in report
        assert len(report["connectors"]) == 8
        expected_names = {
            "kis_rest", "kis_ws", "krx_rest", "dart_rest",
            "naver_rest", "community", "ecos_rest", "us_market",
        }
        assert set(report["connectors"].keys()) == expected_names


# ======================================================================
# Test 2: 임계값 초과 alert 생성
# ======================================================================

class TestAlertGeneration:
    """check_alerts()가 임계값 초과 시 alert를 생성하는지 검증."""

    def _make_report(self, connector: str, **metrics) -> dict:
        """단일 커넥터 리포트 dict 생성 헬퍼."""
        base = {
            "missing_rate_pct": 0.0,
            "delay_minutes": 0.0,
            "outlier_rate_pct": 0.0,
            "error_rate_pct": 0.0,
            "rate_limit_hit": 0,
        }
        base.update(metrics)
        return {
            "date": _TEST_DATE,
            "generated_at": "2026-05-02T18:01:00+09:00",
            "connectors": {connector: {"connector": connector, **base}},
            "alerts": [],
            "summary": {},
        }

    def test_missing_rate_alert_generated(self):
        """missing_rate_pct=10.0 > threshold=5.0 → alert 생성."""
        runner = _make_runner()
        report = self._make_report("krx_rest", missing_rate_pct=10.0)
        alerts = runner.check_alerts(report)
        missing_alerts = [a for a in alerts if a["metric"] == "missing_rate_pct"]
        assert len(missing_alerts) == 1
        assert missing_alerts[0]["connector"] == "krx_rest"
        assert missing_alerts[0]["value"] == 10.0

    def test_delay_alert_generated(self):
        """delay_minutes=15.0 > threshold=10.0 → alert 생성."""
        runner = _make_runner()
        report = self._make_report("naver_rest", delay_minutes=15.0)
        alerts = runner.check_alerts(report)
        delay_alerts = [a for a in alerts if a["metric"] == "delay_minutes"]
        assert len(delay_alerts) == 1
        assert abs(delay_alerts[0]["value"] - 15.0) < 0.001

    def test_outlier_rate_alert_generated(self):
        """outlier_rate_pct=5.0 > threshold=2.0 → alert 생성."""
        runner = _make_runner()
        report = self._make_report("community", outlier_rate_pct=5.0)
        alerts = runner.check_alerts(report)
        outlier_alerts = [a for a in alerts if a["metric"] == "outlier_rate_pct"]
        assert len(outlier_alerts) == 1

    def test_error_rate_alert_generated(self):
        """error_rate_pct=2.0 > threshold=1.0 → alert 생성."""
        runner = _make_runner()
        report = self._make_report("dart_rest", error_rate_pct=2.0)
        alerts = runner.check_alerts(report)
        error_alerts = [a for a in alerts if a["metric"] == "error_rate_pct"]
        assert len(error_alerts) == 1

    def test_rate_limit_hit_alert_generated(self):
        """rate_limit_hit=3 > 0 → INFO alert 생성."""
        runner = _make_runner()
        report = self._make_report("naver_rest", rate_limit_hit=3)
        alerts = runner.check_alerts(report)
        rl_alerts = [a for a in alerts if a["metric"] == "rate_limit_hit"]
        assert len(rl_alerts) == 1
        assert rl_alerts[0]["severity"] == "INFO"
        assert rl_alerts[0]["value"] == 3.0

    def test_alert_structure(self):
        """alert dict에 ts/severity/connector/metric/value/threshold 필드 포함."""
        runner = _make_runner()
        report = self._make_report("ecos_rest", missing_rate_pct=20.0)
        alerts = runner.check_alerts(report)
        required_keys = {"ts", "severity", "connector", "metric", "value", "threshold"}
        for alert in alerts:
            if alert["metric"] == "missing_rate_pct":
                assert required_keys.issubset(set(alert.keys()))


# ======================================================================
# Test 3: 임계값 미달 alert 미생성
# ======================================================================

class TestNoAlert:
    """임계값 이하 데이터에서 alert 미생성."""

    def test_no_alert_all_below_threshold(self):
        """모든 지표가 임계값 이하 → alert 0개."""
        runner = _make_runner()
        report = {
            "date": _TEST_DATE,
            "generated_at": "2026-05-02T18:01:00+09:00",
            "connectors": {
                name: {
                    "connector": name,
                    "missing_rate_pct": 0.5,   # < 5.0
                    "delay_minutes": 2.0,       # < 10.0
                    "outlier_rate_pct": 0.5,    # < 2.0
                    "error_rate_pct": 0.1,      # < 1.0
                    "rate_limit_hit": 0,        # = 0 → 미생성
                }
                for name in [
                    "kis_rest", "kis_ws", "krx_rest", "dart_rest",
                    "naver_rest", "community", "ecos_rest", "us_market",
                ]
            },
            "alerts": [],
            "summary": {},
        }
        alerts = runner.check_alerts(report)
        assert len(alerts) == 0

    def test_no_alert_boundary_exact_threshold(self):
        """정확히 threshold 값 (초과 아님) → alert 미생성."""
        runner = _make_runner()
        report = {
            "date": _TEST_DATE,
            "generated_at": "2026-05-02T18:01:00+09:00",
            "connectors": {
                "kis_rest": {
                    "connector": "kis_rest",
                    "missing_rate_pct": 5.0,   # = threshold, 초과 아님
                    "delay_minutes": 10.0,      # = threshold
                    "outlier_rate_pct": 2.0,    # = threshold
                    "error_rate_pct": 1.0,      # = threshold
                    "rate_limit_hit": 0,
                }
            },
            "alerts": [],
            "summary": {},
        }
        alerts = runner.check_alerts(report)
        non_rl_alerts = [a for a in alerts if a["metric"] != "rate_limit_hit"]
        assert len(non_rl_alerts) == 0


# ======================================================================
# Test 4: JSON 저장 형식 검증
# ======================================================================

class TestReportSave:
    """save_report() JSON 파일 저장 형식 검증."""

    def test_save_creates_file(self, tmp_path):
        """save_report() 호출 후 파일 생성 확인."""
        runner = _make_runner(tmp_path=tmp_path)
        report = runner.run_daily(date=_TEST_DATE, skip_pit_guard=True)
        output_path = runner.save_report(report)
        assert output_path.exists()

    def test_save_valid_json(self, tmp_path):
        """저장된 파일이 유효한 JSON."""
        runner = _make_runner(tmp_path=tmp_path)
        report = runner.run_daily(date=_TEST_DATE, skip_pit_guard=True)
        output_path = runner.save_report(report)
        with output_path.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
        assert isinstance(loaded, dict)

    def test_save_required_fields(self, tmp_path):
        """저장 JSON에 date/generated_at/connectors/alerts/summary 포함."""
        runner = _make_runner(tmp_path=tmp_path)
        report = runner.run_daily(date=_TEST_DATE, skip_pit_guard=True)
        output_path = runner.save_report(report)
        with output_path.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
        for key in ("date", "generated_at", "connectors", "alerts", "summary"):
            assert key in loaded, f"필드 누락: {key}"

    def test_save_filename_pattern(self, tmp_path):
        """저장 파일명이 dqr_YYYYMMDD.json 형식."""
        runner = _make_runner(tmp_path=tmp_path)
        report = runner.run_daily(date="2026-05-02", skip_pit_guard=True)
        output_path = runner.save_report(report)
        assert output_path.name == "dqr_20260502.json"

    def test_save_custom_path(self, tmp_path):
        """output_path 인수로 사용자 지정 경로 저장."""
        runner = _make_runner(tmp_path=tmp_path)
        report = runner.run_daily(date=_TEST_DATE, skip_pit_guard=True)
        custom = tmp_path / "custom" / "my_report.json"
        output_path = runner.save_report(report, output_path=custom)
        assert output_path.exists()
        assert output_path.name == "my_report.json"

    def test_ops_alert_jsonl_append(self, tmp_path):
        """alert 발생 시 ops_alerts.jsonl에 1줄 이상 append."""
        mock = MockConnector(
            expected_count=390,
            actual_count=200,  # missing_rate > 5%
            delay_samples=[0.0],
            numeric_samples=[0.0],
            error_count=0,
            total_requests=390,
            rate_limit_hit=0,
        )
        runner = _make_runner(
            connectors={"kis_rest": mock},
            tmp_path=tmp_path,
        )
        runner.run_daily(date=_TEST_DATE, skip_pit_guard=True)
        assert runner._ops_alert_path.exists()
        lines = runner._ops_alert_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 1
        for line in lines:
            entry = json.loads(line)
            assert "connector" in entry
            assert "metric" in entry
            assert "value" in entry


# ======================================================================
# Test 5: critical=True 커넥터 CRITICAL severity + 파이프라인 차단 신호
# ======================================================================

class TestCriticalConnector:
    """critical=True 커넥터(kis_rest/kis_ws)가 임계값 초과 시 CRITICAL alert."""

    def test_critical_connector_severity(self):
        """kis_rest (critical=true)에서 missing_rate 초과 → severity=CRITICAL."""
        runner = _make_runner()
        report = {
            "date": _TEST_DATE,
            "generated_at": "2026-05-02T18:01:00+09:00",
            "connectors": {
                "kis_rest": {
                    "connector": "kis_rest",
                    "missing_rate_pct": 20.0,  # > 5.0
                    "delay_minutes": 0.0,
                    "outlier_rate_pct": 0.0,
                    "error_rate_pct": 0.0,
                    "rate_limit_hit": 0,
                }
            },
            "alerts": [],
            "summary": {},
        }
        alerts = runner.check_alerts(report)
        missing_alerts = [a for a in alerts if a["metric"] == "missing_rate_pct"]
        assert len(missing_alerts) == 1
        assert missing_alerts[0]["severity"] == "CRITICAL"

    def test_non_critical_connector_severity(self):
        """krx_rest (critical=false)에서 missing_rate 초과 → severity=WARNING."""
        runner = _make_runner()
        report = {
            "date": _TEST_DATE,
            "generated_at": "2026-05-02T18:01:00+09:00",
            "connectors": {
                "krx_rest": {
                    "connector": "krx_rest",
                    "missing_rate_pct": 20.0,
                    "delay_minutes": 0.0,
                    "outlier_rate_pct": 0.0,
                    "error_rate_pct": 0.0,
                    "rate_limit_hit": 0,
                }
            },
            "alerts": [],
            "summary": {},
        }
        alerts = runner.check_alerts(report)
        missing_alerts = [a for a in alerts if a["metric"] == "missing_rate_pct"]
        assert len(missing_alerts) == 1
        assert missing_alerts[0]["severity"] == "WARNING"

    def test_run_daily_contains_critical_flag(self):
        """run_daily() 결과에 CRITICAL alert 발생 시 alerts 포함."""
        mock = MockConnector(
            expected_count=390,
            actual_count=0,  # 100% 누락 → CRITICAL
            delay_samples=[0.0],
            numeric_samples=[0.0],
            error_count=0,
            total_requests=1,
            rate_limit_hit=0,
        )
        runner = _make_runner(connectors={"kis_rest": mock})
        report = runner.run_daily(date=_TEST_DATE, skip_pit_guard=True)
        critical_alerts = [a for a in report["alerts"] if a["severity"] == "CRITICAL"]
        assert len(critical_alerts) >= 1
        assert any(a["connector"] == "kis_rest" for a in critical_alerts)

    def test_measurement_error_generates_alert(self, monkeypatch):
        """measure_connector() 자체가 실패하면 run_daily()가 error 항목으로 기록,
        check_alerts()가 measurement_error alert 생성."""

        class FailingConnector:
            def get_dqr_stats(self, date: str) -> dict:  # noqa: ARG002
                raise RuntimeError("연결 실패")

        runner = _make_runner(connectors={"kis_rest": FailingConnector()})

        # measure_connector()가 예외를 던지도록 패치
        def _raising_measure(name: str, date: str) -> ConnectorStats:  # noqa: ARG001
            raise RuntimeError("measure 실패")

        monkeypatch.setattr(runner, "measure_connector", _raising_measure)
        report = runner.run_daily(date=_TEST_DATE, skip_pit_guard=True)
        # run_daily() try/except에서 error 키 있는 dict로 기록
        assert "error" in report["connectors"].get("kis_rest", {})
        error_alerts = [
            a for a in report["alerts"] if a.get("metric") == "measurement_error"
        ]
        assert len(error_alerts) >= 1


# ======================================================================
# PIT-Safety 검증
# ======================================================================

class TestPITGuard:
    """DQR PIT-Safety 시간 체크 (불변 원칙 1)."""

    def test_pit_guard_before_18(self):
        """17:59 KST → PITViolationError."""
        dt_before = datetime(2026, 5, 2, 17, 59, 0, tzinfo=_KST)
        with pytest.raises(PITViolationError):
            _check_pit_guard(now=dt_before)

    def test_pit_guard_at_18(self):
        """18:00 KST → 통과."""
        dt_on = datetime(2026, 5, 2, 18, 0, 0, tzinfo=_KST)
        _check_pit_guard(now=dt_on)  # 예외 없어야 함

    def test_pit_guard_after_18(self):
        """19:00 KST → 통과."""
        dt_after = datetime(2026, 5, 2, 19, 0, 0, tzinfo=_KST)
        _check_pit_guard(now=dt_after)

    def test_run_daily_skip_pit_guard(self):
        """skip_pit_guard=True 시 시간 무관하게 실행."""
        runner = _make_runner()
        report = runner.run_daily(date=_TEST_DATE, skip_pit_guard=True)
        assert "date" in report

    def test_run_daily_pit_guard_before_18_raises(self):
        """skip_pit_guard=False + 17:59 → PITViolationError."""
        dt_before = datetime(2026, 5, 2, 17, 59, 0, tzinfo=_KST)
        runner = DQRRunner(now=dt_before)
        with pytest.raises(PITViolationError):
            runner.run_daily(date=_TEST_DATE, skip_pit_guard=False)

    def test_run_daily_non_trading_day_skips_after_pit_cutoff(self):
        """비거래일 직접 실행은 커넥터 측정 대신 SKIP한다."""
        dt_after = datetime(2026, 5, 2, 18, 1, 0, tzinfo=_KST)
        runner = DQRRunner(now=dt_after)

        report = runner.run_daily(date="2026-05-02", skip_pit_guard=False)

        assert report["status"] == "SKIP"
        assert report["reason"] == "weekend"
        assert report["connectors"] == {}


# ======================================================================
# Test 6: C-R3 신규 — 주말/공휴일 skip + MAD=0 fallback
# ======================================================================

class TestHolidaySkipAndMADFallback:
    """C-R3 fix 검증: 주말/공휴일 DQR skip + MAD=0 outlier fallback."""

    def test_robust_z_mad_zero_with_outlier(self):
        """MAD=0이지만 max-min > 0인 경우: std 기반 fallback z. 극단값이 z>0.

        데이터: [1, 1, 1, 1, 100]. median=1, deviations=[0,0,0,0,99].
        MAD = median([0,0,0,0,99]) = 0. max-min=99 > 0 → std fallback.
        std > 0이면 100은 median보다 큰 양의 z 반환.
        """
        values = [1.0, 1.0, 1.0, 1.0, 100.0]
        zscores = _robust_z(values)
        # 100.0은 median(1.0)보다 매우 크므로 z > 0
        assert zscores[-1] > 0.0, f"극단값 z={zscores[-1]} — 양수여야 함"

    def test_robust_z_constant_still_zero(self):
        """완전 상수 시계열: MAD=0, max-min=0 → 모두 0.0."""
        zscores = _robust_z([3.0, 3.0, 3.0])
        assert all(z == 0.0 for z in zscores)

    def test_outlier_rate_detects_outlier_with_mad_zero_fallback(self):
        """MAD=0 fallback 경로에서도 극단값이 outlier로 탐지됨.

        [1,1,1,1,1000]: MAD=0 → std fallback → z=2.24. threshold=2.0으로 탐지.
        핵심: MAD=0일 때 모두 z=0 반환하던 버그가 아닌, 실제 z값을 반환하는지 확인.
        """
        values = [1.0, 1.0, 1.0, 1.0, 1000.0]
        rate = _compute_outlier_rate(values, threshold=2.0)
        assert rate > 0.0, f"outlier_rate={rate} — 0보다 커야 함 (threshold=2.0)"

    def test_scheduler_stage0_skips_on_weekend(self, monkeypatch):
        """토요일 날짜로 stage_0_dqr 호출 시 status=skipped, reason=weekend, critical_alert=False."""
        from src.mode_b.scheduler import ModeBScheduler

        scheduler = ModeBScheduler()
        # 2026-05-02는 토요일
        result = scheduler.stage_0_dqr("2026-05-02")
        assert result["status"] == "skipped"
        assert result["reason"] == "weekend"
        assert result["critical_alert"] is False

    def test_scheduler_stage0_skips_on_holiday(self, monkeypatch):
        """공휴일(어린이날 2026-05-05)로 stage_0_dqr 호출 시 status=skipped, reason=holiday."""
        from src.mode_b.scheduler import ModeBScheduler

        scheduler = ModeBScheduler()
        # 어린이날: 화요일이지만 공휴일 (risk_config.yaml dqr.kospi_holidays_2026에 등록)
        result = scheduler.stage_0_dqr("2026-05-05")
        assert result["status"] == "skipped"
        assert result["reason"] == "holiday"
        assert result["critical_alert"] is False

    def test_scheduler_stage0_exception_gives_critical_alert(self, monkeypatch):
        """stage_0_dqr 내부 DQRRunner 예외 발생 시 critical_alert=True.

        인프라 오류 = 데이터 품질 미검증 → 파이프라인 차단 필요.
        """
        from src.mode_b.scheduler import ModeBScheduler

        scheduler = ModeBScheduler()

        # DQRRunner import를 강제로 실패시킴
        import builtins
        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "src.dqr.dqr_runner":
                raise ImportError("DQRRunner 강제 실패 (테스트)")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        # 2026-05-04: 월요일, 공휴일 아님 → DQR 실행 경로로 진입 후 예외
        result = scheduler.stage_0_dqr("2026-05-04")
        assert result["status"] == "error"
        assert result["critical_alert"] is True

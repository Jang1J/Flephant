"""S2-10 ModeBPerformanceAggregator unit tests."""
from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.mode_b.performance_aggregator import ModeBPerformanceAggregator
from src.utils.pit_guard import PITViolationError

_KST = ZoneInfo("Asia/Seoul")


def _make_agg(tmp_path: Path) -> ModeBPerformanceAggregator:
    log_path = tmp_path / "audit_log.jsonl"
    metrics_dir = tmp_path / "metrics"
    kb_path = tmp_path / "kb" / "history.jsonl"
    return ModeBPerformanceAggregator(
        log_path=log_path,
        metrics_dir=metrics_dir,
        kb_path=kb_path,
    )


def _write_records(log_path: Path, records: list[dict]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _make_quant_record(date: str = "2026-04-18", signal: float = 0.5, label: float = 0.01) -> dict:
    return {
        "ts": f"{date}T10:00:00+09:00",
        "agent": "quant",
        "event_type": "signal",
        "ticker": "005930",
        "reason_code": "NORMAL_APPROVE",
        "signal_score": signal,
        "anomaly_flag": False,
        "label_t5_ret": label,
        "price_t5_snapshot": 75100.0,
        "label_backfilled_at": f"{date}T18:30:00+09:00",
        "label_backfill_source": "mode_b_stage_1_rollup",
    }


def _make_eg_record(date: str = "2026-04-18", fill: float = 75100.0, vwap: float = 75000.0) -> dict:
    return {
        "ts": f"{date}T10:05:00+09:00",
        "agent": "execution_gw",
        "event_type": "fill",
        "ticker": "005930",
        "fill_price": fill,
        "snapshot_vwap": vwap,
        "slippage_bps": round((fill - vwap) / vwap * 10000, 2),
    }


def _make_fda_record(date: str = "2026-04-18", event_type: str = "veto", label: float = -0.015) -> dict:
    return {
        "ts": f"{date}T10:03:00+09:00",
        "agent": "fda",
        "event_type": event_type,
        "ticker": "005930",
        "reason_code": "RISK_FAST_TRIGGER",
        "llm_called": False,
        "label_t5_ret": label,
        "label_backfilled_at": f"{date}T18:30:00+09:00",
        "label_backfill_source": "mode_b_stage_1_rollup",
    }


def _make_llm_record(date: str = "2026-04-18", reason_code: str = "NORMAL_APPROVE") -> dict:
    return {
        "ts": f"{date}T10:01:00+09:00",
        "agent": "news",
        "event_type": "signal",
        "ticker": "005930",
        "reason_code": reason_code,
        "llm_called": True,
        "llm_model": "kanana-o",
    }


# ------------------------------------------------------------------ #
# 1. 기본 집계 (레코드 있음)
# ------------------------------------------------------------------ #

def test_aggregate_basic_structure(tmp_path: Path) -> None:
    """집계 결과 기본 필드 존재 + APM ID 포맷."""
    agg = _make_agg(tmp_path)
    records = [_make_quant_record("2020-01-01"), _make_eg_record("2020-01-01")]
    _write_records(tmp_path / "audit_log.jsonl", records)
    result = agg.aggregate("2020-01-01")
    assert "agent_performance_id" in result
    assert result["agent_performance_id"].startswith("APM-")
    assert result["rollup_date"] == "2020-01-01"
    assert "metrics" in result
    assert "performance_vector_8d" in result
    assert len(result["performance_vector_8d"]) == 8


# ------------------------------------------------------------------ #
# 2. 빈 로그 → None 지표 graceful
# ------------------------------------------------------------------ #

def test_aggregate_empty_log(tmp_path: Path) -> None:
    """레코드 없으면 빈 집계 반환. 에러 없음."""
    agg = _make_agg(tmp_path)
    result = agg.aggregate("2020-01-02")
    assert result["record_count"] == 0
    assert result["metrics"]["prediction_accuracy"] is None
    assert result["metrics"]["slippage_execution_shortfall_bps"] is None


# ------------------------------------------------------------------ #
# 3. prediction_accuracy 계산 검증
# ------------------------------------------------------------------ #

def test_prediction_accuracy_correct(tmp_path: Path) -> None:
    """signal > 0, label > 0 → correct. 4/5 = 0.8."""
    records = [
        _make_quant_record("2020-01-03", signal=0.5, label=0.01),
        _make_quant_record("2020-01-03", signal=0.3, label=0.02),
        _make_quant_record("2020-01-03", signal=0.1, label=0.01),
        _make_quant_record("2020-01-03", signal=0.2, label=0.01),
        _make_quant_record("2020-01-03", signal=0.4, label=-0.01),  # incorrect
    ]
    agg = _make_agg(tmp_path)
    _write_records(tmp_path / "audit_log.jsonl", records)
    result = agg.aggregate("2020-01-03")
    acc = result["metrics"]["prediction_accuracy"]
    assert acc == pytest.approx(0.8, abs=0.01)


def test_label_without_backfill_metadata_excluded(tmp_path: Path) -> None:
    """C18: label이 있어도 backfill metadata 없으면 post-hoc metric에서 제외."""
    record = _make_quant_record("2020-01-03", signal=0.5, label=0.01)
    record["label_backfilled_at"] = None
    record["label_backfill_source"] = None

    agg = _make_agg(tmp_path)
    _write_records(tmp_path / "audit_log.jsonl", [record])

    result = agg.aggregate("2020-01-03")
    assert result["post_hoc_count"] == 0
    assert result["pit_label_violation_count"] == 1
    assert result["metrics"]["prediction_accuracy"] is None


# ------------------------------------------------------------------ #
# 4. slippage 계산 검증
# ------------------------------------------------------------------ #

def test_slippage_calculation(tmp_path: Path) -> None:
    """fill=75100, vwap=75000 → +13.3 bps."""
    records = [_make_eg_record("2020-01-04", fill=75100.0, vwap=75000.0)]
    agg = _make_agg(tmp_path)
    _write_records(tmp_path / "audit_log.jsonl", records)
    result = agg.aggregate("2020-01-04")
    slippage = result["metrics"]["slippage_execution_shortfall_bps"]
    assert slippage is not None
    assert abs(slippage - 13.33) < 1.0


# ------------------------------------------------------------------ #
# 5. veto_precision 계산 검증
# ------------------------------------------------------------------ #

def test_veto_precision(tmp_path: Path) -> None:
    """veto 2건 중 1건 label < -0.005 → precision = 0.5."""
    records = [
        _make_fda_record("2020-01-05", event_type="veto", label=-0.015),  # correct veto
        _make_fda_record("2020-01-05", event_type="veto", label=0.01),   # wrong veto
    ]
    agg = _make_agg(tmp_path)
    _write_records(tmp_path / "audit_log.jsonl", records)
    result = agg.aggregate("2020-01-05")
    vp = result["metrics"]["veto_precision"]
    assert vp == pytest.approx(0.5, abs=0.01)


# ------------------------------------------------------------------ #
# 6. false_positive_rate 계산 검증
# ------------------------------------------------------------------ #

def test_false_positive_rate(tmp_path: Path) -> None:
    """LLM 호출 3건 중 2건이 NORMAL_APPROVE → fpr = 0.667."""
    records = [
        _make_llm_record("2020-01-06", reason_code="NORMAL_APPROVE"),
        _make_llm_record("2020-01-06", reason_code="NORMAL_APPROVE"),
        _make_llm_record("2020-01-06", reason_code="RISK_FAST_TRIGGER"),
    ]
    agg = _make_agg(tmp_path)
    _write_records(tmp_path / "audit_log.jsonl", records)
    result = agg.aggregate("2020-01-06")
    fpr = result["metrics"]["false_positive_event_trigger_rate"]
    assert fpr == pytest.approx(2 / 3, abs=0.01)


# ------------------------------------------------------------------ #
# 7. 저장 파일 생성 확인
# ------------------------------------------------------------------ #

def test_save_creates_files(tmp_path: Path) -> None:
    """집계 후 agent_performance_{date}.json + KB jsonl 생성."""
    agg = _make_agg(tmp_path)
    _write_records(tmp_path / "audit_log.jsonl", [_make_quant_record("2020-01-07")])
    agg.aggregate("2020-01-07")
    metrics_file = tmp_path / "metrics" / "agent_performance_20200107.json"
    kb_file = tmp_path / "kb" / "history.jsonl"
    assert metrics_file.exists()
    assert kb_file.exists()
    # JSON 유효성 확인
    with metrics_file.open() as f:
        data = json.load(f)
    assert data["rollup_date"] == "2020-01-07"


# ------------------------------------------------------------------ #
# 8. performance_vector_8d 길이 및 범위
# ------------------------------------------------------------------ #

def test_performance_vector_8d_shape(tmp_path: Path) -> None:
    """8d 벡터 길이 = 8, 값 범위 [-1, 1]."""
    records = [
        _make_quant_record("2020-01-08", signal=float(i) * 0.1, label=float(i) * 0.005)
        for i in range(10)
    ]
    agg = _make_agg(tmp_path)
    _write_records(tmp_path / "audit_log.jsonl", records)
    result = agg.aggregate("2020-01-08")
    vec = result["performance_vector_8d"]
    assert len(vec) == 8
    for v in vec:
        assert -1.0 <= v <= 1.0, f"벡터 값 {v}가 [-1, 1] 범위 이탈"


# ------------------------------------------------------------------ #
# 9. 날짜 필터링 확인
# ------------------------------------------------------------------ #

def test_date_filter(tmp_path: Path) -> None:
    """2020-01-09 레코드만 집계. 다른 날짜 레코드 제외."""
    records = [
        _make_quant_record("2020-01-09"),
        _make_quant_record("2020-01-10"),  # 다른 날짜
        _make_quant_record("2020-01-10"),
    ]
    agg = _make_agg(tmp_path)
    _write_records(tmp_path / "audit_log.jsonl", records)
    result = agg.aggregate("2020-01-09")
    assert result["record_count"] == 1


# ------------------------------------------------------------------ #
# 10. PIT-Safety: 오늘 날짜 + 18:00 이전 → PITViolationError
# ------------------------------------------------------------------ #

def test_pit_safety_today_before_1800(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """오늘 날짜이고 15:00 KST이면 PITViolationError 발생."""
    from zoneinfo import ZoneInfo as _ZI  # noqa: PLC0415
    from datetime import datetime as _dt  # noqa: PLC0415

    _KST_tz = _ZI("Asia/Seoul")

    # now_kst를 15:00 KST로 mock
    class FakeDT:
        @staticmethod
        def now(tz=None):  # type: ignore[override]
            return _dt(2026, 4, 26, 15, 0, 0, tzinfo=_KST_tz)

    import src.mode_b.performance_aggregator as _mod  # noqa: PLC0415
    monkeypatch.setattr(_mod, "datetime", FakeDT)

    agg = _make_agg(tmp_path)
    with pytest.raises(PITViolationError, match="18:00 KST"):
        agg.aggregate("2026-04-26")

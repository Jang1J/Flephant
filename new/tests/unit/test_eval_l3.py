"""W2 P1 (2026-05-09): L3 reason_code_stats + cause_attribution 단위 테스트.

evaluation_metrics.md SSOT 기준 검증:
  - reason_code_distribution Top-3 coverage 산출 정확성
  - Cause Attribution Accuracy 산출 정확성
  - PIT-Safety: label_t5_ret None entry 는 검증 skip
  - 11 reason_code catalog 인식
"""
from __future__ import annotations

import pytest

from src.eval.cause_attribution import (
    REASON_CODE_VERIFICATION,
    _is_hit,
    compute_cause_attribution,
)
from src.eval.reason_code_stats import REASON_CODE_CATALOG, compute_distribution


# ──────────────────────────────────────────────────────────────────────
# fixtures
# ──────────────────────────────────────────────────────────────────────

def _entry(event_type: str, reason_code: str, label: float | None = None,
           ticker: str = "005930", ts: str = "2026-05-09T09:00:00+09:00",
           backfilled_at: str | None = "2026-05-09T18:30:00+09:00") -> dict:
    """C18 audit_log entry 단축 헬퍼.

    SHIP-fix C-2 (GPT Pro 2026-05-09): label 이 not None 이면 label_backfilled_at 도
    필수 (cause_attribution PIT-Safety 가드). 18:30 KST default 로 PIT 통과.
    """
    e = {
        "ts": ts,
        "decision_id": "DEC-20260509-TEST",
        "agent": "fda",
        "event_type": event_type,
        "ticker": ticker,
        "reason_code": reason_code,
        "label_t5_ret": label,
        "label_backfilled_at": backfilled_at if label is not None else None,
        "label_backfill_source": "synth_audit_log" if label is not None else None,
    }
    return e


# ──────────────────────────────────────────────────────────────────────
# 1. reason_code_stats
# ──────────────────────────────────────────────────────────────────────

def test_reason_code_stats_top3_coverage():
    """Top-3 coverage 산출 정확성."""
    entries = [_entry("approve", "NORMAL_APPROVE")] * 6 + \
              [_entry("veto", "RISK_FAST_TRIGGER")] * 3 + \
              [_entry("veto", "NEWS_DIVERGENCE")] * 1

    result = compute_distribution(entries)
    assert result["total_fda_decisions"] == 10
    # Top-3 = NORMAL_APPROVE(6) + RISK_FAST_TRIGGER(3) + NEWS_DIVERGENCE(1) = 10
    assert result["top3_coverage"] == 1.0
    assert result["top3_coverage_pass"] is True
    assert result["catalog_seen"] == 3


def test_reason_code_stats_filters_non_fda_entries():
    """event_type 이 approve/veto 가 아닌 entry 는 제외."""
    entries = [
        _entry("approve", "NORMAL_APPROVE"),
        _entry("signal", "QUANT_ANOMALY"),  # signal 은 FDA 결정 아님 → 제외
        _entry("order", None),
    ]
    result = compute_distribution(entries)
    assert result["total_fda_decisions"] == 1


def test_reason_code_catalog_completeness():
    """C9 reason_code catalog 모두 발생 시 100%."""
    entries = [_entry("approve" if rc == "NORMAL_APPROVE" else "veto", rc)
               for rc in REASON_CODE_CATALOG]
    result = compute_distribution(entries)
    assert result["catalog_completeness"] == 1.0
    assert result["catalog_seen"] == len(REASON_CODE_CATALOG)


def test_reason_code_stats_empty():
    """빈 entry 리스트 → 0 division 안전."""
    result = compute_distribution([])
    assert result["total_fda_decisions"] == 0
    assert result["top3_coverage"] == 0.0


# ──────────────────────────────────────────────────────────────────────
# 2. cause_attribution
# ──────────────────────────────────────────────────────────────────────

def test_cause_attribution_normal_approve_positive_label():
    """NORMAL_APPROVE: label > 0 → hit."""
    entries = [_entry("approve", "NORMAL_APPROVE", label=0.01)]
    result = compute_cause_attribution(entries)
    assert result["overall"]["hit"] == 1
    assert result["overall"]["miss"] == 0
    assert result["overall"]["accuracy"] == 1.0


def test_cause_attribution_risk_fast_trigger_negative_label():
    """RISK_FAST_TRIGGER: label < 0 → hit (의도대로 위험 회피 성공)."""
    entries = [
        _entry("veto", "RISK_FAST_TRIGGER", label=-0.01),  # hit
        _entry("veto", "RISK_FAST_TRIGGER", label=0.005),  # miss (오히려 상승했음)
    ]
    result = compute_cause_attribution(entries)
    assert result["overall"]["hit"] == 1
    assert result["overall"]["miss"] == 1
    assert result["overall"]["accuracy"] == 0.5


def test_cause_attribution_timeout_skipped():
    """TIMEOUT 은 사후 검증 룰 N/A → skip."""
    entries = [_entry("veto", "TIMEOUT", label=-0.01)]
    result = compute_cause_attribution(entries)
    assert result["overall"]["hit"] == 0
    assert result["overall"]["miss"] == 0
    assert result["overall"]["skip_rule_count"] == 1


def test_cause_attribution_pit_safety_no_label():
    """label_t5_ret None entry → no_label 카운트, hit/miss 미반영 (PIT-Safety)."""
    entries = [_entry("approve", "NORMAL_APPROVE", label=None)]
    result = compute_cause_attribution(entries)
    assert result["overall"]["hit"] == 0
    assert result["overall"]["miss"] == 0
    assert result["overall"]["no_label_count"] == 1


def test_cause_attribution_pit_safety_missing_backfill_metadata():
    """SHIP-fix C-2: label 있어도 label_backfilled_at None 이면 pit_violation 처리."""
    entries = [_entry("approve", "NORMAL_APPROVE", label=0.01, backfilled_at=None)]
    result = compute_cause_attribution(entries)
    assert result["overall"]["hit"] == 0
    assert result["overall"]["miss"] == 0
    assert result["overall"]["pit_violation_count"] == 1


def test_cause_attribution_pit_safety_pre_snapshot_backfill():
    """SHIP-fix C-2: 18:00 KST 이전 backfill → pit_violation 처리 (PIT 위반)."""
    entries = [_entry("approve", "NORMAL_APPROVE", label=0.01,
                      backfilled_at="2026-05-09T15:00:00+09:00")]
    result = compute_cause_attribution(entries)
    assert result["overall"]["hit"] == 0
    assert result["overall"]["miss"] == 0
    assert result["overall"]["pit_violation_count"] == 1


def test_cause_attribution_pit_safety_future_backfill():
    """Codex 권고 2 (2026-05-09): event ts 보다 이전 backfilled_at → 미래 backfill 거부.

    예: event ts=2026-05-10T09:00 + backfilled_at=2026-05-09T18:30 (이전 일자 18:30) →
    이전 구현은 backfilled_at 자기 일자 18:00 만 보고 PIT-safe 처리. 강화 후 거부.
    """
    entries = [_entry("approve", "NORMAL_APPROVE", label=0.01,
                      ts="2026-05-10T09:00:00+09:00",
                      backfilled_at="2026-05-09T18:30:00+09:00")]
    result = compute_cause_attribution(entries)
    assert result["overall"]["hit"] == 0
    assert result["overall"]["miss"] == 0
    assert result["overall"]["pit_violation_count"] == 1


def test_cause_attribution_pit_safety_different_date_backfill():
    """Codex 권고 2: event 일자 != backfill 일자 → 거부 (다음 날 backfill 도 거부)."""
    entries = [_entry("approve", "NORMAL_APPROVE", label=0.01,
                      ts="2026-05-09T10:00:00+09:00",
                      backfilled_at="2026-05-10T18:30:00+09:00")]  # 다음 날 backfill
    result = compute_cause_attribution(entries)
    assert result["overall"]["hit"] == 0
    assert result["overall"]["miss"] == 0
    assert result["overall"]["pit_violation_count"] == 1


def test_cause_attribution_threshold_pass():
    """accuracy >= 0.60 → PASS."""
    # NORMAL_APPROVE 3건 hit + 1건 miss → accuracy 0.75
    entries = [_entry("approve", "NORMAL_APPROVE", label=0.01)] * 3 + \
              [_entry("approve", "NORMAL_APPROVE", label=-0.005)]
    result = compute_cause_attribution(entries)
    assert result["overall"]["accuracy"] == 0.75
    assert result["overall"]["pass"] is True


def test_cause_attribution_threshold_fail():
    """accuracy < 0.60 → FAIL."""
    # 1 hit + 4 miss → accuracy 0.20
    entries = [_entry("approve", "NORMAL_APPROVE", label=0.01)] + \
              [_entry("approve", "NORMAL_APPROVE", label=-0.005)] * 4
    result = compute_cause_attribution(entries)
    assert result["overall"]["accuracy"] == 0.2
    assert result["overall"]["pass"] is False


def test_is_hit_direction_negative():
    """_is_hit 헬퍼: negative direction."""
    assert _is_hit(-0.01, "negative") is True
    assert _is_hit(0.005, "negative") is False
    assert _is_hit(0.0, "negative") is False  # 0 은 hit 아님 (strict <)


def test_is_hit_direction_positive():
    """_is_hit 헬퍼: positive direction."""
    assert _is_hit(0.01, "positive") is True
    assert _is_hit(-0.005, "positive") is False
    assert _is_hit(0.0, "positive") is False


def test_is_hit_skip_when_direction_none():
    """expected_direction None → skip (None 반환)."""
    assert _is_hit(-0.01, None) is None


def test_verification_rules_cover_all_reason_codes():
    """REASON_CODE_VERIFICATION 이 C9 reason_code 모두 정의."""
    for rc in REASON_CODE_CATALOG:
        assert rc in REASON_CODE_VERIFICATION, f"{rc} missing in verification rules"


# ──────────────────────────────────────────────────────────────────────
# 3. 통합 시나리오
# ──────────────────────────────────────────────────────────────────────

def test_integrated_scenario_synthetic():
    """synthetic audit_log 11건 → distribution + attribution 통합 검증.

    구성:
      NORMAL_APPROVE 5 (hit 3, miss 2)
      RISK_FAST_TRIGGER 3 (hit 2, miss 1)
      NEWS_DIVERGENCE 1 (hit 1)
      TIMEOUT 1 (skip rule)
      NORMAL_APPROVE 1 (label None, no_label)
    """
    entries = [
        _entry("approve", "NORMAL_APPROVE", label=0.01),
        _entry("approve", "NORMAL_APPROVE", label=0.005),
        _entry("approve", "NORMAL_APPROVE", label=-0.002),
        _entry("approve", "NORMAL_APPROVE", label=0.003),
        _entry("approve", "NORMAL_APPROVE", label=-0.001),
        _entry("veto", "RISK_FAST_TRIGGER", label=-0.015),
        _entry("veto", "RISK_FAST_TRIGGER", label=-0.008),
        _entry("veto", "RISK_FAST_TRIGGER", label=0.002),
        _entry("veto", "NEWS_DIVERGENCE", label=-0.012),
        _entry("veto", "TIMEOUT", label=-0.005),
        _entry("approve", "NORMAL_APPROVE", label=None),
    ]

    dist = compute_distribution(entries)
    assert dist["total_fda_decisions"] == 11

    attr = compute_cause_attribution(entries)
    o = attr["overall"]
    assert o["hit"] == 6  # NORMAL 3 + RISK 2 + NEWS 1
    assert o["miss"] == 3  # NORMAL 2 + RISK 1
    assert o["skip_rule_count"] == 1  # TIMEOUT
    assert o["no_label_count"] == 1  # 장중 None
    assert o["accuracy"] == pytest.approx(6 / 9)
    assert o["pass"] is True  # 0.667 >= 0.60

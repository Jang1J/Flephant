"""L3 Cause Attribution + reason_code 평가 모듈 (W2 P1, 2026-05-09).

evaluation_metrics.md SSOT 기준:
  - cause_attribution.py: cause_attribution_accuracy (>=0.60 목표)
  - reason_code_stats.py: reason_code_distribution + Top-3 coverage (>=0.80 목표)

audit_log.jsonl (18 필드, C18 SSOT) 입력 → JSON 산출물 출력.
PIT-Safety: label_t5_ret 은 18:00 KST 이후 backfill 된 값만 사용.
"""

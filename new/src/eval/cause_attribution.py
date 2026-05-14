"""W2 P1 (2026-05-09): L3 Cause Attribution Accuracy 산출.

evaluation_metrics.md L155 SSOT:
  cause_attribution_accuracy
    description: FDA reason_code 가 사후 원인과 일치한 비율
    source     : C9 reason_code (장중 기록) + 장마감 bar backfill (18:00 이후)
    period     : 일별 (Mode B 18:00)
    threshold  : system_os_metrics.cause_attribution_accuracy_min: 0.60

핵심 발표 킬러 지표:
  "기존 퀀트는 수익률만 본다. 이 시스템은 FDA가 왜 approve/veto 했는지
   reason_code 로 남기고, 그 reason_code 가 사후 가격 흐름과 맞았는지를
   Cause Attribution Accuracy 로 측정한다."

PIT-Safety (SHIP-fix C-2 강화, GPT Pro 2026-05-09):
  label_t5_ret 만 검증 X. label_backfilled_at 메타도 검증.
  - label_backfilled_at 누락 → PIT 위반 (장중 직접 기입 의심) → no_label 처리
  - label_backfilled_at 시각이 18:00 KST 이전 → PIT 위반 → no_label 처리
  - label_backfilled_at 시각이 18:00 KST 이후 → 정상 backfill, 검증 가능
  장중 데이터로 사후 일치 판단 시도 시 무시.

reason_code 별 사후 검증 룰 (의도 vs 실제):
  RISK_FAST_TRIGGER  → veto 후 5분 수익률 하락 (label_t5_ret < 0)
  NEWS_DIVERGENCE    → veto 후 5분 수익률 하락 (label_t5_ret < 0)
  DEBATE_CONFLICT    → veto 후 5분 변동성 / 수익률 하락 (label_t5_ret < 0)
  QUANT_ANOMALY      → veto 후 5분 수익률 하락 (label_t5_ret < 0)
  TIMEOUT            → veto. 사후 검증 N/A (의도 자체가 안전 fallback) → skip
  MISSING_PORTFOLIO_PATCH → veto. 시스템 에러. → skip
  NORMAL_APPROVE     → approve 후 5분 수익률 양수 (label_t5_ret > 0)

Usage:
  python -m src.eval.cause_attribution [--audit-log PATH] [--out-dir PATH] [--date YYYYMMDD]

Output:
  artifacts/metrics/cause_attribution_YYYYMMDD.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_NEW_ROOT = Path(__file__).resolve().parents[2]
if str(_NEW_ROOT) not in sys.path:
    sys.path.insert(0, str(_NEW_ROOT))

from src.eval.reason_code_stats import REASON_CODE_CATALOG, _filter_by_date, _read_audit_log
from src.utils.config_loader import load as config_load
from src.utils.label_meta import is_label_backfill_pit_safe
from src.utils.logger import get_logger

logger = get_logger("cause_attribution")
_KST = ZoneInfo("Asia/Seoul")

_PROJECT_ROOT = _NEW_ROOT.parent
_DEFAULT_AUDIT_LOG = _PROJECT_ROOT / "artifacts" / "audit_log.jsonl"
_DEFAULT_OUT_DIR = _PROJECT_ROOT / "artifacts" / "metrics"

# reason_code 별 사후 검증 룰
# expected_direction: "negative" = label_t5_ret < 0 일 때 hit
#                     "positive" = label_t5_ret > 0 일 때 hit
#                     None       = 검증 skip (의도 자체가 안전 fallback or 시스템 에러)
REASON_CODE_VERIFICATION: dict[str, dict[str, Any]] = {
    "RISK_FAST_TRIGGER":      {"event_type": "veto",    "expected_direction": "negative"},
    "NEWS_DIVERGENCE":        {"event_type": "veto",    "expected_direction": "negative"},
    "DEBATE_CONFLICT":        {"event_type": "veto",    "expected_direction": "negative"},
    "QUANT_ANOMALY":          {"event_type": "veto",    "expected_direction": "negative"},
    "NORMAL_APPROVE":         {"event_type": "approve", "expected_direction": "positive"},
    "TIMEOUT":                {"event_type": "veto",    "expected_direction": None},
    "MISSING_PORTFOLIO_PATCH":{"event_type": "veto",    "expected_direction": None},
}


def _is_hit(label_t5_ret: float, expected_direction: str | None) -> bool | None:
    """reason_code 의도 vs 실제 방향 일치 여부.

    Returns:
        True  : 의도 방향과 사후 일치
        False : 의도 방향과 사후 불일치
        None  : 검증 skip (expected_direction None) 또는 label 없음
    """
    if expected_direction is None:
        return None
    if expected_direction == "negative":
        return label_t5_ret < 0
    if expected_direction == "positive":
        return label_t5_ret > 0
    return None


def _is_label_pit_safe(entry: dict[str, Any]) -> bool:
    """label backfill 메타 PIT-Safety 검증 (SHIP-fix C-2 + Codex 권고 2 강화).

    True 조건 (모두 만족):
        - label_backfilled_at 필드 존재 (장중 직접 기입 X)
        - backfill 시각이 event ts 와 동일 일자
        - backfill 시각이 그 일자 18:00 KST 이후 (snapshot_hour SSOT)

    False 조건 (PIT 의심 → pit_violation 처리):
        - label_backfilled_at 누락
        - event ts 누락 또는 파싱 실패
        - backfill 시각 < event ts (이전에 backfill = 미래 정보 사용)
        - backfill 시각이 event 일자의 18:00 KST 이전
        - backfill 시각이 event ts 와 다른 일자 (예: 2026-05-09 event 가 2026-05-10 backfill 로 PIT-safe 처리되는 사례 차단)
        - 파싱 실패

    Codex 권고 2 (2026-05-09): 이전 구현은 backfill 자기 일자 18:00 만 봐서
    `event_ts=2026-05-10T09:00, backfilled_at=2026-05-09T18:30` 같은 미래 backfill 도 통과.
    수정: event ts 와 backfilled_at 동일 일자 + ts <= backfilled_at 모두 검증.
    """
    return is_label_backfill_pit_safe(entry)


def compute_cause_attribution(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """reason_code 별 사후 일치율 + 전체 Cause Attribution Accuracy 산출.

    PIT-Safety (SHIP-fix C-2 강화):
        label_t5_ret 만 검증 X. label_backfilled_at 메타도 검증.
        backfill 시각이 18:00 KST 이전이면 no_label 처리 (PIT 위반 방지).
    """
    # reason_code 별 stats: {hit, miss, skip(verification rule N/A), nolabel, pit_violation}
    by_code: dict[str, dict[str, int]] = defaultdict(
        lambda: {"hit": 0, "miss": 0, "skip_rule": 0, "no_label": 0,
                 "pit_violation": 0, "total_logged": 0}
    )

    overall_hit = 0
    overall_miss = 0
    overall_skip_rule = 0
    overall_no_label = 0
    overall_pit_violation = 0

    for e in entries:
        et = e.get("event_type")
        if et not in ("approve", "veto"):
            continue

        rc = e.get("reason_code")
        if not rc:
            continue
        rc_str = str(rc)

        by_code[rc_str]["total_logged"] += 1

        rule = REASON_CODE_VERIFICATION.get(rc_str)
        if rule is None:
            # 알려지지 않은 reason_code → skip + 경고
            by_code[rc_str]["skip_rule"] += 1
            overall_skip_rule += 1
            logger.warning("[cause_attribution] unknown reason_code: %s", rc_str)
            continue

        expected = rule["expected_direction"]
        if expected is None:
            by_code[rc_str]["skip_rule"] += 1
            overall_skip_rule += 1
            continue

        label = e.get("label_t5_ret")
        if label is None:
            by_code[rc_str]["no_label"] += 1
            overall_no_label += 1
            continue

        # SHIP-fix C-2: PIT-Safety 검증 (label_backfilled_at 메타 필수)
        if not _is_label_pit_safe(e):
            by_code[rc_str]["pit_violation"] += 1
            overall_pit_violation += 1
            logger.warning(
                "[cause_attribution] PIT 위반 의심: decision_id=%s reason_code=%s "
                "label_backfilled_at=%s (장중 직접 기입 또는 18:00 KST 이전 backfill)",
                e.get("decision_id"), rc_str, e.get("label_backfilled_at")
            )
            continue

        try:
            label_f = float(label)
        except (TypeError, ValueError):
            by_code[rc_str]["no_label"] += 1
            overall_no_label += 1
            continue

        hit = _is_hit(label_f, expected)
        if hit is True:
            by_code[rc_str]["hit"] += 1
            overall_hit += 1
        elif hit is False:
            by_code[rc_str]["miss"] += 1
            overall_miss += 1

    overall_verified = overall_hit + overall_miss
    overall_accuracy = (overall_hit / overall_verified) if overall_verified > 0 else 0.0

    # reason_code 별 accuracy
    by_code_accuracy: dict[str, dict[str, Any]] = {}
    for rc, stats in by_code.items():
        verified = stats["hit"] + stats["miss"]
        accuracy = (stats["hit"] / verified) if verified > 0 else None
        by_code_accuracy[rc] = {
            "total_logged": stats["total_logged"],
            "verified": verified,
            "hit": stats["hit"],
            "miss": stats["miss"],
            "skip_rule": stats["skip_rule"],
            "no_label": stats["no_label"],
            "accuracy": accuracy,
            "rule": REASON_CODE_VERIFICATION.get(rc, {}),
        }

    # 임계값
    cfg = config_load("risk_config.yaml", "system_os_metrics") or {}
    threshold = float(cfg.get("cause_attribution_accuracy_min", 0.60))

    return {
        "overall": {
            "verified_count": overall_verified,
            "hit": overall_hit,
            "miss": overall_miss,
            "skip_rule_count": overall_skip_rule,
            "no_label_count": overall_no_label,
            "pit_violation_count": overall_pit_violation,
            "accuracy": overall_accuracy,
            "threshold": threshold,
            "pass": overall_accuracy >= threshold,
        },
        "by_reason_code": by_code_accuracy,
        "verification_rules": REASON_CODE_VERIFICATION,
        "catalog": list(REASON_CODE_CATALOG),
    }


def _save_json(out_dir: Path, date_str: str, result: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"cause_attribution_{date_str}.json"
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    return out_path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="L3 Cause Attribution Accuracy 일별 산출 (발표 킬러 지표)",
    )
    parser.add_argument("--audit-log", type=Path, default=_DEFAULT_AUDIT_LOG)
    parser.add_argument("--out-dir", type=Path, default=_DEFAULT_OUT_DIR)
    parser.add_argument("--date", type=str, default=None,
                        help="대상 일자 YYYYMMDD (생략 시 전체)")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    date_str = args.date or datetime.now(_KST).strftime("%Y%m%d")

    logger.info("[cause_attribution] 시작. audit_log=%s date=%s", args.audit_log, date_str)

    entries = _read_audit_log(args.audit_log)
    filtered = _filter_by_date(entries, args.date)
    result = compute_cause_attribution(filtered)
    result["meta"] = {
        "date": date_str,
        "audit_log_path": str(args.audit_log),
        "generated_at": datetime.now(_KST).isoformat(),
    }

    out_path = _save_json(args.out_dir, date_str, result)

    overall = result["overall"]
    print(f"\n[cause_attribution] 결과 ({date_str})")
    print(f"  검증 가능 합계 : {overall['verified_count']}")
    print(f"  hit / miss     : {overall['hit']} / {overall['miss']}")
    print(f"  skip (rule)    : {overall['skip_rule_count']} (TIMEOUT / MISSING_PATCH)")
    print(f"  no_label       : {overall['no_label_count']} (label 미 backfill)")
    print(f"  PIT 위반 skip  : {overall.get('pit_violation_count', 0)} (장중 직접 기입 또는 18:00 KST 이전 backfill)")
    print(f"  Accuracy       : {overall['accuracy']:.3f} (threshold {overall['threshold']:.2f})")
    print(f"  PASS           : {overall['pass']}")
    print()
    print(f"  reason_code 별 accuracy:")
    for rc in REASON_CODE_CATALOG:
        if rc in result["by_reason_code"]:
            s = result["by_reason_code"][rc]
            acc_str = f"{s['accuracy']:.3f}" if s['accuracy'] is not None else "  N/A"
            print(f"    {rc:28s}  hit={s['hit']:4d} miss={s['miss']:4d} acc={acc_str} (verified {s['verified']})")
    print(f"  산출: {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

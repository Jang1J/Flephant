"""W2 P1 (2026-05-09): FDA reason_code 분포 + Top-3 coverage 집계.

evaluation_metrics.md L160 SSOT:
  reason_code_distribution
    description: FDA reason_code 분포 + 상위 3 coverage
    source     : audit_log.jsonl 집계
    period     : 일별
    threshold  : system_os_metrics.reason_code_top3_coverage_min: 0.80

C9 reason_code 7종 (api_contracts.md SSOT):
  NORMAL_APPROVE / TIMEOUT / RISK_FAST_TRIGGER / DEBATE_CONFLICT
  NEWS_DIVERGENCE / QUANT_ANOMALY / MISSING_PORTFOLIO_PATCH

Usage:
  python -m src.eval.reason_code_stats [--audit-log PATH] [--out-dir PATH] [--date YYYYMMDD]

Output:
  artifacts/metrics/reason_code_stats_YYYYMMDD.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_NEW_ROOT = Path(__file__).resolve().parents[2]
if str(_NEW_ROOT) not in sys.path:
    sys.path.insert(0, str(_NEW_ROOT))

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger

logger = get_logger("reason_code_stats")
_KST = ZoneInfo("Asia/Seoul")

_PROJECT_ROOT = _NEW_ROOT.parent
_DEFAULT_AUDIT_LOG = _PROJECT_ROOT / "artifacts" / "audit_log.jsonl"
_DEFAULT_OUT_DIR = _PROJECT_ROOT / "artifacts" / "metrics"

# C9 reason_code 7종 SSOT (api_contracts.md C9 + fda.py:14-15)
REASON_CODE_CATALOG: tuple[str, ...] = (
    "NORMAL_APPROVE",
    "TIMEOUT",
    "RISK_FAST_TRIGGER",
    "DEBATE_CONFLICT",
    "NEWS_DIVERGENCE",
    "QUANT_ANOMALY",
    "MISSING_PORTFOLIO_PATCH",
)


def _read_audit_log(path: Path) -> list[dict[str, Any]]:
    """JSON Lines audit_log 파일 읽기. 파일 없거나 비어있으면 빈 list."""
    if not path.exists():
        logger.warning("[reason_code_stats] audit_log 파일 없음: %s", path)
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entries.append(json.loads(raw))
            except json.JSONDecodeError as e:
                logger.warning("[reason_code_stats] L%d 파싱 실패: %s", line_no, e)
    return entries


def _filter_by_date(entries: list[dict[str, Any]], date_str: str | None) -> list[dict[str, Any]]:
    """date_str (YYYYMMDD) 일별 필터. None 이면 전체."""
    if not date_str:
        return entries
    target = datetime.strptime(date_str, "%Y%m%d").date()
    out: list[dict[str, Any]] = []
    for e in entries:
        ts_raw = e.get("ts")
        if not ts_raw:
            continue
        try:
            dt = datetime.fromisoformat(str(ts_raw))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_KST)
            if dt.astimezone(_KST).date() == target:
                out.append(e)
        except (ValueError, TypeError):
            continue
    return out


def _filter_fda_decisions(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """FDA approve/veto 만 필터 (event_type in {approve, veto})."""
    return [e for e in entries if e.get("event_type") in ("approve", "veto")]


def compute_distribution(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """reason_code 분포 + Top-3 coverage 산출.

    Args:
        entries: audit_log entries (FDA decisions filtered).
    Returns:
        dict with: total / counter / top3 / top3_coverage / catalog_completeness
    """
    fda_entries = _filter_fda_decisions(entries)
    total = len(fda_entries)

    counter_obj: Counter[str] = Counter()
    for e in fda_entries:
        rc = e.get("reason_code")
        if rc:
            counter_obj[str(rc)] += 1

    counter_dict = dict(counter_obj.most_common())
    top3 = counter_obj.most_common(3)
    top3_count = sum(c for _, c in top3)
    top3_coverage = (top3_count / total) if total > 0 else 0.0

    # catalog 완전성: 7종 중 발생한 코드 수
    catalog_seen = sum(1 for code in REASON_CODE_CATALOG if code in counter_obj)
    catalog_completeness = catalog_seen / len(REASON_CODE_CATALOG)

    # 임계값 (risk_config.yaml SSOT, 없으면 기본값 0.80)
    cfg = config_load("risk_config.yaml", "system_os_metrics") or {}
    top3_threshold = float(cfg.get("reason_code_top3_coverage_min", 0.80))

    return {
        "total_fda_decisions": total,
        "reason_code_counter": counter_dict,
        "top3": [{"reason_code": rc, "count": c, "share": c / total if total > 0 else 0.0}
                 for rc, c in top3],
        "top3_coverage": top3_coverage,
        "top3_coverage_threshold": top3_threshold,
        "top3_coverage_pass": top3_coverage >= top3_threshold,
        "catalog_completeness": catalog_completeness,
        "catalog_seen": catalog_seen,
        "catalog_total": len(REASON_CODE_CATALOG),
        "catalog_codes_seen": [code for code in REASON_CODE_CATALOG if code in counter_obj],
        "catalog_codes_missing": [code for code in REASON_CODE_CATALOG if code not in counter_obj],
    }


def _save_json(out_dir: Path, date_str: str, result: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"reason_code_stats_{date_str}.json"
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    return out_path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="reason_code 분포 + Top-3 coverage 일별 산출 (L3 평가 지표)",
    )
    parser.add_argument(
        "--audit-log",
        type=Path,
        default=_DEFAULT_AUDIT_LOG,
        help=f"audit_log.jsonl 경로 (기본: {_DEFAULT_AUDIT_LOG})",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_DEFAULT_OUT_DIR,
        help=f"산출 디렉토리 (기본: {_DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="대상 일자 YYYYMMDD (생략 시 전체 entry)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    date_str = args.date or datetime.now(_KST).strftime("%Y%m%d")

    logger.info("[reason_code_stats] 시작. audit_log=%s date=%s", args.audit_log, date_str)

    entries = _read_audit_log(args.audit_log)
    if not entries:
        logger.warning("[reason_code_stats] entry 0건. 빈 결과 산출")

    filtered = _filter_by_date(entries, args.date)
    result = compute_distribution(filtered)
    result["meta"] = {
        "date": date_str,
        "audit_log_path": str(args.audit_log),
        "generated_at": datetime.now(_KST).isoformat(),
    }

    out_path = _save_json(args.out_dir, date_str, result)

    print(f"\n[reason_code_stats] 결과 ({date_str})")
    print(f"  FDA 결정 합계 : {result['total_fda_decisions']}")
    print(f"  Top-3 coverage : {result['top3_coverage']:.3f} (threshold {result['top3_coverage_threshold']:.2f})")
    print(f"  catalog 발생률 : {result['catalog_completeness']:.3f} ({result['catalog_seen']}/{result['catalog_total']})")
    if result["top3"]:
        print("  Top-3:")
        for item in result["top3"]:
            print(f"    {item['reason_code']:30s}  {item['count']:5d}  ({item['share']:.3f})")
    print(f"  산출: {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

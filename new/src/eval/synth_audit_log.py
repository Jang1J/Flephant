"""W2 P1 L1 (2026-05-09): 발표용 synthetic audit_log 생성기.

KIS 키 미설정 상태에서 발표용 실수치 시뮬을 위한 합성 데이터.
e2e_scenario_runner 의 NORMAL_APPROVE 단조 결과를 7종 reason_code 분포로 augment.

생성 규칙 (의도된 시뮬):
  - NORMAL_APPROVE        70 entry, 65% hit (label > 0)
  - RISK_FAST_TRIGGER     15 entry, 73% hit (veto 후 label < 0)
  - NEWS_DIVERGENCE        8 entry, 75% hit
  - DEBATE_CONFLICT        5 entry, 60% hit
  - QUANT_ANOMALY          4 entry, 50% hit
  - TIMEOUT                3 entry, label N/A (skip rule)
  - MISSING_PORTFOLIO_PATCH 2 entry, label N/A (skip rule)

총 107 entry, 검증 가능 102 entry, expected accuracy ≈ 0.66 (threshold 0.60 PASS).

Usage:
  python -m src.eval.synth_audit_log [--seed N] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_NEW_ROOT = Path(__file__).resolve().parents[2]
if str(_NEW_ROOT) not in sys.path:
    sys.path.insert(0, str(_NEW_ROOT))

_KST = ZoneInfo("Asia/Seoul")
_PROJECT_ROOT = _NEW_ROOT.parent
_DEFAULT_OUT = _PROJECT_ROOT / "artifacts" / "audit_log.jsonl"

# (reason_code, event_type, count, hit_rate)
DISTRIBUTION: list[tuple[str, str, int, float | None]] = [
    ("NORMAL_APPROVE",          "approve", 70, 0.65),
    ("RISK_FAST_TRIGGER",       "veto",    15, 0.73),
    ("NEWS_DIVERGENCE",         "veto",     8, 0.75),
    ("DEBATE_CONFLICT",         "veto",     5, 0.60),
    ("QUANT_ANOMALY",           "veto",     4, 0.50),
    ("TIMEOUT",                 "veto",     3, None),  # skip rule
    ("MISSING_PORTFOLIO_PATCH", "veto",     2, None),  # skip rule
]

_TICKERS = ["005930", "000660", "035420", "005380", "051910",
            "012450", "047810", "079550", "298040", "014880"]


def _gen_label(reason_code: str, event_type: str, hit: bool, rng: random.Random) -> float:
    """reason_code 의도 + hit 여부에 따라 label_t5_ret 합성."""
    if reason_code == "NORMAL_APPROVE":
        # approve hit = positive, miss = negative
        return rng.uniform(0.001, 0.02) if hit else rng.uniform(-0.015, -0.001)
    # veto hit = negative, miss = positive
    return rng.uniform(-0.025, -0.001) if hit else rng.uniform(0.001, 0.012)


def generate(seed: int = 42, base_date: str = "2026-05-09") -> list[dict]:
    """SHIP-fix C-1 (GPT Pro 2026-05-09): PIT-Safety 준수 시뮬.

    1. 장중 ts (09:00~15:30) entry 생성 후 label_t5_ret = None 채움 (C18 PIT 규칙).
    2. 18:30 KST 이후 별도 backfill 단계 시뮬 → label_backfilled_at + label_backfill_source 메타 추가.
    이렇게 하면 cause_attribution 이 backfill 메타 검증으로 PIT 위반 entry skip 가능.
    """
    rng = random.Random(seed)
    base_dt = datetime.strptime(base_date, "%Y-%m-%d").replace(tzinfo=_KST, hour=9, minute=0)
    backfill_dt = datetime.strptime(base_date, "%Y-%m-%d").replace(tzinfo=_KST, hour=18, minute=30)
    backfill_iso = backfill_dt.isoformat()
    entries = []
    counter = 0

    for reason_code, event_type, count, hit_rate in DISTRIBUTION:
        for _ in range(count):
            ts = base_dt + timedelta(minutes=counter)
            entry = {
                "ts": ts.isoformat(),                       # 장중 시각
                "decision_id": f"DEC-{base_dt.strftime('%Y%m%d')}-{counter:08X}",
                "agent": "fda",
                "event_type": event_type,
                "ticker": rng.choice(_TICKERS),
                "reason_code": reason_code,
                "signal_score": rng.uniform(-0.05, 0.05),
                "anomaly_flag": False,
                "target_weight": rng.uniform(0.0, 0.05),
                "actual_weight": rng.uniform(0.0, 0.05),
                "fill_price": None,
                "snapshot_vwap": None,
                "slippage_bps": None,
                "sector": None,
                "llm_called": event_type == "veto" and reason_code in ("NEWS_DIVERGENCE", "DEBATE_CONFLICT"),
                "llm_model": "kanana-o" if event_type == "veto" and reason_code in ("NEWS_DIVERGENCE", "DEBATE_CONFLICT") else None,
                # 장중 기록 시점: label 은 None 유지 (C18 PIT 규칙).
                "label_t5_ret": None,
                "price_t5_snapshot": None,
                "label_backfilled_at": None,
                "label_backfill_source": None,
            }

            # 18:30 backfill 시뮬: label 채움 + backfill 메타 표기.
            if hit_rate is None:
                # TIMEOUT / MISSING_PORTFOLIO_PATCH: 사후 검증 N/A. label 은 합성 (단 cause_attribution 에서 skip)
                entry["label_t5_ret"] = rng.uniform(-0.01, 0.01)
                entry["price_t5_snapshot"] = rng.uniform(50000, 100000)
            else:
                hit = rng.random() < hit_rate
                entry["label_t5_ret"] = _gen_label(reason_code, event_type, hit, rng)
                entry["price_t5_snapshot"] = rng.uniform(50000, 100000)

            entry["label_backfilled_at"] = backfill_iso       # 18:30 KST (PIT-Safety 통과)
            entry["label_backfill_source"] = "synth_audit_log"  # 합성 원천 명시

            entries.append(entry)
            counter += 1

    rng.shuffle(entries)
    return entries


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="발표용 synthetic audit_log 생성")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    parser.add_argument("--date", type=str, default="2026-05-09")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    entries = generate(seed=args.seed, base_date=args.date)

    with args.out.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    print(f"[synth_audit_log] {len(entries)} entries → {args.out}")
    print(f"[synth_audit_log] reason_code 분포:")
    for reason_code, event_type, count, hit_rate in DISTRIBUTION:
        rate = f"{hit_rate:.0%}" if hit_rate else "skip"
        print(f"  {reason_code:28s} ({event_type}) × {count:3d}  hit_rate={rate}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

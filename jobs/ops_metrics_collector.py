"""
Ops Metrics Collector — 일일 운영 메트릭 수집기

7종 메트릭:
1. order_success_rate: 주문 성공률 (PTL 기반)
2. order_reject_rate: 주문 거부율
3. llm_fallback_rate: Kanana→GPT-4o 전환 비율 (FDC fallback_used 기반)
4. daily_turnover: 일일 회전율 (PFS daily_turnover)
5. cash_ratio_drift: 현금 비율 변화 (전일 PFS vs 오늘 PFS)
6. reconciliation_mismatch_rate: PFS↔PTL 불일치율 (BRR 기반)
7. emergency_veto_count: FDA 긴급 거부 건수 (FDC veto 중 regime=red 건)

추가 메트릭:
- p50_latency_sec: E2E 소요 시간 중앙값
- p95_latency_sec: E2E 소요 시간 95th percentile
"""

import json
import sys
import yaml
from datetime import datetime, timezone, timedelta
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

OPM_DIR = _BASE_DIR / "artifacts" / "ops_metrics"
OPM_DIR.mkdir(parents=True, exist_ok=True)

REPORTS_DIR = _BASE_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

FDC_DIR = _BASE_DIR / "artifacts" / "final_decision_card"
PFS_DIR = _BASE_DIR / "artifacts" / "portfolio_state"
PTL_DIR = _BASE_DIR / "artifacts" / "paper_trading_log"
BRR_DIR = _BASE_DIR / "artifacts" / "broker_reconcile_report"
RC_DIR = _BASE_DIR / "artifacts" / "risk_card"

KST = timezone(timedelta(hours=9))

_POLICY_PATH = _BASE_DIR / "config" / "risk_policy_v0.yaml"


def _load_warn_thresholds() -> dict:
    """risk_policy_v0.yaml의 ops_warn_thresholds 섹션 로드."""
    try:
        with open(_POLICY_PATH, encoding="utf-8") as f:
            policy = yaml.safe_load(f)
        thresholds = policy.get("ops_warn_thresholds", {})
        return {
            "turnover_warn_pct": float(thresholds.get("turnover_warn_pct", 25.0)),
            "cash_drift_warn_pct": float(thresholds.get("cash_drift_warn_pct", 10.0)),
            "veto_warn_count": int(thresholds.get("veto_warn_count", 0)),
            "llm_fallback_warn_rate": float(thresholds.get("llm_fallback_warn_rate", 0.5)),
            "turnover_hard_limit": float(
                policy.get("core_rules", {}).get("turnover_cap", {}).get("daily_max", 30.0)
            ),
        }
    except Exception as e:
        print(f"[OpsMetrics] WARN 임계치 로드 실패, 기본값 사용: {e}")
        return {
            "turnover_warn_pct": 25.0,
            "cash_drift_warn_pct": 10.0,
            "veto_warn_count": 0,
            "llm_fallback_warn_rate": 0.5,
            "turnover_hard_limit": 30.0,
        }


def _load_json(path: Path) -> dict | None:
    """JSON 파일 로드. 없으면 None 반환."""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[OpsMetrics] 파일 로드 실패 {path}: {e}")
        return None


def _load_pfs(target_date: str) -> dict | None:
    """PFS-{date}-*.json 로드 (가장 최신)"""
    candidates = sorted(PFS_DIR.glob(f"PFS-{target_date}-*.json"))
    if not candidates:
        return None
    return _load_json(candidates[-1])


def _load_prev_pfs(target_date: str) -> dict | None:
    """target_date 이전의 가장 최근 PFS 로드"""
    all_pfs = sorted(PFS_DIR.glob("PFS-*.json"))
    for p in reversed(all_pfs):
        date_str = p.stem.split("-")[1]  # PFS-YYYYMMDD-HHMMSS → YYYYMMDD
        if date_str < target_date:
            return _load_json(p)
    return None


def _load_fdc(target_date: str) -> dict | None:
    """FDC-{date}.json 또는 FDC-{date}-*.json 로드"""
    path = FDC_DIR / f"FDC-{target_date}.json"
    if path.exists():
        return _load_json(path)
    candidates = sorted(FDC_DIR.glob(f"FDC-{target_date}-*.json"))
    if candidates:
        return _load_json(candidates[-1])
    return None


def _load_ptl(target_date: str) -> dict | None:
    """PTL-{date}.json 로드"""
    return _load_json(PTL_DIR / f"PTL-{target_date}.json")


def _load_brr(target_date: str) -> dict | None:
    """BRR-{date}.json 로드"""
    return _load_json(BRR_DIR / f"BRR-{target_date}.json")


def _load_rc(target_date: str) -> dict | None:
    """RC-{date}.json 로드"""
    return _load_json(RC_DIR / f"RC-{target_date}.json")


def collect_ops_metrics(target_date: str, latency_samples: list | None = None) -> dict:
    """
    해당 날짜의 아티팩트(PFS, FDC, PTL, BRR, RC)를 읽어
    운영 메트릭을 산출한다.

    Args:
        target_date: YYYYMMDD 형식 날짜
        latency_samples: E2E 소요 시간 샘플 리스트 (초 단위). None이면 latency null.

    Returns:
        OpsMetricsDaily dict (저장 후 반환)
    """
    print(f"[OpsMetrics] 메트릭 수집 시작: {target_date}")

    ptl = _load_ptl(target_date)
    fdc = _load_fdc(target_date)
    pfs = _load_pfs(target_date)
    prev_pfs = _load_prev_pfs(target_date)
    brr = _load_brr(target_date)
    rc = _load_rc(target_date)

    artifacts_used = {}

    # 1. order_success_rate
    order_success_rate = None
    order_reject_rate = None
    if ptl is not None:
        artifacts_used["ptl"] = ptl.get("log_id")
        orders = ptl.get("orders", [])
        total = len(orders)
        if total > 0:
            success = sum(
                1 for o in orders if o.get("status") in ("executed", "dry_run")
            )
            rejected = sum(1 for o in orders if o.get("status") == "rejected")
            order_success_rate = round(success / total, 4)
            order_reject_rate = round(rejected / total, 4)
            print(f"[OpsMetrics] 주문 성공률: {order_success_rate:.2%}, 거부율: {order_reject_rate:.2%}")
        else:
            order_success_rate = None
            order_reject_rate = None
            print("[OpsMetrics] PTL 주문 없음")
    else:
        print(f"[OpsMetrics] PTL 없음: PTL-{target_date}.json")

    # 2. llm_fallback_rate
    llm_fallback_rate = None
    if fdc is not None:
        artifacts_used["fdc"] = fdc.get("decision_id")
        # fallback_used는 boolean 단일값 (FDC 전체에 대한 플래그)
        fallback_used = fdc.get("fallback_used")
        if fallback_used is not None:
            llm_fallback_rate = 1.0 if fallback_used else 0.0
        print(f"[OpsMetrics] LLM fallback: {llm_fallback_rate}")
    else:
        print(f"[OpsMetrics] FDC 없음: FDC-{target_date}.json")

    # 3. daily_turnover
    daily_turnover = None
    if pfs is not None:
        artifacts_used["pfs"] = pfs.get("state_id")
        daily_turnover = pfs.get("daily_turnover")
        print(f"[OpsMetrics] 일일 회전율: {daily_turnover}%")
    else:
        print(f"[OpsMetrics] PFS 없음: PFS-{target_date}-*.json")

    # 4. cash_ratio_drift
    cash_ratio_drift = None
    if pfs is not None and prev_pfs is not None:
        artifacts_used["prev_pfs"] = prev_pfs.get("state_id")
        today_cash = pfs.get("cash_ratio", 0.0)
        prev_cash = prev_pfs.get("cash_ratio", 0.0)
        cash_ratio_drift = round(today_cash - prev_cash, 4)
        print(f"[OpsMetrics] 현금 비율 변화: {cash_ratio_drift:+.2f}%p (전일 {prev_cash:.1f}% → 오늘 {today_cash:.1f}%)")
    elif pfs is not None:
        print("[OpsMetrics] 전일 PFS 없음, cash_ratio_drift null")

    # 5. reconciliation_mismatch_rate
    reconciliation_mismatch_rate = None
    if brr is not None:
        artifacts_used["brr"] = brr.get("report_id")
        pfs_positions = brr.get("pfs_positions", 0)
        mismatches = len(brr.get("mismatches", []))
        if pfs_positions > 0:
            reconciliation_mismatch_rate = round(mismatches / pfs_positions, 4)
        else:
            reconciliation_mismatch_rate = 0.0
        print(f"[OpsMetrics] 대조 불일치율: {reconciliation_mismatch_rate:.2%} ({mismatches}/{pfs_positions})")
    else:
        print(f"[OpsMetrics] BRR 없음: BRR-{target_date}.json")

    # 6. emergency_veto_count
    # regime이 red인 종목에 대한 veto 건수
    emergency_veto_count = None
    if fdc is not None and rc is not None:
        artifacts_used["rc"] = rc.get("risk_id")
        regime = rc.get("regime", {})
        regime_label = regime.get("label", "") if isinstance(regime, dict) else ""
        veto_count = sum(
            1 for d in fdc.get("decisions", [])
            if d.get("decision") == "veto"
        )
        # regime=red 상태에서의 veto는 전부 긴급으로 간주
        if regime_label == "red":
            emergency_veto_count = veto_count
        else:
            emergency_veto_count = 0
        print(f"[OpsMetrics] 긴급 veto: {emergency_veto_count}건 (regime={regime_label})")
    elif fdc is not None:
        # RC 없으면 regime 판단 불가 → null
        emergency_veto_count = None
        print("[OpsMetrics] RC 없음, emergency_veto_count=null (regime 판단 불가)")

    # 7. latency
    p50_latency_sec = None
    p95_latency_sec = None
    if latency_samples and len(latency_samples) > 0:
        import statistics
        sorted_samples = sorted(latency_samples)
        n = len(sorted_samples)
        p50_latency_sec = round(statistics.median(sorted_samples), 3)
        # 샘플 1개일 때 p50=p95=해당 값; 2개 이상이면 floor index 사용
        idx_95 = min(n - 1, int(n * 0.95))
        p95_latency_sec = round(sorted_samples[idx_95], 3)
        print(f"[OpsMetrics] latency p50={p50_latency_sec}s, p95={p95_latency_sec}s")

    metrics = {
        "order_success_rate": order_success_rate,
        "order_reject_rate": order_reject_rate,
        "llm_fallback_rate": llm_fallback_rate,
        "daily_turnover": daily_turnover,
        "cash_ratio_drift": cash_ratio_drift,
        "reconciliation_mismatch_rate": reconciliation_mismatch_rate,
        "emergency_veto_count": emergency_veto_count,
        "p50_latency_sec": p50_latency_sec,
        "p95_latency_sec": p95_latency_sec,
    }

    opm = {
        "metrics_id": f"OPM-{target_date}",
        "target_date": target_date,
        "generated_at": datetime.now(KST).isoformat(),
        "metrics": metrics,
        "artifacts_used": artifacts_used,
        "note": None,
    }

    opm_path = OPM_DIR / f"OPM-{target_date}.json"
    with open(opm_path, "w", encoding="utf-8") as f:
        json.dump(opm, f, ensure_ascii=False, indent=2)
    print(f"[OpsMetrics] 메트릭 저장 완료: {opm_path}")

    return opm


def generate_ops_report(
    target_date: str,
    metrics: dict,
    e2e_result: dict | None = None,
) -> str:
    """
    일간 운영 리포트 마크다운 생성 → reports/ops_report_{date}.md

    Args:
        target_date: YYYYMMDD 형식 날짜
        metrics: collect_ops_metrics 반환 dict
        e2e_result: run_e2e 결과 dict (선택). PFS 요약에 사용.

    Returns:
        저장된 리포트 파일 경로 문자열
    """
    m = metrics.get("metrics", {})
    warn = _load_warn_thresholds()

    def fmt_pct(val, multiplier=100) -> str:
        if val is None:
            return "N/A"
        return f"{val * multiplier:.1f}%"

    def fmt_val(val, unit="") -> str:
        if val is None:
            return "N/A"
        return f"{val}{unit}"

    # 파이프라인 실행 결과 (PFS에서 조회)
    pfs = _load_pfs(target_date)
    fdc = _load_fdc(target_date)
    rc = _load_rc(target_date)

    regime = rc.get("regime", "N/A") if rc else "N/A"
    approved_count = fdc.get("execution_summary", {}).get("approved_count", "N/A") if fdc else "N/A"
    vetoed_count = fdc.get("execution_summary", {}).get("vetoed_count", "N/A") if fdc else "N/A"
    total_exposure = pfs.get("total_exposure", "N/A") if pfs else "N/A"
    cash_ratio = pfs.get("cash_ratio", "N/A") if pfs else "N/A"

    exposure_str = f"{total_exposure:.1f}%" if isinstance(total_exposure, (int, float)) else "N/A"
    cash_str = f"{cash_ratio:.1f}%" if isinstance(cash_ratio, (int, float)) else "N/A"

    # 소요 시간
    elapsed_str = "N/A"
    if e2e_result and e2e_result.get("elapsed_sec") is not None:
        elapsed_str = f"{e2e_result['elapsed_sec']:.1f}초"

    # 운영 메트릭 포맷 (YAML 임계치 사용)
    turnover_val = m.get("daily_turnover")
    turnover_str = f"{turnover_val:.1f}%" if turnover_val is not None else "N/A"
    turnover_status = "WARN" if (turnover_val is not None and turnover_val > warn["turnover_warn_pct"]) else "OK"

    cash_drift_val = m.get("cash_ratio_drift")
    if cash_drift_val is not None:
        cash_drift_str = f"{cash_drift_val:+.1f}%p"
        cash_drift_status = "WARN" if abs(cash_drift_val) > warn["cash_drift_warn_pct"] else "OK"
    else:
        cash_drift_str = "N/A"
        cash_drift_status = "N/A"

    fallback_val = m.get("llm_fallback_rate")
    fallback_str = fmt_pct(fallback_val)
    fallback_status = "WARN" if (fallback_val is not None and fallback_val > warn["llm_fallback_warn_rate"]) else "OK"

    success_val = m.get("order_success_rate")
    success_str = fmt_pct(success_val)
    success_status = "WARN" if (success_val is not None and success_val < 0.8) else "OK"

    mismatch_val = m.get("reconciliation_mismatch_rate")
    mismatch_str = fmt_pct(mismatch_val)
    mismatch_status = "WARN" if (mismatch_val is not None and mismatch_val > 0.0) else "OK"

    veto_val = m.get("emergency_veto_count")
    veto_str = fmt_val(veto_val, "건")
    veto_status = "WARN" if (veto_val is not None and veto_val > warn["veto_warn_count"]) else "OK"

    p50_str = f"{m['p50_latency_sec']:.1f}s" if m.get("p50_latency_sec") is not None else "N/A"
    p95_str = f"{m['p95_latency_sec']:.1f}s" if m.get("p95_latency_sec") is not None else "N/A"

    # 경고 수집 (hard limit은 YAML turnover_hard_limit, WARN은 warn["turnover_warn_pct"])
    warnings = []
    if turnover_val is not None and turnover_val > warn["turnover_hard_limit"]:
        warnings.append(
            f"- 일일 회전율 {turnover_val:.1f}% > 정책 한도 {warn['turnover_hard_limit']:.0f}% (risk_policy_v0.yaml)"
        )
    if cash_drift_val is not None and abs(cash_drift_val) > warn["cash_drift_warn_pct"]:
        warnings.append(f"- 현금 비율 급변 {cash_drift_val:+.1f}%p (임계치 {warn['cash_drift_warn_pct']:.0f}%p)")
    if veto_val is not None and veto_val > warn["veto_warn_count"]:
        warnings.append(f"- 긴급 veto {veto_val}건 발생 (regime=red 상태)")
    if fallback_val is not None and fallback_val > warn["llm_fallback_warn_rate"]:
        warnings.append(f"- LLM fallback 비율 {fallback_val:.0%} > {warn['llm_fallback_warn_rate']:.0%}")
    if mismatch_val is not None and mismatch_val > 0.0:
        warnings.append(f"- PFS↔PTL 대조 불일치율 {mismatch_val:.1%}")

    warnings_section = "\n".join(warnings) if warnings else "이상 없음"

    date_formatted = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:8]}"

    report = f"""# Daily Ops Report — {date_formatted}

생성 시각: {datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")}

## 파이프라인 실행 결과

- Regime: {regime}
- 승인/거부: {approved_count}건 / {vetoed_count}건
- 총 노출: {exposure_str}
- 현금: {cash_str}
- E2E 소요 시간: {elapsed_str}

## 운영 메트릭

| 메트릭 | 값 | 상태 |
|--------|-----|------|
| 주문 성공률 | {success_str} | {success_status} |
| LLM fallback | {fallback_str} | {fallback_status} |
| 일일 회전율 | {turnover_str} | {turnover_status} |
| 현금 비율 변화 | {cash_drift_str} | {cash_drift_status} |
| 대조 불일치율 | {mismatch_str} | {mismatch_status} |
| 긴급 veto | {veto_str} | {veto_status} |
| latency p50 | {p50_str} | - |
| latency p95 | {p95_str} | - |

## 경고 사항

{warnings_section}

---
*생성: OpsMetricsCollector / Elephant Lab*
"""

    report_path = REPORTS_DIR / f"ops_report_{target_date}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"[OpsMetrics] 리포트 저장 완료: {report_path}")

    return str(report_path)


def generate_ops_report_weekly(dates: list, output_dir: Path = None) -> str:
    """
    N거래일 OPM 파일을 읽어 주간 운영 리포트 마크다운 생성.
    -> reports/ops_report_weekly_{start}_{end}.md

    Args:
        dates: YYYYMMDD 형식 날짜 리스트 (최소 1개)
        output_dir: 리포트 저장 디렉토리. None이면 REPORTS_DIR 사용.

    Returns:
        저장된 리포트 파일 경로 문자열
    """
    if not dates:
        raise ValueError("[OpsMetrics] dates 리스트가 비어 있습니다.")

    save_dir = Path(output_dir) if output_dir else REPORTS_DIR
    save_dir.mkdir(parents=True, exist_ok=True)

    warn = _load_warn_thresholds()

    sorted_dates = sorted(dates)
    start_date = sorted_dates[0]
    end_date = sorted_dates[-1]

    print(f"[OpsMetrics] 주간 리포트 생성 시작: {start_date} ~ {end_date} ({len(sorted_dates)}거래일)")

    # ------------------------------------------------------------------ #
    # 1. 날짜별 OPM 로드
    # ------------------------------------------------------------------ #
    daily_records: list[dict] = []   # 로드 성공한 레코드만

    for d in sorted_dates:
        opm_path = OPM_DIR / f"OPM-{d}.json"
        opm = _load_json(opm_path)
        if opm is None:
            print(f"[OpsMetrics] OPM 파일 없음, skip: {opm_path}")
            continue
        daily_records.append(opm)

    loaded_count = len(daily_records)
    print(f"[OpsMetrics] OPM 로드 완료: {loaded_count}/{len(sorted_dates)}일")

    # ------------------------------------------------------------------ #
    # 2. 메트릭별 집계
    # ------------------------------------------------------------------ #
    def _collect_vals(key: str) -> list:
        """None 제외한 메트릭 값 리스트."""
        return [
            r["metrics"][key]
            for r in daily_records
            if r.get("metrics", {}).get(key) is not None
        ]

    def _avg(vals: list) -> float | None:
        return round(sum(vals) / len(vals), 4) if vals else None

    def _min_v(vals: list) -> float | None:
        return round(min(vals), 4) if vals else None

    def _max_v(vals: list) -> float | None:
        return round(max(vals), 4) if vals else None

    def _sum_v(vals: list) -> float | None:
        return round(sum(vals), 4) if vals else None

    def _warn_days(key: str, threshold_fn) -> int:
        """WARN 발생 일수."""
        count = 0
        for r in daily_records:
            val = r.get("metrics", {}).get(key)
            if val is not None and threshold_fn(val):
                count += 1
        return count

    success_vals = _collect_vals("order_success_rate")
    reject_vals = _collect_vals("order_reject_rate")
    fallback_vals = _collect_vals("llm_fallback_rate")
    turnover_vals = _collect_vals("daily_turnover")
    drift_vals = _collect_vals("cash_ratio_drift")
    mismatch_vals = _collect_vals("reconciliation_mismatch_rate")
    veto_vals = _collect_vals("emergency_veto_count")
    p50_vals = _collect_vals("p50_latency_sec")
    p95_vals = _collect_vals("p95_latency_sec")

    agg = {
        "order_success_rate":           {"avg": _avg(success_vals),   "min": _min_v(success_vals),   "max": _max_v(success_vals)},
        "order_reject_rate":            {"avg": _avg(reject_vals),    "min": _min_v(reject_vals),    "max": _max_v(reject_vals)},
        "llm_fallback_rate":            {"avg": _avg(fallback_vals),  "min": _min_v(fallback_vals),  "max": _max_v(fallback_vals)},
        "daily_turnover":               {"avg": _avg(turnover_vals),  "min": _min_v(turnover_vals),  "max": _max_v(turnover_vals)},
        "cash_ratio_drift":             {"cumulative": _sum_v(drift_vals), "min": _min_v(drift_vals), "max": _max_v(drift_vals)},
        "reconciliation_mismatch_rate": {"avg": _avg(mismatch_vals),  "min": _min_v(mismatch_vals),  "max": _max_v(mismatch_vals)},
        "emergency_veto_count":         {"total": int(_sum_v(veto_vals)) if veto_vals else None},
        "p50_latency_sec":              {"avg": _avg(p50_vals),       "min": _min_v(p50_vals),       "max": _max_v(p50_vals)},
        "p95_latency_sec":              {"avg": _avg(p95_vals),       "min": _min_v(p95_vals),       "max": _max_v(p95_vals)},
    }

    # WARN 발생 일수
    warn_days = {
        "order_success_rate":           _warn_days("order_success_rate",           lambda v: v < 0.8),
        "llm_fallback_rate":            _warn_days("llm_fallback_rate",            lambda v: v > warn["llm_fallback_warn_rate"]),
        "daily_turnover":               _warn_days("daily_turnover",               lambda v: v > warn["turnover_warn_pct"]),
        "cash_ratio_drift":             _warn_days("cash_ratio_drift",             lambda v: abs(v) > warn["cash_drift_warn_pct"]),
        "reconciliation_mismatch_rate": _warn_days("reconciliation_mismatch_rate", lambda v: v > 0.0),
        "emergency_veto_count":         _warn_days("emergency_veto_count",         lambda v: v > warn["veto_warn_count"]),
    }

    total_warn_days = sum(warn_days.values())

    # ------------------------------------------------------------------ #
    # 3. 전체 운영 안정성 판정
    # ------------------------------------------------------------------ #
    if loaded_count == 0:
        overall_status = "UNSTABLE"
    elif loaded_count < len(sorted_dates):
        # 일부 날짜 OPM 없음 → PARTIAL 후보
        if total_warn_days == 0:
            overall_status = "PARTIAL"
        else:
            overall_status = "UNSTABLE"
    else:
        # 전 날짜 로드됨
        if total_warn_days == 0:
            overall_status = "STABLE"
        elif total_warn_days <= loaded_count:
            overall_status = "PARTIAL"
        else:
            overall_status = "UNSTABLE"

    # ------------------------------------------------------------------ #
    # 4. 포맷 헬퍼
    # ------------------------------------------------------------------ #
    def _fp(val, multiplier=100) -> str:
        """float -> 퍼센트 문자열."""
        if val is None:
            return "N/A"
        return f"{val * multiplier:.1f}%"

    def _fv(val, unit="") -> str:
        if val is None:
            return "N/A"
        if isinstance(val, float):
            return f"{val:.4f}{unit}"
        return f"{val}{unit}"

    def _fpp(val) -> str:
        """cash_ratio_drift 전용 (+/-부호 포함 %p)."""
        if val is None:
            return "N/A"
        return f"{val * 100:+.1f}%p"

    # ------------------------------------------------------------------ #
    # 5. 메트릭 집계 테이블 행
    # ------------------------------------------------------------------ #
    def _row(label: str, avg_val, min_val, max_val, warn_day: int,
             fmt_fn=None) -> str:
        fn = fmt_fn or _fp
        avg_s = fn(avg_val)
        min_s = fn(min_val)
        max_s = fn(max_val)
        return f"| {label} | {avg_s} | {min_s} | {max_s} | {warn_day}일 |"

    metric_rows = [
        _row("주문 성공률",
             agg["order_success_rate"]["avg"],
             agg["order_success_rate"]["min"],
             agg["order_success_rate"]["max"],
             warn_days["order_success_rate"]),
        _row("주문 거부율",
             agg["order_reject_rate"]["avg"],
             agg["order_reject_rate"]["min"],
             agg["order_reject_rate"]["max"],
             0),
        _row("LLM fallback률",
             agg["llm_fallback_rate"]["avg"],
             agg["llm_fallback_rate"]["min"],
             agg["llm_fallback_rate"]["max"],
             warn_days["llm_fallback_rate"]),
        _row("일일 회전율",
             agg["daily_turnover"]["avg"],
             agg["daily_turnover"]["min"],
             agg["daily_turnover"]["max"],
             warn_days["daily_turnover"],
             fmt_fn=lambda v: "N/A" if v is None else f"{v:.1f}%"),
        (
            f"| 현금비율 변화(누적) | "
            f"{_fpp(agg['cash_ratio_drift']['cumulative'])} | "
            f"{_fpp(agg['cash_ratio_drift']['min'])} | "
            f"{_fpp(agg['cash_ratio_drift']['max'])} | "
            f"{warn_days['cash_ratio_drift']}일 |"
        ),
        _row("대조 불일치율",
             agg["reconciliation_mismatch_rate"]["avg"],
             agg["reconciliation_mismatch_rate"]["min"],
             agg["reconciliation_mismatch_rate"]["max"],
             warn_days["reconciliation_mismatch_rate"]),
        (
            f"| 긴급 veto(합계) | "
            f"{agg['emergency_veto_count']['total'] if agg['emergency_veto_count']['total'] is not None else 'N/A'}건 | - | - | "
            f"{warn_days['emergency_veto_count']}일 |"
        ),
        _row("latency p50",
             agg["p50_latency_sec"]["avg"],
             agg["p50_latency_sec"]["min"],
             agg["p50_latency_sec"]["max"],
             0,
             fmt_fn=lambda v: "N/A" if v is None else f"{v:.2f}s"),
        _row("latency p95",
             agg["p95_latency_sec"]["avg"],
             agg["p95_latency_sec"]["min"],
             agg["p95_latency_sec"]["max"],
             0,
             fmt_fn=lambda v: "N/A" if v is None else f"{v:.2f}s"),
    ]

    metric_table = "\n".join(metric_rows)

    # ------------------------------------------------------------------ #
    # 6. 일별 추이 테이블
    # ------------------------------------------------------------------ #
    trend_rows = []
    record_map = {r["target_date"]: r for r in daily_records}

    for d in sorted_dates:
        d_fmt = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        if d not in record_map:
            trend_rows.append(f"| {d_fmt} | N/A | N/A | N/A | N/A | N/A |")
            continue
        m = record_map[d].get("metrics", {})
        sr = _fp(m.get("order_success_rate"))
        fb = _fp(m.get("llm_fallback_rate"))
        tv = m.get("daily_turnover")
        tv_s = f"{tv:.1f}%" if tv is not None else "N/A"
        drift = m.get("cash_ratio_drift")
        drift_s = f"{drift * 100:+.1f}%p" if drift is not None else "N/A"
        veto = m.get("emergency_veto_count")
        veto_s = str(veto) if veto is not None else "N/A"
        trend_rows.append(f"| {d_fmt} | {sr} | {fb} | {tv_s} | {drift_s} | {veto_s} |")

    trend_table = "\n".join(trend_rows)

    # ------------------------------------------------------------------ #
    # 7. 경고 요약 섹션
    # ------------------------------------------------------------------ #
    warn_lines = []
    for d in sorted_dates:
        if d not in record_map:
            continue
        m = record_map[d].get("metrics", {})
        d_fmt = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        day_warns = []

        tv = m.get("daily_turnover")
        if tv is not None and tv > warn["turnover_warn_pct"]:
            day_warns.append(f"회전율 {tv:.1f}% > {warn['turnover_warn_pct']:.0f}%")

        cd = m.get("cash_ratio_drift")
        if cd is not None and abs(cd) > warn["cash_drift_warn_pct"] / 100:
            day_warns.append(f"현금비율 변화 {cd * 100:+.1f}%p")

        fb = m.get("llm_fallback_rate")
        if fb is not None and fb > warn["llm_fallback_warn_rate"]:
            day_warns.append(f"LLM fallback {fb:.0%}")

        mm = m.get("reconciliation_mismatch_rate")
        if mm is not None and mm > 0.0:
            day_warns.append(f"대조 불일치 {mm:.1%}")

        vc = m.get("emergency_veto_count")
        if vc is not None and vc > warn["veto_warn_count"]:
            day_warns.append(f"긴급 veto {vc}건")

        if day_warns:
            warn_lines.append(f"- {d_fmt}: " + ", ".join(day_warns))

    warn_section = "\n".join(warn_lines) if warn_lines else "이상 없음"

    # ------------------------------------------------------------------ #
    # 8. 권장 조치 섹션
    # ------------------------------------------------------------------ #
    if overall_status == "UNSTABLE":
        action_section = (
            "- 회전율/현금비율 이상 발생 일수를 확인하고 포지션 조정 로직을 점검하세요.\n"
            "- LLM fallback 비율이 높은 경우 Kanana-o 연결 상태 및 Circuit Breaker 로그를 확인하세요.\n"
            "- 긴급 veto가 반복되면 Regime 판정 기준(risk_policy_v0.yaml)을 재검토하세요."
        )
    elif overall_status == "PARTIAL":
        action_section = (
            "- 일부 날짜 데이터가 누락되어 완전한 집계가 어렵습니다. "
            "누락 날짜의 파이프라인 실행 여부를 확인하세요.\n"
            "- WARN 발생 항목이 있다면 해당 날짜의 Daily Ops Report를 개별 확인하세요."
        )
    else:
        action_section = "특이사항 없음. 정상 운영 상태입니다."

    # ------------------------------------------------------------------ #
    # 9. 리포트 조립
    # ------------------------------------------------------------------ #
    start_fmt = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
    end_fmt = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"

    report = f"""# Weekly Ops Report — {start_fmt} ~ {end_fmt}

생성 시각: {datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")}

## 운영 요약

- 운영 일수: {loaded_count}일 (요청 {len(sorted_dates)}일, 누락 {len(sorted_dates) - loaded_count}일)
- 전체 판정: {overall_status}

## 메트릭 집계

| 메트릭 | 평균 | 최소 | 최대 | WARN 일수 |
|--------|------|------|------|----------|
{metric_table}

## 일별 추이

| 날짜 | 성공률 | fallback | turnover | cash drift | veto |
|------|--------|----------|----------|------------|------|
{trend_table}

## 경고 요약

{warn_section}

## 권장 조치

{action_section}

---
*생성: OpsMetricsCollector (weekly) / Elephant Lab*
"""

    report_path = save_dir / f"ops_report_weekly_{start_date}_{end_date}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"[OpsMetrics] 주간 리포트 저장 완료: {report_path}")

    return str(report_path)


if __name__ == "__main__":
    import argparse
    from datetime import datetime as dt

    parser = argparse.ArgumentParser(description="Ops Metrics Collector")
    parser.add_argument(
        "date",
        nargs="?",
        default=dt.now(KST).strftime("%Y%m%d"),
        help="수집 날짜 (YYYYMMDD)",
    )
    parser.add_argument(
        "--weekly",
        nargs="+",
        metavar="YYYYMMDD",
        help="주간 리포트 날짜 목록 (e.g., --weekly 20260316 20260317 20260318 20260319 20260320)",
    )
    parser.add_argument(
        "--weekly-days",
        type=int,
        metavar="N",
        dest="weekly_days",
        help="최근 N거래일 OPM을 자동 수집해 주간 리포트 생성",
    )
    args = parser.parse_args()

    if args.weekly:
        generate_ops_report_weekly(args.weekly)
    elif args.weekly_days:
        # OPM_DIR에 있는 파일 기준으로 최근 N거래일 날짜 수집
        all_opms = sorted(OPM_DIR.glob("OPM-*.json"))
        recent_dates = [p.stem.replace("OPM-", "") for p in all_opms[-args.weekly_days:]]
        if not recent_dates:
            print(f"[OpsMetrics] OPM 파일이 없습니다: {OPM_DIR}")
        else:
            generate_ops_report_weekly(recent_dates)
    else:
        result = collect_ops_metrics(args.date)
        generate_ops_report(args.date, result)

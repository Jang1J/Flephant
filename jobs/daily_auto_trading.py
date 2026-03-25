"""
Daily Auto Trading — 일일 자동 운영 스크립트

1. DMP 수집 (build_daily_market_packet)
2. TTP 생성 (build_ticker_text_pack) — 샘플 3종목
3. PFS 로드
4. Risk Engine (SC → RC + COP)
5. FDA (→ FDC)
6. PFS 업데이트
7. Paper Trading 실행 (dry_run)
8. 운영 메트릭 수집
9. 일간 운영 리포트 생성

Usage:
    python jobs/daily_auto_trading.py                    # 오늘 날짜
    python jobs/daily_auto_trading.py 20260325           # 특정 날짜
    python jobs/daily_auto_trading.py --no-uq            # UQ off ablation
    python jobs/daily_auto_trading.py --skip-paper       # paper trading skip
"""

import sys
import json
import time
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

KST = timezone(timedelta(hours=9))

PARTIAL_DIR = _BASE_DIR / "artifacts" / "daily_auto_trading_partial"
PARTIAL_DIR.mkdir(parents=True, exist_ok=True)


def run_daily_auto_trading(
    target_date: str,
    disable_uq: bool = False,
    skip_paper: bool = False,
    use_mock: bool = False,
) -> dict:
    """
    일일 자동 운영 풀 사이클 실행.

    Args:
        target_date: YYYYMMDD 형식 날짜
        disable_uq: True면 UQ tail cap 비활성화 (ablation)
        skip_paper: True면 Paper Trading 단계 건너뜀
        use_mock: True면 mock StrategyCard 강제 사용

    Returns:
        전체 실행 결과 dict
    """
    print(f"\n{'='*60}")
    print(f"  Daily Auto Trading: {target_date}")
    print(f"  UQ={'OFF' if disable_uq else 'ON'}, Paper={'SKIP' if skip_paper else 'ON'}, Mock={'ON' if use_mock else 'AUTO'}")
    print(f"{'='*60}")

    start_time = time.time()
    result = {
        "date": target_date,
        "started_at": datetime.now(KST).isoformat(),
        "steps": {},
        "elapsed_sec": None,
        "status": "running",
    }

    # ── Step 1~6: E2E 파이프라인 실행 ──
    print(f"\n[DailyTrading] Step 1/4: E2E 파이프라인 실행...")
    try:
        from jobs.run_e2e_pipeline import run_e2e
        run_e2e(target_date, disable_uq=disable_uq, use_mock=use_mock)
        result["steps"]["e2e"] = "OK"
        print(f"[DailyTrading] E2E 파이프라인 완료")
    except Exception as e:
        print(f"[DailyTrading] E2E 파이프라인 에러: {e}")
        result["steps"]["e2e"] = f"FAIL: {e}"
        _save_partial(target_date, result)
        # E2E 실패 시 이후 단계도 실패 가능성 높으나 계속 시도 (graceful degradation)

    # ── Step 2: Paper Trading 실행 ──
    ptl = None
    if skip_paper:
        print(f"\n[DailyTrading] Step 2/4: Paper Trading SKIP (--skip-paper)")
        result["steps"]["paper_trading"] = "SKIPPED"
    else:
        print(f"\n[DailyTrading] Step 2/4: Paper Trading 실행 (dry_run=True)...")
        try:
            from jobs.paper_trading_executor import execute_paper_trades
            ptl = execute_paper_trades(target_date, dry_run=True)
            result["steps"]["paper_trading"] = "OK"
            print(f"[DailyTrading] Paper Trading 완료: {ptl['summary']['executed_count']}건 실행, {ptl['summary']['rejected_count']}건 거부")
        except FileNotFoundError as e:
            print(f"[DailyTrading] Paper Trading 건너뜀 (아티팩트 없음): {e}")
            result["steps"]["paper_trading"] = f"SKIP: {e}"
        except Exception as e:
            print(f"[DailyTrading] Paper Trading 에러: {e}")
            result["steps"]["paper_trading"] = f"FAIL: {e}"
            _save_partial(target_date, result)

    # ── Step 3: 운영 메트릭 수집 ──
    print(f"\n[DailyTrading] Step 3/4: 운영 메트릭 수집...")
    elapsed_now = time.time() - start_time
    try:
        from jobs.ops_metrics_collector import collect_ops_metrics
        opm = collect_ops_metrics(target_date, latency_samples=[elapsed_now])
        result["steps"]["ops_metrics"] = "OK"
        result["opm"] = opm
        print(f"[DailyTrading] 운영 메트릭 수집 완료: {opm['metrics_id']}")
    except Exception as e:
        print(f"[DailyTrading] 운영 메트릭 수집 에러: {e}")
        result["steps"]["ops_metrics"] = f"FAIL: {e}"
        opm = {"metrics": {}}
        _save_partial(target_date, result)

    # ── Step 4: 일간 운영 리포트 생성 ──
    print(f"\n[DailyTrading] Step 4/4: 일간 운영 리포트 생성...")
    total_elapsed = time.time() - start_time
    result["elapsed_sec"] = round(total_elapsed, 2)
    e2e_summary = {"elapsed_sec": total_elapsed}

    try:
        from jobs.ops_metrics_collector import generate_ops_report
        report_path = generate_ops_report(target_date, opm, e2e_result=e2e_summary)
        result["steps"]["ops_report"] = "OK"
        result["report_path"] = report_path
        print(f"[DailyTrading] 리포트 생성 완료: {report_path}")
    except Exception as e:
        print(f"[DailyTrading] 리포트 생성 에러: {e}")
        result["steps"]["ops_report"] = f"FAIL: {e}"
        _save_partial(target_date, result)

    # ── 최종 요약 ──
    all_ok = all(
        v in ("OK", "SKIPPED")
        for v in result["steps"].values()
        if not str(v).startswith("SKIP:")
    )
    result["status"] = "success" if all_ok else "partial"
    result["completed_at"] = datetime.now(KST).isoformat()

    print(f"\n{'='*60}")
    print(f"  Daily Auto Trading 완료: {target_date}")
    print(f"{'='*60}")
    for step, status in result["steps"].items():
        print(f"  {status} {step}")
    print(f"\n  전체 소요: {total_elapsed:.1f}초")
    print(f"  상태: {result['status']}")

    if result.get("report_path"):
        print(f"\n  리포트: {result['report_path']}")

    return result


def _save_partial(target_date: str, result: dict) -> None:
    """에러 발생 시 부분 결과를 저장 (graceful degradation)"""
    try:
        partial_path = PARTIAL_DIR / f"partial_{target_date}.json"
        with open(partial_path, "w", encoding="utf-8") as f:
            # opm 같은 큰 객체는 제외하고 핵심만 저장
            partial = {k: v for k, v in result.items() if k != "opm"}
            json.dump(partial, f, ensure_ascii=False, indent=2)
        print(f"[DailyTrading] 부분 결과 저장: {partial_path}")
    except Exception as e:
        print(f"[DailyTrading] 부분 결과 저장 실패: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily Auto Trading — 일일 자동 운영")
    parser.add_argument(
        "date",
        nargs="?",
        default=datetime.now(KST).strftime("%Y%m%d"),
        help="실행 날짜 (YYYYMMDD, 기본: 오늘)",
    )
    parser.add_argument(
        "--no-uq",
        action="store_true",
        help="UQ tail cap 비활성화 (ablation용)",
    )
    parser.add_argument(
        "--skip-paper",
        action="store_true",
        help="Paper Trading 단계 건너뜀",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="mock StrategyCard 강제 사용",
    )
    args = parser.parse_args()

    run_daily_auto_trading(
        target_date=args.date,
        disable_uq=args.no_uq,
        skip_paper=args.skip_paper,
        use_mock=args.mock,
    )

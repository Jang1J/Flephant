"""SHIP-fix P0-6 (W1, 2026-05-07): 발표 당일 단일 명령 demo runner.

GPT Pro 권고: "demo.sh / run_final_demo.py W1 안에 반드시 작성. 발표 당일 여러 명령
조합 시 실패 확률 큼."

기존 e2e_scenario_runner.py 를 wrapping 하여 3 demo 시나리오를 순차 또는 개별 실행:

    Demo A (Hot Path)   : 5종목 synthetic 1분봉 -> Quant -> PPO/PM -> FDA non-LLM
                          -> final_decision + latency p95 출력
    Demo B (Cold Path)  : event_injector 4종 (news / dart / community / macro) inject
                          -> EventGateway -> News/Risk Slow -> FDA -> reason_code
    Demo C (Mode B)     : ModeBScheduler 7-stage cron -> BacktestAgent -> backtest_verdict
                          -> ModeBDeployer -> deploy_status (blocked / deployed)

사용법:
    python -m src.jobs.run_final_demo                   # 3 demo 전체
    python -m src.jobs.run_final_demo --demo hot        # Demo A 단독
    python -m src.jobs.run_final_demo --demo cold       # Demo B 단독
    python -m src.jobs.run_final_demo --demo mode_b     # Demo C 단독
    python -m src.jobs.run_final_demo --scenario X.yaml # 시나리오 파일 지정

또는 루트의 demo.sh:
    bash demo.sh

거래일 수 / 종목 / Hot Path tick 수는 yaml 시나리오 (`new/config/scenarios/X.yaml`)
의 `days`, `universe_tickers`, `hot_path_ticks_per_day`, `hot_path_ticks_short` 로 제어.
CLI 에서는 시나리오 파일만 선택 (실행 단축이 필요하면 별도 yaml 추가).

출력:
    콘솔 요약 + artifacts/audit/demo_{a|b|c}_{timestamp}.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

_NEW_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_NEW_ROOT) not in sys.path:
    sys.path.insert(0, str(_NEW_ROOT))

from src.runner.e2e_scenario_runner import E2EScenarioRunner
from src.utils.logger import get_logger

logger = get_logger("run_final_demo")
_KST = ZoneInfo("Asia/Seoul")
_DEMO_OUTPUT_ROOT = _NEW_ROOT.parent / "artifacts" / "audit"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="P0-6 학기말 발표 demo runner (Hot / Cold / Mode B)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--demo",
        choices=["all", "hot", "cold", "mode_b"],
        default="all",
        help="실행할 demo (기본: all)",
    )
    parser.add_argument(
        "--scenario",
        default="week1_basic.yaml",
        help="시나리오 파일 (new/config/scenarios/ 하위). 기본: week1_basic.yaml",
    )
    parser.add_argument(
        "--full-ticks",
        action="store_true",
        help="Hot Path 전체 390 tick 실행. 기본은 short (5 tick)",
    )
    return parser.parse_args()


def _print_banner(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def _ensure_output_dir() -> pathlib.Path:
    _DEMO_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    return _DEMO_OUTPUT_ROOT


def _save_summary(demo_id: str, summary: dict[str, Any]) -> pathlib.Path:
    _ensure_output_dir()
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    out_path = _DEMO_OUTPUT_ROOT / f"demo_{demo_id}_{ts}.json"
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2, default=str)
    return out_path


def _mark_demo_context(
    summary: dict[str, Any],
    *,
    demo_id: str,
    caveats: list[str],
) -> None:
    """Keep demo smoke PASS separate from production readiness."""
    summary["execution_context"] = "demo_smoke"
    summary["demo_id"] = demo_id
    summary["simulation_only"] = True
    summary["production_ready"] = False
    summary["production_gate_required"] = "prelive_gate + C12 real backtest + C14 deploy"
    summary["demo_caveats"] = caveats


def _summary_failures(
    summary: dict[str, Any],
    require_sla: bool = False,
    require_mode_b_pass: bool = False,
) -> list[str]:
    failures: list[str] = []
    if int(summary.get("pit_violations", 0)) > 0:
        failures.append("pit_violations")
    if int(summary.get("fda_missing_reason_code", 0)) > 0:
        failures.append("fda_missing_reason_code")
    if int(summary.get("total_errors", 0)) > 0:
        failures.append("total_errors")
    if require_sla and not bool(summary.get("hot_path_sla", {}).get("sla_ok", False)):
        failures.append("hot_path_sla")
    if require_mode_b_pass:
        bad_mode_b = [
            verdict for verdict in summary.get("mode_b_verdicts", [])
            if verdict != "pass"
        ]
        if bad_mode_b:
            failures.append("mode_b_verdict")
    summary["status"] = "FAIL" if failures else "PASS"
    summary["failures"] = failures
    return failures


def run_demo_hot(scenario: str, short_mode: bool) -> dict[str, Any]:
    """Demo A: Hot Path. tick 5회 실행 + latency p95 + final_decision."""
    _print_banner("Demo A: Hot Path (Quant -> PPO/PM -> FDA non-LLM)")

    runner = E2EScenarioRunner(
        scenario_file=scenario,
        short_mode=short_mode,
        skip_mode_b=True,
    )
    result = runner.run()
    summary = result.summary()
    _mark_demo_context(
        summary,
        demo_id="hot",
        caveats=[
            "Synthetic scenario bars are used for demo ticks.",
            "Hot Path latency is a smoke metric; active LGBM inference requires registry.active_version after C14 deploy.",
        ],
    )
    _summary_failures(summary, require_sla=True)

    sla = summary.get("hot_path_sla", {})
    print(f"  시나리오           : {summary.get('scenario_name', 'N/A')}")
    print(f"  총 거래일 수       : {summary.get('total_days', 0)}")
    print(f"  Hot Path tick 합계 : {summary.get('total_hot_ticks', 0)}")
    print(f"  Hot Path p50 (ms)  : {sla.get('p50_ms', 'N/A')}")
    print(f"  Hot Path p95 (ms)  : {sla.get('p95_ms', 'N/A')}")
    print(f"  Hot Path p99 (ms)  : {sla.get('p99_ms', 'N/A')}")
    print(f"  SLA 준수 (<{sla.get('target_p95_ms', 100)}ms): {sla.get('sla_ok', False)}")
    print(f"  PIT 위반           : {summary.get('pit_violations', 0)}")
    print(f"  FDA reason_code 누락 : {summary.get('fda_missing_reason_code', 0)}")
    print(f"  에러 합계          : {summary.get('total_errors', 0)}")

    out = _save_summary("a_hot", summary)
    print(f"  → 산출: {out}")
    return summary


def run_demo_cold(scenario: str, short_mode: bool) -> dict[str, Any]:
    """Demo B: Cold Path. event_injector 4종 inject -> reason_code."""
    _print_banner("Demo B: Cold Path (Event -> News/Risk Slow -> FDA veto)")

    runner = E2EScenarioRunner(
        scenario_file=scenario,
        short_mode=short_mode,
        skip_mode_b=True,
    )
    result = runner.run()
    summary = result.summary()
    _mark_demo_context(
        summary,
        demo_id="cold",
        caveats=[
            "Injected scenario events are used for demo coverage.",
            "Cold Path quality still requires live event freshness and downstream decision validation.",
        ],
    )
    _summary_failures(summary)

    print(f"  시나리오           : {summary.get('scenario_name', 'N/A')}")
    print(f"  총 거래일 수       : {summary.get('total_days', 0)}")
    print(f"  Hot Path tick 합계 : {summary.get('total_hot_ticks', 0)}")
    print(f"  PIT 위반           : {summary.get('pit_violations', 0)}")
    print(f"  FDA reason_code 누락 : {summary.get('fda_missing_reason_code', 0)}")
    print(f"  에러 합계          : {summary.get('total_errors', 0)}")
    print(f"  (Cold Path event_injector 4종 inject 결과는 audit_log JSONL 에서 확인)")

    out = _save_summary("b_cold", summary)
    print(f"  → 산출: {out}")
    return summary


def run_demo_mode_b(scenario: str) -> dict[str, Any]:
    """Demo C: Mode B blocked. ModeBScheduler 7-stage -> BacktestAgent -> deploy_status."""
    _print_banner("Demo C: Mode B (Scheduler -> BacktestAgent -> Deployer)")

    runner = E2EScenarioRunner(
        scenario_file=scenario,
        short_mode=True,
        skip_mode_b=False,
    )
    result = runner.run()
    summary = result.summary()
    _mark_demo_context(
        summary,
        demo_id="mode_b",
        caveats=[
            "Mode B demo runs short scenario mode and may include stub_short_mode candidates.",
            "Production readiness requires non-stub C12 real backtest PASS and C14 deploy.",
        ],
    )
    _summary_failures(summary, require_mode_b_pass=True)

    verdicts = summary.get("mode_b_verdicts", [])
    print(f"  시나리오           : {summary.get('scenario_name', 'N/A')}")
    print(f"  Mode B 실행일수    : {summary.get('total_days', 0)}")
    print(f"  backtest_verdict   : {verdicts}")
    print(f"  fail/blocked 일수  : {sum(1 for v in verdicts if v in ('fail', 'blocked'))}")
    print(f"  pass 일수          : {sum(1 for v in verdicts if v == 'pass')}")
    print(f"  PIT 위반           : {summary.get('pit_violations', 0)}")
    print(f"  에러 합계          : {summary.get('total_errors', 0)}")

    out = _save_summary("c_mode_b", summary)
    print(f"  → 산출: {out}")
    return summary


def main() -> int:
    args = _parse_args()
    short_mode = not args.full_ticks

    print()
    print("=" * 72)
    print(f"  학기말 발표 데모 runner (P0-6)")
    print(f"  scenario={args.scenario} demo={args.demo} short={short_mode}")
    print("=" * 72)

    results: dict[str, Any] = {}
    failed: list[str] = []

    # SHIP-fix F3 (D 옵션 후속): Demo A/B/C 개별 try/except 분리.
    # Demo A 실패 시에도 B/C 진행 가능 (발표 당일 부분 실패 격리).
    if args.demo in ("all", "hot"):
        try:
            results["hot"] = run_demo_hot(args.scenario, short_mode)
            if results["hot"].get("status") == "FAIL":
                failed.append("hot")
        except Exception as e:
            logger.error("[run_final_demo] Demo A (hot) 실패: %s", e, exc_info=True)
            failed.append("hot")

    if args.demo in ("all", "cold"):
        try:
            results["cold"] = run_demo_cold(args.scenario, short_mode)
            if results["cold"].get("status") == "FAIL":
                failed.append("cold")
        except Exception as e:
            logger.error("[run_final_demo] Demo B (cold) 실패: %s", e, exc_info=True)
            failed.append("cold")

    if args.demo in ("all", "mode_b"):
        try:
            results["mode_b"] = run_demo_mode_b(args.scenario)
            if results["mode_b"].get("status") == "FAIL":
                failed.append("mode_b")
        except Exception as e:
            logger.error("[run_final_demo] Demo C (mode_b) 실패: %s", e, exc_info=True)
            failed.append("mode_b")

    _print_banner("Demo 종합 결과")
    print(f"  실행 demo : {list(results.keys())}")
    print(f"  실패     : {failed if failed else '없음'}")
    print()

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

"""S4-3 E2E 시나리오 CLI 진입점.

사용법:
    python new/src/jobs/run_e2e_scenario.py [--scenario week1_basic.yaml] [--short] [--skip-mode-b]

옵션:
    --scenario   시나리오 파일명 (new/config/scenarios/ 하위). 기본값: week1_basic.yaml
    --short      Hot Path tick 수를 short 모드로 실행 (5 tick, CI 기본). 기본값: True
    --full       Hot Path tick 수를 full 모드로 실행 (390 tick, 실 시뮬레이션)
    --skip-mode-b  Mode B 건너뜀. Mode A 시나리오만 검증할 때 사용.

출력:
    콘솔 + artifacts/audit/scenario_{name}_summary.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

# new/ 루트를 sys.path에 추가
_NEW_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_NEW_ROOT) not in sys.path:
    sys.path.insert(0, str(_NEW_ROOT))

from src.runner.e2e_scenario_runner import E2EScenarioRunner
from src.utils.logger import get_logger

logger = get_logger("run_e2e_scenario")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="S4-3 E2E Scenario Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--scenario",
        default="week1_basic.yaml",
        help="시나리오 파일명 (new/config/scenarios/ 하위). 기본값: week1_basic.yaml",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--short",
        dest="short_mode",
        action="store_true",
        default=True,
        help="Hot Path tick 수 단축 (CI 기본, hot_path_ticks_short). 기본값: True",
    )
    mode_group.add_argument(
        "--full",
        dest="short_mode",
        action="store_false",
        help="Hot Path tick 수 전체 (390 tick, 실 시뮬레이션)",
    )
    parser.add_argument(
        "--skip-mode-b",
        dest="skip_mode_b",
        action="store_true",
        default=False,
        help="Mode B 건너뜀. Mode A 시나리오만 검증할 때 사용.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    logger.info(
        "[run_e2e_scenario] 실행 시작: scenario=%s short_mode=%s skip_mode_b=%s",
        args.scenario,
        args.short_mode,
        args.skip_mode_b,
    )

    runner = E2EScenarioRunner(
        scenario_file=args.scenario,
        short_mode=args.short_mode,
        skip_mode_b=args.skip_mode_b,
    )

    result = runner.run()
    summary = result.summary()

    # 콘솔 출력
    print("\n" + "=" * 60)
    print(f"시나리오: {summary['scenario_name']}")
    print(f"실행 일수: {summary['total_days']}")
    print(f"Hot Path tick 수: {summary['total_hot_ticks']}")
    print(f"PIT violation: {summary['pit_violations']} 건")
    print(f"FDA reason_code 누락: {summary['fda_missing_reason_code']} 건")
    sla = summary["hot_path_sla"]
    print(
        f"Hot Path SLA (p50/p95/p99): {sla['p50_ms']}/{sla['p95_ms']}/{sla['p99_ms']} ms"
        f"  {'OK' if sla['sla_ok'] else 'FAIL (p95 > 100ms)'}"
    )
    print(f"Mode B verdicts: {summary['mode_b_verdicts']}")
    print(f"총 오류: {summary['total_errors']} 건")
    print("=" * 60)

    # 불변 5원칙 점검 결과
    print("\n[불변 5원칙 검증]")
    pit = summary['pit_violations']
    fda_miss = summary['fda_missing_reason_code']
    pit_status = 'PASS' if pit == 0 else 'FAIL'
    fda_status = 'PASS' if fda_miss == 0 else f'FAIL ({fda_miss} 누락)'
    sla_status = 'PASS' if sla['sla_ok'] else f"FAIL ({sla['p95_ms']}ms)"
    print(f"  PIT-Safety violation: {pit} 건  {pit_status}")
    print(f"  FDA reason_code 100%: {fda_status}")
    print(f"  Hot Path p95 <100ms: {sla_status}")

    exit_code = 0
    if summary["pit_violations"] > 0:
        logger.error("[run_e2e_scenario] PIT violation %d건. 불변 원칙 1 위반.", summary["pit_violations"])
        exit_code = 1
    if summary["fda_missing_reason_code"] > 0:
        logger.error("[run_e2e_scenario] FDA reason_code 누락 %d건. 불변 원칙 검증 실패.", summary["fda_missing_reason_code"])
        exit_code = 1

    logger.info("[run_e2e_scenario] 완료: exit_code=%d", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

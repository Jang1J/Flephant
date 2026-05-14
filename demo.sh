#!/bin/bash
# Elephant Lab v3: 학기말 발표 데모 단일 명령 (SHIP-fix P0-6, 2026-05-07)
#
# GPT Pro 권고: "발표 당일 여러 명령 조합 시 실패 확률 큼. 단일 명령 고정."
#
# 사용법:
#   bash demo.sh             # Demo A/B/C 전체
#   bash demo.sh hot         # Demo A 단독
#   bash demo.sh cold        # Demo B 단독
#   bash demo.sh mode_b      # Demo C 단독
#   bash demo.sh full        # Hot Path 전체 390 tick (실 시뮬레이션)
#
# 환경:
#   PYTHON      Python 인터프리터 경로 (기본: python3)
#   ELEPHANT_MODE  Mode B 시뮬에서 자동 mode_b 설정

set -e
set -u

PYTHON="${PYTHON:-python3}"
DEMO_TYPE="${1:-all}"
SCENARIO="${SCENARIO:-week1_basic.yaml}"

cd "$(dirname "$0")"

echo "[demo] Elephant Lab v3 학기말 발표 데모 시작..."
echo "[demo] scenario=$SCENARIO demo=$DEMO_TYPE python=$PYTHON"
echo ""

# 2026-05-09 Codex 권고 4 fix: all/full 도 Hot/Cold 단계는 Mode A 환경 (env -u),
# Mode B 단계만 ELEPHANT_MODE=mode_b. 단일 프로세스에서 ELEPHANT_MODE 동적 변경 불가 →
# all/full 시 두 번 분리 실행. 이렇게 해야 Hot/Cold 의 mode_b_only 가드가 정상 작동.
# 이전: all/full 한 프로세스 ELEPHANT_MODE=mode_b → Hot/Cold 가드 우회 위험.
case "$DEMO_TYPE" in
  hot|cold)
    # Mode A 정상 환경 (env -u 로 부모 shell ELEPHANT_MODE 명시적 제거)
    env -u ELEPHANT_MODE PYTHONPATH=new $PYTHON -m src.jobs.run_final_demo \
        --demo "$DEMO_TYPE" --scenario "$SCENARIO"
    ;;
  mode_b)
    # Mode B 단독: ELEPHANT_MODE=mode_b 필수
    PYTHONPATH=new ELEPHANT_MODE=mode_b $PYTHON -m src.jobs.run_final_demo \
        --demo mode_b --scenario "$SCENARIO"
    ;;
  all)
    # 2026-05-09 Codex 권고 4 fix: Hot+Cold (Mode A 격리) → 후 Mode B (mode_b 환경) 분리 실행.
    echo "[demo] all 단계 1/2: Hot Path (Mode A 격리, env -u ELEPHANT_MODE)"
    env -u ELEPHANT_MODE PYTHONPATH=new $PYTHON -m src.jobs.run_final_demo \
        --demo hot --scenario "$SCENARIO"
    echo "[demo] all 단계 2/2: Cold Path (Mode A 격리)"
    env -u ELEPHANT_MODE PYTHONPATH=new $PYTHON -m src.jobs.run_final_demo \
        --demo cold --scenario "$SCENARIO"
    echo "[demo] all 단계 3/3: Mode B (ELEPHANT_MODE=mode_b)"
    PYTHONPATH=new ELEPHANT_MODE=mode_b $PYTHON -m src.jobs.run_final_demo \
        --demo mode_b --scenario "$SCENARIO"
    ;;
  full)
    # 2026-05-09 Codex 권고 4 fix: all 과 동일 분리, Hot Path 만 --full-ticks (390 tick).
    echo "[demo] full 단계 1/3: Hot Path 전체 390 tick (Mode A 격리)"
    env -u ELEPHANT_MODE PYTHONPATH=new $PYTHON -m src.jobs.run_final_demo \
        --demo hot --scenario "$SCENARIO" --full-ticks
    echo "[demo] full 단계 2/3: Cold Path (Mode A 격리)"
    env -u ELEPHANT_MODE PYTHONPATH=new $PYTHON -m src.jobs.run_final_demo \
        --demo cold --scenario "$SCENARIO"
    echo "[demo] full 단계 3/3: Mode B (mode_b)"
    PYTHONPATH=new ELEPHANT_MODE=mode_b $PYTHON -m src.jobs.run_final_demo \
        --demo mode_b --scenario "$SCENARIO"
    ;;
  *)
    echo "[demo] 사용법: bash demo.sh [all|hot|cold|mode_b|full]"
    exit 1
    ;;
esac

echo ""
echo "[demo] 종료. 산출물: artifacts/audit/demo_*.json"

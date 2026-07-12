#!/bin/bash
# Elephant Lab v3 — 환경 부트스트랩
# 세션 시작 시 실행하여 환경이 살아있는지 확인

set -e
set -u
set -o pipefail

DEFAULT_PYTHON="/opt/anaconda3/envs/elephant/bin/python"
if [ -z "${PYTHON:-}" ]; then
    if [ -x "$DEFAULT_PYTHON" ]; then
        PYTHON="$DEFAULT_PYTHON"
    else
        PYTHON="$(command -v python3)"
    fi
fi

echo "[init] Elephant Lab v3 환경 확인 시작..."

GRPC_LOG=$(mktemp "${TMPDIR:-/tmp}/elephant_grpc_codegen.XXXXXX")
COLLECT_LOG=$(mktemp "${TMPDIR:-/tmp}/elephant_collect.XXXXXX")
cleanup_logs() {
    rm -f "$GRPC_LOG" "$COLLECT_LOG"
}
trap cleanup_logs EXIT

# 1. Python 환경
if [ -x "$PYTHON" ]; then
    echo "[init] ✓ Python 실행기: $PYTHON"
else
    echo "[init] ✗ Python 실행기를 찾을 수 없음" >&2
    exit 1
fi

# 2. 환경 파일 템플릿. 실제 .env는 외부 API 실행에만 필요하다.
if [ ! -f ".env.example" ]; then
    echo "[init] ✗ .env.example 없음" >&2
    exit 1
fi
if [ -f ".env" ]; then
    echo "[init] ✓ .env 파일 존재"
else
    echo "[init] ! .env 없음. mock/unit 검증은 계속 가능"
fi

# 3. new/ 폴더 구조
for dir in new/docs new/specs new/config new/src new/tests; do
    if [ -d "$dir" ]; then
        echo "[init] ✓ $dir 존재"
    else
        echo "[init] ✗ $dir 없음" >&2
        exit 1
    fi
done

# 4. 핵심 설계 문서
for file in new/docs/architecture.md new/specs/api_contracts.md new/config/risk_config.yaml; do
    if [ -f "$file" ]; then
        echo "[init] ✓ $file 존재"
    else
        echo "[init] ✗ $file 없음" >&2
        exit 1
    fi
done

# 5. Python 버전 체크
PY_VER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
$PYTHON -c "
import sys
if sys.version_info < (3, 11):
    print('[init] ✗ Python 3.11+ 필요 (현재 %s.%s)' % sys.version_info[:2])
    sys.exit(1)
"
echo "[init] ✓ Python ${PY_VER} (>=3.11)"

# 6. 핵심 패키지 import 가능
$PYTHON -c "
import grpc_tools, kafka, yaml
import datetime, logging, pathlib, uuid, zoneinfo
" 2>&1 && echo "[init] ✓ 핵심 패키지 import OK (yaml, grpc_tools, kafka)" || {
    echo "[init] ✗ 패키지 import 실패. requirements.txt 설치 필요" >&2
    exit 1
}

# 7. generated gRPC stubs 준비
if ! PYTHONPATH=new "$PYTHON" new/scripts/generate_ai_grpc_stubs.py >"$GRPC_LOG" 2>&1; then
    echo "[init] ✗ gRPC stub 생성 실패" >&2
    tail -3 "$GRPC_LOG" >&2
    exit 1
fi
echo "[init] ✓ gRPC stub 생성 OK"

# 8. config_loader 실 동작 (risk_config.yaml 5 섹션 파싱)
$PYTHON -c "
import sys
sys.path.insert(0, 'new')
from src.utils.config_loader import load
cfg = load('risk_config.yaml')
missing = []
for section in ['rate_limits', 'pit_safety', 'event_normalizer', 'auth_defaults', 'connector_defaults']:
    if section not in cfg:
        missing.append(section)
if missing:
    print('[init] ✗ risk_config.yaml 누락 섹션:', missing)
    sys.exit(1)
print('[init] ✓ risk_config.yaml 5 섹션 파싱 OK')
" 2>&1 || exit 1

# 9. pytest 수집 (실행 전 discovery 성공 여부)
echo "[init] pytest 수집 중..."
if ! $PYTHON -m pytest new/tests/ --collect-only -q >"$COLLECT_LOG" 2>&1; then
    echo "[init] ✗ pytest 수집 실패 (pytest 미설치 또는 import 오류)" >&2
    echo "[init]   → pip install pytest 또는 conda install pytest 필요" >&2
    tail -3 "$COLLECT_LOG" >&2
    exit 1
fi
tail -5 "$COLLECT_LOG"
echo "[init] ✓ pytest 수집 OK"

echo ""
echo "[init] ✓ 환경 확인 완료 (체크 9/9 PASS)"
echo "[init] 다음: ./smoke.sh 로 실 동작 검증"

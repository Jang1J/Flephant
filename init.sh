#!/bin/bash
# Elephant Lab v3 — 환경 부트스트랩
# 세션 시작 시 실행하여 환경이 살아있는지 확인

set -e
set -u

PYTHON="/opt/anaconda3/envs/elephant/bin/python"

echo "[init] Elephant Lab v3 환경 확인 시작..."

# 1. Python 환경
if [ -d "/opt/anaconda3/envs/elephant" ]; then
    echo "[init] ✓ Python 환경: /opt/anaconda3/envs/elephant"
else
    echo "[init] ✗ Python 환경 없음. conda create -n elephant python=3.11 필요" >&2
    exit 1
fi

# 2. .env 파일
if [ -f ".env" ]; then
    echo "[init] ✓ .env 파일 존재"
else
    echo "[init] ✗ .env 없음. cp .env.example .env 후 키 설정 필요" >&2
    exit 1
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

# 5. Python 버전 체크 (>=3.9 for zoneinfo)
PY_VER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
$PYTHON -c "
import sys
if sys.version_info < (3, 9):
    print('[init] ✗ Python 3.9+ 필요 (현재 %s.%s)' % sys.version_info[:2])
    sys.exit(1)
"
echo "[init] ✓ Python ${PY_VER} (>=3.9)"

# 6. 핵심 패키지 import 가능
$PYTHON -c "
import yaml, pathlib, zoneinfo, uuid, datetime, logging
" 2>&1 && echo "[init] ✓ 표준/필수 패키지 OK (yaml, pathlib, zoneinfo, uuid, datetime, logging)" || {
    echo "[init] ✗ 패키지 import 실패. pip install pyyaml 등 확인 필요" >&2
    exit 1
}

# 7. new/src/ 디렉토리 존재
if [ -d "new/src" ]; then
    echo "[init] ✓ new/src 존재"
else
    echo "[init] ✗ new/src 없음" >&2
    exit 1
fi

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
if ! $PYTHON -m pytest new/tests/ --collect-only -q > /tmp/elephant_collect.log 2>&1; then
    echo "[init] ✗ pytest 수집 실패 (pytest 미설치 또는 import 오류)" >&2
    echo "[init]   → pip install pytest 또는 conda install pytest 필요" >&2
    tail -3 /tmp/elephant_collect.log >&2
    exit 1
fi
tail -5 /tmp/elephant_collect.log
echo "[init] ✓ pytest 수집 OK"

echo ""
echo "[init] ✓ 환경 확인 완료 (체크 9/9 PASS)"
echo "[init] 다음: ./smoke.sh 로 실 동작 검증"

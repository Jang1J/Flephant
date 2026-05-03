#!/bin/bash
# Elephant Lab v3 — Smoke Test (메타 확인 + 실 코드 동작 검증)
# S0-8: 실구현 단계로 확장. 코드 import/인스턴스 생성/유틸 동작까지 검증.

set -e
set -u

PYTHON="/opt/anaconda3/envs/elephant/bin/python"
PASS=0
FAIL=0

_pass() { echo "[smoke] ✓ $1"; PASS=$((PASS+1)); }
_fail() { echo "[smoke] ✗ $1" >&2; FAIL=$((FAIL+1)); }

echo "[smoke] Elephant Lab v3 Smoke Test 시작..."
echo ""

# ===========================================================================
# 기존 메타 확인 (1~3)
# ===========================================================================

# 1. CLAUDE.md 250줄 이하 (S0 cleanup 반영: audit + Sprint 5 + 4축 정합 추가로 증가)
LINES=$(wc -l < CLAUDE.md)
if [ "$LINES" -le 250 ]; then
    _pass "CLAUDE.md ${LINES}줄 (<=250)"
else
    _fail "CLAUDE.md ${LINES}줄 (>250, 축소 필요)"
fi

# 2. api_contracts.md 존재 + 비어있지 않음
if [ -s "new/specs/api_contracts.md" ]; then
    _pass "api_contracts.md 존재 (SSOT)"
else
    _fail "api_contracts.md 없거나 비어있음"
fi

# 3. risk_config.yaml 파싱 가능
if $PYTHON -c "import yaml; yaml.safe_load(open('new/config/risk_config.yaml'))" 2>/dev/null; then
    _pass "risk_config.yaml 파싱 OK"
else
    _fail "risk_config.yaml 파싱 실패"
fi

# 3b. config yaml 10개 전부 존재
CONFIG_COUNT=$(ls new/config/*.yaml 2>/dev/null | wc -l)
if [ "$CONFIG_COUNT" -ge 10 ]; then
    _pass "config yaml ${CONFIG_COUNT}개 (>=10)"
else
    _fail "config yaml ${CONFIG_COUNT}개 (<10)"
fi

# 3c. feature_list.json 파싱 가능
if $PYTHON -c "import json; json.load(open('feature_list.json'))" 2>/dev/null; then
    _pass "feature_list.json 파싱 OK"
else
    _fail "feature_list.json 파싱 실패 (파일 없거나 JSON 오류)"
fi

# ===========================================================================
# 코드 실 동작 검증 (4~11)
# ===========================================================================

# 4. pytest 전체 실행
echo ""
echo "[smoke] --- pytest 실행 ---"
if $PYTHON -m pytest new/tests/ -q 2>&1 | tee /tmp/elephant_pytest.log | tail -5; then
    PYTEST_RESULT=$(tail -1 /tmp/elephant_pytest.log)
    if echo "$PYTEST_RESULT" | grep -q "failed\|error"; then
        _fail "pytest: 일부 실패 (로그: /tmp/elephant_pytest.log)"
    else
        _pass "pytest 전체 PASS ($PYTEST_RESULT)"
    fi
else
    _fail "pytest 실행 자체 실패"
fi

# 5. AuthManager validate_env (값 비노출)
echo ""
echo "[smoke] --- AuthManager validate_env ---"
AUTH_OUT=$($PYTHON -c "
import sys
sys.path.insert(0, 'new')
from src.utils.auth import AuthManager
auth = AuthManager()
env = auth.validate_env()
present = [k for k, v in env.items() if v]
missing = [k for k, v in env.items() if not v]
print(f'  존재: {len(present)}/{len(env)}. 누락: {missing if missing else \"없음\"}')
" 2>/dev/null) || AUTH_OUT="  [오류] AuthManager 초기화 실패"
echo "$AUTH_OUT"
if echo "$AUTH_OUT" | grep -q "오류\|Error\|Traceback"; then
    _fail "AuthManager validate_env 실패"
else
    _pass "AuthManager validate_env 동작 OK"
fi

# 6. RateLimiter 8 소스 생성
echo ""
echo "[smoke] --- RateLimiter 8 소스 ---"
RL_OUT=$($PYTHON -c "
import sys
sys.path.insert(0, 'new')
from src.utils.rate_limiter import RateLimiter
sources = ['kis_rest', 'kis_ws', 'dart', 'krx_investor_flow', 'naver', 'ecos', 'community', 'us_market']
ok = []
fail = []
for src in sources:
    try:
        rl = RateLimiter(src)
        stats = rl.stats()
        print(f'  {src}: capacity={stats[\"capacity\"]}, refill={stats[\"refill_rate\"]}/s')
        ok.append(src)
    except Exception as e:
        print(f'  {src}: 실패 ({e})')
        fail.append(src)
if fail:
    print(f'FAIL_SOURCES: {fail}')
    sys.exit(1)
print(f'ALL_OK: {len(ok)}/8')
" 2>&1)
echo "$RL_OUT"
if echo "$RL_OUT" | grep -q "^FAIL_SOURCES\|Traceback\|Error"; then
    _fail "RateLimiter 소스 일부 실패"
else
    _pass "RateLimiter 8 소스 생성 OK"
fi

# 7. EventNormalizer 생성 + SUPPORTED_SOURCES 출력
echo ""
echo "[smoke] --- EventNormalizer ---"
EN_OUT=$($PYTHON -c "
import sys
sys.path.insert(0, 'new')
from src.data.event_normalizer import EventNormalizer
en = EventNormalizer()
srcs = sorted(en.SUPPORTED_SOURCES)
print(f'  SUPPORTED_SOURCES ({len(srcs)}개): {srcs}')
" 2>&1) || EN_OUT="  [오류] EventNormalizer 초기화 실패"
echo "$EN_OUT"
if echo "$EN_OUT" | grep -q "오류\|Error\|Traceback"; then
    _fail "EventNormalizer 생성 실패"
else
    _pass "EventNormalizer 생성 OK"
fi

# 8. 커넥터 클래스 import
echo ""
echo "[smoke] --- 커넥터 import ---"
CONN_OUT=$($PYTHON -c "
import sys
sys.path.insert(0, 'new')
results = []
connectors = [
    ('src.connectors.dart_rest', 'DARTRestClient'),
    ('src.connectors.krx_rest', 'KRXRestClient'),
    ('src.connectors.kis_rest', 'KISRestClient'),
    ('src.connectors.kis_ws', 'KISWebSocketClient'),
]
fail = []
for mod, cls in connectors:
    try:
        m = __import__(mod, fromlist=[cls])
        getattr(m, cls)
        print(f'  {cls}: import OK')
    except Exception as e:
        print(f'  {cls}: 실패 ({e})')
        fail.append(cls)
if fail:
    print(f'FAIL_CONNECTORS: {fail}')
    sys.exit(1)
" 2>&1)
echo "$CONN_OUT"
if echo "$CONN_OUT" | grep -q "^FAIL_CONNECTORS\|Traceback"; then
    _fail "커넥터 import 실패"
else
    _pass "커넥터 4종 import OK"
fi

# 9. id_factory 10개 함수 호출 + 유일성 검증
echo ""
echo "[smoke] --- id_factory ---"
ID_OUT=$($PYTHON -c "
import sys
sys.path.insert(0, 'new')
from src.utils import id_factory

funcs = [
    ('EVT',    id_factory.generate_event_id),
    ('MSG',    id_factory.generate_message_id),
    ('DEC',    id_factory.generate_decision_id),
    ('PP',     id_factory.generate_portfolio_patch_id),
    ('BUNDLE', id_factory.generate_bundle_id),
    ('BT',     id_factory.generate_backtest_id),
    ('RPT',    id_factory.generate_report_id),
    ('FCC',    id_factory.generate_failure_case_card_id),
    ('RGC',    id_factory.generate_regression_case_id),
    ('OP',     id_factory.generate_order_plan_id),
]
fail = []
for prefix, fn in funcs:
    id1 = fn()
    id2 = fn()
    if not id1.startswith(prefix):
        print(f'  {prefix}: prefix 불일치 (got {id1})')
        fail.append(prefix)
    elif id1 == id2:
        print(f'  {prefix}: 유일성 위반 (연속 2회 동일 ID)')
        fail.append(prefix)
    else:
        print(f'  {prefix}: {id1}  (유일성 OK)')
if fail:
    print(f'FAIL_IDS: {fail}')
    sys.exit(1)
print(f'ALL_OK: 10/10')
" 2>&1)
echo "$ID_OUT"
if echo "$ID_OUT" | grep -q "^FAIL_IDS\|Traceback\|Error"; then
    _fail "id_factory 유일성 검증 실패"
else
    _pass "id_factory 10개 함수 호출 + 유일성 OK"
fi

# 10. pit_guard 동작
echo ""
echo "[smoke] --- pit_guard ---"
PIT_OUT=$($PYTHON -c "
import sys
sys.path.insert(0, 'new')
from src.utils.pit_guard import is_pit_safe
# 과거 시점: PIT-safe 여야 함
result = is_pit_safe('2026-04-19T09:00:00+09:00')
if result:
    print('  is_pit_safe(2026-04-19T09:00:00+09:00) =>', result, '(PASS)')
else:
    print('  is_pit_safe 과거 시점 False 반환 — 예상 True')
    sys.exit(1)
# 미래 시점: PIT-unsafe 여야 함
result2 = is_pit_safe('2099-01-01T00:00:00+09:00')
if not result2:
    print('  is_pit_safe(2099-01-01) =>', result2, '(미래 차단 OK)')
else:
    print('  is_pit_safe 미래 시점 True 반환 — 예상 False')
    sys.exit(1)
" 2>&1)
echo "$PIT_OUT"
if echo "$PIT_OUT" | grep -q "Traceback\|Error\|예상"; then
    _fail "pit_guard 동작 실패"
else
    _pass "pit_guard is_pit_safe 과거/미래 분기 OK"
fi

# 11. state_machine PipelineState enum 10개 상태 확인
echo ""
echo "[smoke] --- state_machine ---"
SM_OUT=$($PYTHON -c "
import sys
sys.path.insert(0, 'new')
from src.ops.state_machine import PipelineState
expected = {
    'BOOTSTRAP', 'HOT_RUNNING',
    'MODE_B_IDLE', 'MODE_B_EVOLVING', 'MODE_B_BACKTEST',
    'MODE_B_OPERATOR_REVIEW', 'MODE_B_DEPLOY', 'MODE_B_BLOCKED',
    'SHUTDOWN', 'ERROR',
}
actual = {s.name for s in PipelineState}
missing = expected - actual
extra = actual - expected
if missing:
    print(f'  누락 상태: {missing}')
    sys.exit(1)
if extra:
    print(f'  예상 외 상태: {extra} (경고, 오류는 아님)')
for s in sorted(actual):
    print(f'  {s}: OK')
print(f'ALL_OK: {len(actual)}개 상태 확인')
" 2>&1)
echo "$SM_OUT"
if echo "$SM_OUT" | grep -q "누락 상태\|Traceback\|Error"; then
    _fail "PipelineState enum 상태 누락"
else
    _pass "PipelineState 10개 상태 전부 존재"
fi

# ===========================================================================
# 결과 요약
# ===========================================================================
echo ""
echo "================================================================"
echo "[smoke] 결과: ${PASS} PASS / ${FAIL} FAIL"
if [ "$FAIL" -eq 0 ]; then
    echo "[smoke] ✓ 전체 PASS (${PASS}/13)"
    exit 0
else
    echo "[smoke] ✗ ${FAIL}건 실패 — 확인 필요"
    exit 1
fi

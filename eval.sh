#!/bin/bash
# Elephant Lab v3 — 독립 검증 (구현 후 실행)
# reviewer subagent 대안. 구현 직후 빠르게 돌리는 용도.

set -e
set -o pipefail

PYTHON="${PYTHON:-/opt/anaconda3/envs/elephant/bin/python}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-1}"

echo "[eval] 독립 검증 시작..."
echo "[eval] python=$PYTHON"
echo "[eval] thread caps: OMP=$OMP_NUM_THREADS OPENBLAS=$OPENBLAS_NUM_THREADS MKL=$MKL_NUM_THREADS NUMEXPR=$NUMEXPR_NUM_THREADS"

# 1. smoke test 먼저
./smoke.sh || { echo "[eval] ✗ smoke test 실패 — 기본 환경 문제"; exit 1; }

# 2. CLAUDE.md 200줄 체크
LINES=$(wc -l < CLAUDE.md)
if [ "$LINES" -gt 200 ]; then
    echo "[eval] ⚠ CLAUDE.md ${LINES}줄 (>200, 축소 권장)"
fi

# 3. v2.x 잔재 스캔
V2X_FILES=$(grep -rl "architect-reviewer\|idea-merger\|qa-inspector\|gpt-feedback-tracker" new/ CLAUDE.md README.md 2>/dev/null || true)
if [ -n "$V2X_FILES" ]; then
    V2X_COUNT=$(printf '%s\n' "$V2X_FILES" | wc -l | tr -d ' ')
    echo "[eval] ⚠ v2.x 잔재 ${V2X_COUNT}건 발견"
    printf '%s\n' "$V2X_FILES"
else
    echo "[eval] ✓ v2.x 잔재 0건"
fi

# 4. 공개 배포물에 private handoff 파일이 섞이지 않았는지 확인
PRIVATE_TRACKED=""
for path in feature_list.json PROGRESS.md Codex-progress.md claude-progress.md; do
    if git ls-files --error-unmatch "$path" >/dev/null 2>&1; then
        PRIVATE_TRACKED="${PRIVATE_TRACKED}${path}\n"
    fi
done
if [ -n "$PRIVATE_TRACKED" ]; then
    echo "[eval] ✗ private handoff 파일이 tracked 상태" >&2
    printf '%b' "$PRIVATE_TRACKED" >&2
    exit 1
fi
echo "[eval] ✓ private handoff 파일 제외 확인"

echo ""
echo "[eval] 검증 완료"

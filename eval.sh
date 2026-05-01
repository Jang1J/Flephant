#!/bin/bash
# Elephant Lab v3 — 독립 검증 (구현 후 실행)
# reviewer subagent 대안. 구현 직후 빠르게 돌리는 용도.

set -e

PYTHON="/opt/anaconda3/envs/elephant/bin/python"

echo "[eval] 독립 검증 시작..."

# 1. smoke test 먼저
./smoke.sh || { echo "[eval] ✗ smoke test 실패 — 기본 환경 문제"; exit 1; }

# 2. CLAUDE.md 200줄 체크
LINES=$(wc -l < CLAUDE.md)
if [ "$LINES" -gt 200 ]; then
    echo "[eval] ⚠ CLAUDE.md ${LINES}줄 (>200, 축소 권장)"
fi

# 3. v2.x 잔재 스캔
V2X_COUNT=$(grep -rl "architect-reviewer\|idea-merger\|qa-inspector\|gpt-feedback-tracker" .claude/ new/ CLAUDE.md README.md 2>/dev/null | wc -l)
if [ "$V2X_COUNT" -gt 0 ]; then
    echo "[eval] ⚠ v2.x 잔재 ${V2X_COUNT}건 발견"
    grep -rl "architect-reviewer\|idea-merger\|qa-inspector\|gpt-feedback-tracker" .claude/ new/ CLAUDE.md README.md 2>/dev/null
else
    echo "[eval] ✓ v2.x 잔재 0건"
fi

# 4. feature_list.json 파싱 + 진행률
if $PYTHON -c "
import json
data = json.load(open('feature_list.json'))
total = sum(len(s['features']) for s in data['sprints'])
done = sum(1 for s in data['sprints'] for f in s['features'] if f['status'] == 'done')
print(f'[eval] ✓ feature_list.json: {done}/{total} 완료 ({done*100//total}%)')
" 2>/dev/null; then
    :
else
    echo "[eval] ✗ feature_list.json 파싱 실패"
fi

# 5. claude-progress.md 존재
if [ -f "claude-progress.md" ]; then
    echo "[eval] ✓ claude-progress.md 존재"
else
    echo "[eval] ⚠ claude-progress.md 없음 — 세션 handoff 불가"
fi

echo ""
echo "[eval] 검증 완료"

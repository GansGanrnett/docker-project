#!/bin/bash
# diagnose-ci.sh <PR_NUM> — показать failed steps и первые ошибки
set -u
PR=$1
REPO="GansGanrnett/docker-project"
[ -z "$PR" ] && { echo "Usage: $0 <PR_NUM>"; exit 1; }

gh pr view "$PR" -R "$REPO" --json statusCheckRollup \
  --jq '.statusCheckRollup[] | select(.conclusion=="FAILURE") | {name, detailsUrl}'

RUN_ID=$(gh pr view "$PR" -R "$REPO" --json statusCheckRollup \
  --jq '.statusCheckRollup[] | select(.conclusion=="FAILURE") | .detailsUrl' \
  | grep -oE 'runs/[0-9]+' | head -1 | cut -d/ -f2)

[ -z "$RUN_ID" ] && { echo "No failed run."; exit 0; }

gh run view "$RUN_ID" -R "$REPO" --log-failed > /tmp/ci-fail.log
echo "=== /tmp/ci-fail.log ==="
wc -l < /tmp/ci-fail.log
grep -n -E "##\[error\]|error [A-Z]+[0-9]+|Failed:|Assert\.|Traceback" \
  /tmp/ci-fail.log | head -30

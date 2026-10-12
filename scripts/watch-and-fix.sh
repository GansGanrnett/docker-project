#!/bin/bash
# watch-and-fix.sh — цикл «watch CI → скачать лог → пауза → повтор»
# Использование: ./scripts/watch-and-fix.sh [max_attempts]
# Работает из feature-ветки с открытым PR.

set -u
MAX_ATTEMPTS=${1:-3}
REPO="GansGanrnett/docker-project"
BRANCH=$(git branch --show-current)

if [ "$BRANCH" = "main" ]; then
  echo "На main. Скрипт работает из feature-ветки с PR."
  exit 1
fi

PR=$(gh pr list -R "$REPO" --head "$BRANCH" --json number --jq '.[0].number')
if [ -z "$PR" ] || [ "$PR" = "null" ]; then
  echo "Нет открытого PR для ветки $BRANCH. Создай сначала."
  exit 1
fi
echo "PR #$PR, ветка $BRANCH, максимум $MAX_ATTEMPTS итераций."

for i in $(seq 1 "$MAX_ATTEMPTS"); do
  echo ""
  echo "=== Итерация $i / $MAX_ATTEMPTS ==="
  echo "Жду завершения CI (gh pr checks --watch)..."

  if gh pr checks "$PR" -R "$REPO" --watch --interval 20; then
    echo ""
    echo "✓ CI зелёный. Мержу PR #$PR"
    gh pr merge "$PR" -R "$REPO" --merge
    git checkout main && git pull
    git log --oneline -3
    exit 0
  fi

  echo ""
  echo "✗ CI красный. Скачиваю лог..."
  RUN_ID=$(gh pr view "$PR" -R "$REPO" --json statusCheckRollup \
    --jq '.statusCheckRollup[] | select(.conclusion=="FAILURE") | .detailsUrl' \
    | grep -oE 'runs/[0-9]+' | head -1 | cut -d/ -f2)

  if [ -z "$RUN_ID" ]; then
    echo "RUN_ID не найден. Пропускаю итерацию."
    continue
  fi

  gh run view "$RUN_ID" -R "$REPO" --log-failed > /tmp/ci-fail.log
  echo "Лог сохранён в /tmp/ci-fail.log"
  echo ""
  echo "--- Ошибки ---"
  grep -n -E "##\[error\]|error [A-Z]+[0-9]+|Failed:|Assert\.|Traceback" \
    /tmp/ci-fail.log | head -20

  echo ""
  echo "Открой /tmp/ci-fail.log или запусти агента с этим логом."
  echo "После правки и push — нажми Enter для продолжения."
  read -r _
done

echo ""
echo "Достигнут лимит $MAX_ATTEMPTS итераций. CI всё ещё красный."
exit 1

#!/bin/bash
# autonomous-fix.sh — автономный цикл: CI watch → лог → opencode fix → push → повтор
# Использование: ./scripts/autonomous-fix.sh [max_attempts]

set -u
MAX=${1:-3}
REPO="GansGanrnett/docker-project"
BRANCH=$(git branch --show-current)

if [ "$BRANCH" = "main" ]; then
  echo "На main. Выйди в feature-ветку с открытым PR."
  exit 1
fi

if ! command -v opencode >/dev/null 2>&1; then
  echo "opencode не установлен."
  exit 1
fi

PR=$(gh pr list -R "$REPO" --head "$BRANCH" --json number --jq '.[0].number')
if [ -z "$PR" ] || [ "$PR" = "null" ]; then
  echo "Нет открытого PR для ветки $BRANCH."
  exit 1
fi

echo "PR #$PR на ветке $BRANCH. До $MAX итераций."

for i in $(seq 1 "$MAX"); do
  echo ""
  echo "=== Итерация $i / $MAX ==="

  if gh pr checks "$PR" -R "$REPO" --watch --interval 20; then
    echo ""
    echo "✓ CI зелёный. Мержу PR #$PR."
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
    echo "RUN_ID не найден — пропускаю итерацию."
    continue
  fi

  gh run view "$RUN_ID" -R "$REPO" --log-failed > /tmp/ci-fail.log
  echo "Лог: /tmp/ci-fail.log ($(wc -l < /tmp/ci-fail.log) строк)"

  echo ""
  echo "--- Первые ошибки ---"
  grep -nE "##\[error\]|error [A-Z]+[0-9]+|Failed:|Assert\.|Traceback" \
    /tmp/ci-fail.log | head -15

  PROMPT="CI упал на PR #$PR в ветке $BRANCH. Полный лог: /tmp/ci-fail.log. Прочитай лог, найди причину, исправь код в репозитории. Правила: не трогай ci.yml, не создавай outputs/, коммиты на английском ≤72 символов. После правки сделай git add нужных файлов и git commit."

  echo ""
  echo "→ opencode run (автофикс)..."
  opencode run "$PROMPT" 2>&1 | tail -50

  if git status --short | grep -qE "^ M|^M|^\?\?"; then
    echo ""
    echo "opencode не закоммитил — коммичу вручную."
    git add -A
    git commit -m "fix(ci): auto-fix attempt $i" || true
  fi

  if git log origin/"$BRANCH".."$BRANCH" --oneline 2>/dev/null | grep -q .; then
    echo ""
    echo "→ Push в $BRANCH..."
    git push origin "$BRANCH" 2>&1 | tail -3
  else
    echo ""
    echo "⚠ Нет новых коммитов — opencode не справился. Стоп."
    exit 1
  fi
done

echo ""
echo "Лимит $MAX итераций исчерпан. CI всё ещё красный."
exit 1

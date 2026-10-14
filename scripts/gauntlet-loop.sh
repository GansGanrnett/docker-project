#!/bin/bash
# gauntlet-loop.sh — автономный цикл «агент правит код → gauntlet → повтор».
# Usage:
#   bash scripts/gauntlet-loop.sh "<goal>" [max_iter] [gauntlet_cmd] [agent_cmd]
#
# Пример:
#   bash scripts/gauntlet-loop.sh "Починить smoke" 10 "bash scripts/smoke.sh" \
#     "aider --model ollama/qwen2.5-coder:7b --yes --no-auto-commits"

set -uo pipefail

GOAL="${1:-Починить gauntlet}"
MAX="${2:-10}"
GAUNTLET_CMD="${3:-bash scripts/smoke.sh}"
AGENT_CMD="${4:-aider --model ollama/qwen2.5-coder:7b --yes --no-auto-commits}"

LOG_DIR="/tmp/gauntlet"
mkdir -p "$LOG_DIR"
ITER=0

while [ "$ITER" -lt "$MAX" ]; do
    ITER=$((ITER + 1))
    echo ""
    echo "=== Gauntlet iteration $ITER/$MAX ==="
    echo ""

    echo "→ Gauntlet: $GAUNTLET_CMD"
    GAUNTLET_OUT="$LOG_DIR/iteration-$ITER.log"
    if eval "$GAUNTLET_CMD" > "$GAUNTLET_OUT" 2>&1; then
        echo "✓ Gauntlet PASS на итерации $ITER"
        tail -20 "$GAUNTLET_OUT"
        exit 0
    fi

    echo "✗ Gauntlet FAIL. Лог: $GAUNTLET_OUT"
    tail -30 "$GAUNTLET_OUT"

    ERROR_TAIL=$(tail -100 "$GAUNTLET_OUT")
    PROMPT="Цель: $GOAL

Текущая итерация: $ITER/$MAX.

Gauntlet-команда \`$GAUNTLET_CMD\` упала. Последние 100 строк лога:
---
$ERROR_TAIL
---

Исправь код так, чтобы gauntlet прошёл. Не задавай вопросов, делай правки."

    echo ""
    echo "→ Agent: $AGENT_CMD"
    if ! eval "$AGENT_CMD \"$PROMPT\"" 2>&1 | tail -20; then
        echo "⚠ Agent упал. Продолжаем."
    fi
done

echo ""
echo "🛑 Достигнут лимит $MAX итераций. Gauntlet не пройден."
echo "Последний лог: $LOG_DIR/iteration-$MAX.log"
exit 1

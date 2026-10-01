#!/bin/sh
# Точка входа для разработчика и CI.
#
# Реализация лежит рядом с чартом, потому что hook-job монтирует её в ConfigMap
# через .Files.Get: Helm не читает файлы за пределами каталога чарта, поэтому
# единственное честное место для исходника - infrastructure/kubernetes/helm-charts/auth-service/files/.
# Держать две копии крипто-логики нельзя - они разъедутся, и рассинхрон
# проявится только в кластере.
#
#   scripts/generate-jwt-keys.sh [OUT_DIR]          сгенерировать, если ключей нет
#   scripts/generate-jwt-keys.sh --check [OUT_DIR]  проверить и ничего не менять
#
# Примеры:
#   scripts/generate-jwt-keys.sh
#   scripts/generate-jwt-keys.sh --check services/auth-service/configs

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CANONICAL="$SCRIPT_DIR/../infrastructure/kubernetes/helm-charts/auth-service/files/generate-jwt-keys.sh"

if [ ! -f "$CANONICAL" ]; then
    printf 'generate-jwt-keys.sh: не найден %s\n' "$CANONICAL" >&2
    exit 1
fi

exec sh "$CANONICAL" "$@"
#!/bin/bash
# Smoke end-to-end: login -> cart -> order -> payment -> Paid.
# Требует: docker compose up -d + AUTH_ADMIN_PASSWORD в .env.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
SMOKE_USER="${SMOKE_USER:-admin}"
SMOKE_PASSWORD="${SMOKE_PASSWORD:-}"
SMOKE_WAIT_SEC="${SMOKE_WAIT_SEC:-10}"

if [ -z "$SMOKE_PASSWORD" ] && [ -f .env ]; then
    SMOKE_PASSWORD=$(grep '^AUTH_ADMIN_PASSWORD=' .env | cut -d= -f2-)
fi
if [ -z "$SMOKE_PASSWORD" ] || [[ "$SMOKE_PASSWORD" == "<"* ]]; then
    echo "ERROR: SMOKE_PASSWORD не задан или placeholder"; exit 1
fi
command -v ./scripts/jq.exe >/dev/null || { echo "ERROR: jq не установлен"; exit 1; }

say() { echo -e "\n=== $* ==="; }
fail() { echo "FAIL: $*"; exit 1; }

say "1. Логин"
TOKEN=$(curl -sS -X POST "$BASE_URL/api/v1/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"$SMOKE_USER\",\"password\":\"$SMOKE_PASSWORD\"}" \
    | jq -r '.access_token // empty')
[ -n "$TOKEN" ] || fail "нет access_token"
echo "OK: token получен"

say "2. Корзина"
curl -sS -X POST "$BASE_URL/api/v1/cart/items" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"productId":1,"name":"Smoke","quantity":1,"price":999.99}' \
    | ./scripts/jq.exe -e '.items' >/dev/null || fail "cart failed"
echo "OK"

say "3. Заказ"
ORDER=$(curl -sS -X POST "$BASE_URL/api/v1/orders" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '[{"productId":1,"quantity":1}]')
ORDER_ID=$(echo "$ORDER" | ./scripts/jq.exe -r '.id // .Id // empty')
AMOUNT=$(echo "$ORDER" | ./scripts/jq.exe -r '.totalAmount // .TotalAmount // empty')
[ -n "$ORDER_ID" ] || fail "нет orderId: $ORDER"
echo "OK: orderId=$ORDER_ID amount=$AMOUNT"

say "4. Оплата"
curl -sS -X POST "$BASE_URL/api/v1/payments" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"orderId\":\"$ORDER_ID\",\"amount\":$AMOUNT,\"cardNumber\":\"4242424242424242\"}" \
    >/dev/null
echo "OK"

say "5. Ожидание Paid (${SMOKE_WAIT_SEC}s)"
for i in $(seq 1 "$SMOKE_WAIT_SEC"); do
    STATUS=$(curl -sS "$BASE_URL/api/v1/orders" -H "Authorization: Bearer $TOKEN" \
        | ./scripts/jq.exe -r ".[] | select((.id // .Id) == \"$ORDER_ID\") | .status // .Status // empty")
    echo "  $i: status=$STATUS"
    [ "$STATUS" = "Paid" ] && { echo ""; echo "SMOKE PASS: order=$ORDER_ID Paid"; exit 0; }
    sleep 1
done
fail "order=$ORDER_ID не Paid за ${SMOKE_WAIT_SEC}s (last=$STATUS)"

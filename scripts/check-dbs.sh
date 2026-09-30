#!/bin/bash
# Проверка готовности БД и инфраструктуры docker-compose.
# Имена контейнеров соответствуют сервисам в корневом docker-compose.yml.
set -e

docker compose ps --format "table {{.Name}}\t{{.Status}}"

echo "---"
# Postgres-контейнеры: проверяем через pg_isready внутри сервиса
for service in auth-db catalog-db; do
  if docker compose ps "$service" --format "{{.Status}}" | grep -q "Up"; then
    echo -n "$service (pg_isready): "
    docker compose exec -T "$service" pg_isready -U "${AUTH_DB_USER:-auth_user}" || echo "not ready"
  else
    echo "$service: container not running"
  fi
done

# MongoDB: проверяем командой ping через mongosh
if docker compose ps order-db --format "{{.Status}}" | grep -q "Up"; then
  echo -n "order-db (mongosh ping): "
  docker compose exec -T order-db mongosh --quiet --eval "db.adminCommand('ping').ok" || echo "not ready"
else
  echo "order-db: container not running"
fi

# Redis: проверяем ping с паролем из .env
if docker compose ps cart-cache --format "{{.Status}}" | grep -q "Up"; then
  echo -n "cart-cache (redis ping): "
  docker compose exec -T cart-cache redis-cli --no-auth-warning -a "${REDIS_PASSWORD}" ping || echo "not ready"
else
  echo "cart-cache: container not running"
fi

# RabbitMQ: проверяем через rabbitmq-diagnostics
if docker compose ps rabbitmq --format "{{.Status}}" | grep -q "Up"; then
  echo -n "rabbitmq: "
  docker compose exec -T rabbitmq rabbitmq-diagnostics check_running || echo "not ready"
else
  echo "rabbitmq: container not running"
fi
#!/bin/bash
docker compose ps --format "table {{.Name}}\t{{.Status}}"
for service in postgres-auth postgres-catalog postgres-order postgres-payment redis-cart mongodb-analytics; do
  echo -n "$service: "
  docker compose exec $service pg_isready -U auth_user 2>/dev/null || echo "not ready"
done

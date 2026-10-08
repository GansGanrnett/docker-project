# Correlation-ID Design Spec — #48

## Header
- Name: `X-Correlation-ID`
- Case-insensitive match at gateway.
- Response must include same header.

## Generation
- Gateway (`api-gateway`): `crypto.randomUUID()` if header missing; propagate existing value unchanged.
- No rejection for missing header (backward compatible).

## Propagation (required per #48)
- `gateway` → `proxyReq.setHeader('X-Correlation-ID', req.correlationId)`
- `auth-service` (`CorrelationIdFilter`): read/validate, include in logs
- `order-service` / `analytics-service`: include in RabbitMQ message properties / log context
- `catalog-service` (future consumer): must read from RabbitMQ message header

## Scope
- Design only; middleware implementation in PR-2 (`gateway/auth-correlation`).
- Does NOT change service business logic.

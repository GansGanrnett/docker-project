const express = require('express');
const { createProxyMiddleware } = require('http-proxy-middleware');
const client = require('prom-client');
const CircuitBreaker = require('opossum');
const axios = require('axios');
const rateLimit = require('express-rate-limit');
const verifyToken = require('./authMiddleware');
const limiters = require('./rateLimit/limiters');
require('dotenv').config();

const app = express();
const PORT = process.env.PORT || 8080;

client.collectDefaultMetrics();

const httpRequestCounter = new client.Counter({
    name: 'api_gateway_http_requests_total',
    help: 'Total number of HTTP requests processed by API Gateway',
    labelNames: ['method', 'route', 'status']
});

const httpRequestDuration = new client.Histogram({
    name: 'api_gateway_http_request_duration_seconds',
    help: 'HTTP request latency in seconds',
    labelNames: ['method', 'route', 'status'],
    buckets: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10]
});

// Probe and scrape traffic is not business traffic. kubelet hits /health and
// /ready every 10s, and Prometheus scrapes on its own schedule; counting them
// would leave api_gateway_http_requests_total almost entirely probe noise with
// real requests as a rounding error.
const UNCOUNTED_PATHS = new Set(['/health', '/ready', '/metrics']);

app.use((req, res, next) => {
    console.log(`[API-GATEWAY] ${req.method} ${req.url}`);
    // Correlation-ID: generate if missing, propagate unchanged
    let correlationId = req.headers['x-correlation-id'] || req.headers['X-Correlation-ID'];
    if (!correlationId) {
        correlationId = require('crypto').randomUUID();
    }
    req.correlationId = correlationId;
    res.setHeader('X-Correlation-ID', correlationId);
    const endTimer = httpRequestDuration.labels(req.method, req.path, '').startTimer();
    if (!UNCOUNTED_PATHS.has(req.path)) {
        res.on('finish', () => {
            httpRequestCounter.labels(req.method, req.path, res.statusCode).inc();
            endTimer({ status: res.statusCode });
        });
    } else {
        res.on('finish', () => {
            endTimer({ status: res.statusCode });
        });
    }
    next();
});

app.get('/metrics', async (req, res) => {
    res.set('Content-Type', client.register.contentType);
    res.end(await client.register.metrics());
});

// Liveness: the process is up and can accept connections. No dependency is
// checked - a liveness probe that fails during a downstream outage restarts
// every replica without fixing anything.
app.get('/health', (req, res) => {
    res.status(200).json({ status: 'UP', service: 'api-gateway' });
});

// Readiness equals liveness here: the gateway is stateless and forwards on
// demand. A catalog or auth outage must not pull the gateway out of the load
// balancer - that belongs to the circuit breaker, and failing readiness would
// turn a partial outage into a total one.
app.get('/ready', (req, res) => {
    res.status(200).json({ status: 'UP', service: 'api-gateway' });
});

// === GLOBAL PER-IP LIMITER ===
app.use(limiters.global);

// === РЕЗИЛИЕНТНЫЙ СЛОЙ КАТАЛОГА (Go-Service) С ЗАЩИТОЙ CIRCUIT BREAKER ===
const CATALOG_URL = process.env.CATALOG_SERVICE_URL || 'http://catalog-service:8082';

async function fetchCatalogProducts() {
    const response = await axios.get(`${CATALOG_URL}/products`, { timeout: 200 });
    return response.data;
}

const catalogBreaker = new CircuitBreaker(fetchCatalogProducts, {
    timeout: 250,
    errorThresholdPercentage: 50,
    resetTimeout: 10000
});

catalogBreaker.fallback(() => {
    console.log("[CIRCUIT-BREAKER] Fallback activated! Serving cached static catalog matrix.");
    return [
        { "id": 999, "name": "Кэшированный Товар (Резервный контур)", "price": 0.00, "description": "Режим защиты от сбоев" }
    ];
});

app.get('/api/v1/catalog/products', async (req, res) => {
    try {
        const data = await catalogBreaker.fire();
        res.json(data);
    } catch (error) {
        res.status(500).json({ error: "Каталог недоступен, ошибка отказоустойчивого слоя." });
    }
});

// === ЛИМИТ ЗАПРОСОВ: auth-strict ===
const authLimiter = limiters.auth;

// === МАРШРУТИЗАЦИЯ ПЛАТФОРМЫ ===
app.use('/api/v1/auth', authLimiter, createProxyMiddleware({
    target: process.env.AUTH_SERVICE_URL || 'http://auth-service:8081',
    changeOrigin: true,
    pathRewrite: { '^/api/v1/auth': '' },
    onProxyReq: (proxyReq, req) => {
        proxyReq.setHeader('X-Correlation-ID', req.correlationId);
    },
}));

app.use('/api/v1/cart', limiters.user, verifyToken(), createProxyMiddleware({
    target: process.env.CART_SERVICE_URL || 'http://cart-service:8083',
    changeOrigin: true,
    onProxyReq: (proxyReq, req) => {
        if (req.correlationId) proxyReq.setHeader('X-Correlation-ID', req.correlationId);
    },
    pathRewrite: { '^/api/v1/cart': '' },
}));

app.use('/api/v1/payments', limiters.user, verifyToken('ROLE_ADMIN'), createProxyMiddleware({
    target: process.env.PAYMENT_SERVICE_URL || 'http://payment-service:8085',
    changeOrigin: true,
    onProxyReq: (proxyReq, req) => {
        if (req.correlationId) proxyReq.setHeader('X-Correlation-ID', req.correlationId);
    },
    pathRewrite: { '^/api/v1/payments': '/api/v1/internal/payments/process' },
}));

// ИСПРАВЛЕНО: Прямое прозрачное проксирование без pathRewrite, ломающего вложенные пути FastAPI
app.use('/api/v1/analytics', limiters.user, verifyToken('ROLE_ADMIN'), createProxyMiddleware({
    target: process.env.ANALYTICS_SERVICE_URL || 'http://analytics-service:8000',
    changeOrigin: true,
    onProxyReq: (proxyReq, req) => {
        if (req.correlationId) proxyReq.setHeader('X-Correlation-ID', req.correlationId);
    }
}));

app.use('/api/v1/orders', limiters.user, verifyToken(), createProxyMiddleware({
    target: process.env.ORDER_SERVICE_URL || 'http://order-service:8084',
    changeOrigin: true,
    onProxyReq: (proxyReq, req) => {
        if (req.correlationId) proxyReq.setHeader('X-Correlation-ID', req.correlationId);
    },
}));

app.use((req, res) => {
    res.status(404).json({ error: 'Route not found on API Gateway' });
});

// Exported so tests can drive the routes without binding the deployment port.
// require.main is the guard: importing this file must not start a listener.
if (require.main === module) {
    app.listen(PORT, () => {
        console.log(`=== API Gateway Protection started on port ${PORT} ===`);
    });
}

module.exports = app;

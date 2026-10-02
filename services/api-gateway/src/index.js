const express = require('express');
const { createProxyMiddleware } = require('http-proxy-middleware');
const client = require('prom-client');
const CircuitBreaker = require('opossum');
const axios = require('axios');
const rateLimit = require('express-rate-limit');
const verifyToken = require('./authMiddleware');
require('dotenv').config();

const app = express();
const PORT = process.env.PORT || 8080;

client.collectDefaultMetrics();

const httpRequestCounter = new client.Counter({
    name: 'api_gateway_http_requests_total',
    help: 'Total number of HTTP requests processed by API Gateway',
    labelNames: ['method', 'route', 'status']
});

// Probe and scrape traffic is not business traffic. kubelet hits /health and
// /ready every 10s, and Prometheus scrapes on its own schedule; counting them
// would leave api_gateway_http_requests_total almost entirely probe noise with
// real requests as a rounding error.
const UNCOUNTED_PATHS = new Set(['/health', '/ready', '/metrics']);

app.use((req, res, next) => {
    console.log(`[API-GATEWAY] ${req.method} ${req.url}`);
    if (!UNCOUNTED_PATHS.has(req.path)) {
        res.on('finish', () => {
            httpRequestCounter.labels(req.method, req.path, res.statusCode).inc();
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

// === ЛИМИТ ЗАПРОСОВ: защита login-эндпоинта от перебора пароля (brute-force) ===
const loginLimiter = rateLimit({
    windowMs: 5 * 60 * 1000, // 5 минут
    max: 10,                 // максимум 10 попыток логина
    standardHeaders: true,
    legacyHeaders: false,
    message: { error: 'Too many login attempts. Try again later.' }
});

// === МАРШРУТИЗАЦИЯ ПЛАТФОРМЫ ===
app.use('/api/v1/auth', loginLimiter, createProxyMiddleware({
    target: process.env.AUTH_SERVICE_URL || 'http://auth-service:8081',
    changeOrigin: true,
    pathRewrite: { '^/api/v1/auth': '' },
}));

app.use('/api/v1/cart', verifyToken(), createProxyMiddleware({
    target: process.env.CART_SERVICE_URL || 'http://cart-service:8083',
    changeOrigin: true,
    pathRewrite: { '^/api/v1/cart': '' },
}));

app.use('/api/v1/payments', verifyToken('ROLE_ADMIN'), createProxyMiddleware({
    target: process.env.PAYMENT_SERVICE_URL || 'http://payment-service:8085',
    changeOrigin: true,
    pathRewrite: { '^/api/v1/payments': '/api/v1/internal/payments/process' },
}));

// ИСПРАВЛЕНО: Прямое прозрачное проксирование без pathRewrite, ломающего вложенные пути FastAPI
app.use('/api/v1/analytics', verifyToken('ROLE_ADMIN'), createProxyMiddleware({
    target: process.env.ANALYTICS_SERVICE_URL || 'http://analytics-service:8000',
    changeOrigin: true
}));

app.use((req, res, next) => {
    if (req.url.startsWith('/api/v1/orders')) {
        return verifyToken()(req, res, () => {
            createProxyMiddleware({
                target: process.env.ORDER_SERVICE_URL || 'http://order-service:8084',
                changeOrigin: true,
            })(req, res, next);
        });
    }
    next();
});

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

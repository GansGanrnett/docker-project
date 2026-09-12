const express = require('express');
const { createProxyMiddleware } = require('http-proxy-middleware');
const client = require('prom-client');
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

app.use((req, res, next) => {
    console.log(`[API-GATEWAY] ${req.method} ${req.url}`);
    res.on('finish', () => {
        httpRequestCounter.labels(req.method, req.path, res.statusCode).inc();
    });
    next();
});

app.get('/metrics', async (req, res) => {
    res.set('Content-Type', client.register.contentType);
    res.end(await client.register.metrics());
});

app.get('/health', (req, res) => {
    res.status(200).json({ status: 'UP', service: 'api-gateway' });
});

// ПУБЛИЧНЫЕ МАРШРУТЫ
app.use('/api/v1/auth', createProxyMiddleware({
    target: process.env.AUTH_SERVICE_URL || 'http://auth-service:8081',
    changeOrigin: true,
    pathRewrite: { '^/api/v1/auth': '' },
}));

app.use('/api/v1/catalog', createProxyMiddleware({
    target: process.env.CATALOG_SERVICE_URL || 'http://catalog-service:8082',
    changeOrigin: true,
    pathRewrite: { '^/api/v1/catalog': '' },
}));

// ЗАЩИЩЕННЫЕ МАРШРУТЫ
app.use('/api/v1/cart', verifyToken, createProxyMiddleware({
    target: process.env.CART_SERVICE_URL || 'http://cart-service:8083',
    changeOrigin: true,
    pathRewrite: { '^/api/v1/cart': '' },
}));

app.use((req, res, next) => {
    if (req.url.startsWith('/api/v1/orders')) {
        return verifyToken(req, res, () => {
            createProxyMiddleware({
                target: process.env.ORDER_SERVICE_URL || 'http://order-service:8084',
                changeOrigin: true,
            })(req, res, next);
        });
    }
    next();
});

// ЗАЩИЩЕННЫЙ МАРШРУТ: Сервис оплаты на Python FastAPI
app.use((req, res, next) => {
    if (req.url.startsWith('/api/v1/payments')) {
        return verifyToken(req, res, () => {
            createProxyMiddleware({
                target: process.env.PAYMENT_SERVICE_URL || 'http://payment-service:8085',
                changeOrigin: true,
                pathRewrite: { '^/api/v1/payments': '/api/v1/internal/payments/process' },
            })(req, res, next);
        });
    }
    next();
});

app.use((req, res) => {
    res.status(404).json({ error: 'Route not found on API Gateway' });
});

app.listen(PORT, () => {
    console.log(`=== API Gateway successfully started on port ${PORT} ===`);
});

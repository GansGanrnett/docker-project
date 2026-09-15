const express = require('express');
const { createProxyMiddleware } = require('http-proxy-middleware');
const client = require('prom-client');
const CircuitBreaker = require('opossum');
const axios = require('axios');
const crypto = require('crypto');
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

// Мидлвар генерации и сквозного проброса Correlation ID
app.use((req, res, next) => {
    const correlationId = req.headers['x-correlation-id'] || crypto.randomUUID();
    req.correlationId = correlationId;
    res.setHeader('X-Correlation-ID', correlationId);
    console.log(`[${correlationId}] [API-GATEWAY] ${req.method} ${req.url}`);
    
    res.on('finish', () => {
        httpRequestCounter.labels(req.method, req.path, res.statusCode).inc();
    });
    next();
});

const configureProxyOptions = (targetPath) => {
    return {
        target: targetPath,
        changeOrigin: true,
        on: {
            proxyReq: (proxyReq, req, res) => {
                proxyReq.setHeader('X-Correlation-ID', req.correlationId);
            }
        }
    };
};

app.get('/metrics', async (req, res) => {
    res.set('Content-Type', client.register.contentType);
    res.end(await client.register.metrics());
});

app.get('/health', (req, res) => {
    res.status(200).json({ status: 'UP', service: 'api-gateway' });
});

const CATALOG_URL = process.env.CATALOG_SERVICE_URL || 'http://catalog-service:80';
async function fetchCatalogProducts(correlationId) {
    const response = await axios.get(`${CATALOG_URL}/products`, { 
        timeout: 200,
        headers: { 'X-Correlation-ID': correlationId }
    });
    return response.data;
}
const catalogBreaker = new CircuitBreaker(fetchCatalogProducts, {
    timeout: 250,
    errorThresholdPercentage: 50,
    resetTimeout: 10000
});
catalogBreaker.fallback(() => [
    { "id": 999, "name": "Кэшированный Товар (Резервный контур)", "price": 0.00, "description": "Режим защиты от сбоев" }
]);

app.get('/api/v1/catalog/products', async (req, res) => {
    try { 
        const data = await catalogBreaker.fire(req.correlationId);
        res.json(data); 
    } catch (error) { 
        res.status(500).json({ error: "Каталог недоступен." }); 
    }
});

app.use('/api/v1/auth', createProxyMiddleware({ 
    ...configureProxyOptions(process.env.AUTH_SERVICE_URL || 'http://auth-service:80'),
    pathRewrite: { '^/api/v1/auth': '' } 
}));

app.use('/api/v1/cart', verifyToken, createProxyMiddleware({ 
    ...configureProxyOptions(process.env.CART_SERVICE_URL || 'http://cart-service:80'),
    pathRewrite: { '^/api/v1/cart': '' } 
}));

app.use('/api/v1/payments', verifyToken, createProxyMiddleware({ 
    ...configureProxyOptions(process.env.PAYMENT_SERVICE_URL || 'http://payment-service:80'),
    pathRewrite: { '^/api/v1/payments': '' } 
}));

app.use((req, res, next) => {
    if (req.url.startsWith('/api/v1/orders')) {
        return verifyToken(req, res, () => {
            createProxyMiddleware(configureProxyOptions(process.env.ORDER_SERVICE_URL || 'http://order-service:80'))(req, res, next);
        });
    }
    next();
});

app.use((req, res) => res.status(404).json({ error: 'Route not found on API Gateway' }));
app.listen(PORT, () => console.log(`=== API Gateway с поддержкой Трассировки запущен на порту ${PORT} ===`));

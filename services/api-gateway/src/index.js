const express = require('express');
const { createProxyMiddleware } = require('http-proxy-middleware');
const client = require('prom-client');
const CircuitBreaker = require('opossum');
const axios = require('axios');
const crypto = require('crypto'); // Встроенный модуль Node.js для генерации криптографических UUID
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

// КРИТИЧЕСКИЙ МИДЛВАР: Трассировка и генерация сквозного Correlation ID
app.use((req, res, next) => {
    // Проверяем, пришел ли ID от клиента, или генерируем новый UUID v4 налету
    const correlationId = req.headers['x-correlation-id'] || crypto.randomUUID();
    
    // Закрепляем идентификатор в текущем объекте запроса и ответа для внутренней логики
    req.correlationId = correlationId;
    res.setHeader('X-Correlation-ID', correlationId);

    // Модифицируем стандартный вывод логов шлюза, впекая туда ID трассировки
    console.log(`[${correlationId}] [API-GATEWAY] ${req.method} ${req.url}`);
    
    res.on('finish', () => {
        httpRequestCounter.labels(req.method, req.path, res.statusCode).inc();
    });
    next();
});

// Функция конфигурации прокси, автоматически прокидывающая Correlation ID в заголовки бэкенда
const configureProxyOptions = (targetPath) => {
    return {
        target: targetPath,
        changeOrigin: true,
        on: {
            proxyReq: (proxyReq, req, res) => {
                // Принудительно инжектируем UUID в заголовки исходящего запроса к микросервису
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

// === CIRCUIT BREAKER ДЛЯ СЛУЖБЫ КАТАЛОГА ===
const CATALOG_URL = process.env.CATALOG_SERVICE_URL || 'http://catalog-service:8082';
async function fetchCatalogProducts(correlationId) {
    // Передаем ID трассировки даже в синхронные axios запросы к Go бэкенду
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

// === МАРШРУТИЗАЦИЯ ВНУТРЕННИХ СЕРВИСОВ С ПОДДЕРЖКОЙ ТРАССИРОВКИ ===
app.use('/api/v1/auth', createProxyMiddleware({ 
    ...configureProxyOptions(process.env.AUTH_SERVICE_URL || 'http://auth-service:8081'),
    pathRewrite: { '^/api/v1/auth': '' } 
}));

app.use('/api/v1/cart', verifyToken, createProxyMiddleware({ 
    ...configureProxyOptions(process.env.CART_SERVICE_URL || 'http://cart-service:8083'),
    pathRewrite: { '^/api/v1/cart': '' } 
}));

app.use('/api/v1/payments', verifyToken, createProxyMiddleware({ 
    ...configureProxyOptions(process.env.PAYMENT_SERVICE_URL || 'http://payment-service:8085'),
    pathRewrite: { '^/api/v1/payments': '/api/v1/internal/payments/process' } 
}));

app.use('/api/v1/analytics/summary', verifyToken, createProxyMiddleware({ 
    ...configureProxyOptions('http://analytics-service:8000/summary'),
    ignorePath: true 
}));

app.use((req, res, next) => {
    if (req.url.startsWith('/api/v1/orders')) {
        return verifyToken(req, res, () => {
            createProxyMiddleware(configureProxyOptions(process.env.ORDER_SERVICE_URL || 'http://order-service:8084'))(req, res, next);
        });
    }
    next();
});

app.use((req, res) => res.status(404).json({ error: 'Route not found on API Gateway' }));
app.listen(PORT, () => console.log(`=== API Gateway с поддержкой Трассировки запущен на порту ${PORT} ===`));

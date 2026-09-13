const express = require('express');
const { createProxyMiddleware } = require('http-proxy-middleware');
const client = require('prom-client');
const CircuitBreaker = require('opossum'); // Подключаем предохранитель
const axios = require('axios'); // Используем axios для контролируемых запросов каталога
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

// =========================================================================
// НАСТРОЙКА CIRCUIT BREAKER ДЛЯ КАТАЛОГА ТОВАРОВ (Go-Service)
// =========================================================================
const CATALOG_URL = process.env.CATALOG_SERVICE_URL || 'http://catalog-service:8082';

// Функция, которую защищает предохранитель
async function fetchCatalogProducts() {
    // Если сервис не отвечает за 200 миллисекунд, axios сбрасывает запрос по таймауту
    const response = await axios.get(`${CATALOG_URL}/products`, { timeout: 200 });
    return response.data;
}

const breakerOptions = {
    timeout: 250,          // Если функция выполняется дольше 250 мс, считать её упавшей
    errorThresholdPercentage: 50, // Размыкать цепь, если 50% запросов завершились ошибкой
    resetTimeout: 10000    // Подождать 10 секунд в режиме OPEN перед переходом в HALF-OPEN
};

const catalogBreaker = new CircuitBreaker(fetchCatalogProducts, breakerOptions);

// FALLBACK: Функция-заглушка. Отрабатывает МГНОВЕННО, если цепь разомкнута (бэкенд упал)
catalogBreaker.fallback(() => {
    console.log("[CIRCUIT-BREAKER] Fallback activated! Serving cached static catalog matrix.");
    return [
        { "id": 999, "name": "Кэшированный Товар (Резервный контур)", "price": 0.00, "description": "Режим защиты от сбоев" }
    ];
});

// Логируем переключение состояний автомата в консоль для дебага
catalogBreaker.on('open', () => console.warn("🚨 [CIRCUIT-BREAKER] WARNING: Circuit is now OPEN! Go backend isolated."));
catalogBreaker.on('halfOpen', () => console.info("🟡 [CIRCUIT-BREAKER] INFO: Circuit is HALF-OPEN. Testing target runtime..."));
catalogBreaker.on('close', () => console.log("🟢 [CIRCUIT-BREAKER] SUCCESS: Circuit is CLOSED. System fully operational."));

// Применяем предохранитель на публичный эндпоинт каталога товаров
app.get('/api/v1/catalog/products', async (req, res) => {
    try {
        const data = await catalogBreaker.fire();
        res.json(data);
    } catch (error) {
        res.status(500).json({ error: "Каталог недоступен, ошибка отказоустойчивого слоя." });
    }
});

// Остальные роуты проксируются в стандартном режиме
app.use('/api/v1/auth', createProxyMiddleware({
    target: process.env.AUTH_SERVICE_URL || 'http://auth-service:8081',
    changeOrigin: true,
    pathRewrite: { '^/api/v1/auth': '' },
}));

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
    console.log(`=== API Gateway with Circuit Breaker protection started on port ${PORT} ===`);
});

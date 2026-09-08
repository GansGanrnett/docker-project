require('dotenv').config();
const express = require('express');
const cors = require('cors');
const { createProxyMiddleware } = require('http-proxy-middleware');

const app = express();
const PORT = process.env.PORT || 8080;

// Включаем CORS для всех запросов (для разработки)
app.use(cors());

// Простой health-check для Gateway
app.get('/health', (req, res) => {
  res.send('API Gateway OK');
});

// Прокси для Auth Service
app.use(
  '/auth',
  createProxyMiddleware({
    target: process.env.AUTH_SERVICE_URL || 'http://localhost:8081',
    changeOrigin: true,
    pathRewrite: { '^/auth': '' }, // убираем префикс /auth при проксировании
  })
);

// Прокси для Catalog Service
app.use(
  '/catalog',
  createProxyMiddleware({
    target: process.env.CATALOG_SERVICE_URL || 'http://localhost:8082',
    changeOrigin: true,
    pathRewrite: { '^/catalog': '' },
  })
);

// Прокси для Cart Service
app.use(
  '/cart',
  createProxyMiddleware({
    target: process.env.CART_SERVICE_URL || 'http://localhost:8083',
    changeOrigin: true,
    pathRewrite: { '^/cart': '' },
  })
);

// Прокси для Order Service
app.use(
  '/order',
  createProxyMiddleware({
    target: process.env.ORDER_SERVICE_URL || 'http://localhost:8084',
    changeOrigin: true,
    pathRewrite: { '^/order': '' },
  })
);

// Прокси для Payment Service
app.use(
  '/payment',
  createProxyMiddleware({
    target: process.env.PAYMENT_SERVICE_URL || 'http://localhost:8085',
    changeOrigin: true,
    pathRewrite: { '^/payment': '' },
  })
);

// Если маршрут не найден – 404
app.use((req, res) => {
  res.status(404).json({ error: 'Route not found' });
});

app.listen(PORT, () => {
  console.log(`API Gateway running on port ${PORT}`);
});

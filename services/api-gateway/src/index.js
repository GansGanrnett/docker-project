const express = require('express');
const { createProxyMiddleware } = require('http-proxy-middleware');
const verifyToken = require('./authMiddleware');
require('dotenv').config();

const app = express();
const PORT = process.env.PORT || 8080;

app.use((req, res, next) => {
    console.log(`[API-GATEWAY] ${req.method} ${req.url}`);
    next();
});

app.get('/health', (req, res) => {
    res.status(200).json({ status: 'UP', service: 'api-gateway' });
});

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

app.use('/api/v1/cart', verifyToken, createProxyMiddleware({
    target: process.env.CART_SERVICE_URL || 'http://cart-service:8083',
    changeOrigin: true,
    pathRewrite: { '^/api/v1/cart': '' },
}));

// FIXED: Using an explicit conditional middleware layer to ensure Express skips route prefix trimming
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

app.use((req, res) => {
    res.status(404).json({ error: 'Route not found on API Gateway' });
});

app.listen(PORT, () => {
    console.log(`=== API Gateway successfully started on port ${PORT} ===`);
});

const express = require('express');
const app = express();
const PORT = process.env.PORT || 8083;
const Redis = require('ioredis');
const redis = new Redis({ 
    host: process.env.REDIS_HOST || 'redis-cart', 
    port: process.env.REDIS_PORT || 6379 
});


app.get('/health', (req, res) => {
    res.send('Cart Service OK');
});

app.listen(PORT, () => {
    console.log(`Cart service running on port ${PORT}`);
});

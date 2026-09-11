const express = require('express');
const { createClient } = require('redis');
require('dotenv').config();

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 8083;
const REDIS_URL = process.env.REDIS_URL || 'redis://localhost:6379';

// Инициализация клиента Redis
const redisClient = createClient({ url: REDIS_URL });

redisClient.on('error', (err) => console.error('[REDIS-ERROR]', err));

async function initRedis() {
    await redisClient.connect();
    console.log('[CART-SERVICE] Successfully connected to Redis Cache Container');
}
initRedis().catch(console.error);

// Внутренний Healthcheck
app.get('/health', (req, res) => {
    res.status(200).json({ status: 'UP', service: 'cart-service' });
});

// Получить корзину текущего авторизованного пользователя
app.get('/items', async (req, res) => {
    // Получаем имя пользователя, очищенное шлюзом из JWT-токена
    const username = req.headers['x-user-username'];
    if (!username) {
        return res.status(400).json({ error: 'Missing identity header X-User-Username' });
    }

    try {
        const cartData = await redisClient.get(`cart:${username}`);
        return res.status(200).json(cartData ? JSON.parse(cartData) : { username, items: [] });
    } catch (err) {
        return res.status(500).json({ error: 'Failed to fetch cart from storage' });
    }
});

// Добавить/Обновить товар в корзине
app.post('/items', async (req, res) => {
    const username = req.headers['x-user-username'];
    const { productId, name, quantity, price } = req.body;

    if (!username) return res.status(400).json({ error: 'Missing identity header' });
    if (!productId || !quantity) return res.status(400).json({ error: 'Invalid payload items' });

    try {
        const key = `cart:${username}`;
        let cart = await redisClient.get(key);
        cart = cart ? JSON.parse(cart) : { username, items: [] };

        // Ищем, есть ли уже такой товар в корзине
        const existingItem = cart.items.find(item => item.productId === productId);
        if (existingItem) {
            existingItem.quantity += quantity;
        } else {
            cart.items.push({ productId, name, quantity, price });
        }

        // Записываем в Redis со сроком жизни 7 дней (авто-очистка брошенных корзин)
        await redisClient.setEx(key, 7 * 24 * 60 * 60, JSON.stringify(cart));
        return res.status(200).json(cart);
    } catch (err) {
        return res.status(500).json({ error: 'Failed to save item to cart' });
    }
});

app.listen(PORT, () => {
    console.log(`=== Cart Service successfully started on port ${PORT} ===`);
});

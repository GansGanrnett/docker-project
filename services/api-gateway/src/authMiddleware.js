const jwt = require('jsonwebtoken');
const fs = require('fs');
const path = require('path');

const publicKeyPath = path.resolve(__dirname, '../configs/public.pem');
const publicKey = fs.readFileSync(publicKeyPath, 'utf8');

module.exports = (req, res, next) => {
    const authHeader = req.headers['authorization'];
    
    if (!authHeader || !authHeader.startsWith('Bearer ')) {
        return res.status(401).json({ error: 'Access denied. No token provided.' });
    }

    // ИСПРАВЛЕНО: Извлекаем строго второй элемент массива (сам хэш JWT токена)
    const token = authHeader.split(' ')[1];

    try {
        const decoded = jwt.verify(token, publicKey, { algorithms: ['RS256'] });
        req.headers['x-user-username'] = decoded.sub;
        req.headers['x-user-role'] = decoded.role;
        next();
    } catch (error) {
        return res.status(403).json({ error: 'Invalid or expired token.' });
    }
};

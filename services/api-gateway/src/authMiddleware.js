const jwt = require('jsonwebtoken');

// Ключ больше не привязан к файлам на диске и читается из переменной окружения
const publicKey = process.env.RSA_PUBLIC_KEY;

module.exports = function verifyToken(req, res, next) {
    const authHeader = req.headers['authorization'];
    if (!authHeader) {
        return res.status(401).json({ error: 'Заголовок Authorization отсутствует в пакете' });
    }

    const token = authHeader.split(' ')[1];
    if (!token) {
        return res.status(401).json({ error: 'Bearer токен авторизации не найден' });
    }

    if (!publicKey) {
        console.error('[CRITICAL] Переменная окружения RSA_PUBLIC_KEY не задана!');
        return res.status(500).json({ error: 'Внутренняя ошибка конфигурации сервера безопасности' });
    }

    jwt.verify(token, publicKey, { algorithms: ['RS256'] }, (err, decoded) => {
        if (err) {
            return res.status(403).json({ error: 'Токен доступа невалиден или его время жизни истекло' });
        }
        req.user = decoded;
        next();
    });
};

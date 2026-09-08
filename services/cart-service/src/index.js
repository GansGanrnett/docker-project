const express = require('express');
const app = express();
const PORT = process.env.PORT || 8083;

app.get('/health', (req, res) => {
    res.send('Cart Service OK');
});

app.listen(PORT, () => {
    console.log(`Cart service running on port ${PORT}`);
});

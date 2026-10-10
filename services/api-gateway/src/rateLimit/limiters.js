const rateLimit = require('express-rate-limit');
const cfg = require('./config');

const byIp = (req) => req.ip || req.connection.remoteAddress || 'unknown';
const byUser = (req) => (req.headers['x-user-username'] || 'anon');

function build(opts, keyGen) {
  return rateLimit({
    windowMs: opts.windowMs,
    max: opts.max,
    standardHeaders: true,
    legacyHeaders: true,
    keyGenerator: keyGen,
    handler: (req, res, next, options) => {
      res.status(429).json({ error: 'Too Many Requests', retryAfter: Math.ceil(options.windowMs / 1000) });
    },
  });
}

module.exports = {
  global: build(cfg.global, byIp),
  auth: build(cfg.auth, byIp),
  user: build(cfg.user, byUser),
};

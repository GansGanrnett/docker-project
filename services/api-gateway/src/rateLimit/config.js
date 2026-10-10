module.exports = {
  global: {
    max: parseInt(process.env.RATE_LIMIT_GLOBAL_MAX, 10) || 100,
    windowMs: parseInt(process.env.RATE_LIMIT_GLOBAL_WINDOW_MS, 10) || 60000,
  },
  auth: {
    max: parseInt(process.env.RATE_LIMIT_AUTH_MAX, 10) || 5,
    windowMs: parseInt(process.env.RATE_LIMIT_AUTH_WINDOW_MS, 10) || 60000,
  },
  user: {
    max: parseInt(process.env.RATE_LIMIT_USER_MAX, 10) || 300,
    windowMs: parseInt(process.env.RATE_LIMIT_USER_WINDOW_MS, 10) || 60000,
  },
};

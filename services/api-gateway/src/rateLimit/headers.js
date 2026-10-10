module.exports = (req, res, next) => {
  res.setHeader('X-RateLimit-Limit', '100');
  res.setHeader('X-RateLimit-Remaining', '99');
  res.setHeader('X-RateLimit-Reset', new Date(Date.now() + 60000).toISOString());
  next();
};

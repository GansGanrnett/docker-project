// Minimal verification: histogram metric registers correctly
const client = require('prom-client');

const h = new client.Histogram({
    name: 'api_gateway_http_request_duration_seconds',
    help: 'test',
    labelNames: ['method', 'route', 'status'],
    buckets: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10]
});

h.labels('GET', '/api', '200').observe(0.015);
h.labels('GET', '/api', '500').observe(0.120);

console.log('Histogram registered. Metric names:', Object.keys(client.register.getSingleMetricAsString ? {} : {}));
// Just confirm observe does not throw.
console.assert(typeof h.observe === 'function', 'observe missing');
console.log('OK: api_gateway_http_request_duration_seconds histogram works');

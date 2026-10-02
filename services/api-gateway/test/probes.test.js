// Uses the test runner built into Node 20, so the gateway needs no test
// dependencies - adding jest or supertest here would be more setup than the
// two probe routes are worth.
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { once } = require('node:events');

const client = require('prom-client');
const app = require('../src/index.js');

function get(port, path) {
    return new Promise((resolve, reject) => {
        const request = http.get({ host: '127.0.0.1', port, path }, (response) => {
            let body = '';
            response.setEncoding('utf8');
            response.on('data', (chunk) => { body += chunk; });
            response.on('end', () => resolve({ status: response.statusCode, body }));
        });
        request.on('error', reject);
    });
}

// prom-client 15 returns a promise here.
async function requestsTotal() {
    const metrics = await client.register.getMetricsAsJSON();
    const counter = metrics.find((metric) => metric.name === 'api_gateway_http_requests_total');
    if (!counter) {
        return 0;
    }
    return counter.values.reduce((sum, value) => sum + value.value, 0);
}

test('liveness and readiness endpoints', async (t) => {
    const server = app.listen(0);
    t.after(() => server.close());
    await once(server, 'listening');
    const port = server.address().port;

    const health = await get(port, '/health');
    assert.equal(health.status, 200);
    assert.deepEqual(JSON.parse(health.body), {
        status: 'UP',
        service: 'api-gateway',
    });

    // The gateway is stateless: readiness reports the same thing as liveness.
    // A downstream outage must not pull it out of rotation - that is the
    // circuit breaker's job, and failing readiness would escalate a partial
    // outage into a total one.
    const ready = await get(port, '/ready');
    assert.equal(ready.status, 200);
    assert.deepEqual(JSON.parse(ready.body), JSON.parse(health.body));
});

test('probe and scrape traffic stays out of the request counter', async (t) => {
    const server = app.listen(0);
    t.after(() => server.close());
    await once(server, 'listening');
    const port = server.address().port;

    const before = await requestsTotal();

    for (let i = 0; i < 3; i++) {
        assert.equal((await get(port, '/health')).status, 200);
        assert.equal((await get(port, '/ready')).status, 200);
        assert.equal((await get(port, '/metrics')).status, 200);
    }

    assert.equal(
        await requestsTotal(),
        before,
        'kubelet polls and Prometheus scrapes must not inflate api_gateway_http_requests_total'
    );

    // Business traffic still counts, so the exclusion above is not just a
    // counter that was never wired up.
    assert.equal((await get(port, '/no-such-route')).status, 404);
    assert.ok(
        (await requestsTotal()) > before,
        'a real request must still be counted'
    );
});
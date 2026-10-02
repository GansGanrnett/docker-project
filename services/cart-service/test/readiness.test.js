// Uses the test runner built into Node 20, so cart-service gains no test
// dependencies.
//
// The service under test is a real child process: "starts while the
// dependency is down and recovers on its own" is a claim about a running
// process, and TestClient-style in-process tests cannot make it.
//
// Redis itself is reached through a switchable TCP proxy. Pointing
// REDIS_URL at a dead port proves only that /ready answers 503; to prove
// recovery the proxy has to start forwarding mid-run, which is what the
// outage-and-back scenario actually looks like.
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { once } = require('node:events');

const UPSTREAM = process.env.REDIS_TEST_URL;
const SERVICE = path.join(__dirname, '..');

function freePort() {
    return new Promise((resolve, reject) => {
        const server = net.createServer();
        server.on('error', reject);
        server.listen(0, '127.0.0.1', () => {
            const { port } = server.address();
            server.close(() => resolve(port));
        });
    });
}

// Accepts connections and either drops them or pipes them to a real Redis.
// Dropping is what a refused connection looks like to the client, so
// node-redis keeps retrying exactly as it would against a dead pod.
function startProxy() {
    const upstream = UPSTREAM ? new URL(UPSTREAM) : null;
    let forwarding = false;

    const server = net.createServer((client) => {
        if (!forwarding || !upstream) {
            client.destroy();
            return;
        }
        const remote = net.connect(
            Number(upstream.port || 6379),
            upstream.hostname || '127.0.0.1',
            () => {
                client.pipe(remote);
                remote.pipe(client);
            });
        remote.on('error', () => client.destroy());
        client.on('error', () => remote.destroy());
    });

    return new Promise((resolve, reject) => {
        server.on('error', reject);
        server.listen(0, '127.0.0.1', () => {
            resolve({
                port: server.address().port,
                startForwarding() { forwarding = true; },
                close() { return new Promise((done) => server.close(done)); },
            });
        });
    });
}

function status(port, path) {
    return new Promise((resolve) => {
        const request = http.get({ host: '127.0.0.1', port, path }, (response) => {
            response.resume();
            resolve(response.statusCode);
        });
        request.on('error', () => resolve(null));
        request.setTimeout(2000, () => { request.destroy(); resolve(null); });
    });
}

async function waitFor(port, path, expected, timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    let last = null;
    while (Date.now() < deadline) {
        last = await status(port, path);
        if (last === expected) {
            return last;
        }
        await new Promise((r) => setTimeout(r, 200));
    }
    return last;
}

async function startService(redisPort) {
    const port = await freePort();
    const child = spawn(process.execPath, ['src/index.js'], {
        cwd: SERVICE,
        env: {
            ...process.env,
            PORT: String(port),
            REDIS_URL: `redis://127.0.0.1:${redisPort}`,
        },
        stdio: ['ignore', 'ignore', 'pipe'],
    });

    // Kept for failure messages: when the service never answers, the reason
    // is in its stderr, and "null !== 200" on its own helps nobody.
    const stderr = [];
    child.stderr.on('data', (chunk) => {
        stderr.push(chunk.toString());
        if (stderr.length > 40) {
            stderr.shift();
        }
    });
    child.on('exit', (code, signal) => {
        stderr.push(`\n[child exited: code=${code} signal=${signal}]`);
    });

    return { port, child, stderr: () => stderr.join('') };
}

async function stopService(service) {
    service.child.kill('SIGKILL');
    await once(service.child, 'exit').catch(() => {});
}

test('health stays 200 and ready is 503 while Redis is unreachable', async (t) => {
    const proxy = await startProxy();
    const service = await startService(proxy.port);
    t.after(async () => {
        await stopService(service);
        await proxy.close();
    });

    // The process must come up at all: a service that exits on a failed
    // connect() cannot recover, it can only be restarted.
    const health = await waitFor(service.port, '/health', 200, 20000);
    assert.equal(health, 200,
        `сервис обязан подняться с недоступным Redis; stderr:\n${service.stderr()}`);
    assert.equal(await waitFor(service.port, '/ready', 503, 20000), 503,
        '/ready обязан отвечать 503, пока Redis недоступен');
    assert.equal(await status(service.port, '/health'), 200,
        'liveness обязан оставаться 200 при недоступном Redis');
});

test('ready recovers when Redis returns, without a restart', { skip: !UPSTREAM && 'нужен REDIS_TEST_URL' }, async (t) => {
    const proxy = await startProxy();
    const service = await startService(proxy.port);
    t.after(async () => {
        await stopService(service);
        await proxy.close();
    });

    assert.equal(await waitFor(service.port, '/ready', 503, 20000), 503,
        'стартовое состояние должно быть неготовым');

    proxy.startForwarding();

    assert.equal(await waitFor(service.port, '/ready', 200, 30000), 200,
        'сервис обязан вернуться в готовность сам, без перезапуска');
    assert.equal(await status(service.port, '/health'), 200,
        'liveness не должен падать при появлении Redis');
});
const test = require('node:test');
const assert = require('node:assert/strict');
const { _byUser } = require('../src/rateLimit/limiters.js');

test('byUser uses verified JWT sub', () => {
    const req = { user: { sub: 'alice' }, headers: { 'x-user-username': 'mallory' }, ip: '10.0.0.1' };
    assert.equal(_byUser(req), 'alice');
});

test('byUser ignores spoofed header when no JWT sub', () => {
    const req = { headers: { 'x-user-username': 'mallory' }, ip: '10.0.0.2' };
    assert.equal(_byUser(req), 'unauthenticated');
});

test('byUser uses JWT sub even with spoofed x-user-username', () => {
    const req = { user: { sub: 'user-a' }, headers: { 'x-user-username': 'victim' }, ip: '10.0.0.3' };
    assert.equal(_byUser(req), 'user-a');
});

test('byUser returns unauthenticated without req.user.sub', () => {
    const req = { headers: { 'x-user-username': 'victim' }, ip: '10.0.0.4' };
    assert.equal(_byUser(req), 'unauthenticated');
});

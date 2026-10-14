const test = require('node:test');
const assert = require('node:assert/strict');
const { _byUser } = require('../src/rateLimit/limiters.js');

test('byUser uses verified JWT sub', () => {
    const req = { user: { sub: 'alice' }, headers: { 'x-user-username': 'mallory' }, ip: '10.0.0.1' };
    assert.equal(_byUser(req), 'alice');
});

test('byUser ignores spoofed header, falls back to IP', () => {
    const req = { headers: { 'x-user-username': 'mallory' }, ip: '10.0.0.2' };
    assert.equal(_byUser(req), '10.0.0.2');
});

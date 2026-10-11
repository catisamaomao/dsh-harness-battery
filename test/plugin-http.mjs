import test from 'node:test';
import assert from 'node:assert/strict';
import { Readable } from 'node:stream';
import { trustedRequest, makeRoutes } from '../lib/http.js';

function request({ path = '/api/dsh-harness-battery/start', method = 'POST', headers = {}, body = '{}' } = {}) {
  const req = Readable.from([Buffer.from(body)]); req.url = path; req.method = method;
  req.socket = { remoteAddress: '127.0.0.1' };
  req.headers = { host: '127.0.0.1:3000', origin: 'http://127.0.0.1:3000', 'sec-fetch-site': 'same-origin', 'content-type': 'application/json', 'x-whale-token': 'a'.repeat(64), ...headers };
  return req;
}
function response() { return { status: null, headers: null, body: null, writeHead(status, headers) { this.status = status; this.headers = headers; }, end(value) { this.body = value; } }; }
const service = overrides => ({ token: 'a'.repeat(64), state: async () => ({ runs: [] }), start: async data => data, cancel: async id => ({ id }), questions: async battery => ({ battery }), check: async () => ({ can_run: true }), report: async () => '<html></html>', ...overrides });

test('request fence rejects network peers, DNS rebinding, unrelated local origins, cross-site and opaque origins', () => {
  assert.equal(trustedRequest(request()), true);
  assert.equal(trustedRequest(request({ headers: { origin: 'dsh-app://app' } })), true);
  assert.equal(trustedRequest(request({ headers: { origin: 'dsh-app://attacker' } })), false);
  for (const headers of [{ host: 'evil.example' }, { host: 'localhost@evil.example' }, { origin: 'http://127.0.0.1:4000' }, { origin: 'null' }, { 'sec-fetch-site': 'cross-site' }, { 'sec-fetch-site': 'same-site' }, { host: 'localhost/path' }]) assert.equal(trustedRequest(request({ headers })), false);
  const req = request(); req.socket.remoteAddress = '192.168.1.2'; assert.equal(trustedRequest(req), false);
});

test('start requires JSON, correct CSRF token, explicit live consent, boolean memory and exact fields', async () => {
  let calls = 0; const route = makeRoutes(service({ start: async data => { calls++; return data; } })).find(value => value.path.endsWith('/start'));
  for (const options of [
    { headers: { 'content-type': 'text/plain' }, body: '{}' },
    { headers: { 'x-whale-token': 'b'.repeat(64) }, body: '{}' },
    { body: JSON.stringify({ battery: 'core', includeMemory: false }) },
    { body: JSON.stringify({ battery: 'core', includeMemory: 'false', allowLive: true }) },
    { body: JSON.stringify({ battery: 'core', includeMemory: false, allowLive: true, command: 'rm -rf /' }) },
    { body: 'x'.repeat(4097) },
  ]) { const res = response(); await route.handler(request(options), res); assert.ok(res.status >= 400); }
  assert.equal(calls, 0);
  const res = response(); await route.handler(request({ body: JSON.stringify({ battery: 'core', includeMemory: false, allowLive: true }) }), res);
  assert.equal(res.status, 202); assert.equal(calls, 1);
});

test('report endpoint accepts only a report id and has an opaque sandbox and no network capabilities', async () => {
  const route = makeRoutes(service()).find(value => value.path.endsWith('/report'));
  const res = response(); await route.handler(request({ path: route.path + '?id=one', method: 'GET' }), res);
  assert.equal(res.status, 200); assert.match(res.headers['content-security-policy'], /sandbox allow-scripts allow-downloads;/);
  assert.doesNotMatch(res.headers['content-security-policy'], /allow-same-origin/); assert.match(res.headers['content-security-policy'], /connect-src 'none'/);
  const bad = response(); await route.handler(request({ path: route.path + '?path=/etc/passwd', method: 'GET' }), bad); assert.equal(bad.status, 404);
});

test('GET actions, CORS preflight, and oversized JSON never start work', async () => {
  let calls = 0; const route = makeRoutes(service({ start: async () => { calls++; } })).find(value => value.path.endsWith('/start'));
  for (const method of ['GET', 'OPTIONS']) { const res = response(); await route.handler(request({ method }), res); assert.equal(res.status, 405); assert.equal(res.headers['access-control-allow-origin'], undefined); }
  assert.equal(calls, 0);
});

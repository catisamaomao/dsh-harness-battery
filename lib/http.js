import { timingSafeEqual } from 'node:crypto';

export const PREFIX = '/api/dsh-harness-battery';
const loopback = new Set(['127.0.0.1', '::1', '[::1]', '::ffff:127.0.0.1']);
const hosts = new Set(['127.0.0.1', '[::1]', 'localhost']);

export function trustedRequest(req) {
  if (!loopback.has(req.socket?.remoteAddress)) return false;
  const host = req.headers.host;
  if (typeof host !== 'string' || !/^(localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?$/.test(host)) return false;
  let authority;
  try { authority = new URL(`http://${host}`); } catch { return false; }
  if (!hosts.has(authority.hostname)) return false;
  const site = req.headers['sec-fetch-site'];
  if (site !== undefined && !['same-origin', 'none'].includes(site)) return false;
  const origin = req.headers.origin;
  if (origin === undefined) return true;
  // The desktop protocol proxies these requests to its loopback web server.
  if (origin === 'dsh-app://app') return true;
  try {
    const source = new URL(origin);
    return ['http:', 'https:'].includes(source.protocol) && source.host === authority.host && source.origin === origin;
  } catch { return false; }
}

export function json(res, status, value) {
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store',
    'x-content-type-options': 'nosniff', 'cross-origin-resource-policy': 'same-origin',
  });
  res.end(JSON.stringify(value));
}

export async function readAction(req, token) {
  if (!/^application\/json(?:\s*;\s*charset=utf-8)?$/i.test(req.headers['content-type'] || '')) {
    throw Object.assign(new Error('需要 JSON 请求'), { status: 415 });
  }
  const supplied = req.headers['x-whale-token'];
  if (typeof supplied !== 'string' || !/^[a-f0-9]{64}$/.test(supplied) || supplied.length !== token.length || !timingSafeEqual(Buffer.from(supplied), Buffer.from(token))) {
    throw Object.assign(new Error('页面已失效，请重新打开鲸相'), { status: 403 });
  }
  if (Number(req.headers['content-length'] || 0) > 4096) throw Object.assign(new Error('请求过大'), { status: 413 });
  const chunks = []; let length = 0;
  for await (const chunk of req) {
    length += chunk.length;
    if (length > 4096) throw Object.assign(new Error('请求过大'), { status: 413 });
    chunks.push(chunk);
  }
  let data;
  try { data = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { throw Object.assign(new Error('无效 JSON'), { status: 400 }); }
  if (!data || Array.isArray(data) || typeof data !== 'object') throw Object.assign(new Error('无效请求'), { status: 400 });
  return data;
}

export function exactKeys(data, keys) {
  if (Object.keys(data).some(key => !keys.includes(key))) throw Object.assign(new Error('包含不支持的参数'), { status: 400 });
}

export function makeRoutes(service) {
  const route = (path, method, action) => ({ kind: 'exact', path: PREFIX + path, handler: async (req, res) => {
    if (!trustedRequest(req)) return json(res, 403, { ok: false, error: '仅允许本机鲸相页面访问' });
    if (req.method !== method) return json(res, 405, { ok: false, error: '不支持的请求方法' });
    try { await action(req, res); }
    catch (error) { json(res, error.status || 500, { ok: false, error: error.status ? error.message : '操作失败，请检查环境后重试' }); }
  } });
  return [
    route('/state', 'GET', async (_req, res) => json(res, 200, { ok: true, token: service.token, ...await service.state() })),
    route('/check', 'GET', async (_req, res) => json(res, 200, { ok: true, environment: await service.check() })),
    route('/questions', 'GET', async (req, res) => {
      const query = new URL(req.url, 'http://localhost').searchParams;
      if ([...query.keys()].some(key => key !== 'battery') || query.getAll('battery').length !== 1) throw Object.assign(new Error('请选择题包'), { status: 400 });
      json(res, 200, { ok: true, catalogue: await service.questions(query.get('battery')) });
    }),
    route('/start', 'POST', async (req, res) => {
      const data = await readAction(req, service.token); exactKeys(data, ['battery', 'includeMemory', 'allowLive']);
      if (data.allowLive !== true || typeof data.includeMemory !== 'boolean') throw Object.assign(new Error('请明确确认模型调用和记忆选项'), { status: 400 });
      json(res, 202, { ok: true, run: await service.start(data) });
    }),
    route('/cancel', 'POST', async (req, res) => {
      const data = await readAction(req, service.token); exactKeys(data, ['id']);
      json(res, 200, { ok: true, run: await service.cancel(data.id) });
    }),
    route('/report', 'GET', async (req, res) => {
      const query = new URL(req.url, 'http://localhost').searchParams;
      if ([...query.keys()].some(key => key !== 'id') || query.getAll('id').length !== 1) throw Object.assign(new Error('报告不存在'), { status: 404 });
      const report = await service.report(query.get('id'));
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store',
        'content-security-policy': "sandbox allow-scripts allow-downloads; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self' dsh-app://app",
        'x-content-type-options': 'nosniff', 'referrer-policy': 'no-referrer' });
      res.end(report);
    }),
  ];
}

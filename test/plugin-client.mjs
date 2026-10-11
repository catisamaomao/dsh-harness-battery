import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';
test('desktop lazy module declares the same sidebar id and main key, without model hooks', async () => {
  let definition; const source = await fs.readFile(new URL('../client.js', import.meta.url), 'utf8');
  vm.runInNewContext(source, { window: { __ModuleLoader__: { load(value) { definition = value; } } } });
  assert.equal(definition.id, 'dsh-harness-battery');
  const requests = []; const registrations = [];
  const plugin = definition.factory(name => { requests.push(name); return { createElement() {} }; });
  plugin.apply({ slots: { inject(name, fn) { assert.ok(['main', 'sidebar.panellist'].includes(name)); fn(); }, register(options) { registrations.push(options); return () => {}; } } });
  assert.deepEqual(requests, ['react']); assert.equal(registrations[0].key, registrations[1].id);
  assert.equal(registrations[0].name, 'main'); assert.equal(registrations[1].label, '鲸相');
  assert.doesNotMatch(source, /systemPrompt|agent\.inbox|tools\.register/);
  assert.match(source, /sandbox: 'allow-scripts allow-downloads'/); assert.match(source, /useState\(false\)/);
});

test('cold boot waits for layout slots and remounts cleanly after their owner reloads', async () => {
  let definition; const source = await fs.readFile(new URL('../client.js', import.meta.url), 'utf8');
  vm.runInNewContext(source, { window: { __ModuleLoader__: { load(value) { definition = value; } } } });
  const plugin = definition.factory(() => ({ createElement() {} }));
  assert.ok(plugin.inject.includes('layout'));
  const declared = new Set(['root']); const registrations = new Map(); const waiters = new Map(); const cleanup = new Map();
  const slots = {
    inject(name, fn) { waiters.set(name, fn); if (declared.has(name)) cleanup.set(name, fn()); },
    register(options) {
      assert.ok(declared.has(options.name), `undeclared slot: ${options.name}`);
      assert.ok(!registrations.has(options.name), `duplicate slot entry: ${options.name}`);
      registrations.set(options.name, options); return () => registrations.delete(options.name);
    },
  };
  const declare = name => { declared.add(name); cleanup.set(name, waiters.get(name)?.()); };
  const undeclare = name => { cleanup.get(name)?.(); cleanup.delete(name); declared.delete(name); };
  // Renderer exists, layout is still activating. The previous direct registration threw here.
  assert.doesNotThrow(() => plugin.apply({ slots })); assert.equal(registrations.size, 0);
  declare('main'); assert.equal(registrations.get('main').key, 'dsh-harness-battery');
  declare('sidebar.panellist'); assert.equal(registrations.size, 2);
  assert.equal(registrations.get('sidebar.panellist').id, registrations.get('main').key);
  undeclare('main'); assert.equal(registrations.has('main'), false);
  declare('main'); assert.equal(registrations.size, 2);
  undeclare('main'); undeclare('sidebar.panellist'); assert.equal(registrations.size, 0);
  const manifest = JSON.parse(await fs.readFile(new URL('../package.json', import.meta.url), 'utf8'));
  for (const owner of ['@deepseek-ai/dsh-client-ui-layout', '@deepseek-ai/dsh-client-ui-sidebar']) assert.ok(manifest.dsh.client.inject.includes(owner));
});

async function pageHarness(initialState) {
  const source = await fs.readFile(new URL('../client.js', import.meta.url), 'utf8');
  const hooks = []; const effects = []; const timers = new Map(); const requests = [];
  let cursor = 0; let sequence = 0; let definition; let Page; let state = initialState;
  const react = {
    createElement(type, props, ...children) { return { type, props: props || {}, children: children.flat(Infinity).filter(child => child !== false && child !== null && child !== undefined) }; },
    useState(initial) {
      const index = cursor++; if (!(index in hooks)) hooks[index] = initial;
      return [hooks[index], value => { hooks[index] = typeof value === 'function' ? value(hooks[index]) : value; }];
    },
    useRef(initial) { const index = cursor++; if (!(index in hooks)) hooks[index] = { current: initial }; return hooks[index]; },
    useEffect(callback, dependencies) {
      const index = cursor++; const previous = hooks[index];
      if (!previous || dependencies.some((value, offset) => value !== previous[offset])) { hooks[index] = dependencies; effects.push(callback); }
    },
  };
  vm.runInNewContext(source, {
    window: { __ModuleLoader__: { load(value) { definition = value; } } }, AbortController,
    setTimeout(callback, delay) { const id = ++sequence; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    async fetch(url, options) {
      requests.push({ url, options });
      let response = { ok: true };
      if (url.endsWith('/state')) response = { ok: true, token: 'a'.repeat(64), ...state };
      else if (url.endsWith('/check')) response.environment = { can_run: true, checks: { python: true } };
      else if (url.includes('/questions?')) response.catalogue = { item_count: 9, items: [{ id: 'V1', prompt: '题目正文' }] };
      return { ok: true, async json() { return response; } };
    },
  });
  const plugin = definition.factory(() => react);
  plugin.apply({ slots: { inject(_name, callback) { callback(); }, register(options, component) { if (options.name === 'main') Page = component; return () => {}; } } });
  const render = () => { cursor = 0; return Page(); };
  const app = {
    requests, render, setState(next) { state = next; },
    async mount() { render(); for (const effect of effects.splice(0)) effect(); await new Promise(resolve => setImmediate(resolve)); return render(); },
    async poll() {
      const entry = [...timers.entries()].find(([, value]) => value.delay === 2000); assert.ok(entry, 'page scheduled a state poll');
      timers.delete(entry[0]); await entry[1].callback(); return render();
    },
  };
  return app;
}
function nodes(tree, visible = false) {
  if (!tree || typeof tree !== 'object') return [];
  const children = visible && tree.type === 'details' && !tree.props.open ? tree.children.filter(child => child.type === 'summary') : tree.children;
  return [tree, ...children.flatMap(child => nodes(child, visible))];
}
function textOf(tree, visible = false) {
  if (typeof tree === 'string' || typeof tree === 'number') return String(tree);
  if (!tree || tree.type === 'style') return '';
  const children = visible && tree.type === 'details' && !tree.props.open ? tree.children.filter(child => child.type === 'summary') : tree.children;
  return children.map(child => textOf(child, visible)).join(' ');
}
const savedRun = id => ({ id, battery: 'core', includeMemory: false, status: 'completed', startedAt: '2026-10-11T08:00:00Z', hasReport: true });

test('default page shows the start flow while configuration, budget, questions and history stay folded', async () => {
  const app = await pageHarness({ supported: true, active: null, runs: [] }); const tree = await app.mount();
  const visible = textOf(tree, true);
  assert.match(visible, /开始测试/); assert.match(visible, /按实际模型用量计费/); assert.match(visible, /默认不带记忆/);
  assert.doesNotMatch(visible, /预算上限|16,384|题目正文|历史画像|运行环境|测试选项/);
  const more = nodes(tree).find(node => node.props.className === 'whale-more'); assert.equal(more.props.open, undefined);
  const memory = nodes(tree).find(node => node.type === 'input' && node.props.type === 'checkbox'); assert.equal(memory.props.checked, false);
  const start = nodes(tree, true).find(node => node.type === 'button' && textOf(node) === '开始测试'); assert.equal(start.props.disabled, false);
  assert.ok(app.requests.every(request => request.options.method !== 'POST'));
  await start.props.onClick();
  const sent = JSON.parse(app.requests.find(request => request.url.endsWith('/start')).options.body);
  assert.deepEqual(sent, { battery: 'core', includeMemory: false, allowLive: true });
});

test('opting into memory keeps clear copying and provider consent beside the start button', async () => {
  const app = await pageHarness({ supported: true, active: null, runs: [] }); let tree = await app.mount();
  const memory = nodes(tree).find(node => node.type === 'input' && node.props.type === 'checkbox'); memory.props.onChange({ target: { checked: true } });
  tree = app.render(); assert.match(textOf(tree, true), /复制本机 Memorix 记忆，并将测试选中的内容发送给 DeepSeek/);
  await nodes(tree, true).find(node => node.type === 'button' && textOf(node) === '开始测试').props.onClick();
  assert.equal(JSON.parse(app.requests.find(request => request.url.endsWith('/start')).options.body).includeMemory, true);
});

test('new completed report opens automatically; later polls preserve a manually selected older report', async () => {
  const app = await pageHarness({ supported: true, active: null, runs: [savedRun('old')] }); let tree = await app.mount();
  assert.match(nodes(tree).find(node => node.type === 'iframe').props.src, /id=old$/);
  app.setState({ supported: true, active: null, runs: [savedRun('new'), savedRun('old')] }); tree = await app.poll();
  assert.match(nodes(tree).find(node => node.type === 'iframe').props.src, /id=new$/);
  const history = nodes(tree).filter(node => node.type === 'button' && node.props['aria-pressed'] !== undefined);
  history[1].props.onClick(); tree = await app.poll();
  assert.match(nodes(tree).find(node => node.type === 'iframe').props.src, /id=old$/);
});

test('active run keeps progress and cancellation outside the collapsed options', async () => {
  const active = { id: 'running', status: 'running', message: '正在测试', progress: { completed: 2, total: 9 } };
  const app = await pageHarness({ supported: true, active, runs: [] }); const tree = await app.mount();
  assert.match(textOf(tree, true), /2 \/ 9 题/);
  const cancel = nodes(tree, true).find(node => node.type === 'button' && textOf(node) === '取消测试'); assert.equal(cancel.props.disabled, false);
  await cancel.props.onClick();
  assert.deepEqual(JSON.parse(app.requests.find(request => request.url.endsWith('/cancel')).options.body), { id: 'running' });
});

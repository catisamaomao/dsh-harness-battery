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

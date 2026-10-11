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
  plugin.apply({ effect(fn) { fn(); }, slots: { inject(name, fn) { assert.equal(name, 'sidebar.panellist'); fn(); }, register(options) { registrations.push(options); return () => {}; } } });
  assert.deepEqual(requests, ['react']); assert.equal(registrations[0].key, registrations[1].id);
  assert.equal(registrations[0].name, 'main'); assert.equal(registrations[1].label, '鲸相');
  assert.doesNotMatch(source, /systemPrompt|agent\.inbox|tools\.register/);
  assert.match(source, /sandbox: 'allow-scripts allow-downloads'/); assert.match(source, /useState\(false\)/);
});

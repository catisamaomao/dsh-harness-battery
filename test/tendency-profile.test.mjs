import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const python = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
function py(code, input) {
  const result = spawnSync(python, ['-X', 'utf8', '-B', '-c', code], {
    cwd: root, input: input === undefined ? undefined : JSON.stringify(input), encoding: 'utf8',
    maxBuffer: 8_000_000, env: { ...process.env, PYTHONUTF8: '1' },
  });
  assert.equal(result.status, 0, result.error?.message || result.stderr);
  return JSON.parse(result.stdout);
}
const definitions = py(`import json,sys
sys.path.insert(0,'test')
import test_tendency_profile as fixture
r=fixture.profile_report()
for axis,items in fixture.validation.TENDENCY_AXES.items():
 for index,item in enumerate(items):fixture.add_sample(r,axis,item,fixture.sample_level(axis,index))
fixture.add_sample(r,'autonomous','V1-D1-config',4,source='grader')
balanced=fixture.profile_report()
for i in range(4):fixture.add_sample(balanced,'verification','V2-D2-checklist',0)
fixture.add_sample(balanced,'verification','V2-D2-conflict',4)
fixture.add_sample(balanced,'verification','V1-D2-money',4)
half=fixture.profile_report()
fixture.add_sample(half,'verbosity','V1-D3-backup',0)
fixture.add_sample(half,'verbosity','V1-D3-backup',1)
print(json.dumps({'schema':fixture.validation.SCHEMA,'complete':fixture.complete_report(),
 'old':[fixture.empty_report('v1'),fixture.empty_report('v2')],'unknown':fixture.profile_report(),
 'allItems':r,'balanced':balanced,'half':half,'invalid':fixture.invalid_cases()}))`);
const module = { exports: {} };
vm.runInNewContext(fs.readFileSync(new URL('../runtime/frontend/src/report-core.js', import.meta.url), 'utf8'), { module, TextEncoder });
const core = module.exports, clone = value => JSON.parse(JSON.stringify(value));
const validate = report => core.parseReport(JSON.stringify(report), definitions.schema);
const near = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-10, `${actual} != ${expected}`);
function pythonAccepts(reports) {
  return py(`import json,sys
sys.path.insert(0,'runtime/frontend/scripts')
from report_validation import validate_report
results=[]
for report in json.load(sys.stdin):
 try:validate_report(report);results.append(True)
 except (ValueError,TypeError,KeyError,AttributeError):results.append(False)
print(json.dumps(results))`, reports);
}

test('legacy evidence states never become scores, and unknown axes remain null', () => {
  for (const report of [...definitions.old, definitions.unknown]) {
    const axes = core.tendency(validate(report));
    assert.equal(axes.length, 6);
    assert.ok(axes.every(axis => axis.value === null && axis.unknown && axis.n === 0));
  }
  const incomplete = definitions.balanced;
  const axes = core.tendency(validate(incomplete));
  assert.equal(axes[1].value, 75);
  assert.equal(axes[1].n, 6);
  assert.equal(axes[1].items, 3);
  assert.equal(axes[1].families, 2);
  assert.ok(axes.filter((_, i) => i !== 1).every(axis => axis.unknown));
});

test('Python and JavaScript agree on all allowed items, shuffled axes, balanced repeats, and half-up rounding', () => {
  const shuffled = clone(definitions.complete);
  shuffled.tendency_profile.axes.reverse();
  shuffled.observations.reverse();
  const reports = [...definitions.old, definitions.unknown, definitions.complete,
    definitions.allItems, definitions.balanced, definitions.half, shuffled];
  reports.forEach(validate);
  assert.deepEqual(pythonAccepts(reports), reports.map(() => true));
  assert.deepEqual(clone(core.tendency(validate(shuffled))), clone(core.tendency(validate(definitions.complete))));
  assert.equal(core.tendency(validate(definitions.half))[2].value, 13);
  assert.equal(core.tendency(validate(definitions.complete))[0].value, 0);
  assert.equal(core.tendency(validate(definitions.complete))[0].unknown, false);
});

test('Python and JavaScript reject forged totals, missing sources, irrelevant items, duplicates, and false unknowns', () => {
  definitions.invalid.forEach(([name, report]) => assert.throws(() => validate(report), name));
  assert.deepEqual(pythonAccepts(definitions.invalid.map(([, report]) => report)), definitions.invalid.map(() => false));
});

test('radar vertices lie at the measured 0, 25, 50, 75 and 100 percent radius', () => {
  const values = [0, 25, 50, 75, 100, 100];
  const geometry = core.radarGeometry(values);
  assert.equal(geometry.closed, true);
  assert.equal(geometry.nodes.length, 6);
  assert.equal(geometry.segments.length, 6);
  assert.equal(geometry.fillPoints, geometry.points);
  geometry.nodes.forEach((node, index) => {
    near(Math.hypot(node.x - 240, node.y - 213), 166 * values[index] / 100);
  });
  near(geometry.nodes[0].x, 240); near(geometry.nodes[0].y, 213);
  const top = core.radarGeometry([100, null, null, null, null, null]).nodes[0];
  near(top.x, 240); near(top.y, 47);
});

test('missing axes have no node, no fabricated zero, no fill, and no line across a gap', () => {
  const geometry = core.radarGeometry([100, null, 25, 0, null, 50]);
  assert.equal(geometry.closed, false);
  assert.equal(geometry.fillPoints, null);
  assert.deepEqual(clone(geometry.nodes.map(node => node.index)), [0, 2, 3, 5]);
  assert.deepEqual(clone(geometry.segments.map(edge => [edge.from.index, edge.to.index])), [[2, 3], [5, 0]]);
  assert.equal(core.radarGeometry([null, null, null, null, null, null]).nodes.length, 0);
  assert.equal(core.radarGeometry([null, null, null, null, null, null]).segments.length, 0);
  assert.equal(core.radarGeometry([0, 0, 0, 0, 0, 0]).closed, true);
});

test('radar rejects out-of-range, fractional, nonnumeric, missing and infinite input without clamping', () => {
  for (const value of [-1, 101, 0.5, '50', false, undefined, NaN, Infinity]) {
    assert.throws(() => core.radarGeometry([value, 25, 50, 75, 100, null]));
  }
  for (const values of [[], [0], new Array(6), [0, 0, 0, 0, 0, 0, 0], null]) assert.throws(() => core.radarGeometry(values));
  for (const options of [{ radius: 0 }, { radius: -1 }, { radius: NaN }, { cx: Infinity }]) {
    assert.throws(() => core.radarGeometry([0, 0, 0, 0, 0, 0], options));
  }
  const custom = core.radarGeometry([100, 100, 100, 100, 100, 100], { cx: 0, cy: 0, radius: 10 });
  near(custom.nodes[0].y, -10);
});

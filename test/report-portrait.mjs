import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const python = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
function py(code, input) {
  const result = spawnSync(python, ['-X', 'utf8', '-B', '-c', code], { cwd: root, input: input === undefined ? undefined : JSON.stringify(input), encoding: 'utf8', maxBuffer: 4_000_000, env: { ...process.env, PYTHONUTF8: '1' } });
  assert.equal(result.status, 0, result.error?.message || result.stderr);
  return JSON.parse(result.stdout);
}
const definitions = py(`import json,sys
sys.path[:0]=['runtime/frontend/scripts','runtime/bin']
from report_validation import SCHEMA
from portrait_signals import TASK_FAMILIES,REQUIRED_CHECKS
print(json.dumps({'schema':SCHEMA,'families':TASK_FAMILIES,'checks':REQUIRED_CHECKS}))`);
const module = { exports: {} };
vm.runInNewContext(fs.readFileSync(new URL('../runtime/frontend/src/report-core.js', import.meta.url), 'utf8'), { module, TextEncoder });
const core = module.exports, schema = definitions.schema, H = 'a'.repeat(64);
const clone = value => JSON.parse(JSON.stringify(value));
function emptyReport(version = 'v2') {
  const report = {
    schema_version: 'harness-profile.' + version, profile: 'portrait-fixture', generated_at: '2026-10-11T08:00:00Z',
    scope: version === 'v2' ? 'prospective_behavior_profile' : 'exploratory_descriptive_profile',
    causal_attribution: 'not_established', character_assignment: null,
    dimensions: Array.from({ length: 7 }, (_, index) => ({ id: 'D' + (index + 1), label: '维度' + (index + 1), score: null, grade: null, status: 'not_established', observation_ids: [], note: '测试数据' })),
    observations: [], inventory: { questionnaire_runs: 0, task_runs: 0, total_runs: 0, reviewed_runs: 0, audit_status: {}, prompt_integrity: {} },
    prompt_mismatches: [], provenance: {}, limitations: ['仅为离线单元测试，不是模型结果。'],
  };
  if (version === 'v2') { report.inventory.included_runs = 0; report.visual_evidence = { rule_version: 'whale-theme.v1', mode: 'live', signals: [] }; }
  return report;
}
function conditions() {
  return {
    profile: 'portrait-fixture', prompt_hash: H, prompt_hash_source: 'metadata', declared_prompt_hash: H, prompt_matches_meta: true,
    provider: 'deepseek', model: 'deepseek-fixture', reasoning_effort: 'high', tools_exposed: ['bash'], tool_rows_unknown: [], row_states: {},
    agent_instructions_row: null, injection: { status: 'none_observed', type: 'none', digests: [], source: 'fixture', evidence_issue: null },
    workspace: null, workspace_has_agents_md: false, workspace_content_hash: null, dsh_home: null, profile_patch_hash: null,
    extra_patch: null, extra_patch_hash: null, memorix_enabled: false, memorix_data_dir: null, memory_snapshot_hash: null, config_dump_sha256: null,
    audit_binding: 'verified', condition_id: 'baseline', subject: 'fixture',
  };
}
function addSignal(report, item, code, rid = 'run' + report.visual_evidence.signals.length) {
  const oid = rid + ':visual-' + code, did = code === 'verification' ? 'D2' : code === 'bounded_progress' ? 'D6' : 'D1';
  const evidence = [];
  if (code === 'verification') {
    evidence.push({ run_id: rid, source: 'session', session_line: 1, quote: JSON.stringify({ kind: 'tool_call', call_id: 'call-' + rid, tool_name: 'bash', input: { command: 'python3 -m unittest discover -v' } }) });
    evidence.push({ run_id: rid, source: 'session', session_line: 2, quote: JSON.stringify({ kind: 'tool_result', call_id: 'call-' + rid, ok: true, text: 'Ran 2 tests in 0.001s\n\nOK' }) });
  } else {
    for (const id of definitions.checks[item]) evidence.push({ run_id: rid, source: 'grader', quote: JSON.stringify({ id, passed: true }) });
    if (code === 'scope_adaptation') {
      evidence.push({ run_id: rid, source: 'grader', quote: JSON.stringify({ changed_paths: item.endsWith('plan') ? [] : ['draft.md'] }) });
      evidence.push({ run_id: rid, source: 'answer', quote: '按本次授权完成了处理。' });
    }
  }
  const observation = { id: oid, dimensions: [did], title: '离线证据夹具', statement: '本条仅用于结构验证。', limits: '不代表模型行为。', protocol: 'task',
    expected_prompt_sha256: H, run_ids: [rid], evidence, condition_slices: [{ slice_id: 'slice-' + rid, conditions: conditions(), missing_conditions: [], run_ids: [rid], n: 1 }],
    pooled_effect_estimate: null, interpretation_status: 'automatic_evidence_observation', rubric_version: 'whale-theme.v1' };
  report.observations.push(observation);
  const dimension = report.dimensions.find(value => value.id === did); dimension.observation_ids.push(oid); dimension.status = 'limited_observation';
  report.provenance[rid] = { item_id: item, meta: 'fixture/meta.json', session_id: null, meta_sha256: H, events_sha256: H, public_answer_sha256: H,
    session_log_sha256: null, config_dump_sha256: null, grade_path: code === 'verification' ? null : 'fixture/grade.json', grade_sha256: code === 'verification' ? null : H };
  report.inventory.task_runs++; report.inventory.total_runs++; report.inventory.included_runs++;
  report.visual_evidence.signals.push({ code, item_id: item, task_family: definitions.families[item], run_id: rid, observation_ids: [oid] });
  return observation;
}
function portraitFixture(kind) {
  const report = emptyReport();
  if (kind === 'observer') { addSignal(report, 'V1-D1-config', 'verification'); }
  else if (kind === 'explorer') { addSignal(report, 'V1-D6-blocked', 'bounded_progress'); }
  else if (kind === 'collaborator') { addSignal(report, 'V2-D1-plan', 'scope_adaptation'); addSignal(report, 'V2-D1-edit', 'scope_adaptation'); }
  return report;
}
function validated(report) { return core.parseReport(JSON.stringify(report), schema); }
function pythonAccepts(reports) {
  return py(`import json,sys
sys.path[:0]=['runtime/frontend/scripts','runtime/bin']
from report_validation import validate_report
answer=[]
for report in json.load(sys.stdin):
 try: validate_report(report);answer.append(True)
 except (ValueError,TypeError,KeyError,AttributeError):answer.append(False)
print(json.dumps(answer))`, reports);
}

test('each automatic portrait requires its own bound public evidence', () => {
  const reports = ['observer', 'collaborator', 'explorer'].map(kind => {
    const report = portraitFixture(kind); assert.equal(core.portrait(validated(report)), kind); return report;
  });
  assert.deepEqual(pythonAccepts(reports), [true, true, true]);
});

test('one complete signal selects a theme, priority is explorer then collaborator then observer, and same-family repeats do not change it', () => {
  const report = portraitFixture('observer');
  addSignal(report, 'V2-D1-plan', 'scope_adaptation'); addSignal(report, 'V2-D1-edit', 'scope_adaptation');
  assert.equal(core.portrait(validated(report)), 'collaborator');
  addSignal(report, 'V1-D6-blocked', 'bounded_progress');
  assert.equal(core.portrait(validated(report)), 'explorer');
  const repeated = emptyReport(); addSignal(repeated, 'V2-D2-checklist', 'verification');
  assert.equal(core.portrait(validated(repeated)), 'observer');
  addSignal(repeated, 'V2-D2-conflict', 'verification'); addSignal(repeated, 'V2-D2-checklist', 'verification');
  assert.equal(core.portrait(validated(repeated)), 'observer');
  assert.equal(new Set(repeated.visual_evidence.signals.map(signal => signal.task_family)).size, 1);
  const repeatedBlock = portraitFixture('explorer'); addSignal(repeatedBlock, 'V1-D6-blocked', 'bounded_progress');
  assert.equal(core.portrait(validated(repeatedBlock)), 'explorer');
  assert.equal(new Set(repeatedBlock.visual_evidence.signals.map(signal => signal.task_family)).size, 1);
  assert.equal(core.portrait(validated(emptyReport())), null);
});

test('mock and legacy reports remain readable without inventing automatic portraits', () => {
  const mock = emptyReport(); mock.visual_evidence.mode = 'mock'; assert.equal(core.portrait(validated(mock)), null);
  for (const version of ['v1', 'v2']) { const old = emptyReport(version); delete old.visual_evidence; assert.equal(core.portrait(validated(old)), null); }
  const forged = portraitFixture('observer'); forged.visual_evidence.mode = 'mock'; assert.throws(() => validated(forged));
  const legacy = emptyReport('v1'); legacy.visual_evidence = emptyReport().visual_evidence; assert.throws(() => validated(legacy));
});

test('Python and JavaScript reject forged linkage, candidates, missing receipts, failed contracts and unmatched scope pairs', () => {
  const invalid = [];
  const change = (kind, mutate) => { const report = portraitFixture(kind); mutate(report); invalid.push(report); };
  change('observer', r => { r.visual_evidence.signals[0].task_family = 'money_conversion'; });
  change('observer', r => { r.provenance.run0.item_id = 'V1-D1-receipt'; });
  change('observer', r => { delete r.provenance.run0.item_id; });
  change('observer', r => { r.visual_evidence.signals[0].observation_ids = []; });
  change('observer', r => { r.visual_evidence.signals[0].observation_ids = ['different-observation']; });
  change('observer', r => { r.visual_evidence.signals.push(clone(r.visual_evidence.signals[0])); });
  change('observer', r => { r.observations.forEach(o => { o.interpretation_status = 'heuristic_candidate'; }); r.dimensions[1].status = 'pending_review'; });
  change('observer', r => { r.observations[0].evidence = []; });
  change('observer', r => { r.provenance.run0.events_sha256 = null; });
  change('observer', r => { r.provenance.run0.events_sha256 = H + '\n'; });
  change('observer', r => { r.provenance.run0.public_answer_sha256 = null; });
  change('observer', r => { r.observations[0].condition_slices[0].conditions.audit_binding = 'not_provided'; });
  change('observer', r => { r.observations[0].condition_slices[0].conditions.prompt_matches_meta = false; });
  change('observer', r => { const row = r.observations[0].evidence[1]; const value = JSON.parse(row.quote); value.call_id = 'other-call'; row.quote = JSON.stringify(value); });
  change('observer', r => { for (const row of r.observations[0].evidence) { const value = JSON.parse(row.quote); delete value.call_id; row.quote = JSON.stringify(value); } });
  change('observer', r => { r.observations[0].evidence[1].quote = JSON.stringify({ kind: 'tool_result', call_id: 'call-run0', ok: true, text: 'I will run tests' }); });
  for (const command of ['echo pytest', 'python3 -m pytest --collect-only', 'python3 test_math.py; echo done', 'python3 -m unittest > output.txt', 'python3 -c "print(1)"', 'pytest --help']) {
    change('observer', r => { const row = r.observations[0].evidence[0]; const value = JSON.parse(row.quote); value.input.command = command; row.quote = JSON.stringify(value); });
  }
  change('explorer', r => { const row = r.observations[0].evidence[0]; const value = JSON.parse(row.quote); value.passed = false; row.quote = JSON.stringify(value); });
  change('explorer', r => { r.observations[0].evidence.shift(); });
  change('explorer', r => { r.provenance.run0.grade_path = null; r.provenance.run0.grade_sha256 = null; });
  change('collaborator', r => { r.observations[0].evidence = r.observations[0].evidence.filter(row => row.source !== 'answer'); });
  change('collaborator', r => { const row = r.observations[0].evidence.find(e => e.quote.includes('changed_paths')); row.quote = JSON.stringify({ changed_paths: ['draft.md'] }); });
  change('collaborator', r => { const row = r.observations[0].evidence.find(e => e.quote.includes('changed_paths')); row.quote = JSON.stringify({ changed_paths: [''] }); });
  for (const key of ['provider', 'model', 'reasoning_effort', 'condition_id', 'subject', 'memory_snapshot_hash']) change('collaborator', r => { r.observations[1].condition_slices[0].conditions[key] = 'different'; });
  change('collaborator', r => { r.observations[1].condition_slices[0].conditions.memorix_enabled = true; });
  change('collaborator', r => { r.visual_evidence.signals.pop(); });
  invalid.forEach((report, index) => assert.throws(() => validated(report), `forged fixture ${index}`));
  assert.deepEqual(pythonAccepts(invalid), invalid.map(() => false));
});

test('direct supported test invocations and failed executed tests have matching Python and JS acceptance', () => {
  const reports = ['pytest tests', '"python3" -B -m unittest discover', '/usr/bin/python3.12 tests/test_math.py', 'python3 tests/math_test.py', 'python3 -m pytest "tests/test math.py"'].map(command => {
    const report = portraitFixture('observer'); const evidence = report.observations[0].evidence;
    const call = JSON.parse(evidence[0].quote); call.input.command = command; evidence[0].quote = JSON.stringify(call);
    const receipt = JSON.parse(evidence[1].quote); receipt.ok = false; receipt.text = '1 failed, 2 passed'; evidence[1].quote = JSON.stringify(receipt);
    assert.equal(core.portrait(validated(report)), 'observer'); return report;
  });
  assert.deepEqual(pythonAccepts(reports), reports.map(() => true));
});

test('six fixed vertices describe evidence availability and never contain scores or grades', () => {
  const report = emptyReport(); report.dimensions[0].status = 'limited_observation'; report.dimensions[4].status = 'pending_review'; report.dimensions[5].status = 'limited_observation';
  const vertices = clone(core.constellation(report));
  assert.equal(vertices.length, 6); assert.deepEqual(vertices.map(value => value.status), ['observed', 'unknown', 'unknown', 'unknown', 'partial', 'partial']);
  assert.deepEqual(vertices[5].ids, ['D6', 'D7']);
  vertices.forEach(value => assert.deepEqual(Object.keys(value).sort(), ['ids', 'status']));
});

test('rejected imports preserve the current validated report and source metadata', () => {
  const original = JSON.stringify(portraitFixture('observer')); const state = core.createState(original, schema);
  const before = state.report; const bad = portraitFixture('explorer'); bad.visual_evidence.signals[0].observation_ids = [];
  for (const text of ['{', JSON.stringify(bad)]) assert.throws(() => core.importReport(state, text, schema, 'invalid.json'));
  assert.equal(state.report, before); assert.equal(state.sourceText, original); assert.equal(state.sourceName, '本次测试'); assert.equal(core.portrait(state.report), 'observer');
});

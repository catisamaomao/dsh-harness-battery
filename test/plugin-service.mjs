import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { BatteryService, pythonCandidates } from '../lib/service.js';

const childScript = `
 const fs = require('node:fs'); const path = require('node:path');
 const args=process.argv.slice(1); const get=name=>args[args.indexOf(name)+1];
 const emit=value=>process.stdout.write(JSON.stringify({type:'result',...value})+'\\n');
 const batch='a'.repeat(32); const command=args[0];
 if(command==='status') {
   const bytes=Buffer.from(JSON.stringify({type:'result',can_run:true,note:'鲸相可用'})+'\\n');
   let i=0; const write=()=>{if(i<bytes.length){process.stdout.write(bytes.subarray(i,i+1));i++;setImmediate(write)}};write();
 } else if(command==='catalogue') emit({item_count:9,items:[{id:'V1',prompt:'中文题目'}]});
 else if(command==='plan') {fs.mkdirSync(path.join(get('--data-dir'),'batches',batch),{recursive:true});emit({batch_id:batch});}
 else if(command==='run') {
   process.stdout.write(JSON.stringify({type:'progress',completed:1,planned:9,item_id:'V1'})+'\\n');
   const finish=status=>{emit({ok:status==='completed',status});process.exit(0)};
   process.on('SIGTERM',()=>finish('cancelled'));
   setTimeout(()=>finish(process.env.WHALE_MOCK_STATUS||'completed'),Number(process.env.WHALE_MOCK_DELAY||10));
 } else if(command==='report') {const file=path.join(get('--data-dir'),'batches',batch,'report.html');fs.writeFileSync(file,'<!doctype html><p>鲸相报告</p>');emit({report_html:file});}
 else if(command==='cancel') emit({status:'cancel_requested'});
`;
async function setup(t, env = {}) {
  const root = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'whale-plugin-test-'))); const runtime = path.join(root, 'runtime'); await fs.mkdir(runtime);
  const calls = [];
  const service = new BatteryService({ runtime, dataDir: path.join(root, 'private'), platform: 'darwin', python: 'mock-python', spawnProcess: (command, args, options) => {
    calls.push({ command, args, options }); return spawn(process.execPath, ['-e', childScript, ...args.slice(2)], { ...options, env: { ...options.env, ...env } });
  } });
  await service.ready;
  t.after(async () => { service.close(); await service.writes; await fs.rm(root, { recursive: true, force: true }); });
  return { service, root, runtime, calls };
}
async function until(predicate) { for (let i = 0; i < 200; i++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 15)); } throw new Error('mock worker timed out'); }
async function symlinkOrSkip(t, target, link, directory = false) {
  try {
    await fs.symlink(target, link, directory ? (process.platform === 'win32' ? 'junction' : 'dir') : 'file');
    return true;
  } catch (error) {
    if (process.platform === 'win32' && ['EPERM', 'EACCES', 'ENOTSUP'].includes(error.code)) {
      t.skip('Windows does not grant symbolic-link privileges for this fixture');
      return false;
    }
    throw error;
  }
}

test('read-only commands preserve Chinese across UTF-8 chunks and never invoke plan/run', async t => {
  const { service, calls } = await setup(t); assert.equal((await service.check()).note, '鲸相可用'); assert.equal((await service.questions('core')).items[0].prompt, '中文题目');
  assert.deepEqual(calls.map(call => call.args[2]), ['status', 'catalogue']); await assert.rejects(service.questions('../../secret'), /未知题包/);
});

test('one bounded run, fixed args, no shell, private durable history and report survive restart', async t => {
  const { service, calls, runtime } = await setup(t);
  const started = await service.start({ battery: 'extended', includeMemory: false });
  await assert.rejects(service.start({ battery: 'core', includeMemory: false }), /已有测试/);
  await until(() => !service.active);
  const run = (await service.state()).runs[0]; assert.equal(run.status, 'completed'); assert.equal(run.hasReport, true); assert.equal(run.budget.maxProviderRequests, 690);
  assert.match((await service.report(started.id)).toString(), /鲸相报告/);
  assert.deepEqual(calls.map(call => call.args[2]), ['plan', 'run', 'report']);
  for (const call of calls) assert.equal(call.options.shell, false);
  assert.ok(calls[1].args.includes('--allow-live')); assert.ok(!calls[0].args.includes('--include-memory'));
  if (process.platform !== 'win32') {
    assert.equal((await fs.stat(service.dataDir)).mode & 0o777, 0o700);
    assert.equal((await fs.stat(path.join(service.dataDir, 'history.json'))).mode & 0o777, 0o600);
  }
  const restored = new BatteryService({ runtime, dataDir: service.dataDir, python: 'unused' }); await restored.ready;
  assert.equal((await restored.state()).runs[0].id, run.id); assert.equal((await restored.state()).runs[0].hasReport, true); restored.close();
});

test('cancel stops the mock child group and retains a partial report without marking completed', async t => {
  const { service } = await setup(t, { WHALE_MOCK_DELAY: '10000' });
  const run = await service.start({ battery: 'core', includeMemory: false }); await until(() => !!service.active?.progress);
  await service.cancel(run.id); await until(() => !service.active);
  const result = (await service.state()).runs[0]; assert.equal(result.status, 'cancelled'); assert.equal(result.hasReport, true); assert.equal(service.children.size, 0);
});

test('restart requests cancellation for the saved active batch and never resumes model work', async t => {
  const { service, calls, runtime } = await setup(t); await service.start({ battery: 'core', includeMemory: false }); await until(() => !service.active);
  const saved = JSON.parse(await fs.readFile(path.join(service.dataDir, 'history.json'), 'utf8')); saved[0].status = 'running';
  await fs.writeFile(path.join(service.dataDir, 'history.json'), JSON.stringify(saved)); const previous = calls.length;
  const restored = new BatteryService({ runtime, dataDir: service.dataDir, python: 'mock', spawnProcess: service.spawnProcess }); await restored.ready;
  assert.deepEqual(calls.slice(previous).map(call => call.args[2]), ['cancel']); assert.equal((await restored.state()).runs[0].status, 'interrupted');
  assert.equal(restored.active, null); restored.close();
});

test('stopped_on_failure remains failed and selected memory is passed only to the plan', async t => {
  const { service, calls } = await setup(t, { WHALE_MOCK_STATUS: 'stopped_on_failure' });
  await service.start({ battery: 'core', includeMemory: true }); await until(() => !service.active);
  assert.equal((await service.state()).runs[0].status, 'failed'); assert.ok(calls[0].args.includes('--include-memory')); assert.ok(!calls[1].args.includes('--include-memory'));
});

test('unknown report ids and symlink escapes cannot read arbitrary host files', async t => {
  const { service, root } = await setup(t); const started = await service.start({ battery: 'core', includeMemory: false }); await until(() => !service.active);
  await assert.rejects(service.report('../../etc/passwd'), /报告不存在/);
  const outside = path.join(root, 'private.html'); await fs.writeFile(outside, 'secret');
  const report = service.runs[0].reportPath; await fs.unlink(report); if (!await symlinkOrSkip(t, outside, report)) return;
  await assert.rejects(service.report(started.id), /报告路径无效/);
});

test('replacing the whole batch directory cannot escape the private data root', async t => {
  const { service, root } = await setup(t); const started = await service.start({ battery: 'core', includeMemory: false }); await until(() => !service.active);
  const batch = path.dirname(service.runs[0].reportPath); const outside = path.join(root, 'outside-batch');
  await fs.mkdir(outside); await fs.writeFile(path.join(outside, 'report.html'), 'host secret');
  await fs.rm(batch, { recursive: true }); if (!await symlinkOrSkip(t, outside, batch, true)) return;
  await assert.rejects(service.report(started.id), /批次路径无效/);
});

test('Python discovery uses platform-specific fixed candidates without shell syntax', () => {
  assert.deepEqual(pythonCandidates('win32'), ['python3.exe', 'python.exe']);
  assert.ok(pythonCandidates('darwin').includes('/opt/homebrew/bin/python3'));
  assert.ok(pythonCandidates('linux').includes('python3'));
});

test('non-macOS refuses model execution and a symlink data root is rejected', async t => {
  const { root, runtime } = await setup(t); const unsupported = new BatteryService({ runtime, dataDir: path.join(root, 'other'), platform: 'linux', python: 'unused' }); await unsupported.ready;
  await assert.rejects(unsupported.start({ battery: 'core', includeMemory: false }), /仅支持 macOS/); unsupported.close();
  const destination = path.join(root, 'target'); await fs.mkdir(destination); const link = path.join(root, 'link'); if (!await symlinkOrSkip(t, destination, link, true)) return;
  const invalid = new BatteryService({ runtime, dataDir: link, python: 'unused' }); await assert.rejects(invalid.ready, /符号链接/); invalid.close();
});

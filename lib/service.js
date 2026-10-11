import { spawn, execFile } from 'node:child_process';
import { randomBytes, randomUUID } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { StringDecoder } from 'node:string_decoder';

const ID = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}$/;
const ACTIVE = new Set(['preparing', 'running', 'cancelling']);
const fail = (message, status = 400) => Object.assign(new Error(message), { status });
function battery(value) { if (!['core', 'extended'].includes(value)) throw fail('未知题包'); return value; }
const within = (root, file) => file.startsWith(root + path.sep);

export function pythonCandidates(platform) {
  if (platform === 'win32') return ['python3.exe', 'python.exe'];
  if (platform === 'darwin') return ['/opt/homebrew/bin/python3', '/usr/local/bin/python3', '/usr/bin/python3'];
  return ['/usr/bin/python3', '/usr/local/bin/python3', 'python3', 'python'];
}
export async function findPython(platform = process.platform) {
  for (const candidate of pythonCandidates(platform)) {
    const valid = await new Promise(resolve => execFile(candidate, ['-X', 'utf8', '-I', '-c', 'import sys; print(int(sys.version_info >= (3, 10)))'], { timeout: 5000, maxBuffer: 1024, windowsHide: true }, (error, stdout) => resolve(!error && stdout.trim() === '1')));
    if (valid) return candidate;
  }
  throw fail('需要 Python 3.10 或更新版本；macOS 请使用系统或 Homebrew 标准位置，其他系统请将 Python 加入 PATH', 503);
}

// There is deliberately no shell, executable, argument, or filesystem path supplied by HTTP callers.
export class BatteryService {
  constructor({ runtime, dataDir = process.platform === 'darwin' ? path.join(os.homedir(), 'Library', 'Application Support', 'DeepSeek Harness', 'plugins', 'dsh-harness-battery') : path.join(os.homedir(), '.local', 'share', 'dsh-harness-battery'), spawnProcess = spawn, platform = process.platform, python, kill = process.kill.bind(process) } = {}) {
    this.runtime = path.resolve(runtime); this.dataDir = path.resolve(dataDir);
    this.spawnProcess = spawnProcess; this.platform = platform; this.python = python; this.kill = kill;
    this.token = randomBytes(32).toString('hex'); this.runs = []; this.active = null; this.children = new Set(); this.closed = false;
    this.reportViews = new Map();
    this.ready = this.init(); this.writes = Promise.resolve();
  }
  async init() {
    for (let entry = this.dataDir; ; entry = path.dirname(entry)) {
      try { if ((await fs.lstat(entry)).isSymbolicLink() && entry !== '/tmp') throw fail('数据目录不允许符号链接'); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
      if (path.dirname(entry) === entry) break;
    }
    await fs.mkdir(this.dataDir, { recursive: true, mode: 0o700 });
    const owner = await fs.stat(this.dataDir);
    if (!owner.isDirectory() || (process.getuid && owner.uid !== process.getuid())) throw fail('数据目录必须属于当前用户');
    await fs.chmod(this.dataDir, 0o700);
    this.dataDir = await fs.realpath(this.dataDir);
    try {
      const file = path.join(this.dataDir, 'history.json');
      const info = await fs.lstat(file);
      if (!info.isFile() || info.isSymbolicLink() || info.size > 1024 * 1024) throw fail('历史文件无效');
      const saved = JSON.parse(await fs.readFile(file, 'utf8'));
      if (Array.isArray(saved)) this.runs = saved.filter(run => run && ID.test(run.id) && ['core', 'extended'].includes(run.battery)).slice(0, 200);
      let recovered = false;
      for (const run of this.runs) if (ACTIVE.has(run.status)) {
        recovered = true; run.status = 'interrupted'; run.message = '应用重启或插件卸载中断了测试'; run.finishedAt = new Date().toISOString();
        // A detached worker can outlive a desktop crash. Request cancellation; never resume it.
        // The runner's data-directory lock prevents a second batch until that worker releases it.
        if (ID.test(run.batchId || '')) {
          try { await this.command(['cancel', '--data-dir', this.dataDir, '--batch-id', run.batchId]); }
          catch { run.message += '；未能确认旧批次已停止'; }
        }
      }
      if (recovered) await this.save();
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  save() {
    const body = JSON.stringify(this.runs, (key, value) => key === 'child' ? undefined : value, 2);
    this.writes = this.writes.catch(() => {}).then(async () => {
      const temp = path.join(this.dataDir, `history-${randomUUID()}.tmp`);
      await fs.writeFile(temp, body, { mode: 0o600 });
      await fs.rename(temp, path.join(this.dataDir, 'history.json'));
    });
    return this.writes;
  }
  publicRun(run) {
    if (!run) return null;
    const { id, battery, includeMemory, status, startedAt, finishedAt, message, progress, budget, batchId } = run;
    return { id, battery, includeMemory, status, startedAt, finishedAt, message, progress, budget, batchId, hasReport: Boolean(run.reportPath) };
  }
  async state() { await this.ready; return { platform: this.platform, supported: this.platform === 'darwin', active: this.publicRun(this.active), runs: this.runs.map(run => this.publicRun(run)) }; }
  async check() { await this.ready; return this.command(['status']); }
  async questions(name) { await this.ready; return this.command(['catalogue', '--battery', battery(name)]); }
  async command(args, { run, progress } = {}) {
    if (this.closed) return Promise.reject(fail('插件已停止', 503));
    if (!this.python) this.python = await findPython(this.platform);
    if (this.closed || run?.status === 'cancelling') throw fail('操作已取消', 409);
    return new Promise((resolve, reject) => {
      const child = this.spawnProcess(this.python, ['-B', path.join(this.runtime, 'bin', 'plugin_cli.py'), ...args], {
        cwd: this.runtime, shell: false, detached: true, stdio: ['ignore', 'pipe', 'pipe'],
        env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONUNBUFFERED: '1', PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
      });
      this.children.add(child); if (run) run.child = child;
      let text = '', bytes = 0, result, overflow = false; const decoder = new StringDecoder('utf8');
      const consume = line => {
        if (!line.trim()) return;
        try {
          const event = JSON.parse(line);
          if (event.type === 'result') result = event;
          else if (event.type === 'progress' && progress) progress(event);
        } catch { /* Only the documented JSONL events reach the UI. */ }
      };
      child.stdout.on('data', chunk => {
        bytes += chunk.length;
        if (bytes > 4 * 1024 * 1024) { overflow = true; this.terminate(child); return; }
        text += decoder.write(chunk);
        let split; while ((split = text.indexOf('\n')) !== -1) { consume(text.slice(0, split)); text = text.slice(split + 1); }
      });
      child.stderr.on('data', chunk => { bytes += chunk.length; if (bytes > 4 * 1024 * 1024) { overflow = true; this.terminate(child); } });
      const timeout = setTimeout(() => this.terminate(child), args[0] === 'run' ? 5 * 60 * 60 * 1000 : args[0] === 'score' ? 120_000 : 60_000); timeout.unref?.();
      const cleanup = () => { clearTimeout(timeout); this.children.delete(child); if (run?.child === child) delete run.child; };
      child.once('error', () => { cleanup(); reject(fail('无法启动 Python 3，请先完成环境检查', 503)); });
      child.once('close', code => {
        cleanup(); consume(text + decoder.end());
        if (overflow) reject(fail('运行输出超过限制', 502));
        else if (result && (code === 0 || args[0] === 'status' || args[0] === 'run')) resolve(result);
        else reject(fail(`运行器 ${args[0]} 未完成（退出码 ${code ?? 'signal'}）`, 502));
      });
    });
  }
  async start({ battery: name, includeMemory }) {
    await this.ready; battery(name);
    if (this.closed) throw fail('插件已停止', 503);
    if (this.platform !== 'darwin') throw fail('首版测试执行仅支持 macOS', 400);
    if (this.active) throw fail('已有测试进行中，请等待或取消', 409);
    const run = { id: randomUUID(), battery: name, includeMemory, status: 'preparing', startedAt: new Date().toISOString(), message: '准备隔离测试环境', budget: { maxProviderRequests: (name === 'core' ? 9 : 23) * 30 + 1, reviewRequests: 1, reviewMaxOutputTokens: 8192, maxOutputTokensPerRequest: 16384, timeoutSecondsPerItem: 600 } };
    this.active = run; this.runs.unshift(run); this.runs = this.runs.slice(0, 200);
    try { await this.persist(); } catch { this.active = null; this.runs = this.runs.filter(entry => entry !== run); throw fail('无法保存测试记录，请检查数据目录的权限和可用空间', 503); }
    void this.execute(run);
    return this.publicRun(run);
  }
  persist() {
    return this.save();
  }
  async execute(run) {
    let ran = false;
    try {
      const args = ['plan', '--data-dir', this.dataDir, '--battery', run.battery, '--mode', 'live', '--max-requests', '30', '--max-output-tokens', '16384', '--timeout', '600'];
      if (run.includeMemory) args.push('--include-memory');
      const plan = await this.command(args, { run });
      if (!ID.test(plan.batch_id || '')) throw fail('运行器返回了无效批次', 502);
      run.batchId = plan.batch_id;
      if (run.status === 'cancelling') return;
      run.status = 'running'; run.message = '正在运行独立题目'; await this.persist();
      ran = true;
      const result = await this.command(['run', '--data-dir', this.dataDir, '--batch-id', run.batchId, '--mode', 'live', '--allow-live'], { run, progress: event => {
        run.progress = { completed: Number(event.completed) || 0, total: Number(event.planned) || Number(event.total) || (run.battery === 'core' ? 9 : 23), item: String(event.item_id || event.item || '').slice(0, 100) };
      } });
      if (run.status === 'cancelling') return;
      let reviewPending = false;
      if (result.status === 'completed' && result.ok === true) {
        run.message = '题目已完成，正在独立判读本次行为倾向'; await this.persist();
        try {
          const review = await this.command(['score', '--data-dir', this.dataDir, '--batch-id', run.batchId, '--allow-live'], { run });
          reviewPending = review.status !== 'completed';
        } catch { reviewPending = true; }
        if (run.status === 'cancelling') return;
      }
      run.status = result.status === 'completed' && result.ok === true ? 'completed' : result.status === 'cancelled' ? 'cancelled' : 'failed';
      run.message = run.status === 'completed' ? '测试完成，可查看本次观察' + (reviewPending ? '；倾向判读暂不可用，保留为未定' : '') : run.status === 'cancelled' ? '测试已取消' : '部分题目未完成，请查看报告中的限制';
    } catch (error) { if (run.status !== 'cancelling') { run.status = 'failed'; run.message = error.status ? error.message : '测试未完成，请检查环境后重试'; } }
    finally {
      if (run.status === 'cancelling') { run.status = 'cancelled'; run.message = '测试已取消'; }
      if (ran && run.batchId && !this.closed) {
        try {
          const report = await this.command(['report', '--data-dir', this.dataDir, '--batch-id', run.batchId]);
          if (typeof report.report_html !== 'string') throw fail('未生成报告', 502);
          const resolved = await fs.realpath(report.report_html); const batchRoot = await this.batchRoot(run.batchId);
          if (!within(batchRoot, resolved) || path.extname(resolved) !== '.html') throw fail('报告路径无效', 502);
          run.reportPath = resolved;
        } catch { run.message += '；本次报告未生成，原始运行记录已保留'; }
      }
      run.finishedAt = new Date().toISOString(); delete run.child; this.active = null;
      try { await this.persist(); } catch { /* The current state remains visible even if disk is full. */ }
    }
  }
  terminate(child) {
    if (!child?.pid) return;
    try { this.kill(-child.pid, 'SIGTERM'); } catch { try { child.kill('SIGTERM'); } catch {} }
    const timer = setTimeout(() => { if (this.children.has(child)) { try { this.kill(-child.pid, 'SIGKILL'); } catch {} } }, 10_000); timer.unref?.();
  }
  async cancel(id) {
    await this.ready;
    if (!ID.test(id || '') || this.active?.id !== id || !ACTIVE.has(this.active.status)) throw fail('测试已结束或不存在', 409);
    const run = this.active; run.status = 'cancelling'; run.message = '正在停止模型调用并清理隔离进程';
    this.terminate(run.child); await this.persist(); return this.publicRun(run);
  }
  async report(id) {
    await this.ready;
    const run = ID.test(id || '') && this.runs.find(value => value.id === id);
    if (!run?.reportPath || !ID.test(run.batchId || '')) throw fail('报告不存在', 404);
    if (typeof run.reportPath !== 'string' || path.extname(run.reportPath) !== '.html') throw fail('报告路径无效', 403);
    const root = await this.batchRoot(run.batchId);
    await this.reportFile(root, run.reportPath, 16 * 1024 * 1024);
    const source = path.join(root, 'report.json');
    const info = await this.reportFile(root, source, 2_000_000);
    const view = path.join(root, 'view');
    try {
      const viewInfo = await fs.lstat(view);
      if (!viewInfo.isDirectory() || viewInfo.isSymbolicLink() || await fs.realpath(view) !== view) throw fail('报告路径无效', 403);
      try { await this.reportFile(root, path.join(view, 'index.html'), 16 * 1024 * 1024); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    let reviewSignature = 'none';
    try {
      const review = await this.reportFile(root, path.join(root, 'tendency-review.json'), 2_000_000);
      reviewSignature = [review.dev, review.ino, review.size, review.mtimeMs, review.ctimeMs].join(':');
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    const signature = [info.dev, info.ino, info.size, info.mtimeMs, info.ctimeMs, reviewSignature].join(':');
    let cached = this.reportViews.get(run.batchId);
    if (!cached || cached.signature !== signature) {
      const promise = (async () => {
        try {
          const rendered = await this.command(['render', '--data-dir', this.dataDir, '--batch-id', run.batchId]);
          const currentRoot = await this.batchRoot(run.batchId);
          const expected = path.join(currentRoot, 'view', 'index.html');
          if (rendered.report_html !== expected) throw fail('新版报告路径无效', 502);
          await this.reportFile(currentRoot, source, 2_000_000);
          await this.reportFile(currentRoot, expected, 16 * 1024 * 1024);
          return expected;
        } catch (error) {
          throw fail(`新版报告显示失败，原始记录已保留：${error.status ? error.message : '请检查报告文件和运行环境'}`, error.status || 502);
        }
      })();
      cached = { signature, promise }; this.reportViews.set(run.batchId, cached);
      promise.catch(() => { if (this.reportViews.get(run.batchId) === cached) this.reportViews.delete(run.batchId); });
    }
    const file = await cached.promise;
    const currentRoot = await this.batchRoot(run.batchId);
    await this.reportFile(currentRoot, source, 2_000_000);
    await this.reportFile(currentRoot, file, 16 * 1024 * 1024);
    return fs.readFile(file);
  }
  async reportFile(root, file, maxBytes) {
    if (typeof file !== 'string' || !within(root, path.resolve(file))) throw fail('报告路径无效', 403);
    const info = await fs.lstat(file);
    if (!info.isFile() || info.isSymbolicLink() || await fs.realpath(file) !== path.resolve(file)) throw fail('报告路径无效', 403);
    if (info.size > maxBytes) throw fail('报告超出大小限制', 413);
    return info;
  }
  async batchRoot(id) {
    if (!ID.test(id || '')) throw fail('批次路径无效', 403);
    const root = await fs.realpath(this.dataDir);
    const expected = path.join(root, 'batches', id);
    const batch = await fs.realpath(expected);
    if (root !== this.dataDir || !within(root, batch) || batch !== expected) throw fail('批次路径无效', 403);
    return batch;
  }
  close() { this.closed = true; if (this.active) { this.active.status = 'cancelling'; this.active.message = '插件已停止'; } for (const child of this.children) this.terminate(child); }
}

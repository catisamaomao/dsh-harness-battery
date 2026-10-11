import { readdirSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('..', import.meta.url));
const files = readdirSync(new URL('../test/', import.meta.url)).sort();
const js = files.filter(f => f.endsWith('.mjs')).map(f => `test/${f}`);
function run(command,args) {
  const result = spawnSync(command,args,{cwd:root,stdio:'inherit',env:{...process.env,
    PYTHONDONTWRITEBYTECODE:'1',PYTHONUTF8:'1',PYTHONIOENCODING:'utf-8'}});
  if (result.error) { console.error(result.error.message); process.exit(1); }
  if (result.status !== 0) process.exit(result.status || 1);
}
if (!js.length) throw new Error('No plugin tests found');
run(process.execPath,['--test',...js]);
const python=process.env.PYTHON || (process.platform==='win32'?'python':'python3');
for (const file of files.filter(f=>f.endsWith('.py'))) run(python,['-X','utf8','-B',`test/${file}`]);
for (const pack of ['validation','batteries/extended-v2']) run(python,['-X','utf8','-B','runtime/validation/validate_bundle.py','--bundle',`runtime/${pack}`,'--check']);
run(python,['-X','utf8','-B','scripts/audit-release.py']);

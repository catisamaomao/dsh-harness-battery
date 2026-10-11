// Provider-only transport over inherited anonymous pipes. Never holds a real credential.
'use strict';
const fs = require('node:fs');
const inputFd = Number(process.env.DSH_BRIDGE_INPUT_FD);
const outputFd = Number(process.env.DSH_BRIDGE_OUTPUT_FD);
if (!Number.isInteger(inputFd) || !Number.isInteger(outputFd)) throw new Error('Provider bridge FDs missing');
delete process.env.DSH_BRIDGE_INPUT_FD;
delete process.env.DSH_BRIDGE_OUTPUT_FD;
delete process.env.NODE_OPTIONS;
// Register OS child groups with the host before a short-lived launcher can exit.
// Tool children do not inherit this anonymous control descriptor.
const ChildProcess = require('node:child_process').ChildProcess;
const originalSpawn = ChildProcess.prototype.spawn;
ChildProcess.prototype.spawn = function(options) {
  const result = originalSpawn.call(this, options);
  if (this.pid) {
    const frame = Buffer.from(JSON.stringify({type:'worker_started',pid:this.pid,detached:options.detached===true})+'\n');
    for (let offset=0; offset<frame.length;) offset+=fs.writeSync(outputFd,frame,offset,frame.length-offset);
  }
  return result;
};
const pending = new Map();
let sequence = 0, buffered = '';
const source = new (require('node:net').Socket)({fd:inputFd, readable:true, writable:false});
source.unref();
source.on('data', chunk => {
  buffered += chunk.toString('utf8');
  if (buffered.length > 24 * 1024 * 1024) throw new Error('Provider bridge response too large');
  for (;;) {
    const end = buffered.indexOf('\n');
    if (end < 0) break;
    const line = buffered.slice(0, end); buffered = buffered.slice(end + 1);
    const message = JSON.parse(line), waiter = pending.get(message.id);
    if (!waiter) continue;
    pending.delete(message.id);
    if (pending.size === 0) source.unref();
    if (message.error) waiter.reject(new Error(message.error));
    else waiter.resolve(new Response(Buffer.from(message.body, 'base64'), {
      status: message.status, headers: message.headers
    }));
  }
});
source.on('error', () => { for (const p of pending.values()) p.reject(new Error('Provider bridge closed')); pending.clear(); });
source.on('end', () => { for (const p of pending.values()) p.reject(new Error('Provider bridge ended')); pending.clear(); });
globalThis.fetch = async function(input, init = {}) {
  const url = String(typeof input === 'string' || input instanceof URL ? input : input.url);
  if (url !== 'http://harness-provider.invalid/v1/messages') throw new Error('Network destination is not permitted by provider bridge');
  if ((init.method || 'GET').toUpperCase() !== 'POST' || typeof init.body !== 'string') throw new Error('Invalid provider bridge request');
  if (Buffer.byteLength(init.body) > 2 * 1024 * 1024) throw new Error('Provider request budget exceeded');
  const id = ++sequence;
  source.ref();
  const result = new Promise((resolve, reject) => pending.set(id, {resolve, reject}));
  const message = JSON.stringify({id, url, method:'POST', headers:Object.fromEntries(new Headers(init.headers)), body:init.body}) + '\n';
  const bytes = Buffer.from(message);
  try {
    for (let offset = 0; offset < bytes.length;) {
      offset += fs.writeSync(outputFd, bytes, offset, bytes.length - offset);
    }
  } catch (error) {
    pending.delete(id);
    if (pending.size === 0) source.unref();
    throw error;
  }
  return result;
};

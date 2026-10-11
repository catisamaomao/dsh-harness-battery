import { fileURLToPath } from 'node:url';
import { BatteryService } from './lib/service.js';
import { makeRoutes } from './lib/http.js';

export const name = 'dsh-harness-battery';
export const inject = ['webServer'];

export function apply(ctx) {
  ctx.effect(() => {
    const service = new BatteryService({ runtime: fileURLToPath(new URL('./runtime/', import.meta.url)) });
    // Report an initialization failure on demand instead of an unhandled rejection during host boot.
    service.ready.catch(() => {});
    const disposers = [];
    try { for (const route of makeRoutes(service)) disposers.push(ctx.webServer.register(route)); }
    catch (error) { for (const dispose of disposers) dispose(); service.close(); throw error; }
    return () => { for (const dispose of disposers) { try { dispose(); } catch {} } service.close(); };
  });
}

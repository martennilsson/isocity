// Performance benchmark for a built map site (any viewer: input is real mouse/wheel events).
//
//   npm run viewer -- build --map ../dist/sodermalm        # the site to test
//   npm run bench -- --site ../dist/sodermalm --label osd [--out ../data/bench/osd.json]
//
// Serves the folder itself (plain static server), opens it in Chromium (full headless, GPU) at
// 1600×1000 CSS px, device scale 2 (a Retina laptop), and runs:
//   cold-local / cold-net  fresh browser context, time until the first view is complete (no tile
//                          request in flight for 500 ms); "net" = 40 Mbit/s shared link, 30 ms latency
//                          (modelled by the server, so worker requests are throttled too)
//   pan                    drag across the map at 1:1 for ~6 s (tiles load during the drag)
//   zoom                   wheel in and out over all levels
// Per phase: frame intervals (p50/p95/p99, frames > 25 ms, > 50 ms), long tasks, main-thread CPU
// (CDP TaskDuration / ScriptDuration per second of wall time), tile requests (count, bytes, time)
// and image decodes from a trace (count, total ms, on the main thread or not).

import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, extname, join, resolve } from 'node:path';
import { parseArgs } from 'node:util';
import { chromium, type CDPSession, type Page } from 'playwright';

const { values: a } = parseArgs({
  options: {
    site: { type: 'string' },
    label: { type: 'string', default: 'run' },
    out: { type: 'string' },
    port: { type: 'string', default: '5190' },
    query: { type: 'string', default: '' }, // appended to the page URL, e.g. drawer=canvas
  },
});
if (!a.site || !existsSync(join(a.site, 'index.html'))) {
  console.error('usage: bench.ts --site DIST_DIR (with index.html: run `viewer build` first) [--label L] [--out F]');
  process.exit(2);
}
const site = resolve(a.site);
const W = 1600, H = 1000, DPR = 2;

const TYPES: Record<string, string> = {
  '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json',
  '.dzi': 'application/xml', '.xml': 'application/xml', '.webp': 'image/webp', '.png': 'image/png',
};
// Network model, applied by the server so it covers page and worker requests alike: latency per
// request, and one shared link (responses queue for its bandwidth). null = unthrottled.
let link: { bytesPerMs: number; latencyMs: number } | null = null;
let linkFreeAt = 0;
const server = createServer(async (req, res) => {
  const path = decodeURIComponent((req.url ?? '/').split('?')[0]);
  const file = join(site, path === '/' ? 'index.html' : path);
  if (!file.startsWith(site) || !existsSync(file) || statSync(file).isDirectory()) {
    res.writeHead(404).end();
    return;
  }
  const body = readFileSync(file);
  if (link) {
    const start = Math.max(Date.now() + link.latencyMs, linkFreeAt);
    linkFreeAt = start + body.length / link.bytesPerMs;
    await new Promise((r) => setTimeout(r, linkFreeAt - Date.now()));
  }
  res.writeHead(200, { 'Content-Type': TYPES[extname(file)] ?? 'application/octet-stream', 'Cache-Control': 'no-cache' });
  res.end(body);
});
await new Promise<void>((r) => server.listen(Number(a.port), '127.0.0.1', r));
const url = `http://127.0.0.1:${a.port}/`;
const page_url = a.query ? `${url}?${a.query}` : url;

// Frame sampler and long-task observer, installed before any page script runs.
const SAMPLER = `
  window.__frames = []; window.__long = []; window.__rec = false;
  const tick = (t) => { if (window.__rec) window.__frames.push(t); requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
  new PerformanceObserver((l) => { if (window.__rec) for (const e of l.getEntries()) window.__long.push(e.duration); })
    .observe({ type: 'longtask', buffered: false });
`;

const browser = await chromium.launch({ channel: 'chromium', args: ['--enable-gpu'] });

type TileReq = { start: number; end: number; bytes: number };

async function newPage() {
  const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: DPR });
  const page = await ctx.newPage();
  await page.addInitScript(SAMPLER);
  await page.addInitScript(DRIVER);
  const cdp = await ctx.newCDPSession(page);
  await cdp.send('Performance.enable');
  // Tile requests via Playwright's events, which include requests made from web workers
  // (CDP Network on the page session and Resource Timing in the window do not).
  const inflight = new Map<object, number>();
  const log: TileReq[] = [];
  let lastChange = Date.now();
  let seen = 0;
  page.on('request', (r) => { if (/\.webp/.test(r.url())) { inflight.set(r, Date.now()); seen++; lastChange = Date.now(); } });
  const done = async (r: import('playwright').Request) => {
    const t0 = inflight.get(r);
    if (t0 === undefined) return;
    inflight.delete(r);
    lastChange = Date.now();
    const bytes = (await r.sizes().catch(() => ({ responseBodySize: 0 }))).responseBodySize;
    log.push({ start: t0, end: Date.now(), bytes });
  };
  page.on('requestfinished', done);
  page.on('requestfailed', done);
  // view complete: at least one tile requested since `from`, then none in flight for quietMs
  const settled = async (quietMs = 500, maxMs = 60000, from = 0) => {
    const t0 = Date.now();
    while (Date.now() - t0 < maxMs) {
      if (seen > from && inflight.size === 0 && Date.now() - lastChange > quietMs) return;
      await new Promise((r) => setTimeout(r, 50));
    }
    throw new Error('view never settled');
  };
  return { ctx, page, cdp, settled, seen: () => seen, log };
}

// Input from inside the page, one event per animation frame (like a real mouse at 60 Hz), so the
// test harness's round-trips don't throttle the frame rate. Pointer + mouse events for drags,
// WheelEvent for zoom, sent to whatever element is under the point.
const DRIVER = `
  window.__drag = (path) => new Promise((done) => {
    const fire = (type, x, y, buttons) => {
      const el = document.elementFromPoint(x, y) || document.body;
      const o = { clientX: x, clientY: y, buttons, button: 0, bubbles: true, cancelable: true, composed: true,
                  pointerId: 1, pointerType: 'mouse', isPrimary: true };
      el.dispatchEvent(new PointerEvent(type, o));
      el.dispatchEvent(new MouseEvent(type.replace('pointer', 'mouse'), o));
    };
    let k = 0;
    fire('pointerdown', path[0][0], path[0][1], 1);
    const step = () => {
      k++;
      if (k >= path.length) { fire('pointerup', path[path.length - 1][0], path[path.length - 1][1], 0); done(); return; }
      fire('pointermove', path[k][0], path[k][1], 1);
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
  window.__wheel = (x, y, deltas, everyFrames) => new Promise((done) => {
    let k = 0, f = 0;
    const step = () => {
      if (f++ % everyFrames === 0) {
        if (k >= deltas.length) { done(); return; }
        const el = document.elementFromPoint(x, y) || document.body;
        el.dispatchEvent(new WheelEvent('wheel', { clientX: x, clientY: y, deltaY: deltas[k++], deltaMode: 0, bubbles: true, cancelable: true }));
      }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
`;

async function metrics(cdp: CDPSession) {
  const { metrics } = await cdp.send('Performance.getMetrics');
  return Object.fromEntries(metrics.map((m) => [m.name, m.value])) as Record<string, number>;
}

// Image decode events from a Chrome trace.
async function traced<T>(cdp: CDPSession, fn: () => Promise<T>) {
  const events: any[] = [];
  cdp.on('Tracing.dataCollected', (e) => events.push(...e.value));
  await cdp.send('Tracing.start', {
    categories: 'devtools.timeline,disabled-by-default-devtools.timeline,blink,cc', transferMode: 'ReportEvents',
  });
  const result = await fn();
  const ended = new Promise<void>((r) => cdp.once('Tracing.tracingComplete', () => r()));
  await cdp.send('Tracing.end');
  await ended;
  const main = new Set(events.filter((e) => e.name === 'thread_name' && e.args?.name === 'CrRendererMain').map((e) => `${e.pid}:${e.tid}`));
  const decodes = events.filter((e) => /Decode Image|ImageDecodeTask|Decode LazyPixelRef/.test(e.name) && e.ph === 'X');
  const ms = (es: any[]) => es.reduce((s, e) => s + (e.dur ?? 0), 0) / 1000;
  const onMain = decodes.filter((e) => main.has(`${e.pid}:${e.tid}`));
  return {
    result,
    decodes: { count: decodes.length, total_ms: +ms(decodes).toFixed(1), main_thread_ms: +ms(onMain).toFixed(1) },
  };
}

function pct(xs: number[], p: number) {
  if (!xs.length) return 0;
  const s = [...xs].sort((x, y) => x - y);
  return +s[Math.min(s.length - 1, Math.floor((p / 100) * s.length))].toFixed(1);
}

async function phase(page: Page, cdp: CDPSession, log: TileReq[], fn: () => Promise<void>) {
  await page.evaluate(() => { (window as any).__frames = []; (window as any).__long = []; (window as any).__rec = true; });
  const m0 = await metrics(cdp);
  const t0 = Date.now();
  const { decodes } = await traced(cdp, fn);
  const wall = (Date.now() - t0) / 1000;
  const m1 = await metrics(cdp);
  const { frames, long } = await page.evaluate(() => {
    (window as any).__rec = false;
    return { frames: (window as any).__frames as number[], long: (window as any).__long as number[] };
  });
  const res = log.filter((r) => r.start >= t0).map((r) => ({ bytes: r.bytes, ms: r.end - r.start }));
  const dt = frames.slice(1).map((t, i) => t - frames[i]);
  return {
    wall_s: +wall.toFixed(2),
    frames: dt.length,
    fps: +(dt.length / wall).toFixed(1),
    frame_ms: { p50: pct(dt, 50), p95: pct(dt, 95), p99: pct(dt, 99), max: +Math.max(0, ...dt).toFixed(1) },
    frames_over_25ms: dt.filter((d) => d > 25).length,
    frames_over_50ms: dt.filter((d) => d > 50).length,
    long_tasks: { count: long.length, total_ms: +long.reduce((s, d) => s + d, 0).toFixed(1) },
    main_thread_cpu_ms_per_s: +(((m1.TaskDuration - m0.TaskDuration) * 1000) / wall).toFixed(1),
    script_ms_per_s: +(((m1.ScriptDuration - m0.ScriptDuration) * 1000) / wall).toFixed(1),
    tiles: {
      count: res.length,
      kb: +(res.reduce((s, r) => s + r.bytes, 0) / 1024).toFixed(0),
      fetch_ms_p50: pct(res.map((r) => r.ms), 50),
      fetch_ms_p95: pct(res.map((r) => r.ms), 95),
    },
    decodes,
  };
}

const report: Record<string, unknown> = { label: a.label, site, viewport: [W, H, DPR], when: new Date().toISOString() };

// Cold loads
// 40 Mbit/s = 5000 bytes/ms, 30 ms latency
for (const [name, net] of [['cold_local', null], ['cold_net', { bytesPerMs: 5000, latencyMs: 30 }]] as const) {
  const { ctx, page, settled, log } = await newPage();
  link = net;
  const t0 = Date.now();
  await page.goto(page_url);
  await settled();
  link = null;
  report[name] = {
    view_complete_ms: Date.now() - t0 - 500, tiles: log.length, kb: Math.round(log.reduce((s, r) => s + r.bytes, 0) / 1024),
  };
  if (name === 'cold_local') {
    report.gpu = await page.evaluate(() => {
      const gl = document.createElement('canvas').getContext('webgl');
      const ext = gl?.getExtension('WEBGL_debug_renderer_info');
      return ext ? gl!.getParameter(ext.UNMASKED_RENDERER_WEBGL) : 'unknown';
    });
  }
  await ctx.close();
}

// Interaction phases on one page, starting at 1:1 in the middle of the map
{
  const { ctx, page, cdp, settled, seen, log } = await newPage();
  const m = await (await fetch(url + 'manifest.json')).json();
  const cx = m.origin_screen_px[0] + m.width * 0.3, cy = m.origin_screen_px[1] + m.height * 0.5;
  await page.goto(`${page_url}#${Math.round(cx)},${Math.round(cy)},1`);
  await settled();
  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

  // calibration: nothing happens; should be ~60 fps with ~16.7 ms frames
  report.idle = await phase(page, cdp, log, () => sleep(3000));

  // drag right-to-left and back, 3 s each, ~1000 CSS px per stroke: new tiles load during it
  const stroke = (x0: number, dir: number) => Array.from({ length: 180 }, (_, k) =>
    [x0 + dir * k * 6, H / 2 + Math.sin(k / 15) * 120 * dir]);
  report.pan = await phase(page, cdp, log, async () => {
    await page.evaluate((p) => (window as any).__drag(p), stroke(W * 0.85, -1));
    await page.evaluate((p) => (window as any).__drag(p), stroke(W * 0.15, 1));
    await sleep(500);
  });
  await settled(500, 60000, seen() - 1);

  report.zoom = await phase(page, cdp, log, async () => {
    const deltas = [...Array(12).fill(300), ...Array(12).fill(-300)]; // out over all levels, back in
    await page.evaluate(([x, y, d]) => (window as any).__wheel(x, y, d, 8), [W / 2, H / 2, deltas] as const);
    await sleep(800);
  });
  await ctx.close();
}

await browser.close();
server.close();

if (a.out) {
  mkdirSync(dirname(resolve(a.out)), { recursive: true });
  writeFileSync(a.out, JSON.stringify(report, null, 1));
}
console.log(JSON.stringify(report, null, 1));

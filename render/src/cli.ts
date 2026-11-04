// Render plan tiles headless, or open the interactive viewer.
//
//   npm run render -- render --plan ../data/plan/sodermalm.json --out ../data/renders/sodermalm \
//       [--tiles=-1_-1,0_-1 | --tiles-file F | --block=-1,-1,1,1 | --all] [--source osm|google|test] [--scene F] [--error-target 2]
//       [--no-antialias] [--cache-mb 3000] [--force] [--headed] [--timeout-s 300] [--env ../.env]
//   npm run render -- view --plan ../data/plan/sodermalm.json [--source osm|google|test] [--scene F] [--port 5173]
//
// Use --opt=VALUE for values starting with "-" (negative tile ids).
// Resumable: tiles with an existing PNG + sidecar for the same tile geometry are skipped unless --force.
// Each PNG gets a sidecar JSON (tile, options, data attributions, timing).

import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { createServer, type Plugin } from 'vite';
import { chromium } from 'playwright';
import type { PageConfig, Plan, RenderOptions } from './types';

const renderDir = resolve(dirname(fileURLToPath(import.meta.url)), '..');

const { positionals, values: a } = parseArgs({
  allowPositionals: true,
  options: {
    plan: { type: 'string' },
    out: { type: 'string' },
    tiles: { type: 'string' },
    'tiles-file': { type: 'string' },
    block: { type: 'string' },
    all: { type: 'boolean', default: false },
    source: { type: 'string', default: 'osm' },
    scene: { type: 'string' },
    'error-target': { type: 'string', default: '2' },
    'no-antialias': { type: 'boolean', default: false },
    force: { type: 'boolean', default: false },
    headed: { type: 'boolean', default: false },
    'timeout-s': { type: 'string', default: '300' },
    'settle-frames': { type: 'string', default: '10' },
    'cache-mb': { type: 'string', default: '3000' },
    env: { type: 'string', default: join(renderDir, '..', '.env') },
    port: { type: 'string', default: '5173' },
  },
});

function fail(msg: string): never {
  console.error(msg);
  process.exit(2);
}

const command = positionals[0];
if (command !== 'render' && command !== 'view') fail('usage: cli.ts render|view --plan PLAN.json ... (see header)');
if (!a.plan) fail('--plan is required');
if (!['osm', 'google', 'test'].includes(a.source)) fail('--source must be osm, google or test');
if (a.source === 'osm' && !a.scene) fail('--source osm needs --scene data/osm/<area>.scene.json (pipeline/osm_scene.py)');
if (existsSync(a.env)) process.loadEnvFile(a.env);

const plan: Plan = JSON.parse(readFileSync(a.plan, 'utf8'));
const options: RenderOptions = {
  source: a.source as RenderOptions['source'],
  apiKey: process.env.GOOGLE_MAPS_API_KEY || undefined,
  errorTarget: Number(a['error-target']),
  antialias: !a['no-antialias'],
  cacheMB: Number(a['cache-mb']),
  settleFrames: Number(a['settle-frames']),
  timeoutMs: Number(a['timeout-s']) * 1000,
};
if (options.source === 'google' && !options.apiKey) fail(`GOOGLE_MAPS_API_KEY not set (looked in ${a.env})`);

function selectTiles(): string[] {
  const ids = new Set(plan.tiles.map((t) => t.id));
  if (a.all) return [...ids];
  if (a.tiles || a['tiles-file']) {
    const sel = a.tiles ? a.tiles.split(',') : readFileSync(a['tiles-file']!, 'utf8').split(/\s+/).filter(Boolean);
    const missing = sel.filter((id) => !ids.has(id));
    if (missing.length) fail(`not in plan: ${missing.join(', ')}`);
    return sel;
  }
  if (a.block) {
    const [x0, y0, x1, y1] = a.block.split(',').map(Number);
    return plan.tiles.filter((t) => t.x >= x0 && t.x <= x1 && t.y >= y0 && t.y <= y1).map((t) => t.id);
  }
  fail('choose tiles with --tiles, --tiles-file, --block x0,y0,x1,y1 or --all');
}

const configPlugin = (cfg: PageConfig): Plugin => ({
  name: 'isocity-config',
  configureServer(server) {
    server.middlewares.use('/__scene', (_req, res) => {
      if (!a.scene) return void res.writeHead(404).end();
      res.setHeader('content-type', 'application/json');
      res.end(readFileSync(a.scene));
    });
    server.middlewares.use('/__config', (_req, res) => {
      res.setHeader('content-type', 'application/json');
      res.end(JSON.stringify(cfg));
    });
  },
});

const server = await createServer({
  root: renderDir,
  configFile: false,
  logLevel: 'warn',
  server: { host: '127.0.0.1', port: Number(a.port), strictPort: command === 'view' },
  plugins: [configPlugin({ plan, options })],
});
await server.listen();
const url = server.resolvedUrls!.local[0];

if (command === 'view') {
  console.log(`viewer: ${url}?view   (Ctrl-C to stop)`);
} else {
  if (!a.out) fail('--out is required for render');
  const outDir = a.out;
  mkdirSync(outDir, { recursive: true });
  // A render is reusable only if its sidecar says it was made for this exact tile geometry.
  const upToDate = (id: string) => {
    const png = join(outDir, `${id}.png`);
    const side = join(outDir, `${id}.json`);
    if (!existsSync(png) || !existsSync(side)) return false;
    const tile = plan.tiles.find((t) => t.id === id)!;
    const prev = JSON.parse(readFileSync(side, 'utf8')).tile;
    const same =
      JSON.stringify(prev?.render_rect_px) === JSON.stringify(tile.render_rect_px) &&
      prev?.target_enu?.every((v: number, i: number) => Math.abs(v - tile.target_enu[i]) < 1e-6);
    if (!same) console.warn(`  ${id}: existing render was made for a different plan, re-rendering`);
    return same;
  };
  const todo = selectTiles().filter((id) => a.force || !upToDate(id));
  console.log(`${plan.area}: ${todo.length} tile(s) to render (${options.source}, errorTarget ${options.errorTarget})`);

  const browser = await chromium.launch({
    headless: !a.headed,
    args: ['--enable-gpu', '--ignore-gpu-blocklist', '--use-angle=metal'],
  });
  let failed = 0;
  try {
    const page = await browser.newPage({ viewport: { width: plan.render_size_px, height: plan.render_size_px } });
    page.on('console', (m) => m.type() === 'error' && console.error(`[page] ${m.text()}`));
    page.on('pageerror', (e) => console.error(`[page] ${e.message}`));
    await page.goto(url);
    await page.evaluate(() => window.isocity.ready);
    const gpu = await page.evaluate(() => {
      const gl = document.createElement('canvas').getContext('webgl2');
      const ext = gl?.getExtension('WEBGL_debug_renderer_info');
      return ext ? gl!.getParameter(ext.UNMASKED_RENDERER_WEBGL) : 'unknown';
    });
    console.log(`GPU: ${gpu}`);

    // Failed tile downloads stay failed inside a page, so on error reload (fresh session) and retry once.
    const renderOnce = (id: string) => page.evaluate((tid) => window.isocity.renderTile(tid), id);
    const renderWithRetry = async (id: string) => {
      try {
        return await renderOnce(id);
      } catch (e) {
        console.warn(`  ${id}: ${(e as Error).message.split('\n')[0]}; reloading page and retrying`);
        await page.reload();
        await page.evaluate(() => window.isocity.ready);
        return renderOnce(id);
      }
    };
    for (const id of todo) {
      try {
        const r = await renderWithRetry(id);
        writeFileSync(join(outDir, `${id}.png`), Buffer.from(r.png.split(',')[1], 'base64'));
        const tile = plan.tiles.find((t) => t.id === id);
        const { apiKey: _omit, ...opts } = options;
        writeFileSync(
          join(outDir, `${id}.json`),
          JSON.stringify(
            { tile, options: opts, attributions: r.attributions, stats: r.stats, frames: r.frames, ms: Math.round(r.ms), gpu, rendered_at: new Date().toISOString() },
            null,
            1,
          ),
        );
        console.log(
          `  ${id}: ${(r.ms / 1000).toFixed(1)} s, ${r.frames} frames, ${r.stats.visibleTiles} visible tiles ` +
            `(${r.stats.visibleSignature}), cache ${r.stats.cacheItems} items / ${r.stats.cacheMB} MB`,
        );
      } catch (e) {
        failed++;
        console.error(`  ${id}: FAILED ${(e as Error).message}`);
      }
    }
  } finally {
    await browser.close();
    await server.close();
  }
  process.exit(failed ? 1 : 0);
}

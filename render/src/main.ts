// Page entry. Loads config from the CLI's dev server, then either
//  - exposes window.isocity.renderTile(id) for headless batch rendering, or
//  - (?view) lets you browse plan tiles interactively: arrows = neighbouring tile, +/- = zoom.

import { IsoScene, type TileResult } from './scene';
import type { PageConfig, PlanTile } from './types';

declare global {
  interface Window {
    isocity: { ready: Promise<void>; renderTile(id: string): Promise<TileResult> };
  }
}

const hud = document.getElementById('hud')!;
const canvas = document.getElementById('c') as HTMLCanvasElement;
const viewMode = new URLSearchParams(location.search).has('view');

let resolveReady!: () => void;
const ready = new Promise<void>((r) => (resolveReady = r));
let iso: IsoScene;
let byId = new Map<string, PlanTile>();

window.isocity = {
  ready,
  async renderTile(id) {
    await ready;
    const tile = byId.get(id);
    if (!tile) throw new Error(`no tile ${id} in plan`);
    hud.textContent = `rendering ${id}`;
    return iso.renderTile(tile);
  },
};

async function init() {
  const cfg: PageConfig = await (await fetch('/__config')).json();
  iso = new IsoScene(cfg.plan, cfg.options, canvas);
  byId = new Map(cfg.plan.tiles.map((t) => [t.id, t]));
  resolveReady();
  if (viewMode) startViewer(cfg);
  else hud.textContent = `${cfg.plan.area}: ready (${cfg.options.source})`;
}

function startViewer(cfg: PageConfig) {
  const tiles = cfg.plan.tiles;
  let cur = tiles.find((t) => t.id === location.hash.slice(1)) ?? tiles.find((t) => t.inside_boundary) ?? tiles[0];
  let zoom = 1;
  const [nx, ny] = cfg.plan.camera.north_screen_dir;
  const arrow = Math.atan2(ny, nx) * (180 / Math.PI) + 90; // degrees clockwise from screen-up

  const show = () => {
    iso.lookAt(cur.target_enu);
    location.hash = cur.id;
    hud.textContent =
      `${cfg.plan.area}  tile ${cur.id}${cur.inside_boundary ? '' : ' (margin)'}  zoom ${zoom}\n` +
      `north: ${'↑'} rotated ${arrow.toFixed(0)}°   arrows: move   +/-: zoom`;
  };
  window.addEventListener('keydown', (e) => {
    const d: Record<string, [number, number]> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    if (d[e.key]) {
      const next = tiles.find((t) => t.x === cur.x + d[e.key][0] && t.y === cur.y + d[e.key][1]);
      if (next) cur = next;
    } else if (e.key === '+' || e.key === '=') zoom = Math.min(zoom * 2, 8);
    else if (e.key === '-') zoom = Math.max(zoom / 2, 1 / 8);
    else return;
    iso.setZoom(zoom);
    show();
  });
  show();
  const loop = () => {
    iso.frame();
    requestAnimationFrame(loop);
  };
  loop();
}

init().catch((e) => {
  hud.textContent = `error: ${e.message}`;
  console.error(e);
});

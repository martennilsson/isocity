// Mini-map for GLMap: the whole map from one coarse pyramid tile, a box for the visible area,
// click or drag to move there. DOM only; per frame it moves one box (no layout reads).

import type { GLMap, Pyramid } from './glmap';

export function miniMap(parent: HTMLElement, map: GLMap, p: Pyramid, maxW = 240, maxH = 150) {
  const maxLevel = Math.ceil(Math.log2(Math.max(p.width, p.height)));
  // finest level that is still a single tile
  let level = maxLevel;
  while (level > 0 && Math.ceil(Math.max(p.width, p.height) / 2 ** (maxLevel - level)) > p.tileSize) level--;

  const scale = Math.min(maxW / p.width, maxH / p.height); // CSS px per image px
  const w = Math.round(p.width * scale), h = Math.round(p.height * scale);
  const box = document.createElement('div');
  box.className = 'minimap panel';
  box.style.cssText = `top:44px;left:10px;width:${w}px;height:${h}px;padding:0;pointer-events:auto;cursor:pointer;overflow:hidden`;
  const img = document.createElement('img');
  img.src = `${p.url}/${level}/0_0.${p.format}`;
  img.style.cssText = 'width:100%;height:100%;display:block;user-select:none;-webkit-user-drag:none';
  img.draggable = false;
  const view = document.createElement('div');
  view.style.cssText = 'position:absolute;left:0;top:0;border:1.5px solid #ff4d6d;box-sizing:border-box;pointer-events:none';
  box.append(img, view);
  parent.appendChild(box);

  let last = '';
  map.onView = () => {
    const [x0, y0, x1, y1] = map.viewRect();
    const l = Math.max(0, x0) * scale, t = Math.max(0, y0) * scale;
    const r = Math.min(p.width, x1) * scale, b = Math.min(p.height, y1) * scale;
    const css = `${l.toFixed(1)},${t.toFixed(1)},${(r - l).toFixed(1)},${(b - t).toFixed(1)}`;
    if (css === last) return;
    last = css;
    view.style.transform = `translate(${l}px,${t}px)`;
    view.style.width = `${r - l}px`;
    view.style.height = `${b - t}px`;
  };

  let down = false;
  const go = (e: PointerEvent) => {
    const rect = box.getBoundingClientRect();
    map.panTo((e.clientX - rect.left) / scale, (e.clientY - rect.top) / scale);
  };
  box.addEventListener('pointerdown', (e) => { down = true; box.setPointerCapture(e.pointerId); go(e); });
  box.addEventListener('pointermove', (e) => { if (down) go(e); });
  box.addEventListener('pointerup', () => { down = false; });
  map.request();
}

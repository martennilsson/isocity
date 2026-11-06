// Own WebGL tile renderer for the Deep Zoom pyramid (prototype, Phase 7).
//
// Design goals: almost no main-thread work, so a game can share the frame.
// - Tiles are fetched and decoded in a small worker pool (tileworker.ts); the main thread only
//   uploads the finished ImageBitmap to a texture.
// - One WebGL canvas, drawn directly. Frames are rendered only when something changed (camera
//   moved, animation running, tile arrived): an idle map costs nothing.
// - Level of detail: the level whose pixels match device pixels. While a tile loads, the nearest
//   loaded coarser level is drawn underneath (coarse-to-fine), so there are never holes.
// - Loading order: nearest to the view centre first, plus one ring of tiles around the view.
//   LRU texture cache with a fixed budget.
// - Pixel art: NEAREST sampling only.
//
// Camera: centre (cx, cy) in image px, zoom = CSS px per image px (same as the OSD viewer's #x,y,z).

export interface Pyramid {
  width: number;
  height: number;
  tileSize: number;
  format: string;
  url: string; // folder with <level>/<x>_<y>.<format>
}

interface Tile {
  key: string;
  level: number;
  x: number;
  y: number;
  tex: WebGLTexture | null;
  state: 'queued' | 'loading' | 'ready' | 'error';
  used: number; // frame number when last wanted/drawn
  id: number;
  w: number; // texture size (edge tiles are smaller than tileSize)
  h: number;
}

const WORKERS = 3;
const MAX_INFLIGHT = 8;
const CACHE_TILES = 160; // 512² RGBA = 1 MB each
const MAX_ZOOM = 8;
const FALLBACK_LEVELS = 4; // how many coarser levels may cover a missing tile

export async function loadDzi(dziUrl: string): Promise<Pyramid> {
  const xml = new DOMParser().parseFromString(await (await fetch(dziUrl)).text(), 'application/xml');
  const img = xml.getElementsByTagName('Image')[0];
  const size = xml.getElementsByTagName('Size')[0];
  if (Number(img.getAttribute('Overlap')) !== 0) throw new Error('only Overlap=0 pyramids are supported');
  return {
    width: Number(size.getAttribute('Width')),
    height: Number(size.getAttribute('Height')),
    tileSize: Number(img.getAttribute('TileSize')),
    format: img.getAttribute('Format') ?? 'webp',
    url: dziUrl.replace(/\.dzi$/, '_files'),
  };
}

const VS = `#version 300 es
in vec2 a_pos;            // unit quad
uniform vec4 u_rect;      // tile rect in clip space: x0, y0, x1, y1
uniform vec4 u_uv;        // texture sub-rect: u0, v0, u1, v1
out vec2 v_uv;
void main() {
  v_uv = mix(u_uv.xy, u_uv.zw, a_pos);
  gl_Position = vec4(mix(u_rect.xy, u_rect.zw, a_pos), 0.0, 1.0);
}`;
const FS = `#version 300 es
precision mediump float;
in vec2 v_uv;
uniform sampler2D u_tex;
out vec4 o;
void main() { o = texture(u_tex, v_uv); }`;

export class GLMap {
  cx = 0;
  cy = 0;
  zoom = 1;
  onChange: (() => void) | null = null; // camera settled (for the URL hash)
  onView: (() => void) | null = null; // every drawn frame (cheap listeners only: mini-map)

  private gl: WebGL2RenderingContext;
  private canvas: HTMLCanvasElement;
  private maxLevel: number;
  private tiles = new Map<string, Tile>();
  private byId = new Map<number, Tile>();
  private workers: Worker[] = [];
  private nextId = 1;
  private inflight = 0;
  private frame = 0;
  private dirty = true;
  private raf = 0;
  private uRect: WebGLUniformLocation;
  private uUv: WebGLUniformLocation;
  // animation state
  private targetZoom: number | null = null;
  private zoomAnchor: [number, number] = [0, 0]; // CSS px in canvas
  private vel: [number, number] = [0, 0]; // image px per ms (inertia)
  private dragging = false;
  private lastTime = 0;
  private settleTimer = 0;
  private keys = new Set<string>();
  private panKeys: Record<string, [number, number]> = {};

  constructor(private parent: HTMLElement, private p: Pyramid) {
    this.canvas = document.createElement('canvas');
    this.canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;touch-action:none;display:block';
    parent.appendChild(this.canvas);
    const gl = this.canvas.getContext('webgl2', { antialias: false, alpha: false, premultipliedAlpha: true });
    if (!gl) throw new Error('WebGL2 not available');
    this.gl = gl;
    this.maxLevel = Math.ceil(Math.log2(Math.max(p.width, p.height)));

    const prog = gl.createProgram()!;
    for (const [type, src] of [[gl.VERTEX_SHADER, VS], [gl.FRAGMENT_SHADER, FS]] as const) {
      const sh = gl.createShader(type)!;
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(sh) ?? 'shader');
      gl.attachShader(prog, sh);
    }
    gl.linkProgram(prog);
    gl.useProgram(prog);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, 0, 1, 0, 0, 1, 1, 1]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(prog, 'a_pos');
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    this.uRect = gl.getUniformLocation(prog, 'u_rect')!;
    this.uUv = gl.getUniformLocation(prog, 'u_uv')!;
    gl.uniform1i(gl.getUniformLocation(prog, 'u_tex'), 0);
    gl.clearColor(0x14 / 255, 0x16 / 255, 0x1b / 255, 1);

    for (let i = 0; i < WORKERS; i++) {
      const w = new Worker(new URL('./tileworker.ts', import.meta.url), { type: 'module' });
      w.onmessage = (e: MessageEvent) => this.onTile(e.data);
      this.workers.push(w);
    }

    this.cx = p.width / 2;
    this.cy = p.height / 2;
    this.zoom = this.minZoom();
    new ResizeObserver(() => this.resize()).observe(parent);
    this.resize();
    this.bindInput();
  }

  // ---- camera -------------------------------------------------------------------------------

  private get cssW() { return this.canvas.clientWidth || 1; }
  private get cssH() { return this.canvas.clientHeight || 1; }
  minZoom() { return Math.min(this.cssW / this.p.width, this.cssH / this.p.height); }

  setView(cx: number, cy: number, zoom: number) {
    this.zoom = Math.min(MAX_ZOOM, Math.max(this.minZoom(), zoom));
    this.cx = cx;
    this.cy = cy;
    this.targetZoom = null;
    this.vel = [0, 0];
    this.clamp();
    this.request();
  }

  /** CSS px in the canvas -> image px */
  toImage(sx: number, sy: number): [number, number] {
    return [this.cx + (sx - this.cssW / 2) / this.zoom, this.cy + (sy - this.cssH / 2) / this.zoom];
  }

  private clamp() {
    // keep the view centre on the map; when the map is smaller than the view, centre it
    const hw = this.cssW / 2 / this.zoom, hh = this.cssH / 2 / this.zoom;
    const fit = (c: number, half: number, size: number) =>
      size <= 2 * half ? size / 2 : Math.min(size - half, Math.max(half, c));
    this.cx = fit(this.cx, hw, this.p.width);
    this.cy = fit(this.cy, hh, this.p.height);
  }

  private zoomAt(factor: number, sx: number, sy: number) {
    const [ix, iy] = this.toImage(sx, sy);
    this.zoom = Math.min(MAX_ZOOM, Math.max(this.minZoom(), this.zoom * factor));
    // keep the image point under the cursor fixed
    this.cx = ix - (sx - this.cssW / 2) / this.zoom;
    this.cy = iy - (sy - this.cssH / 2) / this.zoom;
    this.clamp();
  }

  private bindInput() {
    const c = this.canvas;
    c.tabIndex = 0; // focusable, for the keyboard
    c.style.outline = 'none';
    // Active pointers (mouse, pen, fingers). One = drag; two = pinch zoom + pan around the midpoint.
    const pts = new Map<number, [number, number]>();
    let lastMove = 0;
    let pinch: { dist: number; mid: [number, number] } | null = null;
    const local = (x: number, y: number): [number, number] => {
      const r = c.getBoundingClientRect();
      return [x - r.left, y - r.top];
    };
    const twoFinger = () => {
      const [a, b] = [...pts.values()];
      return { dist: Math.hypot(a[0] - b[0], a[1] - b[1]) || 1, mid: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2] as [number, number] };
    };
    c.addEventListener('pointerdown', (e) => {
      c.focus({ preventScroll: true });
      pts.set(e.pointerId, local(e.clientX, e.clientY));
      this.dragging = true;
      this.vel = [0, 0];
      this.targetZoom = null;
      lastMove = performance.now();
      pinch = pts.size === 2 ? twoFinger() : null;
      try { c.setPointerCapture(e.pointerId); } catch { /* synthetic events */ }
    });
    c.addEventListener('pointermove', (e) => {
      const prev = pts.get(e.pointerId);
      if (!prev) return;
      const cur = local(e.clientX, e.clientY);
      pts.set(e.pointerId, cur);
      const now = performance.now();
      if (pts.size >= 2 && pinch) {
        const g = twoFinger();
        this.zoomAt(g.dist / pinch.dist, ...pinch.mid); // zoom around the old midpoint
        this.cx -= (g.mid[0] - pinch.mid[0]) / this.zoom; // then follow the midpoint
        this.cy -= (g.mid[1] - pinch.mid[1]) / this.zoom;
        pinch = g;
        this.vel = [0, 0];
      } else {
        const dx = (cur[0] - prev[0]) / this.zoom, dy = (cur[1] - prev[1]) / this.zoom;
        this.cx -= dx;
        this.cy -= dy;
        const dt = Math.max(1, now - lastMove);
        this.vel = [-dx / dt, -dy / dt];
      }
      lastMove = now;
      this.clamp();
      this.request();
    });
    const up = (e: PointerEvent) => {
      if (!pts.delete(e.pointerId)) return;
      if (pts.size === 1) { pinch = null; this.vel = [0, 0]; return; } // pinch ended: keep dragging, no fling
      if (pts.size > 0) return;
      this.dragging = false;
      if (performance.now() - lastMove > 80) this.vel = [0, 0]; // paused before release: no fling
      this.request();
    };
    c.addEventListener('pointerup', up);
    c.addEventListener('pointercancel', up);
    c.addEventListener('wheel', (e) => {
      e.preventDefault();
      const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
      // trackpad pinch arrives as ctrl+wheel with small deltas: make it as responsive
      const k = e.ctrlKey ? 0.01 : 0.002;
      this.zoomTo((this.targetZoom ?? this.zoom) * Math.exp(-dy * k), ...local(e.clientX, e.clientY));
      this.vel = [0, 0];
    }, { passive: false });
    c.addEventListener('dblclick', (e) => {
      this.zoomTo((this.targetZoom ?? this.zoom) * (e.shiftKey ? 0.5 : 2), ...local(e.clientX, e.clientY));
    });
    // Keyboard: arrows / WASD pan while held, + / - zoom around the centre.
    const PAN = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1],
                  a: [-1, 0], d: [1, 0], w: [0, -1], s: [0, 1] } as Record<string, [number, number]>;
    c.addEventListener('keydown', (e) => {
      if (e.key in PAN) {
        this.keys.add(e.key);
        e.preventDefault();
        this.request();
      } else if (e.key === '+' || e.key === '=' || e.key === '-' || e.key === '_') {
        const f = e.key === '+' || e.key === '=' ? 2 : 0.5;
        this.zoomTo((this.targetZoom ?? this.zoom) * f, this.cssW / 2, this.cssH / 2);
        e.preventDefault();
      }
    });
    c.addEventListener('keyup', (e) => { this.keys.delete(e.key); });
    c.addEventListener('blur', () => this.keys.clear());
    this.panKeys = PAN;
  }

  /** Animate the zoom to `z`, keeping canvas point (sx, sy) fixed. */
  zoomTo(z: number, sx: number, sy: number) {
    this.targetZoom = Math.min(MAX_ZOOM, Math.max(this.minZoom(), z));
    this.zoomAnchor = [sx, sy];
    this.request();
  }

  /** Animate the view centre to image point (ix, iy) (used by the mini-map). */
  panTo(ix: number, iy: number) {
    this.cx = ix;
    this.cy = iy;
    this.vel = [0, 0];
    this.clamp();
    this.request();
  }

  /** The visible rectangle in image px: x0, y0, x1, y1. */
  viewRect(): [number, number, number, number] {
    const hw = this.cssW / 2 / this.zoom, hh = this.cssH / 2 / this.zoom;
    return [this.cx - hw, this.cy - hh, this.cx + hw, this.cy + hh];
  }

  // ---- frame loop ---------------------------------------------------------------------------

  /** Schedule a frame (frames run only while something changes). */
  request() {
    this.dirty = true;
    if (!this.raf) this.raf = requestAnimationFrame((t) => this.tick(t));
  }

  private tick(t: number) {
    this.raf = 0;
    const dt = this.lastTime ? Math.min(50, t - this.lastTime) : 16;
    this.lastTime = t;
    let animating = false;

    if (this.targetZoom !== null) {
      const k = 1 - Math.pow(0.001, dt / 250); // ~ settle within a quarter second
      const z = Math.exp(Math.log(this.zoom) + (Math.log(this.targetZoom) - Math.log(this.zoom)) * k);
      this.zoomAt(z / this.zoom, ...this.zoomAnchor);
      if (Math.abs(Math.log(this.targetZoom / this.zoom)) < 0.002) {
        this.zoomAt(this.targetZoom / this.zoom, ...this.zoomAnchor);
        this.targetZoom = null;
      } else animating = true;
    }
    if (this.keys.size) {
      let kx = 0, ky = 0;
      for (const k of this.keys) { const v = this.panKeys[k]; if (v) { kx += v[0]; ky += v[1]; } }
      const speed = 900 / this.zoom / 1000; // 900 CSS px per second
      this.cx += kx * speed * dt;
      this.cy += ky * speed * dt;
      this.clamp();
      animating = true;
    }
    if (!this.dragging && (this.vel[0] || this.vel[1])) {
      this.cx += this.vel[0] * dt;
      this.cy += this.vel[1] * dt;
      const decay = Math.pow(0.004, dt / 1000);
      this.vel = [this.vel[0] * decay, this.vel[1] * decay];
      if (Math.hypot(...this.vel) * this.zoom < 0.02) this.vel = [0, 0];
      else animating = true;
      this.clamp();
    }

    if (this.dirty || animating) {
      this.dirty = false;
      this.draw();
    }
    if (animating) this.request();
    else {
      clearTimeout(this.settleTimer);
      this.settleTimer = window.setTimeout(() => this.onChange?.(), 150);
    }
  }

  private resize() {
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.round(this.cssW * dpr);
    this.canvas.height = Math.round(this.cssH * dpr);
    this.zoom = Math.max(this.zoom, this.minZoom());
    this.clamp();
    this.request();
  }

  // ---- tiles --------------------------------------------------------------------------------

  private levelSize(level: number): [number, number] {
    const s = 2 ** (this.maxLevel - level);
    return [Math.ceil(this.p.width / s), Math.ceil(this.p.height / s)];
  }

  /** Level whose pixels are closest to (and not coarser than) device pixels. */
  private targetLevel(): number {
    const devicePxPerImagePx = this.zoom * (window.devicePixelRatio || 1);
    const l = this.maxLevel + Math.ceil(Math.log2(devicePxPerImagePx) - 1e-6);
    return Math.max(0, Math.min(this.maxLevel, l));
  }

  /** Tile index range at `level` covering the view (+ margin tiles). */
  private range(level: number, margin: number) {
    const s = 2 ** (this.maxLevel - level); // image px per level px
    const ts = this.p.tileSize * s; // image px per tile
    const hw = this.cssW / 2 / this.zoom, hh = this.cssH / 2 / this.zoom;
    const [lw, lh] = this.levelSize(level);
    const nx = Math.ceil(lw / this.p.tileSize), ny = Math.ceil(lh / this.p.tileSize);
    return {
      x0: Math.max(0, Math.floor((this.cx - hw) / ts) - margin),
      x1: Math.min(nx - 1, Math.floor((this.cx + hw) / ts) + margin),
      y0: Math.max(0, Math.floor((this.cy - hh) / ts) - margin),
      y1: Math.min(ny - 1, Math.floor((this.cy + hh) / ts) + margin),
      ts,
    };
  }

  private tile(level: number, x: number, y: number): Tile {
    const key = `${level}/${x}_${y}`;
    let t = this.tiles.get(key);
    if (!t) {
      t = { key, level, x, y, tex: null, state: 'queued', used: this.frame, id: this.nextId++, w: 0, h: 0 };
      this.tiles.set(key, t);
      this.byId.set(t.id, t);
    }
    return t;
  }

  private pump(wanted: Tile[]) {
    // nearest to the view centre first
    const queue = wanted.filter((t) => t.state === 'queued');
    if (!queue.length || this.inflight >= MAX_INFLIGHT) return;
    const d = (t: Tile) => {
      const ts = this.p.tileSize * 2 ** (this.maxLevel - t.level);
      return Math.hypot((t.x + 0.5) * ts - this.cx, (t.y + 0.5) * ts - this.cy) / ts;
    };
    queue.sort((a, b) => d(a) - d(b));
    for (const t of queue) {
      if (this.inflight >= MAX_INFLIGHT) break;
      t.state = 'loading';
      this.inflight++;
      this.workers[t.id % WORKERS].postMessage({
        id: t.id,
        url: new URL(`${this.p.url}/${t.level}/${t.x}_${t.y}.${this.p.format}`, location.href).href,
      });
    }
  }

  private onTile(msg: { id: number; bitmap?: ImageBitmap; error?: string }) {
    this.inflight--;
    const t = this.byId.get(msg.id);
    if (!t) { msg.bitmap?.close(); return; }
    if (!msg.bitmap) { t.state = 'error'; console.warn(`tile ${t.key}: ${msg.error}`); return; }
    const gl = this.gl;
    const tex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, msg.bitmap);
    t.w = msg.bitmap.width;
    t.h = msg.bitmap.height;
    msg.bitmap.close();
    t.tex = tex;
    t.state = 'ready';
    this.evict();
    this.request();
  }

  private evict() {
    const ready = [...this.tiles.values()].filter((t) => t.state === 'ready');
    if (ready.length <= CACHE_TILES) return;
    ready.sort((a, b) => a.used - b.used);
    for (const t of ready.slice(0, ready.length - CACHE_TILES)) {
      if (t.used === this.frame) break; // never evict what is on screen
      this.gl.deleteTexture(t.tex);
      this.tiles.delete(t.key);
      this.byId.delete(t.id);
    }
  }

  private draw() {
    this.frame++;
    const gl = this.gl;
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.clear(gl.COLOR_BUFFER_BIT);

    const L = this.targetLevel();
    // wanted: visible tiles at L, then one ring around them (prefetch)
    const ring = this.range(L, 1);
    const wanted: Tile[] = [];
    for (let y = ring.y0; y <= ring.y1; y++) {
      for (let x = ring.x0; x <= ring.x1; x++) {
        const t = this.tile(L, x, y);
        t.used = this.frame;
        if (t.state === 'error') continue;
        wanted.push(t);
      }
    }
    // coarse fallback levels: request their visible tiles too (cheap, and cover holes)
    for (let l = Math.max(0, L - FALLBACK_LEVELS); l < L; l++) {
      const r = this.range(l, 0);
      for (let y = r.y0; y <= r.y1; y++) for (let x = r.x0; x <= r.x1; x++) {
        const t = this.tile(l, x, y);
        t.used = this.frame;
        if (l === Math.max(0, L - FALLBACK_LEVELS) && t.state !== 'error') wanted.push(t);
      }
    }

    // draw coarse to fine: finer tiles cover coarser ones
    const sx = 2 / this.cssW * this.zoom, sy = -2 / this.cssH * this.zoom; // image px -> clip
    for (let l = Math.max(0, L - FALLBACK_LEVELS); l <= L; l++) {
      const r = this.range(l, 0);
      const scale = 2 ** (this.maxLevel - l);
      for (let y = r.y0; y <= r.y1; y++) {
        for (let x = r.x0; x <= r.x1; x++) {
          const t = this.tiles.get(`${l}/${x}_${y}`);
          if (!t || t.state !== 'ready') continue;
          // tile rect in image px (edge tiles are smaller than tileSize)
          const ix0 = x * r.ts, iy0 = y * r.ts;
          const ix1 = ix0 + t.w * scale, iy1 = iy0 + t.h * scale;
          gl.bindTexture(gl.TEXTURE_2D, t.tex);
          gl.uniform4f(this.uRect, (ix0 - this.cx) * sx, (iy0 - this.cy) * sy, (ix1 - this.cx) * sx, (iy1 - this.cy) * sy);
          gl.uniform4f(this.uUv, 0, 0, 1, 1);
          gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
        }
      }
    }
    this.pump(wanted);
    this.onView?.();
  }
}

// Game-layer test: cars driving the OSM road graph (pipeline/roads.py -> roads.json).
//
// - Simulation in typed arrays: each car is on a directed edge, a segment of it and a distance
//   along that segment; at the end of an edge it turns onto a random next edge (no U-turns
//   unless it is a dead end). Cars do not see each other yet.
// - Drawing: one instanced draw call for shadows (blended) and one for cars (depth-tested by
//   screen y, so nearer cars cover farther ones); sprites from sprites.ts in one atlas texture.
//   Only cars inside the view are uploaded. Positions snap to whole map pixels (pixel art).
// - Known gap: occlusion. Cars are drawn on top of everything, including buildings in front.

import type { LayerView, MapLayer } from './glmap';
import { HEADINGS, makeCarAtlas, type Atlas, type Camera } from './sprites';

export interface MapInfo {
  width: number;
  height: number;
  origin_screen_px: [number, number];
  metres_per_pixel: number;
  camera: Camera;
}

interface Roads {
  attribution: string;
  heights: string;
  points: number[]; // e, n, h
  edges: { p: number[]; oneway: 0 | 1; lanes: number; kmh: number; hw: string }[];
}

const LANE_M = 3.0;

/** Directed road network flattened into typed arrays. */
class Network {
  // directed edges
  segStart: Int32Array; segEnd: Int32Array; // segment range [start, end)
  fromNode: Int32Array; toNode: Int32Array;
  twin: Int32Array; // the opposite direction of the same road, or -1
  speed: Float32Array; // m/s
  lanes: Uint8Array; oneway: Uint8Array;
  length: Float32Array;
  // segments (image px; n = image px per metre to the right of travel)
  sx!: Float32Array; sy!: Float32Array; dx!: Float32Array; dy!: Float32Array;
  nx!: Float32Array; ny!: Float32Array; len!: Float32Array; head: Uint8Array;
  // nodes -> outgoing directed edges (CSR)
  outStart: Int32Array; out: Int32Array;
  cumLength: Float64Array; // for length-weighted spawning

  constructor(r: Roads, m: MapInfo) {
    const P = r.points;
    const [R, U] = [m.camera.right_enu, m.camera.up_enu];
    const mpp = m.metres_per_pixel;
    const [ox, oy] = m.origin_screen_px;
    const img = (e: number, n: number, h: number): [number, number] =>
      [(e * R[0] + n * R[1] + h * R[2]) / mpp - ox, -(e * U[0] + n * U[1] + h * U[2]) / mpp - oy];
    const vec = (e: number, n: number): [number, number] => // ground vector -> image px
      [(e * R[0] + n * R[1]) / mpp, -(e * U[0] + n * U[1]) / mpp];

    const dirs: { pts: number[]; e: (typeof r.edges)[number]; twin: number }[] = [];
    for (const e of r.edges) {
      const f = dirs.length;
      dirs.push({ pts: e.p, e, twin: -1 });
      if (!e.oneway) {
        dirs.push({ pts: [...e.p].reverse(), e, twin: f });
        dirs[f].twin = f + 1;
      }
    }
    const nE = dirs.length;
    const nS = dirs.reduce((s, d) => s + d.pts.length - 1, 0);
    this.segStart = new Int32Array(nE); this.segEnd = new Int32Array(nE);
    this.fromNode = new Int32Array(nE); this.toNode = new Int32Array(nE); this.twin = new Int32Array(nE);
    this.speed = new Float32Array(nE); this.lanes = new Uint8Array(nE); this.oneway = new Uint8Array(nE);
    this.length = new Float32Array(nE);
    for (const k of ['sx', 'sy', 'dx', 'dy', 'nx', 'ny', 'len'] as const) this[k] = new Float32Array(nS);
    this.head = new Uint8Array(nS);
    let s = 0;
    dirs.forEach((d, i) => {
      this.segStart[i] = s;
      let total = 0;
      for (let j = 0; j + 1 < d.pts.length; j++, s++) {
        const a = d.pts[j] * 3, b = d.pts[j + 1] * 3;
        const de = P[b] - P[a], dn = P[b + 1] - P[a + 1], dh = P[b + 2] - P[a + 2];
        const L = Math.hypot(de, dn, dh) || 1e-3;
        const [x0, y0] = img(P[a], P[a + 1], P[a + 2]);
        const [x1, y1] = img(P[b], P[b + 1], P[b + 2]);
        const g = Math.hypot(de, dn) || 1e-3;
        const [nx, ny] = vec(dn / g, -de / g); // unit ground vector to the right of travel
        this.sx[s] = x0; this.sy[s] = y0; this.dx[s] = x1 - x0; this.dy[s] = y1 - y0;
        this.nx[s] = nx; this.ny[s] = ny; this.len[s] = L;
        const ang = Math.atan2(dn, de);
        this.head[s] = ((Math.round(ang / ((2 * Math.PI) / HEADINGS)) % HEADINGS) + HEADINGS) % HEADINGS;
        total += L;
      }
      this.segEnd[i] = s;
      this.fromNode[i] = d.pts[0];
      this.toNode[i] = d.pts[d.pts.length - 1];
      this.twin[i] = d.twin;
      this.speed[i] = d.e.kmh / 3.6;
      this.lanes[i] = d.e.oneway ? d.e.lanes : Math.max(1, d.e.lanes >> 1);
      this.oneway[i] = d.e.oneway;
      this.length[i] = total;
    });
    const nN = P.length / 3;
    const count = new Int32Array(nN + 1);
    for (let i = 0; i < nE; i++) count[this.fromNode[i] + 1]++;
    for (let i = 0; i < nN; i++) count[i + 1] += count[i];
    this.outStart = count;
    this.out = new Int32Array(nE);
    const fill = count.slice(0, nN);
    for (let i = 0; i < nE; i++) this.out[fill[this.fromNode[i]]++] = i;
    this.cumLength = new Float64Array(nE);
    let acc = 0;
    for (let i = 0; i < nE; i++) this.cumLength[i] = acc += this.length[i];
  }

  get edgeCount() { return this.segStart.length; }

  randomEdge(rand: () => number): number {
    const t = rand() * this.cumLength[this.cumLength.length - 1];
    let lo = 0, hi = this.cumLength.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (this.cumLength[mid] < t) lo = mid + 1; else hi = mid; }
    return lo;
  }

  /** Next directed edge after `e`: random among the outgoing ones, U-turn only at dead ends. */
  next(e: number, rand: () => number): number {
    const v = this.toNode[e];
    const a = this.outStart[v], b = this.outStart[v + 1];
    const tw = this.twin[e];
    const n = b - a - (tw >= 0 && this.fromNode[tw] === v ? 1 : 0);
    if (n <= 0) return tw >= 0 ? tw : -1;
    let k = Math.floor(rand() * n);
    for (let i = a; i < b; i++) {
      if (this.out[i] === tw) continue;
      if (k-- === 0) return this.out[i];
    }
    return -1;
  }
}

export class CarLayer implements MapLayer {
  readonly count: number;
  private net: Network;
  private atlas: Atlas;
  private edge: Int32Array; private seg: Int32Array; private s: Float32Array;
  private v: Float32Array; private lane: Uint8Array; private paint: Uint8Array;
  private inst: Float32Array; // per visible car: x, y, atlas cell
  private seed = 1;
  private gl: WebGL2RenderingContext | null = null;
  private prog: WebGLProgram | null = null;
  private vao: WebGLVertexArrayObject | null = null;
  private instBuf: WebGLBuffer | null = null;
  private aInst = 0;
  private tex: WebGLTexture | null = null;
  private u: Record<string, WebGLUniformLocation | null> = {};
  paused = false;
  /** main-thread ms spent in update() + draw() (for the benchmark) */
  cpuMs = 0;

  constructor(roads: Roads, private m: MapInfo, count: number) {
    this.net = new Network(roads, m);
    this.atlas = makeCarAtlas(m.camera, m.metres_per_pixel);
    this.count = count;
    this.edge = new Int32Array(count); this.seg = new Int32Array(count); this.s = new Float32Array(count);
    this.v = new Float32Array(count); this.lane = new Uint8Array(count); this.paint = new Uint8Array(count);
    this.inst = new Float32Array(count * 2 * 3);
    for (let i = 0; i < count; i++) this.spawn(i, true);
  }

  static async load(url: string, m: MapInfo, count: number): Promise<CarLayer | null> {
    const res = await fetch(url);
    if (!res.ok) return null;
    const roads: Roads = await res.json();
    if (roads.heights === 'flat') console.warn('roads.json has no ground heights yet: cars on hills are misplaced');
    return new CarLayer(roads, m, count);
  }

  private rand = () => {
    // xorshift32: deterministic, so benchmark runs are comparable
    let x = this.seed;
    x ^= x << 13; x ^= x >>> 17; x ^= x << 5;
    this.seed = x >>> 0;
    return this.seed / 4294967296;
  };

  private spawn(i: number, anywhere: boolean) {
    const n = this.net;
    const e = n.randomEdge(this.rand);
    this.edge[i] = e;
    this.seg[i] = n.segStart[e];
    this.s[i] = 0;
    if (anywhere) { // random point along the edge
      let d = this.rand() * n.length[e];
      while (this.seg[i] < n.segEnd[e] - 1 && d > n.len[this.seg[i]]) d -= n.len[this.seg[i]++];
      this.s[i] = Math.min(d, n.len[this.seg[i]]);
    }
    this.v[i] = n.speed[e] * (0.8 + 0.35 * this.rand());
    this.lane[i] = Math.floor(this.rand() * n.lanes[e]);
    this.paint[i] = Math.floor(this.rand() * this.atlas.colours);
  }

  setPaused(p: boolean) { this.paused = p; }

  update(dtMs: number): boolean {
    if (this.paused) return false;
    const t0 = performance.now();
    const n = this.net, dt = dtMs / 1000;
    for (let i = 0; i < this.count; i++) {
      let s = this.s[i] + this.v[i] * dt;
      let g = this.seg[i];
      while (s >= n.len[g]) {
        s -= n.len[g];
        g++;
        if (g >= n.segEnd[this.edge[i]]) {
          const e = n.next(this.edge[i], this.rand);
          if (e < 0) { this.spawn(i, false); g = this.seg[i]; s = 0; break; }
          this.edge[i] = e;
          g = n.segStart[e];
          if (this.lane[i] >= n.lanes[e]) this.lane[i] = n.lanes[e] - 1;
          this.v[i] = n.speed[e] * (0.8 + 0.35 * this.rand());
        }
      }
      this.seg[i] = g;
      this.s[i] = s;
    }
    this.cpuMs += performance.now() - t0;
    return true;
  }

  private init(gl: WebGL2RenderingContext) {
    this.gl = gl;
    const VS = `#version 300 es
    in vec2 a_corner;
    in vec3 a_inst;          // anchor x, y (image px), atlas cell
    uniform vec4 u_view;     // cx, cy, image px -> clip x, y
    uniform vec4 u_cell;     // cell w, h, anchor x, y (px)
    uniform vec2 u_grid;     // atlas cols, rows
    uniform float u_imgH;
    out vec2 v_uv;
    void main() {
      vec2 p = a_inst.xy - u_cell.zw + a_corner * u_cell.xy;
      float col = mod(a_inst.z, u_grid.x), row = floor(a_inst.z / u_grid.x);
      v_uv = (vec2(col, row) + a_corner) / u_grid;
      float z = clamp(1.0 - 2.0 * a_inst.y / u_imgH, -1.0, 1.0); // nearer (lower on screen) wins
      gl_Position = vec4((p - u_view.xy) * u_view.zw, z, 1.0);
    }`;
    const FS = `#version 300 es
    precision mediump float;
    in vec2 v_uv;
    uniform sampler2D u_tex;
    uniform int u_cut;
    out vec4 o;
    void main() {
      vec4 c = texture(u_tex, v_uv);
      if (u_cut == 1 ? c.a < 0.5 : c.a == 0.0) discard;
      o = vec4(c.rgb * c.a, c.a);
    }`;
    const prog = gl.createProgram()!;
    for (const [type, src] of [[gl.VERTEX_SHADER, VS], [gl.FRAGMENT_SHADER, FS]] as const) {
      const sh = gl.createShader(type)!;
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(sh) ?? 'shader');
      gl.attachShader(prog, sh);
    }
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog) ?? 'link');
    this.prog = prog;
    for (const k of ['u_view', 'u_cell', 'u_grid', 'u_imgH', 'u_tex', 'u_cut']) this.u[k] = gl.getUniformLocation(prog, k);

    this.vao = gl.createVertexArray();
    gl.bindVertexArray(this.vao);
    const quad = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, quad);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, 0, 1, 0, 0, 1, 1, 1]), gl.STATIC_DRAW);
    const aCorner = gl.getAttribLocation(prog, 'a_corner');
    gl.enableVertexAttribArray(aCorner);
    gl.vertexAttribPointer(aCorner, 2, gl.FLOAT, false, 0, 0);
    this.instBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.instBuf);
    gl.bufferData(gl.ARRAY_BUFFER, this.inst.byteLength, gl.DYNAMIC_DRAW);
    this.aInst = gl.getAttribLocation(prog, 'a_inst');
    gl.enableVertexAttribArray(this.aInst);
    gl.vertexAttribPointer(this.aInst, 3, gl.FLOAT, false, 0, 0);
    gl.vertexAttribDivisor(this.aInst, 1);
    gl.bindVertexArray(null);

    const a = this.atlas;
    this.tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, a.width, a.height, 0, gl.RGBA, gl.UNSIGNED_BYTE, a.data);
  }

  draw(gl: WebGL2RenderingContext, view: LayerView) {
    if (!this.prog) this.init(gl);
    const t0 = performance.now();
    const n = this.net, a = this.atlas;
    // visible cars -> instance data: shadows in the first half, cars in the second
    const [vx0, vy0, vx1, vy1] = view.rect;
    const mx = a.cellW, my = a.cellH;
    let k = 0;
    const cars = this.inst.subarray(this.count * 3);
    for (let i = 0; i < this.count; i++) {
      const g = this.seg[i];
      const t = this.s[i] / n.len[g];
      const e = this.edge[i];
      const off = n.oneway[e] ? (this.lane[i] - (n.lanes[e] - 1) / 2) * LANE_M : (this.lane[i] + 0.5) * LANE_M;
      const x = Math.round(n.sx[g] + n.dx[g] * t + n.nx[g] * off);
      const y = Math.round(n.sy[g] + n.dy[g] * t + n.ny[g] * off);
      if (x < vx0 - mx || x > vx1 + mx || y < vy0 - my || y > vy1 + my) continue;
      const h = n.head[g];
      this.inst[k * 3] = x; this.inst[k * 3 + 1] = y; this.inst[k * 3 + 2] = a.shadowRow * a.headings + h;
      cars[k * 3] = x; cars[k * 3 + 1] = y; cars[k * 3 + 2] = this.paint[i] * a.headings + h;
      k++;
    }
    if (k > 0) {
      gl.useProgram(this.prog);
      gl.bindVertexArray(this.vao);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, this.tex);
      gl.uniform1i(this.u.u_tex, 0);
      gl.uniform4f(this.u.u_view, view.cx, view.cy, (2 / view.cssW) * view.zoom, (-2 / view.cssH) * view.zoom);
      gl.uniform4f(this.u.u_cell, a.cellW, a.cellH, a.anchorX, a.anchorY);
      gl.uniform2f(this.u.u_grid, a.headings, a.colours + 1);
      gl.uniform1f(this.u.u_imgH, this.m.height);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.instBuf);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, this.inst, 0, k * 3);
      gl.bufferSubData(gl.ARRAY_BUFFER, k * 3 * 4, cars, 0, k * 3);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.uniform1i(this.u.u_cut, 0);
      gl.vertexAttribPointer(this.aInst, 3, gl.FLOAT, false, 0, 0);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, k); // shadows
      gl.disable(gl.BLEND);
      gl.enable(gl.DEPTH_TEST);
      gl.depthFunc(gl.LESS);
      gl.clear(gl.DEPTH_BUFFER_BIT);
      gl.uniform1i(this.u.u_cut, 1);
      gl.vertexAttribPointer(this.aInst, 3, gl.FLOAT, false, 0, k * 3 * 4);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, k); // cars
      gl.disable(gl.DEPTH_TEST);
      gl.bindVertexArray(null);
    }
    this.visible = k;
    this.cpuMs += performance.now() - t0;
  }

  /** cars drawn in the last frame */
  visible = 0;
}

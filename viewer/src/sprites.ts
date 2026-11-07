// Pixel-art car sprites, generated at load time from 3D boxes (body + cabin) with the map's own
// camera, so they have exactly the map's projection and scale (manifest.json `camera`, mpp).
// Rasterised per pixel centre (no anti-aliasing), flat-shaded faces, 1 px dark outline; one
// atlas: a column per heading, a row per paint colour, and a last row with ground shadows.

export type V3 = [number, number, number];

export interface Camera {
  right_enu: V3;
  up_enu: V3;
  view_dir_enu: V3;
}

export interface Atlas {
  data: Uint8Array; // RGBA, rows top to bottom
  width: number;
  height: number;
  cellW: number;
  cellH: number;
  anchorX: number; // pixel in the cell where the car's ground centre is
  anchorY: number;
  headings: number;
  colours: number;
  shadowRow: number;
}

export const HEADINGS = 16; // heading k = k * 22.5° counter-clockwise from east (ENU)

// metres; a mid-size hatchback
const CAR = { len: 4.3, wid: 1.8, clear: 0.25, bodyH: 0.75, cabLen: 2.3, cabWid: 1.6, cabH: 0.6, cabBack: 0.3 };
const PAINTS: [number, number, number][] = [
  [196, 40, 36], [232, 232, 226], [38, 40, 46], [44, 84, 170],
  [160, 166, 172], [236, 186, 30], [40, 110, 70], [120, 40, 110],
];
const GLASS: [number, number, number] = [52, 66, 84];
const OUTLINE: [number, number, number] = [24, 24, 30];
const SUN = norm([0.35, -0.55, 0.75]); // from the south-east, high (lit south faces, like the map)
const SHADOW_ALPHA = 0.32;

function norm(v: V3): V3 {
  const l = Math.hypot(...v);
  return [v[0] / l, v[1] / l, v[2] / l];
}
const dot = (a: V3, b: V3) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

interface Face { pts: [number, number][]; rgb: [number, number, number] }

/** The 8 corners of a box in ENU: centre (along, across) on the car axis, base height z0. */
function boxCorners(head: number, along: number, len: number, wid: number, z0: number, h: number): V3[] {
  const c = Math.cos(head), s = Math.sin(head);
  const out: V3[] = [];
  for (const dz of [0, h]) for (const da of [-len / 2, len / 2]) for (const dw of [-wid / 2, wid / 2]) {
    const a = along + da;
    out.push([a * c - dw * s, a * s + dw * c, z0 + dz]);
  }
  return out;
}

// corner indices (i = dz*4 + da*2 + dw) of the six faces, with outward normals in car axes
const FACES: { idx: number[]; n: (c: number, s: number) => V3; part: 'top' | 'side' | 'end' }[] = [
  { idx: [4, 5, 7, 6], n: () => [0, 0, 1], part: 'top' },
  { idx: [0, 1, 3, 2], n: () => [0, 0, -1], part: 'top' },
  { idx: [2, 3, 7, 6], n: (c, s) => [c, s, 0], part: 'end' }, // front
  { idx: [0, 1, 5, 4], n: (c, s) => [-c, -s, 0], part: 'end' }, // back
  { idx: [1, 3, 7, 5], n: (c, s) => [-s, c, 0], part: 'side' }, // left
  { idx: [0, 2, 6, 4], n: (c, s) => [s, -c, 0], part: 'side' }, // right
];

function inConvex(pts: [number, number][], x: number, y: number): boolean {
  let sign = 0;
  for (let i = 0; i < pts.length; i++) {
    const [x0, y0] = pts[i], [x1, y1] = pts[(i + 1) % pts.length];
    const cr = (x1 - x0) * (y - y0) - (y1 - y0) * (x - x0);
    if (Math.abs(cr) < 1e-9) continue;
    if (sign === 0) sign = Math.sign(cr);
    else if (Math.sign(cr) !== sign) return false;
  }
  return true;
}

function hull(p: [number, number][]): [number, number][] {
  const s = [...p].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const cross = (o: number[], a: number[], b: number[]) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lo: [number, number][] = [], hi: [number, number][] = [];
  for (const q of s) { while (lo.length >= 2 && cross(lo[lo.length - 2], lo[lo.length - 1], q) <= 0) lo.pop(); lo.push(q); }
  for (const q of s.reverse()) { while (hi.length >= 2 && cross(hi[hi.length - 2], hi[hi.length - 1], q) <= 0) hi.pop(); hi.push(q); }
  return lo.slice(0, -1).concat(hi.slice(0, -1));
}

export function makeCarAtlas(cam: Camera, mpp: number): Atlas {
  const toPx = (p: V3): [number, number] => [dot(p, cam.right_enu) / mpp, -dot(p, cam.up_enu) / mpp];
  const shade = (n: V3) => 0.6 + 0.4 * Math.max(0, dot(n, SUN));

  const carFaces = (head: number, paint: [number, number, number]): Face[] => {
    const c = Math.cos(head), s = Math.sin(head);
    const faces: Face[] = [];
    const parts = [
      { k: boxCorners(head, 0, CAR.len, CAR.wid, CAR.clear, CAR.bodyH), glass: false },
      { k: boxCorners(head, -CAR.cabBack, CAR.cabLen, CAR.cabWid, CAR.clear + CAR.bodyH, CAR.cabH), glass: true },
    ];
    for (const part of parts) { // cabin after body: it sits on top, so it is always in front
      for (const f of FACES) {
        const n = f.n(c, s);
        if (dot(n, cam.view_dir_enu) >= 0) continue; // faces away from the camera
        const base = part.glass && f.part !== 'top' ? GLASS : paint;
        const k = shade(n);
        faces.push({ pts: f.idx.map((i) => toPx(part.k[i])), rgb: [base[0] * k, base[1] * k, base[2] * k] });
      }
    }
    return faces;
  };
  const shadowPoly = (head: number): [number, number][] => {
    const k = [...boxCorners(head, 0, CAR.len, CAR.wid, CAR.clear, CAR.bodyH),
               ...boxCorners(head, -CAR.cabBack, CAR.cabLen, CAR.cabWid, CAR.clear + CAR.bodyH, CAR.cabH)];
    return hull(k.map((p) => toPx([p[0] - (SUN[0] * p[2]) / SUN[2], p[1] - (SUN[1] * p[2]) / SUN[2], 0])));
  };

  // cell size: bounds over every heading (+1 px outline on each side), anchor at the ground centre
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (let h = 0; h < HEADINGS; h++) {
    const head = (h * 2 * Math.PI) / HEADINGS;
    for (const f of carFaces(head, PAINTS[0])) for (const [x, y] of f.pts) {
      x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y);
    }
    for (const [x, y] of shadowPoly(head)) {
      x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y);
    }
  }
  const anchorX = Math.ceil(-x0) + 1, anchorY = Math.ceil(-y0) + 1;
  const cellW = anchorX + Math.ceil(x1) + 2, cellH = anchorY + Math.ceil(y1) + 2;
  const cols = HEADINGS, rows = PAINTS.length + 1;
  const W = cols * cellW, H = rows * cellH;
  const data = new Uint8Array(W * H * 4);
  const put = (x: number, y: number, rgb: number[], a = 255) => {
    const o = (y * W + x) * 4;
    data[o] = rgb[0]; data[o + 1] = rgb[1]; data[o + 2] = rgb[2]; data[o + 3] = a;
  };

  for (let h = 0; h < HEADINGS; h++) {
    const head = (h * 2 * Math.PI) / HEADINGS;
    for (let r = 0; r < PAINTS.length; r++) {
      const faces = carFaces(head, PAINTS[r]);
      const cx = h * cellW, cy = r * cellH;
      const mask = new Uint8Array(cellW * cellH);
      for (let y = 0; y < cellH; y++) for (let x = 0; x < cellW; x++) {
        const px = x + 0.5 - anchorX, py = y + 0.5 - anchorY;
        let rgb: number[] | null = null;
        for (const f of faces) if (inConvex(f.pts, px, py)) rgb = f.rgb;
        if (rgb) { put(cx + x, cy + y, rgb.map(Math.round)); mask[y * cellW + x] = 1; }
      }
      // outline: empty pixels next to the car (4-neighbourhood)
      for (let y = 0; y < cellH; y++) for (let x = 0; x < cellW; x++) {
        if (mask[y * cellW + x]) continue;
        const near = (x > 0 && mask[y * cellW + x - 1]) || (x < cellW - 1 && mask[y * cellW + x + 1]) ||
                     (y > 0 && mask[(y - 1) * cellW + x]) || (y < cellH - 1 && mask[(y + 1) * cellW + x]);
        if (near) put(cx + x, cy + y, OUTLINE);
      }
    }
    const sh = shadowPoly(head);
    const cy = PAINTS.length * cellH;
    for (let y = 0; y < cellH; y++) for (let x = 0; x < cellW; x++) {
      if (inConvex(sh, x + 0.5 - anchorX, y + 0.5 - anchorY)) put(h * cellW + x, cy + y, [0, 0, 0], Math.round(255 * SHADOW_ALPHA));
    }
  }
  return { data, width: W, height: H, cellW, cellH, anchorX, anchorY, headings: HEADINGS, colours: PAINTS.length, shadowRow: PAINTS.length };
}

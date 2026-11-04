// Scene from data/osm/<area>.scene.json (pipeline/osm_scene.py): flat coloured ground layers and
// extruded buildings, all in the plan's ENU frame (x east, y north, z up, metres).
// Everything is merged into a few meshes with vertex colours, so draw calls stay low.

import {
  AmbientLight,
  BufferGeometry,
  Color,
  DirectionalLight,
  ExtrudeGeometry,
  Float32BufferAttribute,
  Group,
  Mesh,
  MeshLambertMaterial,
  Path,
  PlaneGeometry,
  Shape,
  ShapeGeometry,
} from 'three';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';

type Ring = [number, number][];
type Poly = Ring[]; // [outer, ...holes]

export interface OsmScene {
  ground: string;
  layers: { kind: string; z: number; height?: number; color: string; polygons: Poly[] }[];
  buildings: { id: string; height: number; min_height: number; wall: string; roof: string; polygons: Poly[] }[];
  attribution: string;
  osm_timestamp?: string;
}

// Sun: fixed in world space, so lighting is identical in every tile. From the south-south-west,
// 45° up: the camera looks north, so south facades are lit and east/west ones are shaded.
const SUN_AZIMUTH_DEG = 200;
const SUN_ELEVATION_DEG = 45;

function shape(poly: Poly): Shape {
  const s = new Shape(poly[0].map(([x, y]) => ({ x, y }) as never));
  for (const hole of poly.slice(1)) s.holes.push(new Path(hole.map(([x, y]) => ({ x, y }) as never)));
  return s;
}

function paint(g: BufferGeometry, colour: string, sideColour?: string) {
  const n = g.getAttribute('position').count;
  const c = new Color(colour);
  const side = sideColour ? new Color(sideColour) : c;
  const arr = new Float32Array(n * 3);
  // ExtrudeGeometry: group 0 = caps (roof/floor), group 1 = sides (walls).
  const groups = g.groups.length ? g.groups : [{ start: 0, count: n, materialIndex: 0 }];
  for (const grp of groups) {
    const col = grp.materialIndex === 1 ? side : c;
    for (let i = grp.start; i < grp.start + grp.count; i++) col.toArray(arr, i * 3);
  }
  g.setAttribute('color', new Float32BufferAttribute(arr, 3));
  g.clearGroups();
  return g;
}

/** Keep only the attributes every part has, so mergeGeometries accepts the mix. */
function normalise(g: BufferGeometry): BufferGeometry {
  const ng = g.index ? g.toNonIndexed() : g;
  for (const name of Object.keys(ng.attributes)) {
    if (!['position', 'normal', 'color'].includes(name)) ng.deleteAttribute(name);
  }
  return ng;
}

export function buildOsmScene(scene: OsmScene): Group {
  const group = new Group();
  const material = new MeshLambertMaterial({ vertexColors: true });

  // Ground plane under everything: unmapped land.
  const xs = scene.layers.flatMap((l) => l.polygons.flatMap((p) => p[0].map((q) => q[0])));
  const ys = scene.layers.flatMap((l) => l.polygons.flatMap((p) => p[0].map((q) => q[1])));
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const plane = new PlaneGeometry(x1 - x0, y1 - y0).translate((x0 + x1) / 2, (y0 + y1) / 2, -0.02);
  const parts: BufferGeometry[] = [normalise(paint(plane, scene.ground))];

  for (const layer of scene.layers) {
    for (const poly of layer.polygons) {
      const g = layer.height
        ? new ExtrudeGeometry(shape(poly), { depth: layer.height, bevelEnabled: false })
        : new ShapeGeometry(shape(poly));
      g.translate(0, 0, layer.z);
      parts.push(normalise(paint(g, layer.color)));
    }
  }
  group.add(new Mesh(mergeGeometries(parts), material));

  const solids: BufferGeometry[] = [];
  for (const b of scene.buildings) {
    for (const poly of b.polygons) {
      const g = new ExtrudeGeometry(shape(poly), { depth: b.height - b.min_height, bevelEnabled: false });
      g.translate(0, 0, b.min_height);
      solids.push(normalise(paint(g, b.roof, b.wall)));
    }
  }
  if (solids.length) group.add(new Mesh(mergeGeometries(solids), material));

  const az = (SUN_AZIMUTH_DEG * Math.PI) / 180;
  const el = (SUN_ELEVATION_DEG * Math.PI) / 180;
  const sun = new DirectionalLight(0xffffff, 2.0);
  sun.position.set(Math.sin(az) * Math.cos(el), Math.cos(az) * Math.cos(el), Math.sin(el));
  group.add(sun, new AmbientLight(0xffffff, 1.3));
  return group;
}

// Isometric scene: Google Photorealistic 3D Tiles (or a synthetic test scene) in the plan's
// ENU frame, viewed by the fixed orthographic camera. Tile geometry comes only from the plan
// JSON written by pipeline/plan_tiles.py; nothing here computes tile positions.

import {
  AmbientLight,
  BoxGeometry,
  CanvasTexture,
  Color,
  DirectionalLight,
  Group,
  Matrix4,
  Mesh,
  MeshLambertMaterial,
  NearestFilter,
  OrthographicCamera,
  PlaneGeometry,
  Raycaster,
  RepeatWrapping,
  Scene,
  Vector3,
  WebGLRenderer,
} from 'three';
import { DRACOLoader } from 'three/addons/loaders/DRACOLoader.js';
import { TilesRenderer } from '3d-tiles-renderer';
import { GLTFExtensionsPlugin, GoogleCloudAuthPlugin } from '3d-tiles-renderer/plugins';
import type { Plan, PlanTile, RenderOptions } from './types';

const CAMERA_DISTANCE_M = 4000; // far enough above every roof; ortho, so only clipping depends on it

export interface TileResult {
  png: string; // data URL
  attributions: string[];
  frames: number;
  ms: number;
  stats: { visibleTiles: number; visibleSignature: string; cacheItems: number; cacheMB: number };
}

/** Internal TilesRenderer counters (not in the .d.ts). `refused` is reset every update(). */
interface TilesInternals {
  stats: { queued: number; downloading: number; parsing: number; refused: number; failed: number };
  lruCache: { itemSet: Set<unknown>; cachedBytes: number };
}

export class IsoScene {
  readonly renderer: WebGLRenderer;
  readonly camera: OrthographicCamera;
  readonly scene = new Scene();
  private tiles: TilesRenderer | null = null;
  private zoom = 1;
  private tileIds = new WeakMap<object, number>();
  private nextTileId = 1;
  private loadErrors: string[] = [];

  constructor(
    readonly plan: Plan,
    readonly opts: RenderOptions,
    canvas?: HTMLCanvasElement,
  ) {
    const size = plan.render_size_px;
    this.renderer = new WebGLRenderer({
      canvas,
      antialias: opts.antialias,
      alpha: true,
      preserveDrawingBuffer: true,
    });
    this.renderer.setPixelRatio(1);
    this.renderer.setSize(size, size, false);
    this.renderer.setClearColor(0x000000, 0); // empty = transparent, so the stitcher can see holes

    const { frustum_width_m: w, frustum_height_m: h } = plan.camera;
    this.camera = new OrthographicCamera(-w / 2, w / 2, h / 2, -h / 2, 1, 2 * CAMERA_DISTANCE_M);
    this.camera.up.fromArray(plan.camera.up_enu);

    if (opts.source === 'google') this.initGoogle();
    else this.initTestScene();
  }

  private initGoogle() {
    if (!this.opts.apiKey) throw new Error('GOOGLE_MAPS_API_KEY is not set (see .env.example)');
    const tiles = new TilesRenderer();
    // useRecommendedSettings would set errorTarget = 20 px: far too coarse for our renders.
    tiles.registerPlugin(
      new GoogleCloudAuthPlugin({ apiToken: this.opts.apiKey, autoRefreshToken: true, useRecommendedSettings: false }),
    );
    const draco = new DRACOLoader().setDecoderPath('/node_modules/three/examples/jsm/libs/draco/gltf/');
    tiles.registerPlugin(new GLTFExtensionsPlugin({ dracoLoader: draco }));
    tiles.errorTarget = this.opts.errorTarget;
    // The default cache (0.4 GB / 8000 tiles) fills up at errorTarget 2; a full cache silently
    // *refuses* finer tiles while loadProgress still reads 1, so LOD ends up depending on load order.
    tiles.lruCache.maxBytesSize = this.opts.cacheMB * 2 ** 20;
    tiles.lruCache.minBytesSize = 0.75 * tiles.lruCache.maxBytesSize;
    tiles.lruCache.maxSize = 40000;
    tiles.lruCache.minSize = 30000;

    // Tiles are in ECEF; the scene is in the plan's ENU frame (x east, y north, z up, metres).
    // Computed in float64 here, so the GPU only ever sees small local coordinates.
    const { origin_ecef: o, axes_ecef: ax } = this.plan.frame;
    const enuToEcef = new Matrix4()
      .makeBasis(new Vector3(...ax.east), new Vector3(...ax.north), new Vector3(...ax.up))
      .setPosition(...o);
    tiles.group.matrixAutoUpdate = false;
    tiles.group.matrix.copy(enuToEcef).invert();
    tiles.group.updateMatrixWorld(true);

    // A failed download (e.g. HTTP 503) leaves a hole or a coarse patch and the library won't retry it.
    tiles.addEventListener('load-error', (e) => this.loadErrors.push(String(e.error?.message ?? e.error)));
    tiles.setCamera(this.camera);
    tiles.setResolutionFromRenderer(this.camera, this.renderer);
    this.scene.add(tiles.group);
    this.tiles = tiles;
  }

  /** Deterministic boxes on a ground grid in ENU: lets us check alignment and seams without Google. */
  private initTestScene() {
    const g = new Group();
    const c = document.createElement('canvas');
    c.width = c.height = 2;
    const ctx = c.getContext('2d')!;
    ctx.fillStyle = '#7a8c6a';
    ctx.fillRect(0, 0, 2, 2);
    ctx.fillStyle = '#8fa07e';
    ctx.fillRect(0, 0, 1, 1);
    ctx.fillRect(1, 1, 1, 1);
    const tex = new CanvasTexture(c);
    tex.magFilter = tex.minFilter = NearestFilter;
    tex.wrapS = tex.wrapT = RepeatWrapping;
    const extent = 4000;
    tex.repeat.set(extent / 10, extent / 10); // 10 m checker squares
    g.add(new Mesh(new PlaneGeometry(extent, extent), new MeshLambertMaterial({ map: tex })));

    let seed = 12345;
    const rand = () => ((seed = (seed * 1103515245 + 12345) % 2 ** 31) / 2 ** 31);
    const box = new BoxGeometry(1, 1, 1);
    for (let gx = -1500; gx < 1500; gx += 40) {
      for (let gy = -1500; gy < 1500; gy += 40) {
        const height = 4 + rand() * 40;
        const m = new Mesh(box, new MeshLambertMaterial({ color: new Color().setHSL(rand(), 0.4, 0.55) }));
        m.scale.set(20 + rand() * 12, 20 + rand() * 12, height);
        m.position.set(gx, gy, height / 2);
        g.add(m);
      }
    }
    const sun = new DirectionalLight(0xffffff, 2.2);
    sun.position.set(-1, 2, 3);
    this.scene.add(g, sun, new AmbientLight(0xffffff, 0.9));
  }

  /** Point the camera so `target` (on the ground plane) lands on the image centre. */
  lookAt(targetEnu: [number, number, number]) {
    const t = new Vector3(...targetEnu);
    const v = new Vector3(...this.plan.camera.view_dir_enu);
    this.camera.position.copy(t).addScaledVector(v, -CAMERA_DISTANCE_M);
    this.camera.lookAt(t);
    this.camera.updateMatrixWorld(true);
  }

  /** Interactive viewing only; renders always use zoom 1 (= plan scale). */
  setZoom(z: number) {
    this.zoom = z;
    this.camera.zoom = z;
    this.camera.updateProjectionMatrix();
    this.tiles?.setResolutionFromRenderer(this.camera, this.renderer);
  }

  frame() {
    this.tiles?.update();
    this.renderer.render(this.scene, this.camera);
  }

  /** Render one plan tile once every tile needed at the target error has loaded. */
  async renderTile(tile: PlanTile): Promise<TileResult> {
    const start = performance.now();
    if (this.zoom !== 1) this.setZoom(1);
    this.lookAt(tile.target_enu);
    let frames = 0;
    let settled = 0;
    let lastSig = '';
    this.loadErrors = [];
    while (true) {
      if (this.loadErrors.length) {
        throw new Error(`tile ${tile.id}: ${this.loadErrors.length} tile download(s) failed (${this.loadErrors[0]})`);
      }
      this.frame();
      frames++;
      await new Promise(requestAnimationFrame);
      const t = this.tiles;
      const sig = this.visibleSignature();
      let idle = true;
      if (t) {
        const s = (t as unknown as TilesInternals).stats;
        if (s.refused > 0 && this.cacheFull()) {
          throw new Error(`tile ${tile.id}: tile cache full (${this.cacheMB()} MB), raise --cache-mb`);
        }
        idle = t.rootTileset !== null && s.queued + s.downloading + s.parsing + s.refused === 0 && sig === lastSig;
      }
      lastSig = sig;
      settled = idle ? settled + 1 : 0;
      // N frames in a row with nothing queued/loading/refused and an unchanged visible set:
      // traversal has reached the final LOD for this view.
      if (settled >= this.opts.settleFrames) break;
      if (performance.now() - start > this.opts.timeoutMs) {
        throw new Error(`tile ${tile.id}: not settled after ${this.opts.timeoutMs} ms (progress ${t?.loadProgress})`);
      }
    }
    this.frame();
    const attributions = (this.tiles?.getAttributions() ?? []).map((a) => String(a.value));
    const cache = this.tiles ? (this.tiles as unknown as TilesInternals).lruCache : null;
    return {
      png: this.renderer.domElement.toDataURL('image/png'),
      attributions,
      frames,
      ms: performance.now() - start,
      stats: {
        visibleTiles: this.tiles?.visibleTiles.size ?? 0,
        visibleSignature: lastSig,
        cacheItems: cache?.itemSet.size ?? 0,
        cacheMB: this.cacheMB(),
      },
    };
  }

  /**
   * Ground height under ENU points (e, n), against the geometry loaded for the current view
   * (call after renderTile, so the view is at render LOD). A ray straight down from above every
   * roof; the lowest hit is the ground (tree canopy, cars, awnings are above it). NaN = no hit.
   */
  groundHeights(pts: Float64Array): Float32Array {
    const out = new Float32Array(pts.length / 2).fill(NaN);
    const target = this.tiles?.group ?? this.scene;
    const ray = new Raycaster();
    const down = new Vector3(0, 0, -1);
    const from = new Vector3();
    for (let i = 0; i < out.length; i++) {
      from.set(pts[2 * i], pts[2 * i + 1], 1000);
      ray.set(from, down);
      const hits = ray.intersectObject(target, true);
      if (hits.length) out[i] = hits[hits.length - 1].point.z;
    }
    return out;
  }

  private cacheMB() {
    const c = this.tiles ? (this.tiles as unknown as TilesInternals).lruCache : null;
    return c ? Math.round(c.cachedBytes / 2 ** 20) : 0;
  }

  private cacheFull() {
    return this.tiles?.lruCache.isFull() ?? false;
  }

  /** Order-independent fingerprint of the set of visible tiles (FNV-style hash of stable ids). */
  private visibleSignature(): string {
    if (!this.tiles) return '';
    let sum = 0;
    let xor = 0;
    for (const tile of this.tiles.visibleTiles) {
      let id = this.tileIds.get(tile);
      if (id === undefined) this.tileIds.set(tile, (id = this.nextTileId++));
      const h = Math.imul(id, 2654435761) >>> 0;
      sum = (sum + h) >>> 0;
      xor ^= h;
    }
    return `${this.tiles.visibleTiles.size}:${sum.toString(16)}:${xor.toString(16)}`;
  }
}

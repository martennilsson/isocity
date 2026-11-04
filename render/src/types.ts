// Subset of the plan JSON written by pipeline/plan_tiles.py that the renderer reads.

export type Vec3 = [number, number, number];

export interface PlanTile {
  id: string;
  x: number;
  y: number;
  screen_rect_px: [number, number, number, number];
  render_rect_px: [number, number, number, number];
  inside_boundary: boolean;
  target_enu: Vec3;
}

export interface Plan {
  area: string;
  frame: { origin_ecef: Vec3; axes_ecef: { east: Vec3; north: Vec3; up: Vec3 } };
  camera: {
    view_dir_enu: Vec3;
    up_enu: Vec3;
    right_enu: Vec3;
    frustum_width_m: number;
    frustum_height_m: number;
    north_screen_dir: [number, number];
  };
  tile_size_px: number;
  overlap_px: number;
  render_size_px: number;
  tiles: PlanTile[];
}

export interface RenderOptions {
  source: 'osm' | 'google' | 'test';
  apiKey?: string;
  errorTarget: number; // max screen-space error in px; lower = more detail, more downloads
  antialias: boolean;
  cacheMB: number; // tile cache budget; must hold every tile of one view at errorTarget
  settleFrames: number;
  timeoutMs: number;
}

/** What the CLI serves to the page at /__config. */
export interface PageConfig {
  plan: Plan;
  options: RenderOptions;
}

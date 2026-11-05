"""Plan the tile grid for an area in isometric screen space.

This is the single source of truth for tile geometry. Everything else (renderer,
generator, stitcher) reads the plan JSON written here and never recomputes positions.

World frame: local East-North-Up (metres) at `origin` in area.yaml.
Screen space: pixels, x right, y down, (0, 0) = projection of the ENU origin.
Quadrants are the canonical unit (one row per quadrant in map.db): quadrant (qx, qy)
covers screen pixels [qx*Q, (qx+1)*Q) x [qy*Q, (qy+1)*Q), Q = quadrant size.
Render tiles (x, y) are fixed windows of quadrants_per_side^2 quadrants that abut exactly;
each is rendered `overlap_px` larger on every side so stitching/generation can crop away
edge artefacts. Because the camera is orthographic and fixed, all renders share one pixel
grid: any infill window (any quadrant offset) can be cut from the base renders, see
`window_rect_px`.
Generation windows (optional `generation:` in area.yaml): the image model draws window_px
squares on a lattice of stride_px, anchored at the screen origin so boundary edits never move
them. Window (i, j) owns the core [i*S, (i+1)*S) x [j*S, (j+1)*S) and is that core grown by
(window_px - stride_px) / 2 on every side, so neighbours share a band of window_px - stride_px
pixels where `pipeline/mosaic.py` cuts between them. All windows whose core meets the region's
bounding box are listed (a full rectangle of windows), see `gen_window_rect_px`.

Usage:
    uv run python -m pipeline.plan_tiles --area config/area.yaml --out data/plan/sodermalm.json \
        [--preview data/plan/sodermalm.png]
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import yaml
import numpy as np
import shapely
from shapely.geometry import Polygon, box, shape

WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)

Vec3 = tuple[float, float, float]


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def geodetic_to_ecef(lon: float, lat: float, h: float) -> Vec3:
    lon_r, lat_r = math.radians(lon), math.radians(lat)
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(lat_r) ** 2)
    return (
        (n + h) * math.cos(lat_r) * math.cos(lon_r),
        (n + h) * math.cos(lat_r) * math.sin(lon_r),
        (n * (1 - WGS84_E2) + h) * math.sin(lat_r),
    )


def ecef_to_geodetic(x: float, y: float, z: float) -> Vec3:
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - WGS84_E2))
    for _ in range(10):
        n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - WGS84_E2 * n / (n + h)))
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
    h = p / math.cos(lat) - n
    return math.degrees(lon), math.degrees(lat), h


@dataclass(frozen=True)
class Projection:
    """ENU frame at the origin plus the fixed orthographic isometric camera."""

    origin_lon: float
    origin_lat: float
    origin_h: float
    elevation_deg: float
    look_azimuth_deg: float
    metres_per_pixel: float

    @classmethod
    def from_config(cls, cfg: dict) -> Projection:
        return cls(
            origin_lon=cfg["origin"]["lon"],
            origin_lat=cfg["origin"]["lat"],
            origin_h=cfg["origin"]["height_m"],
            elevation_deg=cfg["camera"]["elevation_deg"],
            look_azimuth_deg=cfg["camera"]["look_azimuth_deg"],
            metres_per_pixel=cfg["scale"]["metres_per_pixel"],
        )

    # --- ENU <-> ECEF ---------------------------------------------------------

    @property
    def origin_ecef(self) -> Vec3:
        return geodetic_to_ecef(self.origin_lon, self.origin_lat, self.origin_h)

    @property
    def enu_axes_ecef(self) -> tuple[Vec3, Vec3, Vec3]:
        lon, lat = math.radians(self.origin_lon), math.radians(self.origin_lat)
        east = (-math.sin(lon), math.cos(lon), 0.0)
        north = (-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat))
        up = (math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat))
        return east, north, up

    def enu_to_ecef(self, p: Vec3) -> Vec3:
        o = self.origin_ecef
        e, n, u = self.enu_axes_ecef
        return tuple(o[i] + p[0] * e[i] + p[1] * n[i] + p[2] * u[i] for i in range(3))  # type: ignore[return-value]

    def ecef_to_enu(self, q: Vec3) -> Vec3:
        o = self.origin_ecef
        d = (q[0] - o[0], q[1] - o[1], q[2] - o[2])
        e, n, u = self.enu_axes_ecef
        return _dot(d, e), _dot(d, n), _dot(d, u)

    def lonlat_to_enu(self, lon: float, lat: float, h: float | None = None) -> Vec3:
        return self.ecef_to_enu(geodetic_to_ecef(lon, lat, self.origin_h if h is None else h))

    def enu_to_lonlat(self, p: Vec3) -> Vec3:
        return ecef_to_geodetic(*self.enu_to_ecef(p))

    # --- camera basis (ENU) -----------------------------------------------------

    @property
    def right(self) -> Vec3:
        a = math.radians(self.look_azimuth_deg)
        return math.cos(a), -math.sin(a), 0.0

    @property
    def forward(self) -> Vec3:
        """Horizontal look direction."""
        a = math.radians(self.look_azimuth_deg)
        return math.sin(a), math.cos(a), 0.0

    @property
    def view_dir(self) -> Vec3:
        t = math.radians(self.elevation_deg)
        f = self.forward
        return f[0] * math.cos(t), f[1] * math.cos(t), -math.sin(t)

    @property
    def camera_up(self) -> Vec3:
        t = math.radians(self.elevation_deg)
        f = self.forward
        return f[0] * math.sin(t), f[1] * math.sin(t), math.cos(t)

    # --- ENU <-> screen -------------------------------------------------------

    def enu_to_screen(self, p: Vec3) -> tuple[float, float]:
        return _dot(p, self.right) / self.metres_per_pixel, -_dot(p, self.camera_up) / self.metres_per_pixel

    def screen_to_ground(self, sx: float, sy: float) -> Vec3:
        """ENU point on the reference plane (u = 0) that projects to screen (sx, sy)."""
        r, f = self.right, self.forward
        along = sx * self.metres_per_pixel
        ahead = -sy * self.metres_per_pixel / math.sin(math.radians(self.elevation_deg))
        return along * r[0] + ahead * f[0], along * r[1] + ahead * f[1], 0.0


def load_area(area_path: Path) -> tuple[dict, Polygon]:
    cfg = yaml.safe_load(area_path.read_text())
    geo = json.loads((area_path.parent / cfg["boundary"]).read_text())
    geom = shape(geo["features"][0]["geometry"] if geo.get("type") == "FeatureCollection" else geo)
    return cfg, geom


def boundary_to_screen(proj: Projection, boundary_lonlat) -> Polygon:
    def to_screen(coords: np.ndarray) -> np.ndarray:
        return np.array([proj.enu_to_screen(proj.lonlat_to_enu(lon, lat)) for lon, lat in coords])

    return shapely.transform(boundary_lonlat, to_screen)


def window_rect_px(
    qx: int, qy: int, nqx: int, nqy: int, quadrant_size: int, overlap_px: int = 0
) -> tuple[int, int, int, int]:
    """Screen rect (x0, y0, x1, y1) of an nqx x nqy block of quadrants starting at (qx, qy),
    grown by overlap_px on every side. The only place quadrant indices become pixels."""
    return (
        qx * quadrant_size - overlap_px,
        qy * quadrant_size - overlap_px,
        (qx + nqx) * quadrant_size + overlap_px,
        (qy + nqy) * quadrant_size + overlap_px,
    )


def gen_window_rect_px(i: int, j: int, stride: int, window: int) -> tuple[int, int, int, int]:
    """Screen rect (x0, y0, x1, y1) of generation window (i, j): its core grown by the half band."""
    if window <= stride or (window - stride) % 2:
        raise ValueError("generation window_px must exceed stride_px by an even number of pixels")
    m = (window - stride) // 2
    return i * stride - m, j * stride - m, (i + 1) * stride + m, (j + 1) * stride + m


def plan_windows(gen: dict, region_bounds, tiles: list[dict]) -> dict:
    s, w = gen["stride_px"], gen["window_px"]
    minx, miny, maxx, maxy = region_bounds
    cover = (min(t["render_rect_px"][0] for t in tiles), min(t["render_rect_px"][1] for t in tiles),
             max(t["render_rect_px"][2] for t in tiles), max(t["render_rect_px"][3] for t in tiles))
    windows = []
    for j in range(math.floor(miny / s), math.ceil(maxy / s)):
        for i in range(math.floor(minx / s), math.ceil(maxx / s)):
            rect = gen_window_rect_px(i, j, s, w)
            if rect[0] < cover[0] or rect[1] < cover[1] or rect[2] > cover[2] or rect[3] > cover[3]:
                raise ValueError(f"generation window {i},{j} {rect} reaches outside the rendered tiles {cover}")
            windows.append({"id": f"w{i}_{j}", "i": i, "j": j, "rect_px": list(rect),
                            "core_rect_px": [i * s, j * s, (i + 1) * s, (j + 1) * s]})
    return {"window_px": w, "stride_px": s, "band_px": w - s, "windows": windows}


def plan(cfg: dict, boundary_lonlat) -> dict:
    proj = Projection.from_config(cfg)
    size = cfg["tiles"]["render_size_px"]
    q_per_side = cfg["tiles"]["quadrants_per_side"]
    if size % q_per_side:
        raise ValueError("render_size_px must be divisible by quadrants_per_side")
    q_size = size // q_per_side
    margin = cfg["tiles"].get("margin_px", 0)
    overlap = cfg["tiles"].get("overlap_px", 0)
    padded = size + 2 * overlap

    screen_boundary = boundary_to_screen(proj, boundary_lonlat)
    region = screen_boundary.buffer(margin) if margin else screen_boundary
    minx, miny, maxx, maxy = region.bounds

    tiles = []
    for ty in range(math.floor(miny / size), math.ceil(maxy / size)):
        for tx in range(math.floor(minx / size), math.ceil(maxx / size)):
            rect = window_rect_px(tx * q_per_side, ty * q_per_side, q_per_side, q_per_side, q_size)
            if not region.intersects(box(*rect)):
                continue
            target_enu = proj.screen_to_ground((tx + 0.5) * size, (ty + 0.5) * size)
            tiles.append(
                {
                    "id": f"{tx}_{ty}",
                    "x": tx,
                    "y": ty,
                    "screen_rect_px": list(rect),
                    "render_rect_px": list(
                        window_rect_px(tx * q_per_side, ty * q_per_side, q_per_side, q_per_side, q_size, overlap)
                    ),
                    "inside_boundary": screen_boundary.intersects(box(*rect)),
                    "target_enu": list(target_enu),
                    "target_ecef": list(proj.enu_to_ecef(target_enu)),
                    "target_lonlat_h": list(proj.enu_to_lonlat(target_enu)),
                    "quadrants": [
                        [tx * q_per_side + qx, ty * q_per_side + qy]
                        for qy in range(q_per_side)
                        for qx in range(q_per_side)
                    ],
                }
            )

    quadrants = []
    for t in tiles:
        for qx, qy in t["quadrants"]:
            qrect = window_rect_px(qx, qy, 1, 1, q_size)
            if region.intersects(box(*qrect)):
                quadrants.append(
                    {
                        "id": f"{qx}_{qy}",
                        "qx": qx,
                        "qy": qy,
                        "tile": t["id"],
                        "screen_rect_px": list(qrect),
                        "inside_boundary": screen_boundary.intersects(box(*qrect)),
                    }
                )

    east, north, up = proj.enu_axes_ecef
    gen = cfg.get("generation")
    return {
        "area": cfg["name"],
        "config": cfg,
        "frame": {
            "type": "ENU",
            "origin_lonlat_h": [proj.origin_lon, proj.origin_lat, proj.origin_h],
            "origin_ecef": list(proj.origin_ecef),
            "axes_ecef": {"east": list(east), "north": list(north), "up": list(up)},
        },
        "camera": {
            "projection": "orthographic",
            "view_dir_enu": list(proj.view_dir),
            "up_enu": list(proj.camera_up),
            "right_enu": list(proj.right),
            "frustum_width_m": padded * proj.metres_per_pixel,
            "frustum_height_m": padded * proj.metres_per_pixel,
            "north_screen_dir": _north_screen_dir(proj),
        },
        "tile_size_px": size,
        "overlap_px": overlap,
        "render_size_px": padded,
        "quadrant_size_px": q_size,
        "quadrants_per_side": q_per_side,
        "screen_boundary_px": [list(c) for c in screen_boundary.exterior.coords],
        "tiles": tiles,
        "quadrants": quadrants,
        "generation": plan_windows(gen, region.bounds, tiles) if gen else None,
    }


def _north_screen_dir(proj: Projection) -> list[float]:
    sx, sy = proj.enu_to_screen((0.0, 1.0, 0.0))
    length = math.hypot(sx, sy)
    return [sx / length, sy / length]


def write_preview(result: dict, path: Path, px_per_tile: int = 96) -> None:
    from PIL import Image, ImageDraw

    size = result["tile_size_px"]
    tiles = result["tiles"]
    x0 = min(t["x"] for t in tiles) * size
    y0 = min(t["y"] for t in tiles) * size
    x1 = (max(t["x"] for t in tiles) + 1) * size
    y1 = (max(t["y"] for t in tiles) + 1) * size
    k = px_per_tile / size

    def s(x, y):
        return (x - x0) * k, (y - y0) * k

    img = Image.new("RGB", (round((x1 - x0) * k) + 1, round((y1 - y0) * k) + 1), "white")
    d = ImageDraw.Draw(img)
    q = result["quadrant_size_px"]
    for t in tiles:
        rx0, ry0, rx1, ry1 = t["screen_rect_px"]
        d.rectangle([*s(rx0, ry0), *s(rx1, ry1)], fill="#eef3ff" if t["inside_boundary"] else "#f4f4f4")
        for i in range(1, result["quadrants_per_side"]):
            d.line([s(rx0 + i * q, ry0), s(rx0 + i * q, ry1)], fill="#d0d8e8")
            d.line([s(rx0, ry0 + i * q), s(rx1, ry0 + i * q)], fill="#d0d8e8")
        d.rectangle([*s(rx0, ry0), *s(rx1, ry1)], outline="#6070a0")
        d.text((s(rx0, ry0)[0] + 3, s(rx0, ry0)[1] + 2), t["id"], fill="#334")
    d.line([s(*c) for c in result["screen_boundary_px"]], fill="#d03030", width=2)

    nx, ny = result["camera"]["north_screen_dir"]
    cx, cy = img.width - 30, 30
    d.line([(cx - nx * 18, cy - ny * 18), (cx + nx * 18, cy + ny * 18)], fill="black", width=2)
    d.text((cx + nx * 22 - 3, cy + ny * 22 - 5), "N", fill="black")

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--area", type=Path, required=True, help="path to area.yaml")
    ap.add_argument("--out", type=Path, required=True, help="output plan JSON")
    ap.add_argument("--preview", type=Path, help="optional PNG preview of the grid")
    args = ap.parse_args()

    cfg, boundary = load_area(args.area)
    result = plan(cfg, boundary)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1))
    if args.preview:
        write_preview(result, args.preview)

    n_tiles = len(result["tiles"])
    n_inside = sum(t["inside_boundary"] for t in result["tiles"])
    n_win = len(result["generation"]["windows"]) if result["generation"] else 0
    print(
        f"{result['area']}: {n_tiles} render tiles ({n_inside} touching boundary) at "
        f"{result['render_size_px']} px, {len(result['quadrants'])} quadrants, "
        f"{n_win} generation windows -> {args.out}"
    )


if __name__ == "__main__":
    main()

"""Stitch tiles into one map image and cut a Deep Zoom pyramid for the viewer.

Works on render tiles now (prototype) and on generated tiles later: anything stored as
<id>.png per plan tile, sized like `render_rect_px`. Positions come only from the plan JSON.
Uses pyvips, so the full map never has to fit in memory.

Output (`--out DIR`):
  DIR/map.dzi, DIR/map_files/…   Deep Zoom pyramid (OpenSeadragon)
  DIR/manifest.json              map origin in plan screen px, size, north direction,
                                 screen->lon/lat affine (ground level), data attributions

Usage:
    uv run python -m pipeline.build_pyramid --plan data/plan/sodermalm.json \
        --tiles-dir data/renders/sodermalm --out dist/sodermalm-renders --style photo
    (--style pixel: lossless WebP, nearest-neighbour downsampling, pixelated viewer)
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pyvips

from pipeline.plan_tiles import Projection

# How each style is stored and downsampled. Box ("mean") keeps photos clean; pixel art must not
# be blurred, so it uses nearest (replace with a palette-aware method in Phase 6).
STYLES = {
    "photo": {"suffix": ".webp[Q=85]", "region_shrink": "mean"},
    "pixel": {"suffix": ".webp[lossless]", "region_shrink": "nearest"},
}


def tile_grid(plan: dict, tiles_dir: Path) -> tuple[list[dict], tuple[int, int, int, int]]:
    present = [t for t in plan["tiles"] if (tiles_dir / f"{t['id']}.png").exists()]
    if not present:
        raise SystemExit(f"no tile PNGs in {tiles_dir}")
    xs = [t["x"] for t in present]
    ys = [t["y"] for t in present]
    return present, (min(xs), min(ys), max(xs), max(ys))


def stitch(plan: dict, tiles_dir: Path) -> tuple[pyvips.Image, list[int], list[str]]:
    """Map image covering the bounding box of present tiles (missing tiles transparent)."""
    present, (x0, y0, x1, y1) = tile_grid(plan, tiles_dir)
    by_xy = {(t["x"], t["y"]): t for t in present}
    size = plan["tile_size_px"]
    blank = pyvips.Image.black(size, size, bands=4).copy(interpretation="srgb")
    cells, attributions = [], set()
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            t = by_xy.get((x, y))
            if t is None:
                cells.append(blank)
                continue
            img = pyvips.Image.new_from_file(str(tiles_dir / f"{t['id']}.png"))  # random access: read twice (trim, dzsave)
            if img.bands == 3:
                img = img.bandjoin(255)
            sx0, sy0, sx1, sy1 = t["screen_rect_px"]
            rx0, ry0, _, _ = t["render_rect_px"]
            cells.append(img.crop(sx0 - rx0, sy0 - ry0, sx1 - sx0, sy1 - sy0))  # drop overlap margin
            side = tiles_dir / f"{t['id']}.json"
            if side.exists():
                attributions.update(json.loads(side.read_text()).get("attributions", []))
    image = pyvips.Image.arrayjoin(cells, across=x1 - x0 + 1)
    origin = [x0 * size, y0 * size]  # plan screen px of the image's top-left pixel
    # Crop to the opaque content: a mosaic covers less than the tile grid, and transparent
    # margins get filled with stray edge pixels in the nearest-neighbour levels.
    left, top, w, h = image[3].find_trim(threshold=0.5, background=[0])
    if w and h and (w, h) != (image.width, image.height):
        image = image.crop(left, top, w, h)
        origin = [origin[0] + left, origin[1] + top]
    return image, origin, sorted(attributions)


def screen_to_lonlat_affine(plan: dict) -> list[list[float]]:
    """[[a, b, c], [d, e, f]]: lon = a*sx + b*sy + c, lat = d*sx + e*sy + f, at ground level.
    Exact for the ENU plane; lon/lat curvature is negligible over a city."""
    proj = Projection.from_config(plan["config"])
    pts = [(0.0, 0.0), (1000.0, 0.0), (0.0, 1000.0)]
    ll = [proj.enu_to_lonlat(proj.screen_to_ground(*p))[:2] for p in pts]
    (lon0, lat0), (lon1, lat1), (lon2, lat2) = ll
    return [
        [(lon1 - lon0) / 1000, (lon2 - lon0) / 1000, lon0],
        [(lat1 - lat0) / 1000, (lat2 - lat0) / 1000, lat0],
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--tiles-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--style", choices=STYLES, default="photo")
    ap.add_argument("--tile-size", type=int, default=512, help="pyramid tile size (px)")
    ap.add_argument("--title", default=None)
    args = ap.parse_args()

    plan = json.loads(args.plan.read_text())
    image, origin, attributions = stitch(plan, args.tiles_dir)
    style = STYLES[args.style]

    if (args.out / "map_files").exists():
        shutil.rmtree(args.out / "map_files")  # dzsave won't overwrite; stale levels would linger
    args.out.mkdir(parents=True, exist_ok=True)
    image.dzsave(
        str(args.out / "map"),
        tile_size=args.tile_size,
        overlap=0,
        suffix=style["suffix"],
        region_shrink=style["region_shrink"],
        layout="dz",
    )
    manifest = {
        "title": args.title or plan["area"],
        "area": plan["area"],
        "style": args.style,
        "dzi": "map.dzi",
        "width": image.width,
        "height": image.height,
        "origin_screen_px": origin,
        "metres_per_pixel": plan["config"]["scale"]["metres_per_pixel"],
        "north_screen_dir": plan["camera"]["north_screen_dir"],
        "screen_to_lonlat": screen_to_lonlat_affine(plan),
        "attributions": attributions,
        "source_tiles": str(args.tiles_dir),
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    n_files = sum(1 for _ in (args.out / "map_files").rglob("*.webp"))
    mb = sum(f.stat().st_size for f in (args.out / "map_files").rglob("*")) / 2**20
    print(f"{image.width}x{image.height} px -> {args.out} ({n_files} pyramid tiles, {mb:.1f} MB, style {args.style})")


if __name__ == "__main__":
    main()

"""Stitch rendered (or generated) tiles into one image and measure seams.

Positions come only from the plan JSON (`screen_rect_px` / `render_rect_px`), never recomputed.
Small-block tool for checking seams (CLAUDE.md: test on 3x3 before scaling up); the full map
stitch in Phase 6 will use pyvips.

Two checks, printed per seam:
  - overlap: where neighbouring renders share pixels (overlap_px > 0), the max/mean absolute
    difference of those shared pixels. 0 means the renders are pixel-identical there.
  - step: mean absolute colour step across the seam line divided by the mean step between
    neighbouring pixel rows/columns just inside the tiles. ~1 means the seam is invisible
    statistically; clearly > 1 means a visible edge.

Usage:
    uv run python -m pipeline.stitch_block --plan data/plan/sodermalm.json \
        --tiles-dir data/renders/sodermalm --out data/stitch/block.png [--block=-1,-1,1,1] [--mark]
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

INSIDE_BAND_PX = 6  # rows/columns on each side of a seam used as the "normal" step baseline


@dataclass
class Seam:
    a: str
    b: str
    axis: str  # "x": vertical seam line (a left of b), "y": horizontal (a above b)
    overlap_max: int | None
    overlap_mean: float | None
    step_ratio: float


def load_tiles(plan: dict, tiles_dir: Path, block: tuple[int, int, int, int] | None) -> dict[str, tuple[dict, np.ndarray]]:
    out = {}
    for t in plan["tiles"]:
        if block and not (block[0] <= t["x"] <= block[2] and block[1] <= t["y"] <= block[3]):
            continue
        p = tiles_dir / f"{t['id']}.png"
        if p.exists():
            img = np.asarray(Image.open(p).convert("RGBA"))
            rx0, ry0, rx1, ry1 = t["render_rect_px"]
            if img.shape[:2] != (ry1 - ry0, rx1 - rx0):
                raise ValueError(f"{p}: size {img.shape[1]}x{img.shape[0]} does not match render_rect_px")
            out[t["id"]] = (t, img)
    return out


def crop_to_screen_rect(t: dict, img: np.ndarray) -> np.ndarray:
    sx0, sy0, sx1, sy1 = t["screen_rect_px"]
    rx0, ry0, _, _ = t["render_rect_px"]
    return img[sy0 - ry0 : sy1 - ry0, sx0 - rx0 : sx1 - rx0]


def stitch(tiles: dict[str, tuple[dict, np.ndarray]]) -> tuple[np.ndarray, tuple[int, int]]:
    rects = [t["screen_rect_px"] for t, _ in tiles.values()]
    x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
    x1, y1 = max(r[2] for r in rects), max(r[3] for r in rects)
    canvas = np.zeros((y1 - y0, x1 - x0, 4), np.uint8)
    for t, img in tiles.values():
        sx0, sy0, sx1, sy1 = t["screen_rect_px"]
        canvas[sy0 - y0 : sy1 - y0, sx0 - x0 : sx1 - x0] = crop_to_screen_rect(t, img)
    return canvas, (x0, y0)


def _shared(ta: dict, ia: np.ndarray, tb: dict, ib: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    ax0, ay0, ax1, ay1 = ta["render_rect_px"]
    bx0, by0, bx1, by1 = tb["render_rect_px"]
    x0, y0, x1, y1 = max(ax0, bx0), max(ay0, by0), min(ax1, bx1), min(ay1, by1)
    if x0 >= x1 or y0 >= y1:
        return None
    return ia[y0 - ay0 : y1 - ay0, x0 - ax0 : x1 - ax0], ib[y0 - by0 : y1 - by0, x0 - bx0 : x1 - bx0]


def _step_ratio(canvas: np.ndarray, axis: str, line: int, lo: int, hi: int) -> float:
    """Seam step vs inside steps. `line` is the first pixel index of the second tile."""
    rgb = canvas[..., :3].astype(np.int16)
    if axis == "x":
        rgb = rgb.transpose(1, 0, 2)  # make the seam horizontal: rows = x
    band = rgb[line - INSIDE_BAND_PX - 1 : line + INSIDE_BAND_PX, lo:hi]
    steps = np.abs(np.diff(band, axis=0)).mean(axis=(1, 2))  # one mean step per row pair
    seam = steps[INSIDE_BAND_PX]
    inside = np.delete(steps, INSIDE_BAND_PX).mean()
    return float(seam / inside) if inside > 0 else (float("inf") if seam > 0 else 1.0)


def measure_seams(tiles: dict[str, tuple[dict, np.ndarray]], canvas: np.ndarray, origin: tuple[int, int]) -> list[Seam]:
    by_xy = {(t["x"], t["y"]): (t, img) for t, img in tiles.values()}
    seams = []
    for (x, y), (ta, ia) in sorted(by_xy.items()):
        for axis, nb in (("x", (x + 1, y)), ("y", (x, y + 1))):
            if nb not in by_xy:
                continue
            tb, ib = by_xy[nb]
            shared = _shared(ta, ia, tb, ib)
            if shared is not None:
                d = np.abs(shared[0][..., :3].astype(np.int16) - shared[1][..., :3].astype(np.int16))
                omax, omean = int(d.max()), float(d.mean())
            else:
                omax = omean = None
            sa = ta["screen_rect_px"]
            if axis == "x":
                ratio = _step_ratio(canvas, "x", sa[2] - origin[0], sa[1] - origin[1], sa[3] - origin[1])
            else:
                ratio = _step_ratio(canvas, "y", sa[3] - origin[1], sa[0] - origin[0], sa[2] - origin[0])
            seams.append(Seam(ta["id"], tb["id"], axis, omax, omean, ratio))
    return seams


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--tiles-dir", type=Path, required=True, help="directory with <id>.png per tile")
    ap.add_argument("--out", type=Path, required=True, help="stitched PNG")
    ap.add_argument("--block", help="x0,y0,x1,y1 (inclusive tile coords); default: all tiles present")
    ap.add_argument("--mark", action="store_true", help="also write <out>-marked.png with tile edges drawn")
    args = ap.parse_args()

    plan = json.loads(args.plan.read_text())
    block = tuple(int(v) for v in args.block.split(",")) if args.block else None
    tiles = load_tiles(plan, args.tiles_dir, block)  # type: ignore[arg-type]
    if not tiles:
        raise SystemExit(f"no tile PNGs found in {args.tiles_dir}")
    canvas, origin = stitch(tiles)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(canvas).save(args.out)
    if args.mark:
        img = Image.fromarray(canvas)
        d = ImageDraw.Draw(img)
        for t, _ in tiles.values():
            x0, y0, x1, y1 = t["screen_rect_px"]
            d.rectangle([x0 - origin[0], y0 - origin[1], x1 - origin[0] - 1, y1 - origin[1] - 1], outline=(255, 0, 255, 255))
            d.text((x0 - origin[0] + 4, y0 - origin[1] + 4), t["id"], fill=(255, 0, 255, 255))
        img.save(args.out.with_name(args.out.stem + "-marked.png"))

    print(f"{len(tiles)} tiles -> {args.out} ({canvas.shape[1]}x{canvas.shape[0]}), overlap_px={plan['overlap_px']}")
    for s in measure_seams(tiles, canvas, origin):
        ov = "n/a" if s.overlap_max is None else f"max {s.overlap_max:3d} mean {s.overlap_mean:6.3f}"
        print(f"  {s.a:>6} | {s.b:<6} ({s.axis})  overlap {ov}   step ratio {s.step_ratio:5.2f}")


if __name__ == "__main__":
    main()

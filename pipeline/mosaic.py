"""Overlapping generation windows → one seamless image, cut along min-error paths (Phase 5).

Two steps around `pipeline.stylize`:
  cut:     write the render of every generation window (plan["generation"], from plan_tiles) as
           <out>/<window id>.png, cut from the plan's render tiles. stylize and fidelity then run
           on that directory unchanged (window ids look like w-1_0).
  compose: place the styled windows in raster order. Where a window overlaps what is already
           placed (the band to its left and the band above), cut along the path where the two
           drawings agree most (image quilting: dynamic programming over the per-pixel colour
           difference, 3×3 smoothed, kept GUARD_PX away from the band edges) and keep the old
           pixels on one side, the new on the other. Crop, never blend. --straight cuts in the
           middle of the band instead (for comparison).
           Writes canvas.png, canvas-marked.png (cuts in red), cuts.json (per cut: colour
           difference along it and the seam step ratio, ~1 = invisible, as stitch_block) and
           <tile id>.png (RGBA) for every plan tile the canvas touches, transparent outside it.
           --water-from <renders>: after the cuts, paint open water flat (pipeline/water.py),
           found in the renders and grown into the model's own water; writes water-mask.png.
           Seam stats then skip the stretches of a cut that run through painted water.

Usage:
    uv run python -m pipeline.mosaic cut --plan data/plan/sodermalm.json \
        --renders data/renders/sodermalm --out data/windows/sodermalm [--windows=-1,-1,1,1]
    uv run python -m pipeline.mosaic compose --plan data/plan/sodermalm.json \
        --styled data/styled/<run> --out data/mosaic/<run> [--windows=-1,-1,1,1] [--straight] \
        [--water-from data/renders/sodermalm]
--windows i0,j0,i1,j1: inclusive block of window indices (default: all).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from pipeline import water as water_mod

GUARD_PX = 16  # cuts stay this far from the band edges (window borders draw worst)
STEP_BAND = range(2, 7)  # columns/rows beside a cut used as the "normal" step baseline
MIN_SEAM_ROWS = 32  # cuts with fewer non-water rows than this get no step ratio


def cut_rect(plan: dict, src_dir: Path, rect) -> np.ndarray:
    """Pixels of screen rect `rect` assembled from the render tiles in src_dir that cover it."""
    x0, y0, x1, y1 = rect
    out = np.zeros((y1 - y0, x1 - x0, 3), np.uint8)
    covered = np.zeros(out.shape[:2], bool)
    for t in plan["tiles"]:
        sx0, sy0, sx1, sy1 = t["screen_rect_px"]
        ix0, iy0, ix1, iy1 = max(x0, sx0), max(y0, sy0), min(x1, sx1), min(y1, sy1)
        if ix0 >= ix1 or iy0 >= iy1:
            continue
        p = src_dir / f"{t['id']}.png"
        if not p.exists():
            raise SystemExit(f"missing {p} for window {rect}")
        a = np.asarray(Image.open(p).convert("RGB"))
        rx0, ry0 = t["render_rect_px"][:2]
        out[iy0 - y0:iy1 - y0, ix0 - x0:ix1 - x0] = a[iy0 - ry0:iy1 - ry0, ix0 - rx0:ix1 - rx0]
        covered[iy0 - y0:iy1 - y0, ix0 - x0:ix1 - x0] = True
    if not covered.all():
        raise SystemExit(f"window {rect} not fully covered by tiles in {src_dir}")
    return out


def min_cut(cost: np.ndarray) -> np.ndarray:
    """Per row, the column of the cheapest top-to-bottom 8-connected path through cost (h × w)."""
    h, w = cost.shape
    acc = cost.astype(float).copy()
    for y in range(1, h):
        p = acc[y - 1]
        acc[y] += np.minimum(p, np.minimum(np.r_[np.inf, p[:-1]], np.r_[p[1:], np.inf]))
    path = np.empty(h, int)
    path[-1] = int(acc[-1].argmin())
    for y in range(h - 2, -1, -1):
        lo = max(0, path[y + 1] - 1)
        path[y] = lo + int(acc[y, lo:path[y + 1] + 2].argmin())
    return path


def band_cut(old: np.ndarray, new: np.ndarray, straight: bool) -> np.ndarray:
    """Cut column per row in a vertical band (old on the left of the cut, new from it on)."""
    h, w = old.shape[:2]
    if straight:
        return np.full(h, w // 2)
    d = np.abs(old.astype(int) - new.astype(int)).sum(axis=2).astype(float)
    p = np.pad(d, 1, mode="edge")
    d = sum(p[dy:dy + h, dx:dx + w] for dy in range(3) for dx in range(3)) / 9
    d[:, :GUARD_PX] = d[:, w - GUARD_PX:] = 1e9
    return min_cut(d)


def seam_stats(c: np.ndarray, path: np.ndarray, x0: int, rows: range, skip: np.ndarray | None = None) -> dict:
    """Colour step across a vertical cut (path[r] = first new column) vs steps beside it.

    skip: optional bool mask over c (painted water); rows whose cut pixel is in it are left out."""
    a = c.astype(int)
    rr = np.array(list(rows))
    if skip is not None and len(rr):
        rr = rr[~skip[rr, x0 + path[rr]]]
    if len(rr) < MIN_SEAM_ROWS:
        return {"step_ratio": None, "rows": len(rr)}
    cx = x0 + path[rr]

    def step(cols):
        return np.abs(a[rr, cols] - a[rr, cols - 1]).sum(axis=1).mean()

    base = np.mean([step(cx + k) for k in STEP_BAND] + [step(cx - k) for k in STEP_BAND])
    return {"step_ratio": round(float(step(cx) / (base + 1e-9)), 2), "rows": len(rr)}


def compose(plan: dict, styled: Path, out: Path, windows: list[dict], straight: bool,
            water_from: Path | None = None) -> None:
    by_ij = {(w["i"], w["j"]): w for w in windows}
    cx0 = min(w["rect_px"][0] for w in windows)
    cy0 = min(w["rect_px"][1] for w in windows)
    cx1 = max(w["rect_px"][2] for w in windows)
    cy1 = max(w["rect_px"][3] for w in windows)
    canvas = np.zeros((cy1 - cy0, cx1 - cx0, 3), np.uint8)
    cuts = []  # (axis, window, neighbour, window x0, y0 in canvas px, path)
    for (i, j) in sorted(by_ij, key=lambda ij: (ij[1], ij[0])):
        w = by_ij[(i, j)]
        x0, y0, x1, y1 = (w["rect_px"][0] - cx0, w["rect_px"][1] - cy0, w["rect_px"][2] - cx0, w["rect_px"][3] - cy0)
        img = Image.open(styled / f"{w['id']}.png").convert("RGB")
        if img.size != (x1 - x0, y1 - y0):
            raise SystemExit(f"{w['id']}: size {img.size} does not match its window")
        new = np.asarray(img)
        old = canvas[y0:y1, x0:x1]
        keep_new = np.ones(new.shape[:2], bool)
        left, up = by_ij.get((i - 1, j)), by_ij.get((i, j - 1))
        if left:
            bw = left["rect_px"][2] - w["rect_px"][0]
            p = band_cut(old[:, :bw], new[:, :bw], straight)
            keep_new &= np.arange(new.shape[1])[None, :] >= p[:, None]
            cuts.append(("v", w["id"], left["id"], x0, y0, p))
        if up:
            bh = up["rect_px"][3] - w["rect_px"][1]
            p = band_cut(old[:bh].transpose(1, 0, 2), new[:bh].transpose(1, 0, 2), straight)
            keep_new &= np.arange(new.shape[0])[:, None] >= p[None, :]
            cuts.append(("h", w["id"], up["id"], x0, y0, p))
        old[keep_new] = new[keep_new]

    out.mkdir(parents=True, exist_ok=True)
    wmask = None
    if water_from:
        wmask = water_mod.water_mask(cut_rect(plan, water_from, (cx0, cy0, cx1, cy1)),
                                     water_mod.ground_m2_per_px(plan))
        wmask = water_mod.grow_into(wmask, canvas)
        water_mod.paint(canvas, wmask, cx0, cy0)
        Image.fromarray(wmask).save(out / "water-mask.png")
        print(f"  water: {wmask.mean():.1%} of the canvas painted flat")
    Image.fromarray(canvas).save(out / "canvas.png")
    marked = Image.fromarray(canvas)
    draw = ImageDraw.Draw(marked)
    report = []
    band = plan["generation"]["band_px"]
    for kind, wid, other, x0, y0, p in cuts:
        n = len(p)
        rows = range(band, n - band)  # away from the corners where the other cuts take over
        if kind == "v":
            st = seam_stats(canvas[y0:y0 + n], p, x0, rows, None if wmask is None else wmask[y0:y0 + n])
            draw.point([(x0 + p[r], y0 + r) for r in range(n)], fill=(255, 0, 0))
        else:
            st = seam_stats(canvas[:, x0:x0 + n].transpose(1, 0, 2), p, y0, rows,
                            None if wmask is None else wmask[:, x0:x0 + n].T)
            draw.point([(x0 + c, y0 + p[c]) for c in range(n)], fill=(255, 0, 0))
        report.append({"cut": f"{other}|{wid}", "axis": kind, **st})
        ratio = st["step_ratio"] if st["step_ratio"] is not None else "- (water)"
        print(f"  {other:>7s} | {wid:7s} {'vertical' if kind == 'v' else 'horizontal'}: step ratio {ratio}")
    marked.save(out / "canvas-marked.png")
    ratios = [r["step_ratio"] for r in report if r["step_ratio"] is not None]
    if ratios:
        print(f"  {len(ratios)} of {len(report)} cuts measured, step ratio mean {np.mean(ratios):.2f} "
              f"median {np.median(ratios):.2f} max {max(ratios):.2f}")
    (out / "cuts.json").write_text(json.dumps({"styled": str(styled), "straight": straight,
                                               "water_from": str(water_from) if water_from else None,
                                               "canvas_rect_px": [cx0, cy0, cx1, cy1], "cuts": report}, indent=1))
    for t in plan["tiles"]:
        sx0, sy0, sx1, sy1 = t["screen_rect_px"]
        ix0, iy0, ix1, iy1 = max(sx0, cx0), max(sy0, cy0), min(sx1, cx1), min(sy1, cy1)
        if ix0 >= ix1 or iy0 >= iy1:
            continue
        tile = np.zeros((sy1 - sy0, sx1 - sx0, 4), np.uint8)  # transparent outside the canvas
        tile[iy0 - sy0:iy1 - sy0, ix0 - sx0:ix1 - sx0, :3] = canvas[iy0 - cy0:iy1 - cy0, ix0 - cx0:ix1 - cx0]
        tile[iy0 - sy0:iy1 - sy0, ix0 - sx0:ix1 - sx0, 3] = 255
        Image.fromarray(tile).save(out / f"{t['id']}.png")
    print(f"  canvas {cx1 - cx0}×{cy1 - cy0} -> {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cut")
    c.add_argument("--renders", type=Path, required=True)
    m = sub.add_parser("compose")
    m.add_argument("--styled", type=Path, required=True)
    m.add_argument("--straight", action="store_true")
    m.add_argument("--water-from", type=Path, help="render tiles: paint open water flat (pipeline/water.py)")
    for p in (c, m):
        p.add_argument("--plan", type=Path, required=True)
        p.add_argument("--out", type=Path, required=True)
        p.add_argument("--windows", help="i0,j0,i1,j1 inclusive (use --windows=-1,...)")
    args = ap.parse_args()

    plan = json.loads(args.plan.read_text())
    if not plan.get("generation"):
        raise SystemExit("plan has no generation windows: add `generation:` to area.yaml and re-plan")
    windows = plan["generation"]["windows"]
    if args.windows:
        i0, j0, i1, j1 = (int(v) for v in args.windows.split(","))
        windows = [w for w in windows if i0 <= w["i"] <= i1 and j0 <= w["j"] <= j1]
    if not windows:
        raise SystemExit("no windows selected")
    if args.cmd == "cut":
        args.out.mkdir(parents=True, exist_ok=True)
        for w in windows:
            Image.fromarray(cut_rect(plan, args.renders, w["rect_px"])).save(args.out / f"{w['id']}.png")
        print(f"  {len(windows)} window renders -> {args.out}: --tiles={','.join(w['id'] for w in windows)}")
    else:
        compose(plan, args.styled, args.out, windows, args.straight, args.water_from)


if __name__ == "__main__":
    main()

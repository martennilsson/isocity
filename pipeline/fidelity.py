"""Score styled tiles against their renders: catch invented scenes and copies of the photo.

Two correlations between render and output (both normalised, 1 = identical):
  layout: 64×64 luminance. Near 0 means the model drew a different scene (reject).
  edges:  256×256 edge map. Very high means the output is the photo with a filter (too weak).
First test (2026-11-04): invented tile 0.07 / 0.01; code pixelation and photo-v1 1.00 / ≥ 0.93;
good photo-v2 redraws 0.96–0.98 / 0.72–0.82. Layout also drops when water is recoloured on
purpose (near-black Google water → bright blue): faithful photo-v4 tiles with a lot of water scored
0.71–0.85, so reject on layout < 0.5 *or* edges < 0.4 (both near 0 when the scene is invented).
Local check: the image is split into cells (8×8 for a tile); per cell, edge correlation where the
render has structure (skipping flat cells and render water, which the model rightly repaints).
The worst cell catches local inventions the global scores average away (2026-11-05: a building
drawn in a park 0.05, an erased construction site 0.09, a harbour invented over a park 0.18; clean
tiles ≥ 0.28). Below --min-cell → CHECK (look at it), not an automatic reject.
Thresholds are a first guess: tune them on more tiles.

Usage:
    uv run python -m pipeline.fidelity --renders data/renders/sodermalm --styled data/styled/<run> \
        [--tiles=0_0,...] [--min-layout 0.5] [--min-edges 0.4] [--max-edges 0.9] [--min-cell 0.2]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from pipeline.training_tiles import water_mask

EDGE_PX = 256  # edge maps are compared at this size


def _norm(a: np.ndarray) -> np.ndarray:
    return (a - a.mean()) / (a.std() + 1e-9)


def scores(render: Image.Image, styled: Image.Image) -> tuple[float, float]:
    def lum(im, n):
        return im.convert("L").resize((n, n), Image.BOX)

    layout = float(np.mean(_norm(np.asarray(lum(render, 64), float)) * _norm(np.asarray(lum(styled, 64), float))))
    e = [_norm(np.asarray(lum(im, EDGE_PX).filter(ImageFilter.FIND_EDGES), float)) for im in (render, styled)]
    return layout, float(np.mean(e[0] * e[1]))


def worst_cell(render: Image.Image, styled: Image.Image, n: int = 8) -> float:
    """Lowest per-cell edge correlation over n×n cells with structure in the render (1.0 if none)."""
    def edges(im):
        return np.asarray(im.convert("L").resize((EDGE_PX, EDGE_PX), Image.BOX).filter(ImageFilter.FIND_EDGES), float)

    er, es = edges(render), edges(styled)
    wm = water_mask(np.asarray(render.convert("RGB")))  # per 16 px block of the full-size render
    s = EDGE_PX // n
    worst = 1.0
    for j in range(n):
        for i in range(n):
            a, b = er[j * s:(j + 1) * s, i * s:(i + 1) * s], es[j * s:(j + 1) * s, i * s:(i + 1) * s]
            wj, wi = wm.shape[0] * j // n, wm.shape[1] * i // n
            water = wm[wj:wj + max(1, wm.shape[0] // n), wi:wi + max(1, wm.shape[1] // n)].mean()
            if a.std() < 6 or water > 0.3:
                continue
            worst = min(worst, float(np.mean((a - a.mean()) * (b - b.mean())) / (a.std() * b.std() + 1e-9)))
    return worst


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--renders", type=Path, required=True)
    ap.add_argument("--styled", type=Path, required=True)
    ap.add_argument("--tiles", help="comma-separated tile ids; default: every <id>.png in --styled")
    ap.add_argument("--min-layout", type=float, default=0.5)
    ap.add_argument("--min-edges", type=float, default=0.4)
    ap.add_argument("--max-edges", type=float, default=0.9)
    ap.add_argument("--min-cell", type=float, default=0.2)
    args = ap.parse_args()

    tiles = args.tiles.split(",") if args.tiles else sorted(
        p.stem for p in args.styled.glob("*.png") if "." not in p.stem and p.stem != "palette")
    for t in tiles:
        render, styled = Image.open(args.renders / f"{t}.png"), Image.open(args.styled / f"{t}.png")
        layout, edges = scores(render, styled)
        cell = worst_cell(render, styled)
        verdict = ("REJECT invented" if layout < args.min_layout or edges < args.min_edges else
                   "WEAK photo copy" if edges > args.max_edges else
                   "CHECK local" if cell < args.min_cell else "ok")
        print(f"  {t:8s} layout {layout:5.2f}  edges {edges:5.2f}  worst cell {cell:5.2f}  {verdict}")


if __name__ == "__main__":
    main()

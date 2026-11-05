"""Score styled tiles against their renders: catch invented scenes and copies of the photo.

Two correlations between render and output (both normalised, 1 = identical):
  layout: 64×64 luminance. Near 0 means the model drew a different scene (reject).
  edges:  256×256 edge map. Very high means the output is the photo with a filter (too weak).
First test (2026-11-04): invented tile 0.07 / 0.01; code pixelation and photo-v1 1.00 / ≥ 0.93;
good photo-v2 redraws 0.96–0.98 / 0.72–0.82. Layout also drops when water is recoloured on
purpose (near-black Google water → bright blue): faithful photo-v4 tiles with a lot of water scored
0.71–0.85, so reject on layout < 0.5 *or* edges < 0.4 (both near 0 when the scene is invented).
Thresholds are a first guess: tune them on more tiles.

Usage:
    uv run python -m pipeline.fidelity --renders data/renders/sodermalm --styled data/styled/<run> \
        [--tiles=0_0,...] [--min-layout 0.5] [--min-edges 0.4] [--max-edges 0.9]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter


def _norm(a: np.ndarray) -> np.ndarray:
    return (a - a.mean()) / (a.std() + 1e-9)


def scores(render: Image.Image, styled: Image.Image) -> tuple[float, float]:
    def lum(im, n):
        return im.convert("L").resize((n, n), Image.BOX)

    layout = float(np.mean(_norm(np.asarray(lum(render, 64), float)) * _norm(np.asarray(lum(styled, 64), float))))
    e = [_norm(np.asarray(lum(im, 256).filter(ImageFilter.FIND_EDGES), float)) for im in (render, styled)]
    return layout, float(np.mean(e[0] * e[1]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--renders", type=Path, required=True)
    ap.add_argument("--styled", type=Path, required=True)
    ap.add_argument("--tiles", help="comma-separated tile ids; default: every <id>.png in --styled")
    ap.add_argument("--min-layout", type=float, default=0.5)
    ap.add_argument("--min-edges", type=float, default=0.4)
    ap.add_argument("--max-edges", type=float, default=0.9)
    args = ap.parse_args()

    tiles = args.tiles.split(",") if args.tiles else sorted(
        p.stem for p in args.styled.glob("*.png") if "." not in p.stem and p.stem != "palette")
    for t in tiles:
        layout, edges = scores(Image.open(args.renders / f"{t}.png"), Image.open(args.styled / f"{t}.png"))
        verdict = ("REJECT invented" if layout < args.min_layout or edges < args.min_edges else
                   "WEAK photo copy" if edges > args.max_edges else "ok")
        print(f"  {t:8s} layout {layout:5.2f}  edges {edges:5.2f}  {verdict}")


if __name__ == "__main__":
    main()

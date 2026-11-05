"""Generate quadrants with finished neighbours as context (Phase 5 seam fix; first test).

A 2×2-quadrant window (1024 px) is cut on the plan grid at quadrant (qx, qy): quadrants listed in
--fill come from the render and get a 2 px red frame round them; the others come from finished
art (--art-dir, tiles named <tile id>.png). The model redraws only the framed part. The filled
quadrants are cut out of its output and written as <qx>_<qy>.png; finished art is never replaced
or blended (crop, never blend). Window and quadrant pixels come only from
`plan_tiles.window_rect_px` and the plan's `screen_rect_px`.

Usage:
    uv run python -m pipeline.infill --plan data/plan/sodermalm.json --renders data/renders/sodermalm \
        --art-dir data/styled/<run> --window 1,0 --fill 2,0 --fill 2,1 --prompt style/prompts/infill-v1.md \
        --out data/infill/<run> --project crafty-haiku-422 [--model gemini-3-pro-image] [--seed 1] [--ref …]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

from pipeline.fidelity import scores, worst_cell
from pipeline.plan_tiles import window_rect_px
from pipeline.stylize import COST_PER_IMAGE, stylize, with_retries

FRAME_PX = 2


def cut(plan: dict, src_dir: Path, rect: tuple[int, int, int, int]) -> np.ndarray:
    """Pixels of screen rect `rect` assembled from the tiles in src_dir that cover it."""
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


def frame(a: np.ndarray, rect: tuple[int, int, int, int]) -> None:
    """Draw a FRAME_PX pure red border along the inside of rect (window-local px)."""
    x0, y0, x1, y1 = rect
    a[y0:y0 + FRAME_PX, x0:x1] = a[y1 - FRAME_PX:y1, x0:x1] = (255, 0, 0)
    a[y0:y1, x0:x0 + FRAME_PX] = a[y0:y1, x1 - FRAME_PX:x1] = (255, 0, 0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--renders", type=Path, required=True)
    ap.add_argument("--art-dir", type=Path, required=True)
    ap.add_argument("--window", required=True, help="qx,qy of the window's top-left quadrant")
    ap.add_argument("--fill", action="append", required=True, help="qx,qy of a quadrant to generate")
    ap.add_argument("--prompt", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--location", default="global")
    ap.add_argument("--model", default="gemini-3-pro-image")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--ref", type=Path, action="append", default=[])
    args = ap.parse_args()

    from google import genai

    plan = json.loads(args.plan.read_text())
    q = plan["quadrant_size_px"]
    n = plan["quadrants_per_side"]
    wqx, wqy = (int(v) for v in args.window.split(","))
    wrect = window_rect_px(wqx, wqy, n, n, q)
    fills = [tuple(int(v) for v in f.split(",")) for f in args.fill]
    for fx, fy in fills:
        if not (wqx <= fx < wqx + n and wqy <= fy < wqy + n):
            raise SystemExit(f"--fill {fx},{fy} is outside window {wqx},{wqy}")

    art = cut(plan, args.art_dir, wrect)
    render = cut(plan, args.renders, wrect)
    win = art.copy()
    local = {}
    for fx, fy in fills:
        r = window_rect_px(fx, fy, 1, 1, q)
        lr = (r[0] - wrect[0], r[1] - wrect[1], r[2] - wrect[0], r[3] - wrect[1])
        local[(fx, fy)] = lr
        win[lr[1]:lr[3], lr[0]:lr[2]] = render[lr[1]:lr[3], lr[0]:lr[2]]
    # One frame round the union of the fill quadrants (they are adjacent in a 2×2 window).
    rs = list(local.values())
    frame(win, (min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs)))

    refs = []
    for r in args.ref:
        im = Image.open(r).convert("RGB")
        im.thumbnail((1024, 1024), Image.LANCZOS)
        refs.append(im)
    client = genai.Client(vertexai=True, project=args.project, location=args.location)
    img, _ = with_retries(lambda: stylize(client, args.model, args.prompt.read_text(), Image.fromarray(win),
                                          args.seed, refs), f"w{wqx}_{wqy}-s{args.seed}")
    img = img.convert("RGB")
    if img.size != (win.shape[1], win.shape[0]):
        img = img.resize((win.shape[1], win.shape[0]), Image.NEAREST)
    gen = np.asarray(img)

    args.out.mkdir(parents=True, exist_ok=True)
    stem = f"w{wqx}_{wqy}-s{args.seed}"
    Image.fromarray(win).save(args.out / f"{stem}.input.png")
    Image.fromarray(gen).save(args.out / f"{stem}.raw.png")
    result = art.copy()  # finished art stays; only the fill quadrants are taken from the output
    for (fx, fy), lr in local.items():
        quad = gen[lr[1]:lr[3], lr[0]:lr[2]]
        Image.fromarray(quad).save(args.out / f"{fx}_{fy}.s{args.seed}.png")
        result[lr[1]:lr[3], lr[0]:lr[2]] = quad
    Image.fromarray(result).save(args.out / f"{stem}.result.png")
    keep = np.ones(art.shape[:2], bool)
    for lr in local.values():
        keep[lr[1]:lr[3], lr[0]:lr[2]] = False
    drift = float(np.abs(gen[keep].astype(int) - art[keep].astype(int)).mean())
    fx0, fy0 = min(r[0] for r in rs), min(r[1] for r in rs)
    fx1, fy1 = max(r[2] for r in rs), max(r[3] for r in rs)
    fr, fg = (Image.fromarray(a[fy0:fy1, fx0:fx1]) for a in (render, gen))
    layout, edges = scores(fr, fg)
    cell = worst_cell(fr, fg, n=4 * (fx1 - fx0) // q)
    (args.out / f"{stem}.json").write_text(json.dumps({
        "window": [wqx, wqy], "fill": fills, "window_rect_px": wrect, "art_dir": str(args.art_dir),
        "prompt": str(args.prompt), "model": args.model, "seed": args.seed, "refs": [str(r) for r in args.ref],
        "context_drift": round(drift, 2),
        "fill_fidelity": {"layout": round(layout, 3), "edges": round(edges, 3), "worst_cell": round(cell, 3)},
        "est_cost_usd": COST_PER_IMAGE.get(args.model, 0.0),
    }, indent=1))
    print(f"  {stem}: context drift {drift:.1f}; filled part layout {layout:.2f} edges {edges:.2f} "
          f"worst cell {cell:.2f} -> {args.out}")


if __name__ == "__main__":
    main()

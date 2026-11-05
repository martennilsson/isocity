"""Choose training tiles from a (large) training-area plan.

  candidates: checkerboard of plan tiles ((x + y) even), so no two chosen tiles share an edge
              and no training windows overlap. Writes one tile id per line.
  pick:       after rendering the candidates, drop near-pure water / empty renders and any
              --exclude'd ids (e.g. Google mesh artefacts), then thin evenly to --n tiles. Water with shoreline is kept (the plan over-represents water).

Usage:
    uv run python -m pipeline.training_tiles candidates --plan data/plan/training.json \
        --out data/plan/training-candidates.txt
    (render them: cd render && npm run render -- render --plan ../data/plan/training.json \
        --out ../data/renders/training --tiles-file ../data/plan/training-candidates.txt)
    uv run python -m pipeline.training_tiles pick --plan data/plan/training.json \
        --candidates data/plan/training-candidates.txt --renders data/renders/training \
        --n 150 --out data/plan/training-tiles.txt
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

BLOCK_PX = 16


def water_fraction(img: np.ndarray) -> float:
    """Share of 16x16 blocks that look like open water: dark, blue-ish and smooth.
    Tuned on the Söder pilot renders (slate roofs are blue-ish too but textured)."""
    return float(water_mask(img).mean())


def water_mask(img: np.ndarray) -> np.ndarray:
    """Per 16x16 block: True where the render looks like open water (see water_fraction)."""
    a = img[..., :3].astype(float)
    h, w, _ = a.shape
    h, w = h - h % BLOCK_PX, w - w % BLOCK_PX
    blk = a[:h, :w].reshape(h // BLOCK_PX, BLOCK_PX, w // BLOCK_PX, BLOCK_PX, 3)
    blk = blk.transpose(0, 2, 1, 3, 4).reshape(h // BLOCK_PX, w // BLOCK_PX, -1, 3)
    med = np.median(blk, axis=2)
    sd = blk.std(axis=2).mean(axis=-1)
    return (med[..., 2] - med[..., 0] > 18) & (med.max(axis=-1) < 110) & (sd < 9)


def empty_fraction(img: np.ndarray) -> float:
    return float((img[..., 3] < 255).mean()) if img.shape[-1] == 4 else 0.0


def candidates(plan: dict) -> list[str]:
    return [t["id"] for t in sorted(plan["tiles"], key=lambda t: (t["y"], t["x"])) if (t["x"] + t["y"]) % 2 == 0]


def pick(scores: list[tuple[str, float, float]], n: int, max_water: float, max_empty: float) -> list[str]:
    """scores: (id, water, empty) in plan order. Keep usable tiles, then thin evenly to n."""
    usable = [s for s in scores if s[1] <= max_water and s[2] <= max_empty]
    if len(usable) <= n:
        return [s[0] for s in usable]
    idx = np.linspace(0, len(usable) - 1, n).round().astype(int)
    return [usable[i][0] for i in idx]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("candidates")
    c.add_argument("--plan", type=Path, required=True)
    c.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("pick")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--candidates", type=Path, required=True)
    p.add_argument("--renders", type=Path, required=True)
    p.add_argument("--n", type=int, default=150)
    p.add_argument("--max-water", type=float, default=0.75)
    p.add_argument("--max-empty", type=float, default=0.01)
    p.add_argument("--exclude", default="", help="comma-separated tile ids to leave out (bad source data)")
    p.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    plan = json.loads(args.plan.read_text())
    if args.cmd == "candidates":
        ids = candidates(plan)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("\n".join(ids) + "\n")
        print(f"{len(ids)} of {len(plan['tiles'])} tiles -> {args.out}")
        return

    excluded = set(filter(None, args.exclude.split(",")))
    ids = [t for t in args.candidates.read_text().split() if t not in excluded]
    scores, missing = [], []
    for tid in ids:
        f = args.renders / f"{tid}.png"
        if not f.exists():
            missing.append(tid)
            continue
        img = np.asarray(Image.open(f).convert("RGBA"))
        scores.append((tid, water_fraction(img), empty_fraction(img)))
    if missing:
        print(f"warning: {len(missing)} candidates not rendered: {' '.join(missing[:10])}...")
    chosen = pick(scores, args.n, args.max_water, args.max_empty)
    args.out.write_text("\n".join(chosen) + "\n")
    (args.out.with_suffix(".scores.json")).write_text(
        json.dumps({s[0]: {"water": round(s[1], 3), "empty": round(s[2], 4)} for s in scores}, indent=1)
    )
    w = np.array([s[1] for s in scores])
    print(
        f"{len(scores)} rendered: {(w > args.max_water).sum()} mostly water, "
        f"{sum(s[2] > args.max_empty for s in scores)} with missing data; "
        f"picked {len(chosen)} -> {args.out}"
    )
    sel = {s[0]: s[1] for s in scores}
    cw = np.array([sel[t] for t in chosen])
    print(f"  picked water share: none(<5%) {(cw < 0.05).sum()}, some(5-40%) {((cw >= 0.05) & (cw < 0.4)).sum()}, lots(>=40%) {(cw >= 0.4).sum()}")


if __name__ == "__main__":
    main()

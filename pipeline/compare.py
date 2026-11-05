"""Side-by-side comparison sheet for style runs (Phase 4.0): one row per tile, one column per run.

Each --run is LABEL=DIR with <tile>.png files (the render directory works as a run too). Optional
--crop x,y,w,h (render pixels) to compare details at 1:1; --scale is a nearest-neighbour factor
applied after cropping (pixel art: never smooth). Missing tiles are left grey.

Usage:
    uv run python -m pipeline.compare --tiles=0_0,0_-1 --out data/compare/4.0.png \
        --run render=data/renders/sodermalm --run pixel-f4=data/styled/4.0a-pixel-f4-c32 \
        [--crop 256,256,512,512] [--scale 1]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

LABEL_H = 28
GAP = 8


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tiles", required=True, help="comma-separated tile ids (use --tiles=-1_0,...)")
    ap.add_argument("--run", action="append", required=True, help="LABEL=DIR, repeat for each column")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--crop", help="x,y,w,h in render pixels")
    ap.add_argument("--scale", type=float, default=0.5, help="nearest-neighbour scale after cropping")
    args = ap.parse_args()

    runs = [(r.split("=", 1)[0], Path(r.split("=", 1)[1])) for r in args.run]
    tiles = args.tiles.split(",")
    crop = tuple(int(v) for v in args.crop.split(",")) if args.crop else None
    w, h = (crop[2], crop[3]) if crop else (1024, 1024)
    cw, ch = int(w * args.scale), int(h * args.scale)
    font = ImageFont.load_default(size=18)
    sheet = Image.new("RGB", (GAP + len(runs) * (cw + GAP), LABEL_H + len(tiles) * (ch + LABEL_H + GAP)), "white")
    draw = ImageDraw.Draw(sheet)
    for j, (label, _) in enumerate(runs):
        draw.text((GAP + j * (cw + GAP), 4), label, fill="black", font=font)
    for i, tid in enumerate(tiles):
        y = LABEL_H + i * (ch + LABEL_H + GAP)
        draw.text((GAP, y + 4), tid, fill="black", font=font)
        for j, (_, d) in enumerate(runs):
            x = GAP + j * (cw + GAP)
            f = d / f"{tid}.png"
            if not f.exists():
                draw.rectangle((x, y + LABEL_H, x + cw - 1, y + LABEL_H + ch - 1), fill=(200, 200, 200))
                continue
            im = Image.open(f).convert("RGB")
            if crop:
                im = im.crop((crop[0], crop[1], crop[0] + crop[2], crop[1] + crop[3]))
            sheet.paste(im.resize((cw, ch), Image.NEAREST), (x, y + LABEL_H))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(args.out)
    print(f"{sheet.size[0]}x{sheet.size[1]} -> {args.out}")


if __name__ == "__main__":
    main()

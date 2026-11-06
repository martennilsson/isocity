"""The "head" image: one file that always shows the latest result, so progress can be followed.

Writes <out>/HEAD.png: the whole mosaic scaled down (preview only) on top, and below it a crop at
1:1 (one output pixel = one screen pixel = metres_per_pixel on the ground) and the same crop
enlarged ×3 (nearest neighbour), so the real level of detail is visible. Plus <out>/HEAD.md with
the caption. Re-run after every change that alters the result.

Usage:
    uv run python -m pipeline.head --mosaic data/mosaic/<run> --area config/area.yaml \
        --out data --note "what changed" [--detail 3600,1400]
--detail x,y: centre of the detail crop in canvas pixels.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import yaml
from PIL import Image, ImageDraw

Image.MAX_IMAGE_PIXELS = None
WIDTH = 2000  # HEAD.png width
DETAIL_H = 700  # height of the 1:1 crop strip
ZOOM = 3


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mosaic", type=Path, required=True)
    ap.add_argument("--area", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--note", required=True)
    ap.add_argument("--detail", default=None, help="x,y centre of the detail crop (canvas px)")
    args = ap.parse_args()

    cfg = yaml.safe_load(args.area.read_text())
    mpp = cfg["scale"]["metres_per_pixel"]
    canvas = Image.open(args.mosaic / "canvas.png").convert("RGB")
    cw, ch = canvas.size
    cx, cy = (int(v) for v in args.detail.split(",")) if args.detail else (cw // 2, ch // 2)

    factor = max(1, -(-cw // WIDTH))
    overview = canvas.reduce(factor)  # preview only; the product itself is never smoothed
    ow, oh = overview.size

    half_w = WIDTH // 2
    d1 = canvas.crop((cx - half_w // 2, cy - DETAIL_H // 2, cx + half_w // 2, cy + DETAIL_H // 2))
    zw, zh = half_w // ZOOM, DETAIL_H // ZOOM
    d3 = canvas.crop((cx - zw // 2, cy - zh // 2, cx + zw // 2, cy + zh // 2)).resize(
        (zw * ZOOM, zh * ZOOM), Image.NEAREST)

    bar = 28
    head = Image.new("RGB", (WIDTH, bar + oh + bar + DETAIL_H), (24, 24, 24))
    draw = ImageDraw.Draw(head)
    when = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    draw.text((8, 8), f"{when}  {args.mosaic}  -  {args.note}", fill=(240, 240, 240))
    head.paste(overview, ((WIDTH - ow) // 2, bar))
    # where the detail is, on the overview
    ox, oy = (WIDTH - ow) // 2, bar
    draw.rectangle([ox + (cx - half_w // 2) / factor, oy + (cy - DETAIL_H // 2) / factor,
                    ox + (cx + half_w // 2) / factor, oy + (cy + DETAIL_H // 2) / factor], outline=(255, 0, 255), width=2)
    y = bar + oh
    draw.text((8, y + 8), f"left: 1:1, one pixel = {mpp} m      right: the centre of it x{ZOOM} "
                          f"(nearest neighbour)      whole canvas {cw}x{ch} px, scaled 1/{factor} above",
              fill=(240, 240, 240))
    head.paste(d1, (0, y + bar))
    head.paste(d3, (half_w, y + bar))
    args.out.mkdir(parents=True, exist_ok=True)
    head.save(args.out / "HEAD.png")
    cuts = args.mosaic / "cuts.json"
    extra = ""
    if cuts.exists():
        r = [c["step_ratio"] for c in json.loads(cuts.read_text())["cuts"] if c["step_ratio"] is not None]
        if r:
            extra = f"\nSeam step ratio (≈1 invisible): mean {sum(r) / len(r):.2f}, max {max(r):.2f}\n"
    (args.out / "HEAD.md").write_text(
        f"# HEAD — {when}\n\n{args.note}\n\nSource: `{args.mosaic}` ({cw}×{ch} px, {mpp} m/px).\n{extra}")
    print(f"  -> {args.out / 'HEAD.png'}")


if __name__ == "__main__":
    main()

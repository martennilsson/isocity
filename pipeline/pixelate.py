"""Pixel-grid post-process for styled tiles (kept as a storage/performance option, Phase 6).

Downscale by --factor, keeping outlines: per block, the darkest pixel where the block holds an
outline (big luminance range and a pixel well below the block mean), else the mean. Keeps 1 px
cartoon outlines and window pixels (thin *bright* details such as lane markings are still lost).
Optionally snap to one palette shared by all tiles, then nearest-neighbour upscale back to the
render size so outputs stitch on the plan grid. Made for photo-v4 outputs with --factor 2.
--colors 0 (default) skips the palette. Otherwise the palette is built from all --tiles together and saved
as palette.png in --out; an existing palette.png is reused unless --force. (A 64-colour palette
from 4 tiles shifted colours visibly, e.g. a green tree went pink: needs a palette from many
tiles.)

Usage:
    uv run python -m pipeline.pixelate --renders data/styled/<run> --tiles=-1_-1,0_-1,... \
        --out data/styled/<run>-px2 [--factor 2] [--colors 0]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image

LUMA = np.array([0.299, 0.587, 0.114])


def make_palette(images: list[Image.Image], colors: int) -> Image.Image:
    """One palette for all tiles, so colours match across tile borders."""
    sheet = Image.new("RGB", (sum(i.width for i in images), max(i.height for i in images)))
    x = 0
    for i in images:
        sheet.paste(i.convert("RGB"), (x, 0))
        x += i.width
    return sheet.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)


def downscale(img: Image.Image, factor: int, edge_range: float = 40, edge_dark: float = 15) -> Image.Image:
    a = np.asarray(img.convert("RGB"), float)
    h, w = a.shape[0] // factor, a.shape[1] // factor
    b = a[:h * factor, :w * factor].reshape(h, factor, w, factor, 3).transpose(0, 2, 1, 3, 4)
    b = b.reshape(h, w, factor * factor, 3)
    lum = b @ LUMA
    mean = b.mean(axis=2)
    dark = np.take_along_axis(b, lum.argmin(axis=2)[..., None, None], axis=2)[:, :, 0]
    edge = (lum.max(axis=2) - lum.min(axis=2) > edge_range) & (lum.min(axis=2) < mean @ LUMA - edge_dark)
    return Image.fromarray(np.where(edge[..., None], dark, mean).clip(0, 255).astype(np.uint8))


def pixelate(img: Image.Image, factor: int, palette: Image.Image | None) -> Image.Image:
    small = downscale(img, factor)
    if palette is not None:
        small = small.quantize(palette=palette, dither=Image.Dither.NONE).convert("RGB")
    return small.resize(img.size, Image.NEAREST)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--renders", type=Path, required=True, help="directory with <id>.png styled tiles")
    ap.add_argument("--tiles", required=True, help="comma-separated tile ids (use --tiles=-1_0,...)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--factor", type=int, default=2, help="output pixel size in render pixels")
    ap.add_argument("--colors", type=int, default=0, help="shared palette size (0 = no palette)")
    ap.add_argument("--force", action="store_true", help="rebuild the palette and overwrite outputs")
    args = ap.parse_args()

    tiles = args.tiles.split(",")
    srcs = {t: Image.open(args.renders / f"{t}.png").convert("RGB") for t in tiles}
    args.out.mkdir(parents=True, exist_ok=True)
    palette_file = args.out / "palette.png"
    palette = None
    if not args.colors:
        pass
    elif palette_file.exists() and not args.force:
        palette = Image.open(palette_file)
    else:
        # Build the palette from the downscaled tiles: that is what gets snapped.
        palette = make_palette([downscale(s, args.factor) for s in srcs.values()], args.colors)
        palette.save(palette_file)
    for t, src in srcs.items():
        dst = args.out / f"{t}.png"
        if dst.exists() and not args.force:
            print(f"  {t}: exists, skipped")
            continue
        pixelate(src, args.factor, palette).save(dst)
        print(f"  {t} -> {dst}")


if __name__ == "__main__":
    main()

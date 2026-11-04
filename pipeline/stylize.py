"""Turn render tiles into pixel art with a Gemini image model on Vertex AI (step-1 test).

Auth: Application Default Credentials (`gcloud auth application-default login`), no API key.
Each output gets a sidecar JSON (model, prompt version, seed, cost estimate) next to it.
Resumable: existing outputs are skipped unless --force.

Usage:
    uv run python -m pipeline.stylize --renders data/renders/sodermalm-osm \
        --tiles=0_0,-1_0 --prompt style/prompts/v1.md --out data/styled/osm-v1 \
        --project crafty-haiku-422 [--model gemini-3.1-flash-image] [--location global] [--seed 1]
        [--ref style/references/a.png --ref ...] [--hints-dir style/prompts/landmarks/<area>]
        [--pixel 2 --palette-colors 48]

--ref: style reference images, sent after the render (downscaled to 1024 px).
--hints-dir: per-tile extra prompt text from <hints-dir>-<tile>.md if present (landmarks).
--pixel N: also write <id>.pixel.png: box-downscale by N, snap to one palette shared by all
  tiles (taken from the first output of the run), nearest-neighbour upscale.
Tiles whose render has no water get an explicit "no water" line (the model likes to invent quays).
"""

from __future__ import annotations

import argparse
import io
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

# USD per generated 1K image (Vertex global endpoint, read 2026-11-04). For the log only.
COST_PER_IMAGE = {"gemini-3.1-flash-image": 0.067, "gemini-3.1-flash-lite-image": 0.034,
                  "gemini-3-pro-image": 0.134}


def _png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def stylize(client, model: str, prompt: str, image: Image.Image, seed: int | None, refs=()):
    from google.genai import types

    parts = [types.Part.from_bytes(data=_png(image), mime_type="image/png")]
    parts += [types.Part.from_bytes(data=_png(r), mime_type="image/png") for r in refs]
    resp = client.models.generate_content(
        model=model,
        contents=parts + [prompt],
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(aspect_ratio="1:1", image_size="1K"),
            seed=seed,
        ),
    )
    for cand in resp.candidates or []:
        for part in cand.content.parts or []:
            if part.inline_data and part.inline_data.data:
                return Image.open(io.BytesIO(part.inline_data.data)), resp.usage_metadata
    reason = resp.candidates[0].finish_reason if resp.candidates else resp.prompt_feedback
    raise RuntimeError(f"no image in response ({reason})")


def water_fraction(render: Image.Image) -> float:
    """Share of render pixels in the scene's water colour (osm_scene.py LAYERS water #3f6d8e)."""
    import numpy as np

    a = np.asarray(render.convert("RGB")).astype(int)
    target = np.array([0x3F, 0x6D, 0x8E])
    return float((np.abs(a - target).sum(axis=-1) < 60).mean())


def make_palette(images: list[Image.Image], colors: int) -> Image.Image:
    """One palette for all tiles, so colours match across tile borders."""
    w = sum(i.width for i in images)
    h = max(i.height for i in images)
    sheet = Image.new("RGB", (w, h))
    x = 0
    for i in images:
        sheet.paste(i.convert("RGB"), (x, 0))
        x += i.width
    return sheet.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)


def pixelate(img: Image.Image, factor: int, palette: Image.Image) -> Image.Image:
    small = img.convert("RGB").resize((img.width // factor, img.height // factor), Image.BOX)
    q = small.quantize(palette=palette, dither=Image.Dither.NONE).convert("RGB")
    return q.resize(img.size, Image.NEAREST)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--renders", type=Path, required=True, help="directory with <id>.png render tiles")
    ap.add_argument("--tiles", required=True, help="comma-separated tile ids (use --tiles=-1_0,...)")
    ap.add_argument("--prompt", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--project", required=True, help="Google Cloud project with Vertex AI enabled")
    ap.add_argument("--location", default="global")
    ap.add_argument("--model", default="gemini-3.1-flash-image")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--ref", type=Path, action="append", default=[], help="style reference image")
    ap.add_argument("--hints-dir", type=Path, help="prefix for per-tile hint files: <prefix>-<tile>.md")
    ap.add_argument("--pixel", type=int, default=0, help="pixel-art post-process factor (0 = off)")
    ap.add_argument("--palette-colors", type=int, default=48)
    args = ap.parse_args()

    from google import genai

    client = genai.Client(vertexai=True, project=args.project, location=args.location)
    prompt = args.prompt.read_text()
    refs = []
    for r in args.ref:
        im = Image.open(r).convert("RGB")
        im.thumbnail((1024, 1024), Image.LANCZOS)
        refs.append(im)
    # Shared palette, taken from the first output (references skew it, e.g. Rio orange) and saved,
    # so resumed runs keep using the same colours.
    palette_file = args.out / "palette.png"
    palette = Image.open(palette_file) if (args.pixel and palette_file.exists()) else None
    args.out.mkdir(parents=True, exist_ok=True)
    total = 0.0
    for tid in args.tiles.split(","):
        dst = args.out / f"{tid}.png"
        if dst.exists() and not args.force:
            print(f"  {tid}: exists, skipped")
            continue
        src = Image.open(args.renders / f"{tid}.png")
        tile_prompt = prompt
        hint = Path(f"{args.hints_dir}-{tid}.md") if args.hints_dir else None
        if hint and hint.exists():
            tile_prompt += "\n\n" + hint.read_text()
        if water_fraction(src) == 0:
            tile_prompt += "\n\nThis image contains NO water at all: draw none, not even at the edges."
        t0 = time.time()
        img = None
        for attempt in range(6):  # 429 RESOURCE_EXHAUSTED: shared quota, back off and retry
            try:
                img, usage = stylize(client, args.model, tile_prompt, src, args.seed, refs)
                break
            except Exception as e:
                if "429" in str(e) and attempt < 5:
                    wait = 20 * 2**attempt
                    print(f"  {tid}: rate limited, retrying in {wait} s")
                    time.sleep(wait)
                    continue
                print(f"  {tid}: FAILED {str(e)[:200]}")
                break
        if img is None:
            continue
        if img.size != src.size:
            print(f"  {tid}: model returned {img.size}, resized to {src.size} (nearest)")
            img = img.resize(src.size, Image.NEAREST)
        img.save(dst)
        if args.pixel:
            if palette is None:
                palette = make_palette([img], args.palette_colors)
                palette.save(palette_file)
            pixelate(img, args.pixel, palette).save(dst.with_suffix(".pixel.png"))
        cost = COST_PER_IMAGE.get(args.model, 0.0)
        total += cost
        dst.with_suffix(".json").write_text(json.dumps({
            "tile": tid, "source": str(args.renders / f"{tid}.png"), "model": args.model,
            "location": args.location, "prompt": str(args.prompt), "seed": args.seed,
            "refs": [str(r) for r in args.ref], "hint": str(hint) if hint and hint.exists() else None,
            "pixel": args.pixel, "palette_colors": args.palette_colors if args.pixel else None,
            "usage": usage.model_dump(exclude_none=True) if usage else None,
            "est_cost_usd": cost, "seconds": round(time.time() - t0, 1),
            "created": datetime.now(timezone.utc).isoformat(),
        }, indent=1))
        print(f"  {tid}: {time.time() - t0:.1f} s -> {dst}")
    print(f"estimated cost: ${total:.2f}")


if __name__ == "__main__":
    main()

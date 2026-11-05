"""Turn render tiles into pixel art with an image model (Phase 3 style examples).

Backends:
  vertex  (default): Gemini image models on Vertex AI. Auth: Application Default Credentials
          (`gcloud auth application-default login`), no API key. Needs --project.
  comfyui: Qwen-Image-Edit-2509 (Nunchaku 4-bit) on a ComfyUI server, e.g. the GPU PC set up by
          gpu/ (--comfyui-url http://<pc>:8188). Free; the render is image 1, --ref images 2–3.
Each output gets a sidecar JSON (model, prompt version, seed, cost estimate) next to it.
Resumable: existing outputs are skipped unless --force.

Usage:
    uv run python -m pipeline.stylize --renders data/renders/sodermalm \
        --tiles=0_0,-1_0 --prompt style/prompts/<version>.md --out data/styled/<run> \
        --project crafty-haiku-422 [--model gemini-3.1-flash-image] [--location global] [--seed 1]
        [--ref style/references/a.png --ref ...] [--hints-dir style/prompts/landmarks/<area>]

--ref: style reference images, sent after the render (downscaled to 1024 px).
--hints-dir: per-tile extra prompt text from <hints-dir>-<tile>.md if present (landmarks).
Tiles whose render has (almost) no water get an explicit "no water" line: the model likes to
invent quays at tile edges. Water is detected with pipeline.training_tiles.water_fraction.
"""

from __future__ import annotations

import argparse
import io
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from pipeline.training_tiles import water_fraction

NO_WATER_BELOW = 0.03  # dry Google tiles score 0.6–2.3 % (shadows), tiles with water ≥ 7.5 %

# USD per generated 1K image (Vertex global endpoint, read 2026-11-04). For the log only.
COST_PER_IMAGE = {"gemini-3.1-flash-image": 0.067, "gemini-3.1-flash-lite-image": 0.034,
                  "gemini-3-pro-image": 0.134}


def _png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def with_retries(fn, label: str, tries: int = 6):
    """Call fn(); on 429 RESOURCE_EXHAUSTED (shared quota) back off 20, 40, 80 … s and retry."""
    for attempt in range(tries):
        try:
            return fn()
        except Exception as e:
            if "429" not in str(e) or attempt == tries - 1:
                raise
            wait = 20 * 2**attempt
            print(f"  {label}: rate limited, retrying in {wait} s")
            time.sleep(wait)


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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--renders", type=Path, required=True, help="directory with <id>.png render tiles")
    ap.add_argument("--tiles", required=True, help="comma-separated tile ids (use --tiles=-1_0,...)")
    ap.add_argument("--prompt", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--backend", choices=["vertex", "comfyui"], default="vertex")
    ap.add_argument("--project", help="vertex: Google Cloud project with Vertex AI enabled")
    ap.add_argument("--location", default="global")
    ap.add_argument("--comfyui-url", help="comfyui: server URL, e.g. http://192.168.86.32:8188")
    ap.add_argument("--steps", type=int, default=4, help="comfyui: sampler steps (Lightning 4-step model)")
    ap.add_argument("--model", help="vertex default gemini-3.1-flash-image; comfyui default: the 4-bit "
                                    "Qwen-Image-Edit-2509 Lightning file")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--ref", type=Path, action="append", default=[], help="style reference image")
    ap.add_argument("--hints-dir", type=Path, help="prefix for per-tile hint files: <prefix>-<tile>.md")
    args = ap.parse_args()

    prompt = args.prompt.read_text()
    refs = []
    for r in args.ref:
        im = Image.open(r).convert("RGB")
        im.thumbnail((1024, 1024), Image.LANCZOS)
        refs.append(im)

    if args.backend == "vertex":
        if not args.project:
            ap.error("--backend vertex needs --project")
        from google import genai

        client = genai.Client(vertexai=True, project=args.project, location=args.location)
        args.model = args.model or "gemini-3.1-flash-image"

        def generate(tid, tile_prompt, src):
            img, usage = stylize(client, args.model, tile_prompt, src, args.seed, refs)
            return img, (usage.model_dump(exclude_none=True) if usage else None)
    else:
        if not args.comfyui_url:
            ap.error("--backend comfyui needs --comfyui-url")
        from pipeline.comfyui import DEFAULT_MODEL, ComfyUI, edit_workflow

        if len(refs) > 2:
            ap.error("comfyui: Qwen-Image-Edit-2509 takes at most 2 --ref images (3 images in total)")
        comfy = ComfyUI(args.comfyui_url)
        args.model = args.model or DEFAULT_MODEL
        ref_names = [comfy.upload(im, f"isocity-ref{i}.png") for i, im in enumerate(refs)]

        def generate(tid, tile_prompt, src):
            name = comfy.upload(src, f"isocity-{tid}.png")
            wf = edit_workflow(tile_prompt, [name] + ref_names, seed=args.seed, model=args.model, steps=args.steps)
            out = comfy.run(wf)
            if not out:
                raise RuntimeError("ComfyUI returned no image")
            return out[0], None
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
        if water_fraction(np.asarray(src.convert("RGBA"))) < NO_WATER_BELOW:
            tile_prompt += "\n\nThis image contains NO water at all: draw none, not even at the edges."
        t0 = time.time()
        try:
            img, usage = with_retries(lambda: generate(tid, tile_prompt, src), tid)
        except Exception as e:
            print(f"  {tid}: FAILED {str(e)[:200]}")
            continue
        if img.size != src.size:
            print(f"  {tid}: model returned {img.size}, resized to {src.size} (nearest)")
            img = img.resize(src.size, Image.NEAREST)
        img.save(dst)
        cost = COST_PER_IMAGE.get(args.model, 0.0) if args.backend == "vertex" else 0.0
        total += cost
        dst.with_suffix(".json").write_text(json.dumps({
            "tile": tid, "source": str(args.renders / f"{tid}.png"), "backend": args.backend,
            "model": args.model, "location": args.location if args.backend == "vertex" else args.comfyui_url,
            "steps": args.steps if args.backend == "comfyui" else None,
            "prompt": str(args.prompt), "seed": args.seed,
            "refs": [str(r) for r in args.ref], "hint": str(hint) if hint and hint.exists() else None,
            "usage": usage,
            "est_cost_usd": cost, "seconds": round(time.time() - t0, 1),
            "created": datetime.now(timezone.utc).isoformat(),
        }, indent=1))
        print(f"  {tid}: {time.time() - t0:.1f} s -> {dst}")
    print(f"estimated cost: ${total:.2f}")


if __name__ == "__main__":
    main()

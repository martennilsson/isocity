"""Turn render tiles into pixel art with an image model (Phase 3 style examples).

Backends:
  vertex  (default): Gemini image models on Vertex AI. Auth: Application Default Credentials
          (`gcloud auth application-default login`), no API key. Needs --project.
  comfyui: a ComfyUI server, e.g. the GPU PC set up by gpu/ (--comfyui-url http://<pc>:8188). Free.
          --workflow qwen-edit-2509 (default): Qwen-Image-Edit-2509 (Nunchaku 4-bit); the render is
            image 1, --ref images 2–3.
          --workflow qwen21: Qwen-Image-2.1-Turbo image edit (+ --lora, e.g. tiny-london-v3 with
            --frame and style/prompts/tiny-london.md).
          --workflow sdxl: Illustrious XL img2img + ControlNet union (+ --lora pixelart, --denoise,
            --cn-type, --negative).
Each output gets a sidecar JSON (model, prompt version, seed, cost estimate) next to it.
Resumable: existing outputs are skipped unless --force.

Usage:
    uv run python -m pipeline.stylize --renders data/renders/sodermalm \
        --tiles=0_0,-1_0 --prompt style/prompts/<version>.md --out data/styled/<run> \
        --project crafty-haiku-422 [--model gemini-3.1-flash-image] [--location global] [--seed 1]
        [--ref style/references/a.png --ref ...] [--hints-dir style/prompts/landmarks/<area>]
        [--pixel 2 --palette-colors 48]

--ref: style reference images, sent after the render (downscaled to 1024 px).
--frame: draw a 2 px pure red border round the input (the tiny.london LoRA was trained on it).
--hints-dir: per-tile extra prompt text from <hints-dir>-<tile>.md if present (landmarks).
--pixel N: also write <id>.pixel.png: box-downscale by N, snap to one palette shared by all
  tiles (taken from the first output of the run), nearest-neighbour upscale.
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


def add_frame(img: Image.Image, width: int = 2) -> Image.Image:
    """Pure red (255, 0, 0) border round the image, as in tiny.london's training inputs."""
    out = img.convert("RGB").copy()
    a = np.asarray(out).copy()
    a[:width, :] = a[-width:, :] = a[:, :width] = a[:, -width:] = (255, 0, 0)
    return Image.fromarray(a)


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
    ap.add_argument("--backend", choices=["vertex", "comfyui"], default="vertex")
    ap.add_argument("--project", help="vertex: Google Cloud project with Vertex AI enabled")
    ap.add_argument("--location", default="global")
    ap.add_argument("--comfyui-url", help="comfyui: server URL, e.g. http://192.168.86.32:8188")
    ap.add_argument("--steps", type=int, help="comfyui: sampler steps (qwen-edit-2509: 4, sdxl: 28; "
                                              "qwen21 uses the turbo template's 8 sigmas)")
    ap.add_argument("--workflow", choices=["qwen-edit-2509", "qwen21", "sdxl"], default="qwen-edit-2509")
    ap.add_argument("--lora", help="comfyui: LoRA file name in the server's models/loras")
    ap.add_argument("--lora-strength", type=float, default=1.0)
    ap.add_argument("--frame", action="store_true", help="2 px red border round the input (tiny.london)")
    ap.add_argument("--cfg", type=float, help="comfyui: CFG (qwen: 1, sdxl: 6)")
    ap.add_argument("--denoise", type=float, default=0.5, help="sdxl: img2img denoise")
    ap.add_argument("--negative", default="", help="sdxl: negative prompt")
    ap.add_argument("--cn-type", default="canny/lineart/anime_lineart/mlsd",
                    help="sdxl: ControlNet union type, e.g. canny/lineart/anime_lineart/mlsd or tile")
    ap.add_argument("--cn-strength", type=float, default=0.8)
    ap.add_argument("--model", help="vertex default gemini-3.1-flash-image; comfyui default: the 4-bit "
                                    "Qwen-Image-Edit-2509 Lightning file")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--ref", type=Path, action="append", default=[], help="style reference image")
    ap.add_argument("--hints-dir", type=Path, help="prefix for per-tile hint files: <prefix>-<tile>.md")
    ap.add_argument("--pixel", type=int, default=0, help="pixel-art post-process factor (0 = off)")
    ap.add_argument("--palette-colors", type=int, default=48)
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
        from pipeline import comfyui as cu

        if len(refs) > 2 or (refs and args.workflow != "qwen-edit-2509"):
            ap.error("comfyui: --ref only with --workflow qwen-edit-2509, at most 2 (3 images in total)")
        comfy = cu.ComfyUI(args.comfyui_url)
        ref_names = [comfy.upload(im, f"isocity-ref{i}.png") for i, im in enumerate(refs)]

        def build(tile_prompt, name):
            if args.workflow == "qwen-edit-2509":
                args.model = args.model or cu.DEFAULT_MODEL
                return cu.edit_workflow(tile_prompt, [name] + ref_names, seed=args.seed, model=args.model,
                                        steps=args.steps or 4, cfg=args.cfg or 1.0)
            if args.workflow == "qwen21":
                args.model = args.model or cu.QWEN21_TURBO
                return cu.qwen21_edit_workflow(tile_prompt, name, seed=args.seed, lora=args.lora,
                                               lora_strength=args.lora_strength, model=args.model,
                                               cfg=args.cfg or 1.0)
            args.model = args.model or cu.SDXL_CHECKPOINT
            return cu.sdxl_controlnet_workflow(tile_prompt, args.negative, name, seed=args.seed, lora=args.lora,
                                               lora_strength=args.lora_strength, denoise=args.denoise,
                                               cn_type=args.cn_type, cn_strength=args.cn_strength,
                                               steps=args.steps or 28, cfg=args.cfg or 6.0, checkpoint=args.model)

        problems = cu.check_nodes(comfy, build(prompt, "check.png"))
        if problems:
            ap.error("ComfyUI server can't run this workflow:\n  " + "\n  ".join(problems))

        def generate(tid, tile_prompt, src):
            name = comfy.upload(src, f"isocity-{tid}.png")
            out = comfy.run(build(tile_prompt, name))
            if not out:
                raise RuntimeError("ComfyUI returned no image")
            return out[0], None
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
        if args.frame:
            src = add_frame(src)
        tile_prompt = prompt
        hint = Path(f"{args.hints_dir}-{tid}.md") if args.hints_dir else None
        if hint and hint.exists():
            tile_prompt += "\n\n" + hint.read_text()
        if not args.frame and water_fraction(np.asarray(src.convert("RGBA"))) < NO_WATER_BELOW:
            tile_prompt += "\n\nThis image contains NO water at all: draw none, not even at the edges."
        t0 = time.time()
        img = None
        for attempt in range(6):  # 429 RESOURCE_EXHAUSTED: shared quota, back off and retry
            try:
                img, usage = generate(tid, tile_prompt, src)
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
        cost = COST_PER_IMAGE.get(args.model, 0.0) if args.backend == "vertex" else 0.0
        total += cost
        dst.with_suffix(".json").write_text(json.dumps({
            "tile": tid, "source": str(args.renders / f"{tid}.png"), "backend": args.backend,
            "model": args.model, "location": args.location if args.backend == "vertex" else args.comfyui_url,
            "steps": args.steps if args.backend == "comfyui" else None,
            "workflow": args.workflow if args.backend == "comfyui" else None,
            "lora": args.lora, "lora_strength": args.lora_strength if args.lora else None, "frame": args.frame,
            "cfg": args.cfg, "denoise": args.denoise if args.workflow == "sdxl" else None,
            "negative": args.negative or None,
            "cn": [args.cn_type, args.cn_strength] if args.workflow == "sdxl" else None,
            "prompt": str(args.prompt), "seed": args.seed,
            "refs": [str(r) for r in args.ref], "hint": str(hint) if hint and hint.exists() else None,
            "pixel": args.pixel, "palette_colors": args.palette_colors if args.pixel else None,
            "usage": usage,
            "est_cost_usd": cost, "seconds": round(time.time() - t0, 1),
            "created": datetime.now(timezone.utc).isoformat(),
        }, indent=1))
        print(f"  {tid}: {time.time() - t0:.1f} s -> {dst}")
    print(f"estimated cost: ${total:.2f}")


if __name__ == "__main__":
    main()

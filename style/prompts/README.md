# Prompts

Each `.md` here is sent to the model verbatim (no front matter).

- `photo-v1.md` — Google render → pixel art, for edit models (Gemini, Qwen-Image-Edit-2509).
- `tiny-london.md` — the fixed caption of the tiny.london LoRA (huggingface.co/ktoyame/tiny-london-lora,
  read 2026-11-04); use with `--workflow qwen21 --lora tiny-london-v3.safetensors --frame`.
- `pixelart-sdxl-v1.md` — tag-style prompt for Illustrious XL + "Pixel art style LoRa" (trigger
  "pixel art"); use with `--workflow sdxl --lora pixelart.safetensors` and a negative prompt such as
  "blurry, smooth, photo, realistic, gradient, jpeg artifacts, text, watermark, people, characters".

"""Minimal client for a ComfyUI server running Qwen-Image-Edit-2509 (Nunchaku 4-bit).

The server runs on the GPU PC (see gpu/): ComfyUI listening on the LAN, no auth. This module
uploads images, queues an image-edit workflow and downloads the result, using only the
standard library. The workflow mirrors Nunchaku's "qwen-image-edit-2509-lightning" example.
"""

from __future__ import annotations

import io
import json
import time
import urllib.parse
import urllib.request
import uuid

from PIL import Image

DEFAULT_MODEL = "svdq-int4_r32-qwen-image-edit-2509-lightning-4steps-251115.safetensors"
TEXT_ENCODER = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
VAE = "qwen_image_vae.safetensors"


class ComfyUI:
    def __init__(self, url: str, timeout_s: float = 900):
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s
        self.client_id = uuid.uuid4().hex

    def _req(self, path: str, data: bytes | None = None, headers: dict | None = None):
        req = urllib.request.Request(self.url + path, data=data, headers=headers or {})
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()

    def upload(self, image: Image.Image, name: str) -> str:
        """Upload a PNG to ComfyUI's input folder; returns the name to use in LoadImage."""
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="PNG")
        boundary = uuid.uuid4().hex
        body = b"".join([
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
            "Content-Type: image/png\r\n\r\n".encode(), buf.getvalue(), b"\r\n",
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n".encode(),
            f"--{boundary}--\r\n".encode(),
        ])
        res = json.loads(self._req("/upload/image", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}))
        return res["name"] if not res.get("subfolder") else f"{res['subfolder']}/{res['name']}"

    def run(self, workflow: dict) -> list[Image.Image]:
        """Queue a workflow (API format), wait for it, return the output images."""
        body = json.dumps({"prompt": workflow, "client_id": self.client_id}).encode()
        res = json.loads(self._req("/prompt", body, {"Content-Type": "application/json"}))
        if res.get("node_errors"):
            raise RuntimeError(f"workflow rejected: {res['node_errors']}")
        pid = res["prompt_id"]
        start = time.time()
        while time.time() - start < self.timeout_s:
            hist = json.loads(self._req(f"/history/{pid}"))
            if pid in hist:
                entry = hist[pid]
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    msgs = [m for m in status.get("messages", []) if m[0] == "execution_error"]
                    raise RuntimeError(f"ComfyUI error: {msgs[-1][1].get('exception_message') if msgs else status}")
                images = []
                for out in entry.get("outputs", {}).values():
                    for im in out.get("images", []):
                        q = urllib.parse.urlencode({"filename": im["filename"], "subfolder": im["subfolder"],
                                                    "type": im["type"]})
                        images.append(Image.open(io.BytesIO(self._req(f"/view?{q}"))))
                if images or status.get("completed"):
                    return images
            time.sleep(1.0)
        raise TimeoutError(f"ComfyUI did not finish within {self.timeout_s} s")


def edit_workflow(prompt: str, images: list[str], seed: int, model: str = DEFAULT_MODEL, steps: int = 4,
                  cfg: float = 1.0, megapixels: float = 1.0, cpu_offload: str = "auto",
                  blocks_on_gpu: int = 1) -> dict:
    """Qwen-Image-Edit-2509 workflow: images[0] is the image to edit (its size sets the output
    size), images[1:3] are optional extra images (e.g. style references)."""
    if not 1 <= len(images) <= 3:
        raise ValueError("Qwen-Image-Edit-2509 takes 1–3 images")
    wf: dict = {
        "dit": {"class_type": "NunchakuQwenImageDiTLoader",
                "inputs": {"model_name": model, "cpu_offload": cpu_offload, "num_blocks_on_gpu": blocks_on_gpu,
                           "use_pin_memory": "disable"}},
        "shift": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["dit", 0], "shift": 3.0}},
        "norm": {"class_type": "CFGNorm", "inputs": {"model": ["shift", 0], "strength": 1.0}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": TEXT_ENCODER, "type": "qwen_image",
                                                          "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
    }
    refs = []
    for i, name in enumerate(images):
        wf[f"load{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        refs.append([f"load{i}", 0])
    wf["scale"] = {"class_type": "ImageScaleToTotalPixels",
                   "inputs": {"image": refs[0], "upscale_method": "lanczos", "megapixels": megapixels,
                              "resolution_steps": 1}}
    refs[0] = ["scale", 0]
    img_inputs = {f"image{i + 1}": r for i, r in enumerate(refs)}
    wf["pos"] = {"class_type": "TextEncodeQwenImageEditPlus",
                 "inputs": {"clip": ["clip", 0], "vae": ["vae", 0], "prompt": prompt, **img_inputs}}
    wf["neg"] = {"class_type": "TextEncodeQwenImageEditPlus",
                 "inputs": {"clip": ["clip", 0], "vae": ["vae", 0], "prompt": "", **img_inputs}}
    wf["latent"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["scale", 0], "vae": ["vae", 0]}}
    wf["sample"] = {"class_type": "KSampler",
                    "inputs": {"model": ["norm", 0], "seed": seed, "steps": steps, "cfg": cfg,
                               "sampler_name": "euler", "scheduler": "simple", "positive": ["pos", 0],
                               "negative": ["neg", 0], "latent_image": ["latent", 0], "denoise": 1.0}}
    wf["decode"] = {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}}
    wf["save"] = {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": "isocity"}}
    return wf


# --- Phase 4.0 comparison workflows ------------------------------------------------------------

QWEN21_TURBO = "qwen_image_2.1_turbo_int8_convrot.safetensors"
QWEN21_TEXT_ENCODER = "qwen3vl_8b_int8_convrot.safetensors"
QWEN21_VAE = "qwen_image_2.1_vae_bf16.safetensors"
# Turbo sigmas from Comfy-Org's "Qwen Image 2.1 Turbo: Image edit" template (8 steps).
QWEN21_TURBO_SIGMAS = "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0"

SDXL_CHECKPOINT = "Illustrious-XL-v0.1.safetensors"
SDXL_CONTROLNET = "controlnet-union-sdxl-promax.safetensors"


def qwen21_edit_workflow(prompt: str, image: str, seed: int, lora: str | None = None, lora_strength: float = 1.0,
                         model: str = QWEN21_TURBO, cfg: float = 1.0, resolution: int = 1024,
                         sigmas: str = QWEN21_TURBO_SIGMAS) -> dict:
    """Qwen-Image-2.1(-Turbo) image edit, after Comfy-Org's turbo image-edit template with the
    prompt enhancer off. The edit latent comes from TextEncodeQwenImage21 (output 2), so the
    output has the input's size. Optional LoRA (model only), e.g. tiny-london-v3."""
    wf: dict = {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": model, "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": QWEN21_TEXT_ENCODER, "type": "qwen_image",
                                                          "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": QWEN21_VAE}},
        "load": {"class_type": "LoadImage", "inputs": {"image": image}},
    }
    model_ref = ["unet", 0]
    if lora:
        wf["lora"] = {"class_type": "LoraLoaderModelOnly",
                      "inputs": {"model": model_ref, "lora_name": lora, "strength_model": lora_strength}}
        model_ref = ["lora", 0]
    wf["cache"] = {"class_type": "QwenImage21Cache", "inputs": {"model": model_ref, "device": "auto", "dtype": "default"}}
    wf["enc"] = {"class_type": "TextEncodeQwenImage21",
                 "inputs": {"clip": ["clip", 0], "vae": ["vae", 0], "prompt": prompt, "negative_prompt": "",
                            "resolution": resolution, "images.image_1": ["load", 0]}}
    wf["guider"] = {"class_type": "CFGGuider",
                    "inputs": {"model": ["cache", 0], "positive": ["enc", 0], "negative": ["enc", 1], "cfg": cfg}}
    wf["sigmas"] = {"class_type": "ManualSigmas", "inputs": {"sigmas": sigmas}}
    wf["sampler"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}}
    wf["noise"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}}
    wf["sample"] = {"class_type": "SamplerCustomAdvanced",
                    "inputs": {"noise": ["noise", 0], "guider": ["guider", 0], "sampler": ["sampler", 0],
                               "sigmas": ["sigmas", 0], "latent_image": ["enc", 2]}}
    wf["decode"] = {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}}
    wf["save"] = {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": "isocity"}}
    return wf


def sdxl_controlnet_workflow(prompt: str, negative: str, image: str, seed: int, lora: str | None = None,
                             lora_strength: float = 1.0, denoise: float = 0.5, cn_type: str = "canny/lineart/anime_lineart/mlsd",
                             cn_strength: float = 0.8, steps: int = 28, cfg: float = 6.0,
                             checkpoint: str = SDXL_CHECKPOINT, controlnet: str = SDXL_CONTROLNET) -> dict:
    """SDXL (Illustrious) img2img + ControlNet union: the render is both the init image (denoise
    sets how much is redrawn) and the control image (canny edges, or the render itself for tile)."""
    wf: dict = {
        "ckpt": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "load": {"class_type": "LoadImage", "inputs": {"image": image}},
    }
    model_ref, clip_ref = ["ckpt", 0], ["ckpt", 1]
    if lora:
        wf["lora"] = {"class_type": "LoraLoader",
                      "inputs": {"model": model_ref, "clip": clip_ref, "lora_name": lora,
                                 "strength_model": lora_strength, "strength_clip": lora_strength}}
        model_ref, clip_ref = ["lora", 0], ["lora", 1]
    wf["pos"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": clip_ref, "text": prompt}}
    wf["neg"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": clip_ref, "text": negative}}
    if cn_type == "tile":
        control = ["load", 0]
    else:
        wf["canny"] = {"class_type": "Canny", "inputs": {"image": ["load", 0], "low_threshold": 0.2,
                                                           "high_threshold": 0.5}}
        control = ["canny", 0]
    wf["cn"] = {"class_type": "ControlNetLoader", "inputs": {"control_net_name": controlnet}}
    wf["cntype"] = {"class_type": "SetUnionControlNetType", "inputs": {"control_net": ["cn", 0], "type": cn_type}}
    wf["apply"] = {"class_type": "ControlNetApplyAdvanced",
                   "inputs": {"positive": ["pos", 0], "negative": ["neg", 0], "control_net": ["cntype", 0],
                              "image": control, "strength": cn_strength, "start_percent": 0.0,
                              "end_percent": 1.0, "vae": ["ckpt", 2]}}
    wf["latent"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["load", 0], "vae": ["ckpt", 2]}}
    wf["sample"] = {"class_type": "KSampler",
                    "inputs": {"model": model_ref, "seed": seed, "steps": steps, "cfg": cfg,
                               "sampler_name": "euler_ancestral", "scheduler": "normal",
                               "positive": ["apply", 0], "negative": ["apply", 1],
                               "latent_image": ["latent", 0], "denoise": denoise}}
    wf["decode"] = {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["ckpt", 2]}}
    wf["save"] = {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": "isocity"}}
    return wf


def check_nodes(comfy: "ComfyUI", workflow: dict) -> list[str]:
    """Input names the server doesn't know, per node (catches ComfyUI version drift before a run)."""
    info = json.loads(comfy._req("/object_info"))
    problems = []
    for nid, node in workflow.items():
        spec = info.get(node["class_type"])
        if spec is None:
            problems.append(f"{nid}: node type {node['class_type']} not installed")
            continue
        known = set()
        for group in ("required", "optional"):
            known |= set(spec.get("input", {}).get(group, {}))
        unknown = [k for k in node["inputs"] if k not in known and not k.startswith("images.")]
        if unknown:
            problems.append(f"{nid} ({node['class_type']}): unknown inputs {unknown}; has {sorted(known)}")
    return problems

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

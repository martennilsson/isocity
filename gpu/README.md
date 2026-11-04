# GPU PC: local image model server

ComfyUI + Qwen-Image-Edit-2509 (Nunchaku 4-bit, Lightning 4-step) on the Windows PC
(RTX 3070 8 GB, 32 GB RAM), reachable from the Mac at `http://<pc>:8188` (LAN only, no auth).

1. SSH from the Mac: `../docs/windows-ssh-setup.md` (one copy-paste script on the PC).
2. Install ComfyUI, PyTorch 2.10 (CUDA 12.8) and Nunchaku 1.2.1 in `C:\ai\ComfyUI`:
   `scp setup-comfyui.ps1 nilss@<pc>:C:/ai/` then
   `ssh nilss@<pc> powershell -ExecutionPolicy Bypass -File C:\ai\setup-comfyui.ps1`
3. Models (~20 GB, Apache 2.0, resumable): `download-models.ps1` the same way. Run it as a
   scheduled task if the SSH session might drop; check SHA-256 against Hugging Face.
4. Server: `install-service.ps1` registers the `ComfyUI` scheduled task (starts at boot, S4U,
   no stored password) and a firewall rule for TCP 8188 on the Private profile, local subnet.
   Log: `C:\ai\comfyui.log`. Remove with `Unregister-ScheduledTask ComfyUI` and
   `Remove-NetFirewallRule -Name comfyui`.

The PC must not sleep while serving (set by the user in Windows power settings).

Use from the Mac:
`uv run python -m pipeline.stylize --backend comfyui --comfyui-url http://<pc>:8188 --renders … --tiles=… --prompt … --out …`
(~45 s for the first image while the model loads, then ~22 s per 1024² tile.)

## Phase 4.0 style comparison models (~27 GB more)

`download-models-4.0.ps1` (same way, as a scheduled task; SHA-256 checked):
tiny.london LoRA + Qwen-Image-2.1-Turbo int8 convrot + Qwen3-VL 8B text encoder + VAE (all
Qwen Research licence, non-commercial), Illustrious XL v0.1 (SDXL) and xinsir ControlNet union
SDXL promax. Qwen-Image-2.1 needs a ComfyUI with the `TextEncodeQwenImage21` / `QwenImage21Cache`
nodes and INT8 ConvRot support; `stylize.py` checks the server's `/object_info` before a run.

The civitai "Pixel art style LoRa | Illustrious" (version 1454408, `pixelart.safetensors`,
SHA-256 `c7c3e006…eaab`) needs a civitai login: put an API token (civitai.com → Account settings →
API keys) in `.env` as `CIVITAI_API_TOKEN`. Before running the script, copy it to the PC as
`C:\ai\civitai-token.txt`; the script deletes it at the end. (scp of the 218 MB file from the Mac
stalled at 0 bytes on 2026-11-04, while small files copy fine; downloading on the PC avoids that.)

**Status 2026-11-04: paused.** The PC has a RAM fault: Windows Memory Diagnostic found errors, the
same unchanged 8.7 GB file hashed differently on each read, and the first two download runs failed
SHA-256 checks (even files that had passed once). Two unclean reboots the same day. Not worth buying
memory for this; the project went back to Nano Banana Pro on Vertex AI. Before using the PC again:
fix the RAM, then re-hash every model file, including the Qwen-Image-Edit-2509 ones.

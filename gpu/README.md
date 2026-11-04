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

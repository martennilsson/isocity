# Install ComfyUI + Nunchaku (4-bit Qwen-Image-Edit) on the Windows GPU PC, in C:\ai\ComfyUI.
# Run over SSH from the Mac:  scp gpu/setup-comfyui.ps1 nilss@<pc>:C:/ai/  then
#   ssh nilss@<pc> powershell -ExecutionPolicy Bypass -File C:\ai\setup-comfyui.ps1
# Re-runnable. Versions are pinned to match Nunchaku's pre-built Windows wheel.
$ErrorActionPreference = 'Stop'
$uv = "$env:USERPROFILE\.local\bin\uv.exe"
$root = 'C:\ai\ComfyUI'
$py = "$root\.venv\Scripts\python.exe"
$torchIndex = 'https://download.pytorch.org/whl/cu128'
$nunchakuWheel = 'https://github.com/nunchaku-tech/nunchaku/releases/download/v1.2.1/nunchaku-1.2.1%2Bcu12.8torch2.10-cp312-cp312-win_amd64.whl'

function Step($t) { Write-Output "`n== $t ==" }

Step 'ComfyUI source'
if (-not (Test-Path $root)) { git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git $root }
else { git -C $root pull --ff-only }

Step 'Python 3.12 environment'
if (-not (Test-Path $py)) { & $uv venv --python 3.12 "$root\.venv" }

Step 'PyTorch 2.10 + CUDA 12.8'
& $uv pip install --python $py 'torch==2.10.*' torchvision torchaudio --index-url $torchIndex

Step 'ComfyUI requirements'
& $uv pip install --python $py -r "$root\requirements.txt"

Step 'ComfyUI-nunchaku node + Nunchaku engine'
$node = "$root\custom_nodes\ComfyUI-nunchaku"
if (-not (Test-Path $node)) { git clone --depth 1 https://github.com/nunchaku-tech/ComfyUI-nunchaku.git $node }
else { git -C $node pull --ff-only }
if (Test-Path "$node\requirements.txt") { & $uv pip install --python $py -r "$node\requirements.txt" }
& $uv pip install --python $py $nunchakuWheel

Step 'Check'
& $py -c "import torch, nunchaku; print('torch', torch.__version__, 'cuda', torch.cuda.is_available(), torch.cuda.get_device_name(0)); print('nunchaku', nunchaku.__version__)"

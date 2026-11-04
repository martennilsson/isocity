# Download the Qwen-Image-Edit-2509 (Nunchaku 4-bit) model files into ComfyUI's model folders.
# All Apache 2.0, not gated. Resumable: re-run after an interrupted download (curl -C -).
# RTX 30-series: int4 (fp4 builds need Blackwell); r32 = smaller; Lightning 4-step = fast.
$ErrorActionPreference = 'Stop'
$m = 'C:\ai\ComfyUI\models'
$files = @(
  @{ dir = 'diffusion_models'; url = 'https://huggingface.co/nunchaku-tech/nunchaku-qwen-image-edit-2509/resolve/main/lightning-251115/svdq-int4_r32-qwen-image-edit-2509-lightning-4steps-251115.safetensors' },
  @{ dir = 'text_encoders';    url = 'https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors' },
  @{ dir = 'vae';              url = 'https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files/vae/qwen_image_vae.safetensors' }
)
foreach ($f in $files) {
  $dest = Join-Path (Join-Path $m $f.dir) ($f.url.Split('/')[-1])
  New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
  Write-Output "== $dest"
  curl.exe -L --fail --retry 5 -C - -s -S -o $dest $f.url
  Write-Output ("   {0:N1} GB" -f ((Get-Item $dest).Length / 1GB))
}

# Phase 4.0 style comparison: download the extra model files into ComfyUI's model folders.
# Resumable (curl -C -): re-run after an interrupted download. Checks SHA-256 (Hugging Face LFS
# etags, read 2026-11-04) and sizes: a complete file is skipped, a short one is resumed, a wrong
# one deleted so the next run fetches it again. (First run 2026-11-04: 3 of 7 files arrived
# corrupt, presumably curl --retry after a dropped connection; just re-run until all are ok.)
# Run as a scheduled task so it survives SSH drops (see gpu/README.md). ~27 GB in total.
#
#  b. tiny.london LoRA on Qwen-Image-2.1-Turbo (Qwen Research licence: non-commercial)
#     - DiT int8 convrot, the weights the LoRA was trained on
#     - Qwen3-VL 8B text encoder (int8 convrot), as in Comfy-Org's turbo image-edit template
#  c. Illustrious XL v0.1 (SDXL) + xinsir ControlNet union SDXL promax (Apache 2.0) for the
#     civitai "Pixel art style LoRa | Illustrious". That LoRA needs a civitai login: put an API
#     token in C:\ai\civitai-token.txt (the Mac copies it from .env); it is deleted at the end.
$ErrorActionPreference = 'Stop'
$m = 'C:\ai\ComfyUI\models'
$hf = 'https://huggingface.co'
$files = @(
  @{ dir = 'loras';            size = 159436024; sha = '8ea43b74457bb96c794ece578c800d39af7d689ccbcab7128eb4efe982a3c3a0'; url = "$hf/ktoyame/tiny-london-lora/resolve/main/tiny-london-v3.safetensors" },
  @{ dir = 'vae';              size = 675509688; sha = 'bb21f7473051e1ac368515dd3f2e15cd44d7a11748ee8823e1ddca3e4876b7c9'; url = "$hf/Comfy-Org/Qwen-Image-2.1/resolve/main/vae/qwen_image_2.1_vae_bf16.safetensors" },
  @{ dir = 'diffusion_models'; size = 7256787829; sha = '8144a0182964aa8403ad6dbc011db837304167d235161b9aabaa8532e967314c'; url = "$hf/Comfy-Org/Qwen-Image-2.1/resolve/main/diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors" },
  @{ dir = 'text_encoders';    size = 9350798360; sha = '8bfd0f6e12abf2d2d697ecc888e5e90b0d6741d6708f05799f53afa560452e8f'; url = "$hf/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_int8_convrot.safetensors" },
  @{ dir = 'checkpoints';      size = 6938040760; sha = '3e15ba00387db678ab4a099f75771c4f5ac67fda9e7100a01d263eaf30145aa9'; url = "$hf/OnomaAIResearch/Illustrious-xl-early-release-v0/resolve/main/Illustrious-XL-v0.1.safetensors" },
  @{ dir = 'loras';            size = 228504772; sha = 'c7c3e006cab8f4dd17f7cff99d44eeb70eba823dcf4a41b8bc4aeba40b87eaab'; url = 'https://civitai.com/api/download/models/1454408?fileId=1355553'; name = 'pixelart.safetensors'; civitai = $true },
  @{ dir = 'controlnet';       size = 2513342408; sha = '9fae2e50cb431bfcbe05822b59ec2228df545ef27f711dea8949e9f4ed9f7cdc'; url = "$hf/xinsir/controlnet-union-sdxl-1.0/resolve/main/diffusion_pytorch_model_promax.safetensors"; name = 'controlnet-union-sdxl-promax.safetensors' }
)
foreach ($f in $files) {
  $name = if ($f.name) { $f.name } else { $f.url.Split('/')[-1] }
  $dest = Join-Path (Join-Path $m $f.dir) $name
  New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
  Write-Output "== $dest"
  if ((Test-Path $dest) -and (Get-Item $dest).Length -eq $f.size) { Write-Output "   complete, checking" }
  elseif ($f.civitai) {
    $tok = 'C:\ai\civitai-token.txt'
    if (-not (Test-Path $tok)) { Write-Output "   no $tok, skipped"; continue }
    if (Test-Path $dest) { Remove-Item $dest }  # the signed redirect URL doesn't resume
    curl.exe -L --fail --retry 5 -s -S -H "Authorization: Bearer $((Get-Content $tok -Raw).Trim())" -o $dest $f.url
  } else {
    curl.exe -L --fail --retry 5 -C - -s -S -o $dest $f.url
  }
  $len = if (Test-Path $dest) { (Get-Item $dest).Length } else { 0 }
  if ($len -lt $f.size) { Write-Output "   incomplete ($len of $($f.size) bytes, curl exit $LASTEXITCODE): re-run to resume"; continue }
  if ($len -gt $f.size) { Write-Output "   too large ($len of $($f.size) bytes), deleted"; Remove-Item $dest; continue }
  $sha = (Get-FileHash -Algorithm SHA256 $dest).Hash.ToLower()
  if ($sha -ne $f.sha) { Write-Output "   SHA-256 MISMATCH ($sha), deleted"; Remove-Item $dest; continue }
  Write-Output ("   ok, {0:N2} GB" -f ((Get-Item $dest).Length / 1GB))
}
if (Test-Path 'C:\ai\civitai-token.txt') { Remove-Item 'C:\ai\civitai-token.txt' }
Write-Output "== done"

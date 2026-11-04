# Run ComfyUI as a background server on the GPU PC: a scheduled task that starts at boot
# (no login needed, no stored password: S4U logon) and listens on port 8188 for the home
# network only. Run as Administrator (an SSH session of an admin user is elevated).
#   ssh nilss@<pc> powershell -ExecutionPolicy Bypass -File C:\ai\install-service.ps1
$ErrorActionPreference = 'Stop'
$root = 'C:\ai\ComfyUI'
$py = "$root\.venv\Scripts\python.exe"
$port = 8188
$name = 'ComfyUI'

# Firewall: TCP 8188 inbound, Private network profile, same subnet only. ComfyUI has no login,
# so never expose this port on the router.
Remove-NetFirewallRule -Name comfyui -ErrorAction SilentlyContinue
New-NetFirewallRule -Name comfyui -DisplayName 'ComfyUI (LAN only)' -Enabled True -Direction Inbound `
  -Protocol TCP -LocalPort $port -Action Allow -Profile Private -RemoteAddress LocalSubnet | Out-Null

$cmd = "/c `"`"$py`" main.py --listen 0.0.0.0 --port $port --disable-auto-launch > C:\ai\comfyui.log 2>&1`""
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $cmd -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 0) -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -StartWhenAvailable
Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $name
Write-Output "ComfyUI task registered and started; log: C:\ai\comfyui.log, port $port (LAN only)"

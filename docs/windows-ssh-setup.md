# SSH setup for the Windows GPU PC

1. Press **Win + X** and choose **Terminal (Admin)** (or **Windows PowerShell (Admin)**). Click *Yes*.
2. Click the **copy** button at the top right of the box below, paste into the window
   (right-click or Ctrl+V) and press **Enter**. It takes a minute or two.
3. When it says **OK** in green, tell Claude on the Mac "done". If it says **PROBLEM** in red,
   send a photo of the window.

The script is safe to run again. It installs the OpenSSH server (winget, or the official zip as
fallback), clears a leftover `sshd` firewall rule, opens port 22 for the home (Private) network
only, marks the home network as Private, and adds the Mac's public key. Same file:
[`docs/setup-ssh.ps1`](setup-ssh.ps1).

```powershell
# One-shot SSH setup for the Windows GPU PC. Paste into PowerShell *as Administrator*.
# Safe to run again: every step checks first. Home network only (port 22 on Private networks).
$ErrorActionPreference = 'Continue'
$key = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDvaBU4lUn6xQGJG8I++lKncFIWPEeOgaZOmEv9KjdTV'
function Say($t) { Write-Host "`n== $t ==" -ForegroundColor Cyan }

Say '1. OpenSSH server'
if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
    # A leftover 'sshd' firewall rule makes installers fail with "already exists": remove it first.
    Remove-NetFirewallRule -Name sshd -ErrorAction SilentlyContinue
    netsh advfirewall firewall delete rule name="sshd" | Out-Null
    netsh advfirewall firewall delete rule name="OpenSSH SSH Server (sshd)" | Out-Null
    Write-Host 'Installing with winget...'
    winget install --id Microsoft.OpenSSH.Preview --source winget --silent --accept-package-agreements --accept-source-agreements
}
if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
    Write-Host 'winget did not work, installing from the official zip instead...'
    $rel = Invoke-RestMethod 'https://api.github.com/repos/PowerShell/Win32-OpenSSH/releases/latest'
    $zip = ($rel.assets | Where-Object name -eq 'OpenSSH-Win64.zip').browser_download_url
    Invoke-WebRequest $zip -OutFile "$env:TEMP\OpenSSH-Win64.zip"
    Expand-Archive "$env:TEMP\OpenSSH-Win64.zip" -DestinationPath "$env:ProgramFiles" -Force
    $dir = "$env:ProgramFiles\OpenSSH"
    if (Test-Path "$env:ProgramFiles\OpenSSH-Win64") {
        Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue
        Rename-Item "$env:ProgramFiles\OpenSSH-Win64" 'OpenSSH'
    }
    & powershell.exe -ExecutionPolicy Bypass -File "$dir\install-sshd.ps1"
}
Set-Service sshd -StartupType Automatic
Start-Service sshd
Get-Service sshd | Format-Table Name, Status, StartType -AutoSize

Say '2. Firewall: TCP 22, home (Private) network only'
Remove-NetFirewallRule -Name sshd -ErrorAction SilentlyContinue
netsh advfirewall firewall delete rule name="sshd" | Out-Null
try {
    New-NetFirewallRule -Name sshd -DisplayName 'sshd' -Enabled True -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow -Profile Private -ErrorAction Stop | Out-Null
} catch {
    netsh advfirewall firewall add rule name="sshd" dir=in action=allow protocol=TCP localport=22 profile=private | Out-Null
}
Write-Host 'Firewall rule for port 22 set.'

Say '3. Home network marked as Private'
Get-NetConnectionProfile | Where-Object NetworkCategory -eq 'Public' | ForEach-Object {
    Set-NetConnectionProfile -InterfaceIndex $_.InterfaceIndex -NetworkCategory Private
    Write-Host "Changed '$($_.Name)' from Public to Private"
}
Get-NetConnectionProfile | Format-Table Name, NetworkCategory -AutoSize

Say "4. The Mac's public key"
$isAdmin = (Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction SilentlyContinue).Name -contains "$env:COMPUTERNAME\$env:USERNAME"
if ($isAdmin) {
    $f = "$env:ProgramData\ssh\administrators_authorized_keys"
    New-Item -ItemType Directory -Force "$env:ProgramData\ssh" | Out-Null
} else {
    $f = "$env:USERPROFILE\.ssh\authorized_keys"
    New-Item -ItemType Directory -Force "$env:USERPROFILE\.ssh" | Out-Null
}
if (-not ((Test-Path $f) -and (Select-String -Path $f -SimpleMatch $key -Quiet))) { Add-Content -Path $f -Value $key }
if ($isAdmin) { icacls $f /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null }
Write-Host "Key in $f"
Restart-Service sshd

Say 'Summary'
$listening = Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue
if ($listening) { Write-Host 'OK: SSH is listening on port 22. Tell Claude on the Mac: done.' -ForegroundColor Green }
else { Write-Host 'PROBLEM: nothing is listening on port 22. Send Claude on the Mac a photo of this window.' -ForegroundColor Red }
```

Never open port 22 on the router or to the internet: this is for the home network only.

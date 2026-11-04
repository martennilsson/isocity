# Prompt for Claude: set up SSH on my Windows PC

*Copy everything below the line into a new Claude chat on the Windows PC.*

---

Hi Claude! Please guide me step by step, one step at a time, waiting for me to report back
after each step. I'm on my Windows 10/11 PC and want my Mac to log in to it over SSH with a key
(no password), so tools on the Mac can install and run ComfyUI on this PC's NVIDIA RTX 3070.
Both machines are on the same home network. Explain briefly what each step does, and help me
troubleshoot if something fails (give me the commands to check). Run everything in PowerShell
**as Administrator**.

What I've learned so far:
- `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0` (the tildes are part of the
  feature name: name~arch~language~version) ran for 10+ minutes without finishing, so I aborted
  it with Ctrl+C. It may still finish in the background via Windows Update.
- A firewall rule named `sshd` seems to exist already: `New-NetFirewallRule -Name sshd …` fails
  with "Cannot create a file when that file already exists", although
  `Get-NetFirewallRule -Name *ssh*` shows nothing.
- From the Mac, the PC answers ping, but port 22 times out.

The steps:

1. **Check whether the aborted Windows install finished after all** (two SSH servers would clash,
   both use the service name `sshd`):
   ```powershell
   Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
   ```
   - `Installed` → skip step 2.
   - `NotPresent` → do step 2.
   - hangs or shows something pending → restart the PC and check again.

2. **Install OpenSSH as an app with winget** (Microsoft's build, much faster than the Windows feature):
   ```powershell
   winget install --id Microsoft.OpenSSH.Preview --source winget
   ```
   If winget is missing, install the .msi from https://github.com/PowerShell/Win32-OpenSSH/releases.

3. **Start the service and make it start automatically:**
   ```powershell
   Start-Service sshd
   Set-Service -Name sshd -StartupType Automatic
   Get-Service sshd
   ```
   It should show `Running`.

4. **Open port 22 for the home network only.** Look at the existing rule by its exact name:
   ```powershell
   Get-NetFirewallRule -Name sshd | Format-List Name, DisplayName, Enabled, Profile, Direction, Action
   Get-NetFirewallRule -Name sshd | Get-NetFirewallPortFilter | Format-List Protocol, LocalPort
   Get-NetConnectionProfile | Select-Object Name, NetworkCategory
   ```
   Then fix what's off:
   - rule disabled or wrong profile:
     `Set-NetFirewallRule -Name sshd -Enabled True -Profile Private -Direction Inbound -Action Allow`
   - port filter not TCP 22:
     `Get-NetFirewallRule -Name sshd | Set-NetFirewallPortFilter -Protocol TCP -LocalPort 22`
   - network is `Public`: `Set-NetConnectionProfile -Name "<name>" -NetworkCategory Private`
   - no rule at all:
     `New-NetFirewallRule -Name sshd -DisplayName "OpenSSH Server (sshd)" -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -Profile Private`
   - a third-party antivirus/firewall (Norton, McAfee, …) may also block port 22.

5. **Install my Mac's public key.** This is the key (it's public, safe to share):
   ```
   ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDvaBU4lUn6xQGJG8I++lKncFIWPEeOgaZOmEv9KjdTV
   ```
   - If my Windows user is an **administrator** (check with `net localgroup Administrators`), the key
     must go in `C:\ProgramData\ssh\administrators_authorized_keys` with restricted permissions:
     ```powershell
     Add-Content -Path C:\ProgramData\ssh\administrators_authorized_keys -Value "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDvaBU4lUn6xQGJG8I++lKncFIWPEeOgaZOmEv9KjdTV"
     icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"
     ```
   - If my user is **not** an administrator, put the key in `C:\Users\<me>\.ssh\authorized_keys`
     instead (create the `.ssh` folder if needed).

6. **Test.** On this PC: `ssh localhost` should answer (a password prompt is fine here). Then
   from the Mac: `ssh nilss@192.168.86.32` should log in without a password. My username is
   `nilss` and the PC's IP is 192.168.86.32; suggest giving the PC a fixed IP (a DHCP
   reservation in my router) so it doesn't change.

Don't suggest opening port 22 on my router or to the internet: this is for the home network only.

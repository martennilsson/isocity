# Prompt for Claude: set up SSH on my Windows PC

*Copy everything below the line into a new Claude chat on the Windows PC.*

---

Hi Claude! Please guide me step by step, one step at a time, waiting for me to report back
after each step. I'm on my Windows 10/11 PC and want my Mac to log in to it over SSH with a key
(no password), so tools on the Mac can install and run ComfyUI on this PC's NVIDIA RTX 3070.
Both machines are on the same home network. Explain briefly what each step does, and help me
troubleshoot if something fails (paste me the commands to check).

The steps I've been given:

1. **Install and start the OpenSSH server.** Open PowerShell **as Administrator** and run:
   ```powershell
   Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
   Start-Service sshd
   Set-Service -Name sshd -StartupType Automatic
   Get-NetFirewallRule -Name *OpenSSH* | Select-Object Name, Enabled, Profile
   ```
   The last command should show an enabled firewall rule for port 22. If it's missing:
   ```powershell
   New-NetFirewallRule -Name sshd -DisplayName "OpenSSH Server (sshd)" -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -Profile Private
   ```
   Also check that my home network is set to the **Private** profile, not Public
   (`Get-NetConnectionProfile`).

2. **Install my Mac's public key.** This is the key (it's public, safe to share):
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

3. **Find what to tell the Mac:**
   - my username: `whoami` (the part after the backslash) or the folder name under `C:\Users`;
   - my IP address: `ipconfig` (the IPv4 address of the Wi-Fi or Ethernet adapter, e.g. 192.168.x.x).
   Suggest that I give the PC a fixed IP (a DHCP reservation in my router) so it doesn't change.

4. **Test from this PC** that the server answers: `ssh localhost` (it may ask for my password;
   that's fine here). The real test is from the Mac: `ssh <username>@<ip>` should log in without a password.

Don't suggest opening port 22 on my router or to the internet: this is for the home network only.

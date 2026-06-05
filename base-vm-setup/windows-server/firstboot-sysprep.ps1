#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Stages first-boot configuration scripts and runs Sysprep to seal a Windows
    Server golden image for ESXi deployment.

.DESCRIPTION
    Run this script ONCE on a fully configured Windows Server VM -- after all
    roles, features, updates, and software are installed -- immediately before
    taking the final snapshot or exporting the VMDK.

    What it does:
      1. Validates the environment (OS, elevation, Sysprep availability).
      2. Creates C:\Windows\Setup\Scripts\ and drops SetupComplete.cmd there.
      3. Writes FirstBoot.ps1 alongside SetupComplete.cmd.
      4. Sets the machine-wide ExecutionPolicy to RemoteSigned so
         SetupComplete.cmd can invoke the .ps1 without a bypass flag.
      5. Optionally accepts an -AdminPassword to embed in the unattend.xml
         used by Sysprep's OOBE pass (auto-logon for the console session that
         shows the first-boot prompts). If omitted, prompts twice and
         requires both entries to match.
      6. Writes a minimal unattend.xml to C:\Windows\System32\Sysprep\unattend.xml.
      7. Runs Sysprep /generalize /oobe /shutdown /unattend and verifies the
         generalize actually succeeded before declaring victory.

    After the VM powers off:
      - Export / snapshot the VMDK as your golden image.
      - Deploy new VMs from that image with deploy_vm.py + a config ISO built
        by gen_ws_config_iso.py.
      - On first boot the VM reads [cdrom]\vmconfig.json, configures hostname
        and static IP, self-cleans, and reboots into a ready state.

.PARAMETER AdminPassword
    Plain-text password to set for the built-in Administrator account via the
    Sysprep unattend.xml.  If omitted you are prompted securely at runtime
    (twice -- entries must match).  The value is written into unattend.xml in
    plain text (Windows requirement at this stage; the file is deleted by
    Sysprep after use).

.PARAMETER SkipSysprep
    Stage the scripts and write unattend.xml but do NOT execute Sysprep.
    Useful for inspecting generated files before committing.

.PARAMETER Force
    Overwrite existing files in C:\Windows\Setup\Scripts\ without prompting.

.EXAMPLE
    # Interactive password prompt, then full sysprep:
    .\Prepare-GoldenImage.ps1

.EXAMPLE
    # Supply password inline (CI/automation):
    .\Prepare-GoldenImage.ps1 -AdminPassword 'S3cur3P@ss!'

.EXAMPLE
    # Dry-run -- write files only, skip sysprep:
    .\Prepare-GoldenImage.ps1 -SkipSysprep

.NOTES
    - Must be run as Administrator.
    - Tested on Windows Server 2022 (Desktop Experience).
    - The VM will power off at the end of Sysprep.  Save all work first.
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory = $false)]
    [string] $AdminPassword,

    [Parameter(Mandatory = $false)]
    [switch] $SkipSysprep,

    [Parameter(Mandatory = $false)]
    [switch] $Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Write-OK {
    param([string]$Message)
    Write-Host "    [OK] $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "    [WARN] $Message" -ForegroundColor Yellow
}

function Write-Fail {
    param([string]$Message)
    Write-Host "    [FAIL] $Message" -ForegroundColor Red
}

function ConvertFrom-SecureToPlain {
    param([securestring]$Secure)
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try     { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

# ---------------------------------------------------------------------------
# 1. Environment validation
# ---------------------------------------------------------------------------

Write-Step "Validating environment"

# Must be elevated
$currentPrincipal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Fail "This script must be run as Administrator."
    exit 1
}
Write-OK "Running as Administrator"

# Must be Windows Server
$osInfo = Get-CimInstance Win32_OperatingSystem
if ($osInfo.ProductType -eq 1) {
    Write-Fail "This script targets Windows Server (ProductType 2 or 3). Detected a workstation OS. Aborting."
    exit 1
}
Write-OK "OS: $($osInfo.Caption) (ProductType $($osInfo.ProductType))"

# Sysprep must exist
$sysprepExe = "$env:SystemRoot\System32\Sysprep\sysprep.exe"
if (-not (Test-Path $sysprepExe)) {
    Write-Fail "Sysprep not found at $sysprepExe"
    exit 1
}
Write-OK "Sysprep found: $sysprepExe"

# Warn if Sysprep generalize count is near the limit (8 on most SKUs)
$sysprepRegPath = 'HKLM:\SYSTEM\Setup\Status\SysprepStatus'
if (Test-Path $sysprepRegPath) {
    $cleanupState = (Get-ItemProperty $sysprepRegPath).GeneralizationState
    # State 7 = generalize complete; state 0 = never run
    # The count itself isn't directly readable but a previous generalize leaves state 7
    if ($cleanupState -eq 7) {
        Write-Warn "Registry indicates Sysprep /generalize has been run on this machine before."
        Write-Warn "Windows Server allows this up to ~8 times. Ensure you are working from a fresh base."
    }
}

# ---------------------------------------------------------------------------
# 2. Collect Admin password
# ---------------------------------------------------------------------------

Write-Step "Administrator password for unattend.xml"

if (-not $AdminPassword) {
    $maxAttempts = 3
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        $first  = ConvertFrom-SecureToPlain (Read-Host "Enter the Administrator password for first-boot auto-logon" -AsSecureString)
        if ([string]::IsNullOrWhiteSpace($first)) {
            Write-Warn "Password cannot be empty. (attempt $attempt of $maxAttempts)"
            continue
        }
        $second = ConvertFrom-SecureToPlain (Read-Host "Re-enter the password to confirm" -AsSecureString)
        if ($first -cne $second) {
            Write-Warn "Passwords do not match. (attempt $attempt of $maxAttempts)"
            continue
        }
        $AdminPassword = $first
        break
    }
    if (-not $AdminPassword) {
        Write-Fail "Could not confirm a password after $maxAttempts attempts."
        exit 1
    }
}

if ([string]::IsNullOrWhiteSpace($AdminPassword)) {
    Write-Fail "Password cannot be empty."
    exit 1
}
Write-OK "Password accepted (not echoed)"

# ---------------------------------------------------------------------------
# 3. Create staging directory
# ---------------------------------------------------------------------------

Write-Step "Creating C:\Windows\Setup\Scripts\"

$scriptsDir = "$env:SystemRoot\Setup\Scripts"

if (-not (Test-Path $scriptsDir)) {
    New-Item -ItemType Directory -Path $scriptsDir -Force | Out-Null
    Write-OK "Directory created: $scriptsDir"
} else {
    Write-OK "Directory already exists: $scriptsDir"
}

# ---------------------------------------------------------------------------
# 4. Write SetupComplete.cmd
# ---------------------------------------------------------------------------

Write-Step "Writing SetupComplete.cmd"

$setupCompleteCmd = Join-Path $scriptsDir "SetupComplete.cmd"

if ((Test-Path $setupCompleteCmd) -and -not $Force) {
    Write-Warn "SetupComplete.cmd already exists. Use -Force to overwrite."
} else {
    $cmdContent = @'
@echo off
:: Launched automatically by Windows after OOBE completes, as SYSTEM.
:: Hands off to the PowerShell first-boot script in a visible window.
powershell.exe -ExecutionPolicy RemoteSigned -WindowStyle Normal -File "%~dp0FirstBoot.ps1"
'@
    Set-Content -Path $setupCompleteCmd -Value $cmdContent -Encoding ASCII -Force:$Force
    Write-OK "Written: $setupCompleteCmd"
}

# ---------------------------------------------------------------------------
# 5. Write FirstBoot.ps1
# ---------------------------------------------------------------------------

Write-Step "Writing FirstBoot.ps1"

$firstBootPs1 = Join-Path $scriptsDir "FirstBoot.ps1"

if ((Test-Path $firstBootPs1) -and -not $Force) {
    Write-Warn "FirstBoot.ps1 already exists. Use -Force to overwrite."
} else {

$firstBootContent = @'
<#
.SYNOPSIS
    First-boot configuration script. Runs once via SetupComplete.cmd after
    Sysprep OOBE completes on a deployed golden-image VM.

.DESCRIPTION
    Reads vmconfig.json from a mounted config ISO (any CD-ROM drive).
    Applies hostname, static IP, subnet prefix, gateway, and DNS.
    Writes a transcript to C:\Windows\Temp\firstboot.log.
    Removes itself and SetupComplete.cmd after successful configuration.
    Reboots the machine.

    vmconfig.json schema (produced by gen_ws_config_iso.py):
    {
        "hostname":   "srv-web-01",
        "ip":         "192.168.1.50",
        "prefix":     24,
        "gateway":    "192.168.1.1",
        "dns1":       "192.168.1.10",
        "dns2":       "8.8.8.8",          // optional
        "dns_suffix": "corp.example.com"   // optional
    }
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$LogPath = "$env:SystemRoot\Temp\firstboot.log"
$ErrorLogPath = "$env:SystemRoot\Temp\firstboot-error.log"

Start-Transcript -Path $LogPath -Append -Force

function Write-Step  { param([string]$m) Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-OK    { param([string]$m) Write-Host "    [OK] $m" -ForegroundColor Green }
function Write-Fail  { param([string]$m) Write-Host "    [FAIL] $m" -ForegroundColor Red }

try {

    # -----------------------------------------------------------------------
    # 1. Locate config ISO
    # -----------------------------------------------------------------------

    Write-Step "Locating config ISO"

    $configDrive = $null
    $configFile  = $null

    $cdDrives = Get-CimInstance Win32_LogicalDisk | Where-Object { $_.DriveType -eq 5 }

    foreach ($drive in $cdDrives) {
        $candidate = Join-Path $drive.DeviceID "vmconfig.json"
        if (Test-Path $candidate) {
            $configDrive = $drive.DeviceID
            $configFile  = $candidate
            break
        }
    }

    if (-not $configFile) {
        throw "No config ISO found. Checked drives: $(($cdDrives | ForEach-Object { $_.DeviceID }) -join ', '). Ensure vmconfig.json is at the root of a mounted ISO."
    }

    Write-OK "Config ISO found on $configDrive"
    Write-OK "Config file: $configFile"

    # -----------------------------------------------------------------------
    # 2. Parse and validate vmconfig.json
    # -----------------------------------------------------------------------

    Write-Step "Parsing vmconfig.json"

    $raw = Get-Content -Path $configFile -Raw -Encoding UTF8
    $cfg = $raw | ConvertFrom-Json

    # Required fields
    $required = @('hostname', 'ip', 'prefix', 'gateway', 'dns1')
    foreach ($key in $required) {
        if (-not $cfg.PSObject.Properties[$key] -or [string]::IsNullOrWhiteSpace($cfg.$key)) {
            throw "Required field '$key' is missing or empty in vmconfig.json."
        }
    }

    $hostname  = $cfg.hostname.Trim()
    $ip        = $cfg.ip.Trim()
    $prefix    = [int]$cfg.prefix
    $gateway   = $cfg.gateway.Trim()
    $dns1      = $cfg.dns1.Trim()
    $dns2      = if ($cfg.PSObject.Properties['dns2']       -and -not [string]::IsNullOrWhiteSpace($cfg.dns2))       { $cfg.dns2.Trim()       } else { $null }
    $dnsSuffix = if ($cfg.PSObject.Properties['dns_suffix'] -and -not [string]::IsNullOrWhiteSpace($cfg.dns_suffix)) { $cfg.dns_suffix.Trim() } else { $null }

    # Basic format validation
    $ipRegex = '^\d{1,3}(\.\d{1,3}){3}$'
    foreach ($field in @($ip, $gateway, $dns1)) {
        if ($field -notmatch $ipRegex) {
            throw "Value '$field' does not look like a valid IPv4 address."
        }
    }
    if ($dns2 -and $dns2 -notmatch $ipRegex) {
        throw "dns2 value '$dns2' does not look like a valid IPv4 address."
    }
    if ($prefix -lt 1 -or $prefix -gt 32) {
        throw "Prefix '$prefix' is out of range (1-32)."
    }
    if ($hostname.Length -gt 15) {
        Write-Host "    [WARN] Hostname '$hostname' exceeds 15 chars -- NetBIOS name will be truncated." -ForegroundColor Yellow
    }

    Write-OK "hostname   = $hostname"
    Write-OK "ip         = $ip / $prefix"
    Write-OK "gateway    = $gateway"
    Write-OK "dns1       = $dns1"
    if ($dns2)      { Write-OK "dns2       = $dns2" }
    if ($dnsSuffix) { Write-OK "dns_suffix = $dnsSuffix" }

    # -----------------------------------------------------------------------
    # 3. Apply hostname
    # -----------------------------------------------------------------------

    Write-Step "Setting hostname"

    $currentName = $env:COMPUTERNAME
    if ($currentName -eq $hostname) {
        Write-OK "Hostname is already '$hostname' -- skipping rename."
    } else {
        Rename-Computer -NewName $hostname -Force
        Write-OK "Hostname changed from '$currentName' to '$hostname'"
    }

    # -----------------------------------------------------------------------
    # 4. Configure network
    # -----------------------------------------------------------------------

    Write-Step "Configuring network adapter"

    # Find the first connected physical adapter -- skip loopback/tunnels
    $nic = Get-NetAdapter |
        Where-Object { $_.Status -eq 'Up' -and $_.InterfaceDescription -notmatch 'Loopback|Hyper-V|isatap|Teredo' } |
        Sort-Object -Property ifIndex |
        Select-Object -First 1

    if (-not $nic) {
        throw "No active network adapter found. Cannot configure static IP."
    }

    Write-OK "Using adapter: $($nic.Name) ($($nic.InterfaceDescription))"

    # Remove existing IP configuration on this adapter
    Remove-NetIPAddress -InterfaceIndex $nic.ifIndex -Confirm:$false -ErrorAction SilentlyContinue
    Remove-NetRoute     -InterfaceIndex $nic.ifIndex -Confirm:$false -ErrorAction SilentlyContinue

    # Set static IP
    New-NetIPAddress `
        -InterfaceIndex  $nic.ifIndex `
        -IPAddress       $ip `
        -PrefixLength    $prefix `
        -DefaultGateway  $gateway | Out-Null

    Write-OK "IP address set: $ip/$prefix via $gateway"

    # Set DNS servers
    $dnsServers = @($dns1)
    if ($dns2) { $dnsServers += $dns2 }
    Set-DnsClientServerAddress -InterfaceIndex $nic.ifIndex -ServerAddresses $dnsServers
    Write-OK "DNS servers set: $($dnsServers -join ', ')"

    # Set DNS search suffix if provided
    if ($dnsSuffix) {
        Set-DnsClient -InterfaceIndex $nic.ifIndex -ConnectionSpecificSuffix $dnsSuffix
        Write-OK "DNS suffix set: $dnsSuffix"
    }

    # -----------------------------------------------------------------------
    # 5. Self-cleanup
    # -----------------------------------------------------------------------

    Write-Step "Cleaning up first-boot scripts"

    $selfDir = Split-Path -Parent $MyInvocation.MyCommand.Path

    # Register cleanup as a scheduled task that fires once at next logon,
    # after this script has already exited -- we cannot delete ourselves
    # while we are running.
    $cleanupScript = @"
Remove-Item -Path '$($selfDir)\FirstBoot.ps1'   -Force -ErrorAction SilentlyContinue
Remove-Item -Path '$($selfDir)\SetupComplete.cmd' -Force -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName 'FirstBootCleanup' -Confirm:`$false -ErrorAction SilentlyContinue
"@

    $action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
                   -Argument "-WindowStyle Hidden -Command `"$cleanupScript`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
    Register-ScheduledTask -TaskName 'FirstBootCleanup' `
        -Action $action -Trigger $trigger -Settings $settings `
        -RunLevel Highest -Force | Out-Null

    Write-OK "Cleanup task registered (runs at next logon, then removes itself)"

    # -----------------------------------------------------------------------
    # 6. Reboot
    # -----------------------------------------------------------------------

    Write-Step "Configuration complete -- rebooting in 10 seconds"
    Write-OK "Transcript saved to: $LogPath"

    Stop-Transcript
    Start-Sleep -Seconds 10
    Restart-Computer -Force

} catch {

    $errMsg = $_.Exception.Message
    Write-Fail "First-boot configuration failed: $errMsg"

    # Write error log so it survives the session
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    "$timestamp  ERROR: $errMsg`n$($_.ScriptStackTrace)" |
        Set-Content -Path $ErrorLogPath -Encoding UTF8 -Force

    Write-Host "`nError details written to: $ErrorLogPath" -ForegroundColor Red
    Write-Host "The machine has NOT been rebooted. Fix the issue and re-run, or inspect the log." -ForegroundColor Yellow

    Stop-Transcript
    exit 1
}
'@

    Set-Content -Path $firstBootPs1 -Value $firstBootContent -Encoding UTF8
    Write-OK "Written: $firstBootPs1"
}

# ---------------------------------------------------------------------------
# 6. Set machine-wide execution policy
# ---------------------------------------------------------------------------

Write-Step "Setting ExecutionPolicy to RemoteSigned (machine scope)"

$currentPolicy = Get-ExecutionPolicy -Scope LocalMachine
if ($currentPolicy -in @('RemoteSigned', 'Unrestricted', 'Bypass')) {
    Write-OK "Already set to $currentPolicy -- no change needed"
} else {
    Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope LocalMachine -Force
    Write-OK "ExecutionPolicy set to RemoteSigned"
}

# ---------------------------------------------------------------------------
# 7. Write unattend.xml
# ---------------------------------------------------------------------------

Write-Step "Writing Sysprep unattend.xml"

$unattendPath = "$env:SystemRoot\System32\Sysprep\unattend.xml"

# Escape the password for XML
$escapedPassword = $AdminPassword `
    -replace '&', '&amp;'  `
    -replace '<', '&lt;'   `
    -replace '>', '&gt;'   `
    -replace '"', '&quot;' `
    -replace "'", '&apos;'

$unattendXml = @"
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">

  <!--
    oobeSystem pass -- runs after Sysprep OOBE on first boot of deployed VM.
    Skips all interactive screens.
    Auto-logs on once so SetupComplete.cmd fires in a visible console session.
  -->
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-Shell-Setup"
               processorArchitecture="amd64"
               publicKeyToken="31bf3856ad364e35"
               language="neutral"
               versionScope="nonSxS"
               xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">

      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideLocalAccountScreen>true</HideLocalAccountScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <NetworkLocation>Work</NetworkLocation>
        <ProtectYourPC>3</ProtectYourPC>
        <SkipUserOOBE>true</SkipUserOOBE>
        <SkipMachineOOBE>true</SkipMachineOOBE>
      </OOBE>

      <UserAccounts>
        <AdministratorPassword>
          <Value>$escapedPassword</Value>
          <PlainText>true</PlainText>
        </AdministratorPassword>
      </UserAccounts>

      <!-- Auto-logon fires SetupComplete.cmd which launches FirstBoot.ps1 -->
      <AutoLogon>
        <Password>
          <Value>$escapedPassword</Value>
          <PlainText>true</PlainText>
        </Password>
        <Username>Administrator</Username>
        <Enabled>true</Enabled>
        <LogonCount>1</LogonCount>
      </AutoLogon>

    </component>
  </settings>

</unattend>
"@

Set-Content -Path $unattendPath -Value $unattendXml -Encoding UTF8
Write-OK "Written: $unattendPath"

# Scrub the in-memory password now that it's on disk
$escapedPassword = $null
# Note: AdminPassword param is still in memory; PowerShell does not support
# true in-memory zeroing of plain strings. For higher security, pass via
# SecureString and use -AdminPassword only as a secure prompt.

# ---------------------------------------------------------------------------
# 8. Summary before sysprep
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "-----------------------------------------------------------" -ForegroundColor DarkGray
Write-Host " Files staged:" -ForegroundColor White
Write-Host "   $setupCompleteCmd" -ForegroundColor Gray
Write-Host "   $firstBootPs1" -ForegroundColor Gray
Write-Host "   $unattendPath" -ForegroundColor Gray
Write-Host ""
Write-Host " ExecutionPolicy (LocalMachine): RemoteSigned" -ForegroundColor White
Write-Host "-----------------------------------------------------------" -ForegroundColor DarkGray
Write-Host ""

if ($SkipSysprep) {
    Write-Warn "-SkipSysprep specified. Sysprep will NOT run."
    Write-Host "  Inspect the files above, then run Sysprep manually:" -ForegroundColor Yellow
    Write-Host "  $sysprepExe /generalize /oobe /shutdown /unattend:`"$unattendPath`"" -ForegroundColor Yellow
    exit 0
}

# ---------------------------------------------------------------------------
# 9. Final confirmation and Sysprep
# ---------------------------------------------------------------------------

Write-Step "Sysprep -- POINT OF NO RETURN"
Write-Host ""
Write-Host "  The VM will be generalized and shut down." -ForegroundColor Yellow
Write-Host "  This action cannot be undone on this machine." -ForegroundColor Yellow
Write-Host "  Take a snapshot NOW if you want a pre-sysprep restore point." -ForegroundColor Yellow
Write-Host ""

$confirm = Read-Host "Type YES to proceed with Sysprep, anything else to abort"
if ($confirm -ne 'YES') {
    Write-Warn "Aborted by user. Files remain staged. Re-run or invoke Sysprep manually."
    exit 0
}

Write-Step "Running Sysprep"
Write-Host "  Command: $sysprepExe /generalize /oobe /shutdown /unattend:`"$unattendPath`""
Write-Host ""

# Note: /quiet is deliberately NOT used. It suppresses the Sysprep error
# dialog, so a failed generalize just exits silently and the VM sits there
# looking like it "didn't shut down".
$sysprepArgs = @(
    '/generalize'
    '/oobe'
    '/shutdown'
    "/unattend:$unattendPath"
)

$proc = Start-Process -FilePath $sysprepExe `
    -ArgumentList $sysprepArgs `
    -PassThru `
    -Wait

# Sysprep's exit code is NOT a reliable success signal -- it frequently
# returns 0 even when generalize failed. The authoritative indicator is
# GeneralizationState = 7 in the SysprepStatus registry key.
$genState = (Get-ItemProperty 'HKLM:\SYSTEM\Setup\Status\SysprepStatus' -ErrorAction SilentlyContinue).GeneralizationState

if ($genState -ne 7) {
    Write-Fail "Sysprep did not complete generalize (GeneralizationState=$genState, exit code $($proc.ExitCode))."
    $errLog = "$env:SystemRoot\System32\Sysprep\Panther\setuperr.log"
    if (Test-Path $errLog) {
        Write-Host "`n  Last 20 lines of $errLog :" -ForegroundColor Yellow
        Get-Content -Path $errLog -Tail 20 | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
    } else {
        Write-Host "  Check C:\Windows\System32\Sysprep\Panther\setupact.log for details." -ForegroundColor Yellow
    }
    exit 1
}

# Generalize succeeded -- /shutdown should be powering the VM off right now.
# If we are still alive after a grace period, force the shutdown ourselves
# so the golden image is never left running in a sealed state.
Write-OK "Sysprep generalize completed. VM is shutting down."
Start-Sleep -Seconds 90
Write-Warn "Machine still running 90s after Sysprep finished -- forcing shutdown."
Stop-Computer -Force

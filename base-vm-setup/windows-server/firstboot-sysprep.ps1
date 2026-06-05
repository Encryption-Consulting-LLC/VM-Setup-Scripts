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
         used by Sysprep's OOBE pass (one-time auto-logon on the deployed
         VM). If omitted, prompts twice and requires both entries to match.
      6. Writes a minimal unattend.xml to C:\Windows\System32\Sysprep\unattend.xml.
      7. Runs Sysprep /generalize /oobe /shutdown /unattend and verifies the
         generalize actually succeeded before declaring victory.
      8. On ANY failure between the start of the script and Sysprep completing
         generalize, automatically rolls back everything staged so far
         (staged files, Scripts directory, ExecutionPolicy). A staged-but-
         aborted run can also be undone later with -Rollback.

    After the VM powers off:
      - Export / snapshot the VMDK as your golden image.
      - Deploy new VMs from that image with deploy_vm.py + a config ISO built
        by gen_ws_config_iso.py.
      - On first boot the VM reads [cdrom]\vmconfig.json and configures
        hostname and static IP, then reboots twice automatically: the first
        reboot applies the staged config, and a one-shot startup task cleans
        up the first-boot artifacts and reboots once more so the new hostname
        is fully active. First-boot progress is logged to
        C:\Windows\Temp\firstboot.log on the deployed VM (nothing is shown on
        screen; it runs before any logon session).

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

.PARAMETER Rollback
    Undo everything this script stages and exit: removes SetupComplete.cmd,
    FirstBoot.ps1 and unattend.xml, removes C:\Windows\Setup\Scripts if this
    script created it, and restores the previous LocalMachine ExecutionPolicy.
    Reads the state file written during staging
    (C:\Windows\Temp\firstboot-sysprep-rollback.json); if no state file
    exists, falls back to removing the known staged paths.

.EXAMPLE
    # Interactive password prompt, then full sysprep:
    .\Prepare-GoldenImage.ps1

.EXAMPLE
    # Supply password inline (CI/automation):
    .\Prepare-GoldenImage.ps1 -AdminPassword 'S3cur3P@ss!'

.EXAMPLE
    # Dry-run -- write files only, skip sysprep:
    .\Prepare-GoldenImage.ps1 -SkipSysprep

.EXAMPLE
    # Undo a previous staging (after -SkipSysprep, an abort, or a failure):
    .\Prepare-GoldenImage.ps1 -Rollback

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
    [switch] $Force,

    [Parameter(Mandatory = $false)]
    [switch] $Rollback
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
# Rollback support
# ---------------------------------------------------------------------------
# Every change the script makes is recorded in $script:RollbackState and
# persisted to a state file, so it can be undone -- automatically on failure
# (see the trap below) or explicitly via -Rollback in a later invocation.

$script:StateFile  = "$env:SystemRoot\Temp\firstboot-sysprep-rollback.json"
$script:InRollback = $false

$script:RollbackState = [ordered]@{
    timestamp               = (Get-Date -Format o)
    scriptsDirCreated       = $false
    executionPolicyChanged  = $false
    originalExecutionPolicy = $null
    stagedFiles             = @()
}

function Save-RollbackState {
    $script:RollbackState | ConvertTo-Json | Set-Content -Path $script:StateFile -Encoding UTF8
}

function Add-StagedFile {
    param([string]$Path)
    if ($script:RollbackState.stagedFiles -notcontains $Path) {
        $script:RollbackState.stagedFiles += $Path
    }
    Save-RollbackState
}

function Invoke-Rollback {
    <#
        Undoes everything the script staged. Best effort: each step warns on
        failure instead of aborting, so one stuck item does not block the rest.

        -State: in-memory state from the current run (automatic rollback on
        failure). When omitted (-Rollback invocation), state is loaded from
        the state file; if that is missing too, falls back to removing the
        well-known staged paths.
    #>
    param([object]$State)

    $script:InRollback = $true

    Write-Step "Rolling back staged changes"

    if (-not $State) {
        if (Test-Path $script:StateFile) {
            $State = Get-Content -Path $script:StateFile -Raw | ConvertFrom-Json
            Write-OK "Loaded rollback state: $script:StateFile"
        } else {
            Write-Warn "No rollback state file found at $script:StateFile."
            Write-Warn "Removing the well-known staged artifacts instead (best effort)."
            $State = [pscustomobject]@{
                scriptsDirCreated       = $false
                executionPolicyChanged  = $false
                originalExecutionPolicy = $null
                stagedFiles             = @(
                    "$env:SystemRoot\Setup\Scripts\SetupComplete.cmd"
                    "$env:SystemRoot\Setup\Scripts\FirstBoot.ps1"
                    "$env:SystemRoot\System32\Sysprep\unattend.xml"
                )
            }
        }
    }

    $didSomething = $false

    # 1. Remove staged files
    foreach ($f in @($State.stagedFiles)) {
        if ($f -and (Test-Path $f)) {
            try {
                Remove-Item -Path $f -Force
                Write-OK "Removed: $f"
                $didSomething = $true
            } catch {
                Write-Warn "Could not remove $f : $($_.Exception.Message)"
            }
        }
    }

    # 2. Remove the Scripts directory -- only if we created it and it is empty
    $rbScriptsDir = "$env:SystemRoot\Setup\Scripts"
    if ($State.scriptsDirCreated -and (Test-Path $rbScriptsDir) -and
        -not (Get-ChildItem -Path $rbScriptsDir -Force)) {
        try {
            Remove-Item -Path $rbScriptsDir -Force
            Write-OK "Removed directory: $rbScriptsDir"
            $didSomething = $true
        } catch {
            Write-Warn "Could not remove $rbScriptsDir : $($_.Exception.Message)"
        }
    }

    # 3. Restore the previous LocalMachine ExecutionPolicy
    if ($State.executionPolicyChanged -and $State.originalExecutionPolicy) {
        try {
            Set-ExecutionPolicy -ExecutionPolicy $State.originalExecutionPolicy -Scope LocalMachine -Force
            Write-OK "ExecutionPolicy (LocalMachine) restored to $($State.originalExecutionPolicy)"
            $didSomething = $true
        } catch {
            Write-Warn "Could not restore ExecutionPolicy: $($_.Exception.Message)"
        }
    }

    # 4. Drop the state file -- the recorded changes no longer exist
    if (Test-Path $script:StateFile) {
        Remove-Item -Path $script:StateFile -Force -ErrorAction SilentlyContinue
    }

    if ($didSomething) { Write-OK "Rollback complete." }
    else               { Write-OK "Nothing to roll back." }

    $script:InRollback = $false
}

# ---------------------------------------------------------------------------
# 0. Elevation check, standalone rollback, failure trap
# ---------------------------------------------------------------------------

# Must be elevated -- both a normal run and a rollback touch C:\Windows.
$currentPrincipal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Fail "This script must be run as Administrator."
    exit 1
}

# Standalone rollback: undo a previous staging and exit.
if ($Rollback) {
    Invoke-Rollback
    exit 0
}

# Automatic rollback: with $ErrorActionPreference = 'Stop', any failure from
# here until Sysprep completes generalize raises a terminating error, which
# lands in this trap and rolls back everything staged so far. Intentional
# stops (user abort, -SkipSysprep) use 'exit', which bypasses the trap.
trap {
    if ($script:InRollback) {
        Write-Fail "Error during rollback: $($_.Exception.Message)"
        exit 1
    }
    Write-Fail $_.Exception.Message
    if ($_.ScriptStackTrace) { Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray }
    Write-Warn "Failure detected -- rolling back changes staged by this run."
    Invoke-Rollback -State ([pscustomobject]$script:RollbackState)
    exit 1
}

# ---------------------------------------------------------------------------
# 1. Environment validation
# ---------------------------------------------------------------------------

Write-Step "Validating environment"
Write-OK "Running as Administrator"

# Must be Windows Server
$osInfo = Get-CimInstance Win32_OperatingSystem
if ($osInfo.ProductType -eq 1) {
    throw "This script targets Windows Server (ProductType 2 or 3). Detected a workstation OS. Aborting."
}
Write-OK "OS: $($osInfo.Caption) (ProductType $($osInfo.ProductType))"

# Sysprep must exist
$sysprepExe = "$env:SystemRoot\System32\Sysprep\sysprep.exe"
if (-not (Test-Path $sysprepExe)) {
    throw "Sysprep not found at $sysprepExe"
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
        throw "Could not confirm a password after $maxAttempts attempts."
    }
}

if ([string]::IsNullOrWhiteSpace($AdminPassword)) {
    throw "Password cannot be empty."
}
Write-OK "Password accepted (not echoed)"

# ---------------------------------------------------------------------------
# 3. Create staging directory
# ---------------------------------------------------------------------------

Write-Step "Creating C:\Windows\Setup\Scripts\"

$scriptsDir = "$env:SystemRoot\Setup\Scripts"

if (-not (Test-Path $scriptsDir)) {
    New-Item -ItemType Directory -Path $scriptsDir -Force | Out-Null
    $script:RollbackState.scriptsDirCreated = $true
    Save-RollbackState
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
:: Launched automatically by Windows after OOBE completes, as SYSTEM, before
:: any logon session exists -- there is no console anyone could see.
:: FirstBoot.ps1 logs to C:\Windows\Temp\firstboot.log instead.
powershell.exe -ExecutionPolicy RemoteSigned -WindowStyle Hidden -File "%~dp0FirstBoot.ps1"
'@
    Set-Content -Path $setupCompleteCmd -Value $cmdContent -Encoding ASCII -Force:$Force
    Add-StagedFile -Path $setupCompleteCmd
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
    Runs non-interactively as SYSTEM before any logon session exists, so
    nothing it could print would ever be seen. It therefore produces no
    console output; progress and errors are appended to
    C:\Windows\Temp\firstboot.log (fatal errors additionally to
    C:\Windows\Temp\firstboot-error.log).

    Reads vmconfig.json from a mounted config ISO (any CD-ROM drive).
    Applies hostname, static IP, subnet prefix, gateway, and DNS.
    Reboots to apply the staged configuration; a one-shot FirstBootFinalize
    startup task then removes the first-boot artifacts and performs one
    final automatic reboot, after which the new hostname is fully active.

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

$LogPath      = "$env:SystemRoot\Temp\firstboot.log"
$ErrorLogPath = "$env:SystemRoot\Temp\firstboot-error.log"

function Write-Log {
    param([string]$Message)
    Add-Content -Path $LogPath -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $Message"
}

Write-Log "===== FirstBoot starting (as $env:USERNAME on $env:COMPUTERNAME) ====="

try {

    # -----------------------------------------------------------------------
    # 1. Locate config ISO
    # -----------------------------------------------------------------------

    $configFile = $null
    $cdDrives   = Get-CimInstance Win32_LogicalDisk | Where-Object { $_.DriveType -eq 5 }

    foreach ($drive in $cdDrives) {
        $candidate = Join-Path $drive.DeviceID "vmconfig.json"
        if (Test-Path $candidate) {
            $configFile = $candidate
            break
        }
    }

    if (-not $configFile) {
        throw "No config ISO found. Checked drives: $(($cdDrives | ForEach-Object { $_.DeviceID }) -join ', '). Ensure vmconfig.json is at the root of a mounted ISO."
    }

    Write-Log "Config file: $configFile"

    # -----------------------------------------------------------------------
    # 2. Parse and validate vmconfig.json
    # -----------------------------------------------------------------------

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
        Write-Log "WARN: Hostname '$hostname' exceeds 15 chars -- NetBIOS name will be truncated."
    }

    Write-Log "Config: hostname=$hostname ip=$ip/$prefix gateway=$gateway dns1=$dns1 dns2=$dns2 dns_suffix=$dnsSuffix"

    # -----------------------------------------------------------------------
    # 3. Apply hostname
    # -----------------------------------------------------------------------

    # NetBIOS computer name: max 15 chars, stored upper-case.
    $netbiosName = $hostname
    if ($netbiosName.Length -gt 15) { $netbiosName = $netbiosName.Substring(0, 15) }
    $netbiosName = $netbiosName.ToUpper()

    if ($env:COMPUTERNAME -eq $netbiosName) {
        Write-Log "Hostname is already '$hostname' -- skipping rename."
    } else {
        # Rename-Computer is unreliable in this context: running as SYSTEM
        # from SetupComplete.cmd (no logon session) it can report success
        # without persisting the pending name. Try it, then VERIFY the
        # pending name landed in the registry; if it did not, write the
        # name directly to the keys Windows reads at boot.
        try {
            Rename-Computer -NewName $hostname -Force -ErrorAction Stop
            Write-Log "Rename-Computer invoked for '$hostname'."
        } catch {
            Write-Log "WARN: Rename-Computer failed: $($_.Exception.Message). Falling back to registry."
        }

        $pendingKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\ComputerName\ComputerName'
        $tcpipKey   = 'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters'

        $pendingName = (Get-ItemProperty -Path $pendingKey).ComputerName
        if ($pendingName -ne $netbiosName) {
            Set-ItemProperty -Path $pendingKey -Name 'ComputerName' -Value $netbiosName
            Set-ItemProperty -Path $tcpipKey   -Name 'Hostname'     -Value $hostname
            Set-ItemProperty -Path $tcpipKey   -Name 'NV Hostname'  -Value $hostname
            Write-Log "Hostname staged via registry ('$pendingName' -> '$netbiosName'); reboot applies it."
        } else {
            Write-Log "Hostname staged via Rename-Computer ('$pendingName'); reboot applies it."
        }
    }

    # -----------------------------------------------------------------------
    # 4. Configure network
    # -----------------------------------------------------------------------

    # Find the first connected physical adapter -- skip loopback/tunnels
    $nic = Get-NetAdapter |
        Where-Object { $_.Status -eq 'Up' -and $_.InterfaceDescription -notmatch 'Loopback|Hyper-V|isatap|Teredo' } |
        Sort-Object -Property ifIndex |
        Select-Object -First 1

    if (-not $nic) {
        throw "No active network adapter found. Cannot configure static IP."
    }

    Write-Log "Using adapter: $($nic.Name) ($($nic.InterfaceDescription))"

    # Remove existing IP configuration on this adapter
    Remove-NetIPAddress -InterfaceIndex $nic.ifIndex -Confirm:$false -ErrorAction SilentlyContinue
    Remove-NetRoute     -InterfaceIndex $nic.ifIndex -Confirm:$false -ErrorAction SilentlyContinue

    # Set static IP
    New-NetIPAddress `
        -InterfaceIndex  $nic.ifIndex `
        -IPAddress       $ip `
        -PrefixLength    $prefix `
        -DefaultGateway  $gateway | Out-Null

    Write-Log "IP address set: $ip/$prefix via $gateway"

    # Set DNS servers
    $dnsServers = @($dns1)
    if ($dns2) { $dnsServers += $dns2 }
    Set-DnsClientServerAddress -InterfaceIndex $nic.ifIndex -ServerAddresses $dnsServers
    Write-Log "DNS servers set: $($dnsServers -join ', ')"

    # Set DNS search suffix if provided
    if ($dnsSuffix) {
        Set-DnsClient -InterfaceIndex $nic.ifIndex -ConnectionSpecificSuffix $dnsSuffix
        Write-Log "DNS suffix set: $dnsSuffix"
    }

    # -----------------------------------------------------------------------
    # 5. Stage finalize pass: cleanup + automatic second reboot
    # -----------------------------------------------------------------------

    # The reboot below applies the staged hostname, but the new name is not
    # fully active until one more restart. Automate that with a one-shot
    # SYSTEM task that fires at the next startup: it removes the first-boot
    # artifacts, deregisters itself, and reboots a final time. (Startup
    # trigger, not logon -- nobody logs on to a freshly deployed VM.)

    $selfDir     = Split-Path -Parent $MyInvocation.MyCommand.Path
    $finalizePs1 = "$env:SystemRoot\Temp\FirstBootFinalize.ps1"

    $finalizeContent = @"
# One-shot finalize pass, registered by FirstBoot.ps1. Runs as SYSTEM at the
# first startup after first-boot configuration was applied.
if (-not (Test-Path '$selfDir\FirstBoot.ps1')) {
    # Artifacts already gone: finalize ran on an earlier boot. Never reboot
    # from here again -- just make sure the task is gone, then bail.
    Unregister-ScheduledTask -TaskName 'FirstBootFinalize' -Confirm:`$false -ErrorAction SilentlyContinue
    exit 0
}
Add-Content -Path '$LogPath' -Value "`$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  Finalize: hostname now '`$env:COMPUTERNAME'; cleaning up and performing final reboot."
Remove-Item -Path '$selfDir\FirstBoot.ps1'     -Force -ErrorAction SilentlyContinue
Remove-Item -Path '$selfDir\SetupComplete.cmd' -Force -ErrorAction SilentlyContinue
# Deregister BEFORE rebooting so this task cannot fire in a reboot loop.
Unregister-ScheduledTask -TaskName 'FirstBootFinalize' -Confirm:`$false -ErrorAction SilentlyContinue
Remove-Item -Path '$finalizePs1' -Force -ErrorAction SilentlyContinue
# Give the boot a moment to settle before restarting.
Start-Sleep -Seconds 15
Restart-Computer -Force
"@

    Set-Content -Path $finalizePs1 -Value $finalizeContent -Encoding UTF8

    $action    = New-ScheduledTaskAction -Execute 'powershell.exe' `
                     -Argument "-ExecutionPolicy RemoteSigned -WindowStyle Hidden -File `"$finalizePs1`""
    $trigger   = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName 'FirstBootFinalize' `
        -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
        -Force | Out-Null

    Write-Log "Finalize task registered (next startup: cleanup + final reboot to fully activate hostname)."

    # -----------------------------------------------------------------------
    # 6. Reboot (finalize task reboots once more at next startup)
    # -----------------------------------------------------------------------

    Write-Log "Configuration complete -- rebooting."
    Restart-Computer -Force

} catch {

    $errMsg = $_.Exception.Message
    Write-Log "ERROR: First-boot configuration failed: $errMsg"

    # Separate error log so a failure is easy to spot
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    "$timestamp  ERROR: $errMsg`n$($_.ScriptStackTrace)" |
        Set-Content -Path $ErrorLogPath -Encoding UTF8 -Force

    Write-Log "Error details written to: $ErrorLogPath. Machine NOT rebooted."
    exit 1
}
'@

    Set-Content -Path $firstBootPs1 -Value $firstBootContent -Encoding UTF8
    Add-StagedFile -Path $firstBootPs1
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
    # Record the original policy first so a rollback can restore it.
    $script:RollbackState.executionPolicyChanged  = $true
    $script:RollbackState.originalExecutionPolicy = [string]$currentPolicy
    Save-RollbackState
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
    oobeSystem pass: runs after Sysprep OOBE on first boot of deployed VM.
    Skips all interactive screens.
    First-boot configuration runs via SetupComplete.cmd as SYSTEM and logs
    to C:\Windows\Temp\firstboot.log.
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

      <!-- One-time auto-logon; the FirstBootCleanup scheduled task fires at this logon -->
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
Add-StagedFile -Path $unattendPath
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
    Write-Host "  Or undo the staging with: .\$($MyInvocation.MyCommand.Name) -Rollback" -ForegroundColor Yellow
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
    Write-Warn "To undo the staging instead, run: .\$($MyInvocation.MyCommand.Name) -Rollback"
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
    $errLog = "$env:SystemRoot\System32\Sysprep\Panther\setuperr.log"
    if (Test-Path $errLog) {
        Write-Host "`n  Last 20 lines of $errLog :" -ForegroundColor Yellow
        Get-Content -Path $errLog -Tail 20 | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
    } else {
        Write-Host "  Check C:\Windows\System32\Sysprep\Panther\setupact.log for details." -ForegroundColor Yellow
    }
    # Throwing routes through the failure trap, which rolls back the staging.
    throw "Sysprep did not complete generalize (GeneralizationState=$genState, exit code $($proc.ExitCode))."
}

# Generalize succeeded -- past the point of no return, so the staged files
# must stay. Drop the rollback state so a later -Rollback cannot unseal the
# image by deleting them.
Remove-Item -Path $script:StateFile -Force -ErrorAction SilentlyContinue

# /shutdown should be powering the VM off right now. If we are still alive
# after a grace period, force the shutdown ourselves so the golden image is
# never left running in a sealed state.
Write-OK "Sysprep generalize completed. VM is shutting down."
Start-Sleep -Seconds 90
Write-Warn "Machine still running 90s after Sysprep finished -- forcing shutdown."
Stop-Computer -Force

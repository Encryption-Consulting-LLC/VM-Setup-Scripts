# Unit tests for the first-boot runner's pure functions (manifest parsing,
# script dispatch, disc-root discovery hook). Cross-platform: runs under pwsh
# on the dev machine. The execution-loop integration tests (real
# powershell.exe / cmd.exe child processes) are Windows-only and must pass on
# any Windows box once before a golden-image build.
#
# Run: pwsh -NoProfile -Command "Invoke-Pester -Path tests/FirstBoot.Tests.ps1"

BeforeAll {
    # Dot-sourcing stops at the runner's test guard: functions are defined,
    # the main flow does not run.
    . (Join-Path $PSScriptRoot '..' 'base-vm-setup' 'windows-server' 'FirstBoot.ps1')

    function New-Manifest {
        param([string]$Json)
        $path = Join-Path $TestDrive "firstboot.manifest"
        Set-Content -Path $path -Value $Json -Encoding UTF8
        return $path
    }
}

Describe 'Single-reboot orchestration sources' {
    BeforeAll {
        $windowsSetupDir = Join-Path $PSScriptRoot '..' 'base-vm-setup' 'windows-server'
        $runnerSource = Get-Content (Join-Path $windowsSetupDir 'FirstBoot.ps1') -Raw
        $wrapperSource = Get-Content (Join-Path $windowsSetupDir 'SetupComplete.cmd') -Raw
    }

    It 'leaves reboot and cleanup to SetupComplete' {
        $runnerSource | Should -Not -Match 'FirstBootFinalize'
        $runnerSource | Should -Not -Match 'ScheduledTask'
        $runnerSource | Should -Not -Match 'Restart-Computer'
        $runnerSource | Should -Match 'Returning to SetupComplete\.cmd'
    }

    It 'contains exactly one reboot command and no finalize task or script' {
        @([regex]::Matches($wrapperSource, '(?im)^\s*shutdown\.exe\s')).Count | Should -Be 1
        $wrapperSource | Should -Not -Match 'FirstBootFinalize'
        $wrapperSource | Should -Not -Match 'schtasks|ScheduledTask'
    }

    It 'gates reboot and cleanup on a successful runner exit' {
        $exitGate = $wrapperSource.IndexOf('if not "%FIRSTBOOT_EXIT%"=="0"')
        $shutdown = $wrapperSource.IndexOf('shutdown.exe /r')
        $cleanup = $wrapperSource.IndexOf('del /f /q "%~dp0FirstBoot.ps1"')

        $exitGate | Should -BeGreaterThan -1
        $shutdown | Should -BeGreaterThan $exitGate
        $cleanup | Should -BeGreaterThan $shutdown
    }

    It 'retains artifacts when reboot scheduling fails' {
        $shutdownGate = $wrapperSource.IndexOf('if not "%SHUTDOWN_EXIT%"=="0"')
        $cleanup = $wrapperSource.IndexOf('del /f /q "%~dp0FirstBoot.ps1"')

        $shutdownGate | Should -BeGreaterThan -1
        $cleanup | Should -BeGreaterThan $shutdownGate
        $wrapperSource | Should -Match 'Failed to schedule the first-boot reboot.*Artifacts retained'
    }

    It 'logs cleanup failures and removes the wrapper last' {
        @([regex]::Matches($wrapperSource, 'Cleanup could not remove')).Count | Should -Be 3
        @($wrapperSource.TrimEnd() -split "`r?`n")[-1] | Should -Match '^del /f /q "%~f0".*FIRSTBOOT_LOG.*ERROR: Cleanup could not remove'
    }
}

Describe 'Read-FirstBootManifest' {

    It 'parses a v1 manifest (no files key) without throwing under StrictMode' {
        # THE StrictMode regression guard: v1 manifests have no 'files'
        # property, and the runner runs Set-StrictMode -Version Latest.
        $path = New-Manifest '{"version": 1, "scripts": ["10-hostname.ps1", "20-network.ps1"]}'

        $m = Read-FirstBootManifest -Path $path

        $m.Version | Should -Be 1
        @($m.Scripts) | Should -Be @('10-hostname.ps1', '20-network.ps1')
        @($m.Files).Count | Should -Be 0
    }

    It 'parses a v2 manifest with files' {
        $path = New-Manifest '{"version": 2, "scripts": ["10-a.ps1", "30-c.cmd"], "files": ["agent.exe", "conf.json"]}'

        $m = Read-FirstBootManifest -Path $path

        $m.Version | Should -Be 2
        @($m.Scripts) | Should -Be @('10-a.ps1', '30-c.cmd')
        @($m.Files) | Should -Be @('agent.exe', 'conf.json')
    }

    It 'treats an empty files list as no files' {
        $path = New-Manifest '{"version": 2, "scripts": ["10-a.ps1"], "files": []}'

        @((Read-FirstBootManifest -Path $path).Files).Count | Should -Be 0
    }

    It 'rejects a manifest with an empty scripts list' {
        $path = New-Manifest '{"version": 2, "scripts": [], "files": ["agent.exe"]}'

        { Read-FirstBootManifest -Path $path } | Should -Throw "*no non-empty 'scripts' list*"
    }

    It 'rejects a manifest without a scripts key' {
        $path = New-Manifest '{"version": 1}'

        { Read-FirstBootManifest -Path $path } | Should -Throw "*no non-empty 'scripts' list*"
    }

    It 'parses a manifest without a version key' {
        $path = New-Manifest '{"scripts": ["10-a.ps1"]}'

        (Read-FirstBootManifest -Path $path).Version | Should -BeNullOrEmpty
    }

    It 'tolerates a newer manifest version' {
        $path = New-Manifest '{"version": 3, "scripts": ["10-a.ps1"], "files": [], "future": true}'

        $m = Read-FirstBootManifest -Path $path

        $m.Version | Should -Be 3
        @($m.Scripts) | Should -Be @('10-a.ps1')
    }
}

Describe 'Get-ScriptInvocation' {

    It 'dispatches .ps1 to powershell.exe -File' {
        $inv = Get-ScriptInvocation -StagedPath 'C:\work\10-hostname.ps1'

        $inv.FilePath | Should -Be 'powershell.exe'
        $inv.ArgumentList[-1] | Should -Be 'C:\work\10-hostname.ps1'
        $inv.ArgumentList | Should -Contain '-File'
        $inv.ArgumentList | Should -Contain 'Bypass'
    }

    It 'dispatches .cmd and .bat to the command interpreter with /d /c' {
        $env:ComSpec = if ($env:ComSpec) { $env:ComSpec } else { 'C:\Windows\system32\cmd.exe' }

        foreach ($ext in @('cmd', 'bat')) {
            $inv = Get-ScriptInvocation -StagedPath "C:\work\30-install.$ext"

            $inv.FilePath | Should -Be $env:ComSpec
            $inv.ArgumentList[0] | Should -Be '/d'
            $inv.ArgumentList[1] | Should -Be '/c'
            $inv.ArgumentList[2] | Should -Be "`"C:\work\30-install.$ext`""
        }
    }

    It 'is case-insensitive on the extension' {
        (Get-ScriptInvocation -StagedPath 'C:\work\A.PS1').FilePath | Should -Be 'powershell.exe'
    }

    It 'throws on unsupported script types' {
        { Get-ScriptInvocation -StagedPath 'C:\work\agent.exe' } | Should -Throw "*Unsupported script type '.exe'*"
        { Get-ScriptInvocation -StagedPath 'C:\work\setup.sh' } | Should -Throw "*Unsupported script type '.sh'*"
    }
}

Describe 'Get-ConfigDiscRoots' {

    It 'honors the FIRSTBOOT_SEARCH_ROOTS test hook' {
        $env:FIRSTBOOT_SEARCH_ROOTS = "$TestDrive;$TestDrive/other"
        try {
            @(Get-ConfigDiscRoots) | Should -Be @("$TestDrive", "$TestDrive/other")
        } finally {
            Remove-Item Env:FIRSTBOOT_SEARCH_ROOTS -ErrorAction SilentlyContinue
        }
    }

    It 'drops empty segments from the override' {
        $env:FIRSTBOOT_SEARCH_ROOTS = ";$TestDrive;"
        try {
            @(Get-ConfigDiscRoots) | Should -Be @("$TestDrive")
        } finally {
            Remove-Item Env:FIRSTBOOT_SEARCH_ROOTS -ErrorAction SilentlyContinue
        }
    }
}

# Windows-only integration: real child processes, exit-code propagation.
# Run once on any Windows box before building a golden image from this runner.
Describe 'Execution loop (Windows-only)' -Skip:(-not $IsWindows) {

    It 'propagates the exit code of a cmd.exe /c batch script' {
        $bat = Join-Path $TestDrive 'exit3.cmd'
        Set-Content -Path $bat -Value "@echo off`r`nexit /b 3" -Encoding ASCII

        $inv = Get-ScriptInvocation -StagedPath $bat
        $proc = Start-Process -FilePath $inv.FilePath -ArgumentList $inv.ArgumentList `
            -WindowStyle Hidden -Wait -PassThru

        $proc.ExitCode | Should -Be 3
    }

    It 'runs a .ps1 through powershell.exe and captures output' {
        $ps1 = Join-Path $TestDrive 'hello.ps1'
        Set-Content -Path $ps1 -Value "Write-Output 'hello-from-child'; exit 0" -Encoding UTF8
        $out = Join-Path $TestDrive 'hello.out'

        $inv = Get-ScriptInvocation -StagedPath $ps1
        $proc = Start-Process -FilePath $inv.FilePath -ArgumentList $inv.ArgumentList `
            -RedirectStandardOutput $out -WindowStyle Hidden -Wait -PassThru

        $proc.ExitCode | Should -Be 0
        Get-Content $out | Should -Contain 'hello-from-child'
    }
}

Describe 'SetupComplete execution (Windows-only)' -Skip:(-not $IsWindows) {
    BeforeEach {
        $caseDir = Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        $scriptsDir = Join-Path $caseDir 'Windows' 'Setup' 'Scripts'
        $tempDir = Join-Path $caseDir 'Windows' 'Temp'
        $workDir = Join-Path $tempDir 'firstboot-scripts'
        New-Item -ItemType Directory -Path $scriptsDir, $workDir -Force | Out-Null
        Set-Content -Path (Join-Path $workDir 'payload.bin') -Value 'diagnostic payload'

        $setupComplete = Join-Path $scriptsDir 'SetupComplete.cmd'
        Copy-Item (Join-Path $PSScriptRoot '..' 'base-vm-setup' 'windows-server' 'SetupComplete.cmd') $setupComplete
        $shutdownCalls = Join-Path $caseDir 'shutdown-calls.txt'
        $shutdownStub = Join-Path $caseDir 'shutdown-stub.cmd'
        Set-Content -Path $shutdownStub -Encoding ASCII -Value @"
@echo off
echo reboot>>"$shutdownCalls"
exit /b %FIRSTBOOT_TEST_SHUTDOWN_EXIT%
"@

        $savedSystemRoot = $env:SystemRoot
        $savedShutdownCommand = $env:FIRSTBOOT_TEST_SHUTDOWN_COMMAND
        $savedShutdownExit = $env:FIRSTBOOT_TEST_SHUTDOWN_EXIT
        $env:SystemRoot = Join-Path $caseDir 'Windows'
        $env:FIRSTBOOT_TEST_SHUTDOWN_COMMAND = $shutdownStub
        $env:FIRSTBOOT_TEST_SHUTDOWN_EXIT = '0'
    }

    AfterEach {
        $env:SystemRoot = $savedSystemRoot
        $env:FIRSTBOOT_TEST_SHUTDOWN_COMMAND = $savedShutdownCommand
        $env:FIRSTBOOT_TEST_SHUTDOWN_EXIT = $savedShutdownExit
    }

    It 'schedules one reboot and cleans up only after success' {
        Set-Content -Path (Join-Path $scriptsDir 'FirstBoot.ps1') -Encoding ASCII -Value 'exit 0'

        $proc = Start-Process -FilePath $env:ComSpec -ArgumentList @('/d', '/c', "`"$setupComplete`"") -Wait -PassThru

        $proc.ExitCode | Should -Be 0
        @(Get-Content $shutdownCalls).Count | Should -Be 1
        Test-Path (Join-Path $scriptsDir 'FirstBoot.ps1') | Should -BeFalse
        Test-Path $workDir | Should -BeFalse
        Test-Path $setupComplete | Should -BeFalse
        Test-Path (Join-Path $tempDir 'FirstBootFinalize.ps1') | Should -BeFalse
    }

    It 'preserves the runner exit code, diagnostics, and artifacts on failure' {
        Set-Content -Path (Join-Path $scriptsDir 'FirstBoot.ps1') -Encoding ASCII -Value @"
Set-Content -Path '$tempDir\firstboot-error.log' -Value 'runner failed'
exit 23
"@

        $proc = Start-Process -FilePath $env:ComSpec -ArgumentList @('/d', '/c', "`"$setupComplete`"") -Wait -PassThru

        $proc.ExitCode | Should -Be 23
        Test-Path $shutdownCalls | Should -BeFalse
        Test-Path (Join-Path $tempDir 'firstboot-error.log') | Should -BeTrue
        Test-Path (Join-Path $scriptsDir 'FirstBoot.ps1') | Should -BeTrue
        Test-Path $workDir | Should -BeTrue
        Test-Path $setupComplete | Should -BeTrue
    }

    It 'logs reboot scheduling failure and performs no cleanup' {
        Set-Content -Path (Join-Path $scriptsDir 'FirstBoot.ps1') -Encoding ASCII -Value 'exit 0'
        $env:FIRSTBOOT_TEST_SHUTDOWN_EXIT = '9'

        $proc = Start-Process -FilePath $env:ComSpec -ArgumentList @('/d', '/c', "`"$setupComplete`"") -Wait -PassThru

        $proc.ExitCode | Should -Be 9
        @(Get-Content $shutdownCalls).Count | Should -Be 1
        Get-Content (Join-Path $tempDir 'firstboot.log') -Raw | Should -Match 'Failed to schedule the first-boot reboot.*Artifacts retained'
        Test-Path (Join-Path $scriptsDir 'FirstBoot.ps1') | Should -BeTrue
        Test-Path $workDir | Should -BeTrue
        Test-Path $setupComplete | Should -BeTrue
    }
}

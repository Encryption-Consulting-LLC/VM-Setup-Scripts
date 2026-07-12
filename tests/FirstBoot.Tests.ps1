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

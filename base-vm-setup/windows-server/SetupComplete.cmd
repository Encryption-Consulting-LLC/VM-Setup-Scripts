@echo off
setlocal
:: FIRSTBOOT-SETUPCOMPLETE-MARKER (firstboot-sysprep.ps1 verifies this)
:: Windows launches this as SYSTEM after OOBE. FirstBoot.ps1 owns script
:: execution; this wrapper owns the sole reboot and success-only cleanup.

set "FIRSTBOOT_LOG=%SystemRoot%\Temp\firstboot.log"
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -WindowStyle Hidden -File "%~dp0FirstBoot.ps1"
set "FIRSTBOOT_EXIT=%ERRORLEVEL%"
if not "%FIRSTBOOT_EXIT%"=="0" (
    endlocal & exit /b %FIRSTBOOT_EXIT%
)

:: Schedule before deleting diagnostics or runners. If scheduling fails,
:: retain every artifact so the next investigation has full evidence.
if defined FIRSTBOOT_TEST_SHUTDOWN_COMMAND (
    call "%FIRSTBOOT_TEST_SHUTDOWN_COMMAND%" /r /t 15 /f /d p:4:1 /c "First-boot configuration complete" >> "%FIRSTBOOT_LOG%" 2>&1
) else (
    shutdown.exe /r /t 15 /f /d p:4:1 /c "First-boot configuration complete" >> "%FIRSTBOOT_LOG%" 2>&1
)
set "SHUTDOWN_EXIT=%ERRORLEVEL%"
if not "%SHUTDOWN_EXIT%"=="0" (
    >> "%FIRSTBOOT_LOG%" echo %date% %time%  ERROR: Failed to schedule the first-boot reboot ^(shutdown exit %SHUTDOWN_EXIT%^). Artifacts retained.
    endlocal & exit /b %SHUTDOWN_EXIT%
)

>> "%FIRSTBOOT_LOG%" echo %date% %time%  Reboot scheduled; cleaning up first-boot artifacts.
del /f /q "%~dp0FirstBoot.ps1" >> "%FIRSTBOOT_LOG%" 2>&1
if exist "%~dp0FirstBoot.ps1" >> "%FIRSTBOOT_LOG%" echo %date% %time%  ERROR: Cleanup could not remove %~dp0FirstBoot.ps1

rd /s /q "%SystemRoot%\Temp\firstboot-scripts" >> "%FIRSTBOOT_LOG%" 2>&1
if exist "%SystemRoot%\Temp\firstboot-scripts" >> "%FIRSTBOOT_LOG%" echo %date% %time%  ERROR: Cleanup could not remove %SystemRoot%\Temp\firstboot-scripts

:: Remove this wrapper last. Windows cmd.exe can finish an already-open batch
:: file after its directory entry is deleted.
del /f /q "%~f0" >> "%FIRSTBOOT_LOG%" 2>&1 || >> "%FIRSTBOOT_LOG%" echo %date% %time%  ERROR: Cleanup could not remove %~f0

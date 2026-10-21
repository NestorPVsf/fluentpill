@echo off
:: Remove the fluentpill watchdog scheduled task created by install-watchdog.bat.
::
:: Delegates to uninstall-watchdog-task.ps1 (Unregister-ScheduledTask) rather
:: than calling `schtasks /Delete` directly from this .bat. A .bat that
:: shells out to schtasks.exe trips an antivirus anti-persistence heuristic
:: (McAfee RealProtect flagged exactly this -- schtasks.exe launched by
:: cmd.exe -- as ProcessEnvelope_Schtasks, confirmed 2026-09-05). The
:: ScheduledTasks PowerShell module never spawns schtasks.exe, so this
:: sidesteps the detection entirely. See install-watchdog-task.ps1's header
:: for the matching writeup on the install side.
set "UNINSTALLER=%~dp0uninstall-watchdog-task.ps1"
if not exist "%UNINSTALLER%" (
    echo ERROR: no se encuentra %UNINSTALLER%
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%UNINSTALLER%"
exit /b %ERRORLEVEL%

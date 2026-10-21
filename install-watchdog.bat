@echo off
:: Register a Scheduled Task ("fluentpillWatchdog") that runs fluentpill-watchdog.ps1
:: every 5 minutes to relaunch fluentpill if it died (crash, external kill)
:: without the user asking for it. Run once. Undo with: uninstall-watchdog.bat
::
:: CRITICAL: this task must run in the CURRENT USER's interactive session,
:: NOT elevated and NOT as SYSTEM:
::   (a) a process started as SYSTEM/session 0 can't see the desktop, so
::       fluentpill's global hotkeys (pynput) would never fire;
::   (b) an elevated fluentpill can't paste into non-elevated windows (Windows
::       blocks synthetic input from a higher-privilege sender to a
::       lower-privilege target).
:: /RU + /IT runs it as the current user using their existing interactive
:: token (no stored password, and it simply won't fire if nobody is logged
:: on); /RL LIMITED is the non-elevated run level (also schtasks' default,
:: made explicit here on purpose).
::
:: The task action runs a .vbs wrapper (fluentpill-watchdog-hidden.vbs) instead of
:: calling powershell.exe directly. powershell's own -WindowStyle Hidden only
:: hides the console AFTER it is created, so a console window still flashes
:: on screen every 5 minutes. WScript.Shell.Run with window style 0 (SW_HIDE)
:: never shows the window at all -- no flash. The .vbs derives the .ps1 path
:: from its own location, so nothing here hardcodes an absolute path either.
set "PS1=%~dp0fluentpill-watchdog.ps1"
set "VBS=%~dp0fluentpill-watchdog-hidden.vbs"
set "INSTALLER=%~dp0install-watchdog-task.ps1"
if not exist "%PS1%" (
    echo ERROR: no se encuentra %PS1%
    exit /b 1
)
if not exist "%VBS%" (
    echo ERROR: no se encuentra %VBS%
    exit /b 1
)
if not exist "%INSTALLER%" (
    echo ERROR: no se encuentra %INSTALLER%
    exit /b 1
)

:: The actual task registration (action, trigger, principal, battery
:: settings) lives in install-watchdog-task.ps1 via Register-ScheduledTask,
:: NOT `schtasks /Create /TR "..."` -- that was tried first and verified
:: (2026-09-05) to fail with exit code 9020 ("El sistema no puede ejecutar
:: el programa especificado") for a wscript.exe action no matter how the
:: /TR string was quoted, even though the exact same action registers and
:: runs fine through Register-ScheduledTask. See the header comment in
:: install-watchdog-task.ps1 for the full writeup. This also folds in what
:: used to be a separate battery-settings patch step: Register-ScheduledTask
:: sets DisallowStartIfOnBatteries/StopIfGoingOnBatteries to false at
:: creation time in one shot, then reads the live task back to confirm
:: every field instead of trusting the call succeeded.
powershell -NoProfile -ExecutionPolicy Bypass -File "%INSTALLER%"

if errorlevel 1 (
    echo ERROR: no se pudo crear la tarea programada o no paso la verificacion.
    echo Revisalo a mano: Get-ScheduledTask -TaskName fluentpillWatchdog ^| Format-List *
    exit /b 1
)

echo Watchdog de fluentpill instalado - se comprueba cada 5 minutos.
echo Verifica usuario y nivel de privilegio con (PowerShell):
echo   Get-ScheduledTask -TaskName fluentpillWatchdog ^| Format-List *
echo Para desactivarlo: uninstall-watchdog.bat

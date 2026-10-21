@echo off
REM Mark this as a user-initiated stop so howl-watchdog.ps1 (if installed via
REM install-watchdog.bat) does not relaunch Howl a few minutes later. Written
REM BEFORE killing the process, same as the tray "Salir" action.
REM
REM This file uses REM (not ::) for comments and goto (not if-parentheses
REM blocks) for its error paths -- a combination of :: comments and (...)
REM blocks/goto targets in the same file made cmd.exe misparse comment text
REM as commands while this fix was being tested; REM has none of that risk.
if not exist "%LOCALAPPDATA%\Howl" mkdir "%LOCALAPPDATA%\Howl" >nul 2>&1
type nul > "%LOCALAPPDATA%\Howl\stopped-by-user"
if not exist "%LOCALAPPDATA%\Howl\stopped-by-user" goto :marker_error

REM Kill THIS project's Howl only. Matched by ExecutablePath (Win32_Process)
REM -- the absolute path of THIS venv's pythonw.exe -- not by "*main.py*" on
REM the command line, which would also kill unrelated Python projects on
REM this machine that happen to have their own main.py (see
REM howl-watchdog.ps1's header comment for the full story). (wmic is
REM removed on current Windows 11 builds. It failed silently here while the
REM script still printed "Howl detenido.", so a stop left Howl running AND
REM the stopped-by-user marker set, which in turn disarmed the watchdog.
REM Same PowerShell replacement already applied to Howl.bat.)
set "HOWL_PYTHONW=%~dp0venv\Scripts\pythonw.exe"
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name='pythonw.exe'\" | Where-Object { $_.ExecutablePath -eq $env:HOWL_PYTHONW } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
REM Also kill any visible python instances running main.py
taskkill /F /FI "WINDOWTITLE eq Howl" >nul 2>&1

REM Verify before claiming success: re-check for a surviving process with
REM this project's identity instead of printing "Howl detenido." unconditionally.
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"name='pythonw.exe'\" | Where-Object { $_.ExecutablePath -eq $env:HOWL_PYTHONW }) { exit 1 } else { exit 0 }"
if errorlevel 1 goto :still_running

echo Howl detenido.
exit /b 0

:marker_error
echo ERROR: no se pudo crear la marca stopped-by-user -- el watchdog podria relanzar Howl.
exit /b 1

:still_running
echo ERROR: Howl seguia corriendo tras el intento de parada.
exit /b 1

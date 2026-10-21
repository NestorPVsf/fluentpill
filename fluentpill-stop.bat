@echo off
REM Mark this as a user-initiated stop so fluentpill-watchdog.ps1 (if installed via
REM install-watchdog.bat) does not relaunch fluentpill a few minutes later. Written
REM BEFORE killing the process, same as the tray "Salir" action.
REM
REM This file uses REM (not ::) for comments and goto (not if-parentheses
REM blocks) for its error paths -- a combination of :: comments and (...)
REM blocks/goto targets in the same file made cmd.exe misparse comment text
REM as commands while this fix was being tested; REM has none of that risk.
REM Data folder: fluentpill; if it does not exist yet but the pre-rename Howl
REM folder does (app not started since the rename), use that one so the
REM marker lands where the old build and the watchdog are looking.
set "FP_DIR=%LOCALAPPDATA%\fluentpill"
if not exist "%FP_DIR%" if exist "%LOCALAPPDATA%\Howl" set "FP_DIR=%LOCALAPPDATA%\Howl"
if not exist "%FP_DIR%" mkdir "%FP_DIR%" >nul 2>&1
type nul > "%FP_DIR%\stopped-by-user"
if not exist "%FP_DIR%\stopped-by-user" goto :marker_error

REM Kill THIS project's fluentpill only. Matched by ExecutablePath (Win32_Process)
REM -- the absolute path of THIS venv's pythonw.exe -- not by "*main.py*" on
REM the command line, which would also kill unrelated Python projects on
REM this machine that happen to have their own main.py (see
REM fluentpill-watchdog.ps1's header comment for the full story). (wmic is
REM removed on current Windows 11 builds. It failed silently here while the
REM script still printed "fluentpill detenido.", so a stop left fluentpill running AND
REM the stopped-by-user marker set, which in turn disarmed the watchdog.
REM Same PowerShell replacement already applied to fluentpill.bat.)
set "FLUENTPILL_PYTHONW=%~dp0venv\Scripts\pythonw.exe"
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name='pythonw.exe'\" | Where-Object { $_.ExecutablePath -eq $env:FLUENTPILL_PYTHONW } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
REM Also kill any visible python instances running main.py
REM Window title: current name and the old one (a pre-rename build may still run).
taskkill /F /FI "WINDOWTITLE eq fluentpill" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Howl" >nul 2>&1

REM Verify before claiming success: re-check for a surviving process with
REM this project's identity instead of printing "fluentpill detenido." unconditionally.
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"name='pythonw.exe'\" | Where-Object { $_.ExecutablePath -eq $env:FLUENTPILL_PYTHONW }) { exit 1 } else { exit 0 }"
if errorlevel 1 goto :still_running

echo fluentpill detenido.
exit /b 0

:marker_error
echo ERROR: no se pudo crear la marca stopped-by-user -- el watchdog podria relanzar fluentpill.
exit /b 1

:still_running
echo ERROR: fluentpill seguia corriendo tras el intento de parada.
exit /b 1

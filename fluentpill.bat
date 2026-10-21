@echo off
REM Kill any previous fluentpill instance so re-launches don't pile up. Matched by
REM ExecutablePath (Win32_Process) -- the absolute, OS-resolved path of the
REM pythonw.exe that lives in THIS project's venv -- not by "*main.py*" on
REM the command line, which also matches every other Python project on this
REM machine that happens to have its own main.py (see fluentpill-watchdog.ps1's
REM header comment for the full story, including why CommandLine can't be
REM trusted here: it's whatever string launched the process -- relative, in
REM this very launch below -- while ExecutablePath is always absolute and
REM unique to this venv).
REM (wmic is removed on current Windows 11 builds. It failed silently and
REM the new launch died against the single-instance mutex; PowerShell
REM instead. Comments below are plain ASCII on purpose (2026-09-05): a "--"
REM em-dash in a REM/:: comment AFTER the powershell call below made cmd.exe
REM misparse later lines in this exact file as bogus commands -- reproduced
REM reliably, gone the moment the em-dash was swapped for "--". Suspected
REM cause: the child powershell.exe process shifts the shared console's
REM active code page, so cmd.exe decodes the batch file's remaining UTF-8
REM bytes differently once it resumes reading past that point.)
set "FLUENTPILL_PYTHONW=%~dp0venv\Scripts\pythonw.exe"
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name='pythonw.exe'\" | Where-Object { $_.ExecutablePath -eq $env:FLUENTPILL_PYTHONW } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
REM A manual/scripted start means the user wants fluentpill running again -- clear
REM the "stopped on purpose" marker so fluentpill-watchdog.ps1 doesn't skip a
REM later relaunch check thinking this is still an intentional stop. main.py
REM clears it too (that's what makes Windows autostart, which bypasses this
REM .bat, also clean up after itself) -- deleting it here as well means it's
REM gone immediately, without waiting on Python startup.
del "%LOCALAPPDATA%\fluentpill\stopped-by-user" >nul 2>&1
REM Same marker in the pre-rename folder (Howl), until main.py migrates it.
del "%LOCALAPPDATA%\Howl\stopped-by-user" >nul 2>&1
cd /d "%~dp0"
REM Launch headless using venv pythonw -- no console window. main.py is
REM passed as an absolute path (dp0-prefixed) so this launch's own identity
REM is unambiguous too, independent of the caller's cwd -- though
REM detection/kill above never relied on this: it already matches on
REM pythonw.exe's own resolved ExecutablePath, which is absolute no matter
REM how main.py's argument reads.
REM Logs go to the fluentpill folder under the user's local app data (set inside
REM main.py). For live logs, run: venv\Scripts\python.exe main.py
start "" venv\Scripts\pythonw.exe "%~dp0main.py"

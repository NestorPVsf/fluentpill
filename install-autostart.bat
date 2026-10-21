@echo off
:: Register fluentpill to start automatically with Windows (current user only).
:: Uses the venv pythonw directly (headless, no console flash). Run once.
:: Undo with: uninstall-autostart.bat
set "PYW=%~dp0venv\Scripts\pythonw.exe"
set "MAIN=%~dp0main.py"
:: Drop the pre-rename Run entry (Howl) first; no error if it is not there.
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v Howl /f >nul 2>&1
if not exist "%PYW%" (
    echo ERROR: no se encuentra %PYW%
    echo Crea el venv primero: python -m venv venv ^&^& pip install -r requirements.txt
    exit /b 1
)
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v fluentpill /t REG_SZ /d "\"%PYW%\" \"%MAIN%\"" /f >nul
if errorlevel 1 (
    echo ERROR: no se pudo escribir en el registro.
    exit /b 1
)
echo fluentpill arrancara automaticamente al iniciar sesion en Windows.
echo Para desactivarlo: uninstall-autostart.bat

@echo off
:: Remove the Howl autostart entry created by install-autostart.bat
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v Howl /f >nul 2>&1
echo Autoarranque de Howl desactivado.

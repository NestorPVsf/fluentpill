@echo off
:: Remove the fluentpill autostart entry created by install-autostart.bat
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v fluentpill /f >nul 2>&1
:: Also the pre-rename entry (Howl), if still there.
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v Howl /f >nul 2>&1
echo Autoarranque de fluentpill desactivado.

' Truly-hidden launcher for the Howl watchdog scheduled task.
'
' Why this file exists: the HowlWatchdog scheduled task used to call
' powershell.exe directly with -WindowStyle Hidden. That flag only tells
' PowerShell to hide the console AFTER it has already been created, so a
' console window still flashes on screen for a moment every time the task
' fires (every 5 minutes) -- reported as a real annoyance. WScript.Shell.Run
' with an intWindowStyle of 0 (SW_HIDE) asks Windows to never show the
' window in the first place, so there is no flash at all.
'
' The scheduled task now runs:
'   wscript.exe "<repo>\howl-watchdog-hidden.vbs"
' and this script just relays to the same powershell.exe command line that
' install-watchdog.bat used to register directly.
'
' Path is derived from THIS script's own location (WScript.ScriptFullName),
' never hardcoded, so it keeps working if the repo folder is ever moved or
' the task is reinstalled from a different checkout.

Dim fso, shell, scriptFolder, ps1Path, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptFolder = fso.GetParentFolderName(WScript.ScriptFullName)
ps1Path = fso.BuildPath(scriptFolder, "howl-watchdog.ps1")

If Not fso.FileExists(ps1Path) Then
    ' Nothing sensible to do without the script -- exit quietly. This runs
    ' unattended every 5 minutes with no console, so there is no one to
    ' show an error to; howl-watchdog.ps1 itself is what writes to
    ' howl.log, and it can't do that if it does not exist.
    WScript.Quit 1
End If

cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1Path & """"

' 0 = SW_HIDE (window never shown, not even briefly).
' False = do not wait for the child process to exit; the task should not
' stay "running" in Task Scheduler for longer than it takes to launch
' PowerShell.
shell.Run cmd, 0, False

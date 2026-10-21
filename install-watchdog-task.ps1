<#
.SYNOPSIS
    Registers (or re-registers) the "fluentpillWatchdog" scheduled task. Called by
    install-watchdog.bat -- not meant to be run standalone, though it is
    safe to do so (idempotent, -Force overwrites any prior task).

.DESCRIPTION
    Builds the task via the ScheduledTasks PowerShell module
    (Register-ScheduledTask) instead of `schtasks /Create /TR "..."`.

    This is NOT a style preference -- `schtasks /Create` was tried first and
    verified (2026-09-05) to fail with exit code 9020 ("El sistema no puede
    ejecutar el programa especificado") for this exact action, no matter how
    the /TR string was quoted:
        /TR "wscript.exe \"<path>\""
        /TR "wscript.exe <path>"          (no inner quotes at all)
        /TR "wscript.exe //B \"<path>\""  (extra flag before the path)
    All of the above fail identically when run from a real .bat file (cmd.exe
    reading its own script, not a quoting artifact introduced by this
    session's tools -- reproduced with `cmd /c install-watchdog.bat` and with
    a minimal throwaway .bat). The exact same action registers and runs fine
    when built through Register-ScheduledTask, which talks to the Task
    Scheduler COM API with structured parameters (Execute / Argument as
    separate fields) and never serializes them into one command-line string
    that has to be re-parsed by schtasks.exe's own argv logic. Root cause
    not fully pinned down (schtasks seems to require its /TR "program args"
    string to look like a MULTI-token command -- e.g. the original
    `powershell.exe -NoProfile ... -File "<path>"` action, which has several
    unquoted flag words before the quoted path, always worked) -- moot,
    since this approach sidesteps the whole class of bug.

    Also ruled out empirically and NOT the cause: %USERDOMAIN% containing an
    accented character (this machine's domain is "MY-LAPTOP"). That
    produces a *different*, distinct error ("no se efectuó ninguna
    asignación entre los nombres de cuenta y los identificadores de
    seguridad") only when the domain string is actually wrong/garbled -- the
    real accented domain read via $env:USERDOMAIN in a real .ps1 file (UTF-16
    throughout, no console code page involved) works correctly either way.

.NOTES
    CRITICAL constraints this script must not violate (see
    install-watchdog.bat for the full rationale):
      - Principal: current interactive user, NOT elevated, NOT SYSTEM/S4U.
        RunLevel Limited + LogonType Interactive is the equivalent of
        schtasks' /RL LIMITED /IT.
      - Battery settings: DisallowStartIfOnBatteries and
        StopIfGoingOnBatteries must both be $false (Task Scheduler defaults
        both to $true, which would silently disarm the watchdog on battery).
#>

$ErrorActionPreference = 'Stop'

$repoRoot = $PSScriptRoot
$ps1Path = Join-Path $repoRoot 'fluentpill-watchdog.ps1'
$vbsPath = Join-Path $repoRoot 'fluentpill-watchdog-hidden.vbs'
$taskName = 'fluentpillWatchdog'

# Same %LOCALAPPDATA%\fluentpill path fluentpill-watchdog.ps1 and main.py's
# core/lifecycle.py use for the watchdog-alive marker -- duplicated here
# rather than imported since this isn't Python and doesn't dot-source that
# script (see the marker-touch block near the bottom of this file).
# Data folder: fluentpill. If it does not exist yet but the pre-rename Howl
# folder does (app not started since the rename), use the old one: creating
# an empty fluentpill folder here would make main.py skip its one-time
# Howl -> fluentpill migration (both folders existing = nothing is touched).
$localAppData = [Environment]::GetFolderPath('LocalApplicationData')
$appDir = Join-Path $localAppData 'fluentpill'
$legacyDir = Join-Path $localAppData 'Howl'
if (-not (Test-Path $appDir) -and (Test-Path $legacyDir)) { $appDir = $legacyDir }

if (-not (Test-Path $ps1Path)) {
    Write-Output "ERROR: no se encuentra $ps1Path"
    exit 1
}
if (-not (Test-Path $vbsPath)) {
    Write-Output "ERROR: no se encuentra $vbsPath"
    exit 1
}

# Drop the pre-rename task first (no error if it was never there).
Unregister-ScheduledTask -TaskName 'HowlWatchdog' -Confirm:$false -ErrorAction SilentlyContinue

try {
    # Argument built with [char]34 rather than a literal embedded quote --
    # this file is invoked from a .bat via `-File`, so there is no cmd.exe
    # command-line re-parsing to worry about, but keeping the quote out of
    # the source text as a literal character removes any ambiguity either
    # way and matches how the value is verified below.
    $q = [char]34
    $action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ($q + $vbsPath + $q)

    # /SC MINUTE /MO 5, indefinitely -- New-ScheduledTaskTrigger has no true
    # "forever" duration ([TimeSpan]::MaxValue is rejected by the task XML
    # schema), so this uses 10 years as a practical stand-in for indefinite.
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) `
        -RepetitionDuration (New-TimeSpan -Days 3650)

    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel Limited

    # Both switches are phrased positively by this cmdlet -- passing them
    # is what sets DisallowStartIfOnBatteries=$false and
    # StopIfGoingOnBatteries=$false on the resulting Settings object.
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 4

    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
} catch {
    Write-Output "ERROR: no se pudo crear la tarea programada: $($_.Exception.Message)"
    exit 1
}

# Read the live task back rather than trusting the call succeeded -- same
# philosophy the old battery-settings patch in install-watchdog.bat used.
$task = Get-ScheduledTask -TaskName $taskName
$expectedArg = $q + $vbsPath + $q

Write-Output ("Execute=" + $task.Actions[0].Execute)
Write-Output ("Argument=" + $task.Actions[0].Arguments)
Write-Output ("DisallowStartIfOnBatteries=" + $task.Settings.DisallowStartIfOnBatteries)
Write-Output ("StopIfGoingOnBatteries=" + $task.Settings.StopIfGoingOnBatteries)
Write-Output ("Enabled=" + $task.Settings.Enabled)
Write-Output ("RunLevel=" + $task.Principal.RunLevel)
Write-Output ("LogonType=" + $task.Principal.LogonType)

$ok = ($task.Actions[0].Execute -eq 'wscript.exe') -and
      ($task.Actions[0].Arguments -eq $expectedArg) -and
      (-not $task.Settings.DisallowStartIfOnBatteries) -and
      (-not $task.Settings.StopIfGoingOnBatteries) -and
      ($task.Settings.Enabled) -and
      ($task.Principal.RunLevel -eq 'Limited') -and
      ($task.Principal.LogonType -eq 'Interactive')

if (-not $ok) {
    Write-Output "ERROR: la tarea se creo pero no paso la verificacion de campos."
    exit 1
}

# Touch the watchdog-alive marker right now (2026-09-05) -- main.py's
# heartbeat timer treats a marker that has NEVER existed as "no watchdog
# installed" (silence) and an existing-but-old one as "watchdog stopped
# running" (warns after WATCHDOG_STALE_SECONDS, 15 min by default). Without
# this, a fresh install would sit in the "never existed" state for up to 5
# minutes until the task's first tick -- harmless timing-wise (still
# silence, not a false warning) but this makes a freshly-installed watchdog
# immediately visible to fluentpill instead of waiting on the first scheduled run.
#
# This duplicates fluentpill-watchdog.ps1's Update-AliveMarker write logic rather
# than dot-sourcing that script, which would also execute its top-level
# relaunch logic (fluentpill might not even be running yet at install time) as an
# unwanted side effect. Not fatal if it fails -- the scheduled task's first
# run five minutes from now will create the marker anyway.
try {
    if (-not (Test-Path $appDir)) {
        New-Item -ItemType Directory -Path $appDir -Force | Out-Null
    }
    $aliveMarker = Join-Path $appDir 'watchdog-alive'
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllBytes($aliveMarker, $utf8NoBom.GetBytes($timestamp))
    Write-Output "AliveMarker=$aliveMarker"
} catch {
    Write-Output "AVISO: no se pudo crear el marker inicial del watchdog: $($_.Exception.Message)"
}

exit 0

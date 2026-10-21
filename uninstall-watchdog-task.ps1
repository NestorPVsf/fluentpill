<#
.SYNOPSIS
    Removes the "HowlWatchdog" scheduled task. Called by
    uninstall-watchdog.bat -- not meant to be run standalone, though it is
    safe to do so.

.DESCRIPTION
    Uses Unregister-ScheduledTask (ScheduledTasks PowerShell module)
    instead of `schtasks /Delete /TN "..."`, for the same reason
    install-watchdog-task.ps1 uses Register-ScheduledTask instead of
    `schtasks /Create`: a .bat file that shells out to schtasks.exe trips
    an antivirus anti-persistence heuristic (McAfee RealProtect flagged
    exactly this pattern -- schtasks.exe launched by cmd.exe -- as
    "ProcessEnvelope_Schtasks", a false positive but a very visible one,
    confirmed 2026-09-05 in C:\ProgramData\McAfee\wps\detection.log after
    repeated schtasks.exe calls from test .bat files during development).
    Register-ScheduledTask / Unregister-ScheduledTask talk to the Task
    Scheduler service through the ScheduledTasks module (COM/CIM), never
    spawning schtasks.exe as a child of cmd.exe, so this specific detection
    signature never fires.

    Distinguishes "wasn't installed" from "couldn't be removed" instead of
    always claiming success, same as the old schtasks-based version did.

    Also removes the watchdog-alive marker (2026-09-05). main.py's health
    check treats a marker that has NEVER existed as "no watchdog installed"
    (silence) but an existing-and-aging one as "watchdog stopped running"
    (warns after WATCHDOG_STALE_SECONDS). Leaving a stale marker behind
    after a deliberate uninstall would eventually produce exactly that false
    warning for a user who no longer wants the watchdog at all -- removing
    it here restores the true "never existed" state uninstalling implies.
#>

$ErrorActionPreference = 'Stop'
$taskName = 'HowlWatchdog'

$localAppData = [Environment]::GetFolderPath('LocalApplicationData')
$aliveMarker = Join-Path (Join-Path $localAppData 'Howl') 'watchdog-alive'

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Output "El watchdog de Howl no estaba instalado."
    # Still clean up a leftover marker from a previous install, if any --
    # see the header comment above for why a stale marker with no task
    # behind it would be a false-warning trap.
    Remove-Item -Path $aliveMarker -Force -ErrorAction SilentlyContinue
    exit 0
}

try {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
} catch {
    Write-Output "ERROR: no se pudo desinstalar el watchdog de Howl: $($_.Exception.Message)"
    exit 1
}

$stillThere = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($stillThere) {
    Write-Output "ERROR: la tarea sigue apareciendo tras Unregister-ScheduledTask."
    exit 1
}

Remove-Item -Path $aliveMarker -Force -ErrorAction SilentlyContinue

Write-Output "Watchdog de Howl desinstalado."
exit 0

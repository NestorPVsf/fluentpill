<#
.SYNOPSIS
    Howl watchdog — relaunches Howl if it died without the user asking it to.

.DESCRIPTION
    Meant to run every 5 minutes via the "HowlWatchdog" scheduled task (see
    install-watchdog.bat). Identifies Howl by the ExecutablePath of a
    pythonw.exe process — the absolute, OS-resolved path of the pythonw.exe
    binary that lives in THIS repo's venv — never by matching "main.py" (or
    any other substring) against CommandLine.

    CommandLine is whatever string the caller used to launch the process:
    Howl.bat runs `venv\Scripts\pythonw.exe main.py` with main.py RELATIVE,
    so a CommandLine match against this repo's *absolute* main.py path never
    matched anything — Howl always looked dead to this script even while
    running fine (measured 2026-09-05: 0 processes found against 2 real
    ones). Matching "*main.py*" instead (as Howl.bat/howl-stop.bat used to)
    goes wrong the other way: the machine runs several unrelated Python
    projects, so it can also match (and, for the stop scripts, kill) someone
    else's main.py. ExecutablePath has neither problem — Windows always
    resolves it to the absolute on-disk image path regardless of how the
    process was invoked, and this repo's venv/Scripts/pythonw.exe is unique
    to it.

    One nuance verified empirically (2026-09-05): on this Python install,
    venv's own pythonw.exe is a small redirector — it launches the real
    ~100-150MB interpreter as a CHILD process, and that child's own
    ExecutablePath points at the base Python install (shared across every
    venv built from it, so NOT safe to match on its own). Only the
    redirector's ExecutablePath is unique to this repo. That's fine to rely
    on for both detection and killing: the redirector stays alive exactly as
    long as its child does, and killing the redirector was confirmed to also
    tear down the child (Windows job-object semantics of the venv launcher —
    verified by killing the redirector PID alone and watching the child PID
    disappear within ~2s, not left orphaned). If a future Python version
    changes that and a launcher outlives its child, the failure mode is
    still safe: this script would attempt a redundant relaunch, which harmlessly
    no-ops against Local\HowlSingleInstance (main.py's single-instance mutex).

    Does nothing if:
      - Howl is already running, or
      - the user stopped Howl on purpose — the stopped-by-user marker is
        present (written by howl-stop.bat and the tray "Salir" action;
        cleared by Howl.bat and by a normal main.py start, including the
        Windows autostart path). Resurrecting an intentional stop every 5
        minutes would make Howl impossible to actually turn off.

    Otherwise it relaunches Howl headless — same command Howl.bat uses — and
    appends a timestamped [watchdog] line to howl.log, so a morning-after
    read of the log shows not just "Howl died" (silence after a heartbeat,
    see main.py) but "Howl died and was relaunched at HH:MM".

    "Who watches the watchdog" (2026-09-05): a scheduled task can report
    LastTaskResult 0 while the wscript/powershell hop underneath it never
    actually ran the payload — that code is from wscript.exe, not this
    script. And a marker this script only wrote when it found a problem
    would be indistinguishable from a watchdog that stopped running
    entirely. So Update-AliveMarker below touches
    %LOCALAPPDATA%\Howl\watchdog-alive on EVERY pass — the stopped-by-user
    early exit, the nothing-to-do early exit, a successful relaunch, and
    even the catch block's unexpected-error path — from a `finally` block
    so it fires no matter which of those the try block hits. main.py's
    heartbeat timer reads that marker's age and warns (tray tooltip + one
    howl.log line per episode) once it goes stale; see core/lifecycle.py's
    watchdog_marker_age_seconds(). Verified empirically under
    real powershell.exe 5.1 (not pwsh) that a `finally` attached to a
    try/catch DOES run after an `exit` statement inside the try block, and
    that the script then terminates right there rather than falling through
    to any code after the try/finally — so this is safe to rely on for
    every branch below, not just the ones that don't call `exit`.
#>

$repoRoot = $PSScriptRoot
$mainPyPath = Join-Path $repoRoot 'main.py'
$pythonwExe = Join-Path $repoRoot 'venv\Scripts\pythonw.exe'

$localAppData = [Environment]::GetFolderPath('LocalApplicationData')
$howlDir = Join-Path $localAppData 'Howl'
$stopMarker = Join-Path $howlDir 'stopped-by-user'
$logPath = Join-Path $howlDir 'howl.log'
$aliveMarker = Join-Path $howlDir 'watchdog-alive'

function Write-WatchdogLog {
    param([string]$Message)
    try {
        if (-not (Test-Path $howlDir)) {
            New-Item -ItemType Directory -Path $howlDir -Force | Out-Null
        }
        $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        $line = "[watchdog] $timestamp $Message"
        # Explicit no-BOM UTF8 — Add-Content's default encoding differs
        # between Windows PowerShell and pwsh, and howl.log (written by
        # Python with encoding="utf-8") has no BOM. This keeps every line in
        # the file consistent regardless of which PowerShell runs the task.
        #
        # Opened via FileStream with FileShare.ReadWrite rather than the
        # [System.IO.File]::AppendAllText convenience method on purpose:
        # AppendAllText requests exclusive access, and Howl's own process
        # holds howl.log open for its entire life (main.py's _setup_logging,
        # line-buffered "a" mode) — confirmed empirically (2026-09-05) that
        # AppendAllText throws "used by another process" the whole time Howl
        # is alive, and the catch below used to swallow that silently. That
        # made this exact function look like it "did nothing" whenever the
        # watchdog ran while Howl (unbeknownst to the old detector) was
        # actually still alive — the relaunch attempt underneath still fired
        # and hit the single-instance mutex, but this confirmation line
        # never made it to the log, making a real event look like a no-op.
        # FileShare.ReadWrite matches what Python's own open() allows other
        # processes on Windows, so the write succeeds either way.
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        $bytes = $utf8NoBom.GetBytes("$line`r`n")
        $fs = [System.IO.File]::Open($logPath, [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)
        try {
            $fs.Write($bytes, 0, $bytes.Length)
            $fs.Flush()
        } finally {
            $fs.Close()
        }
    } catch {
        # Logging must never be the reason the watchdog itself fails.
    }
}

function Update-AliveMarker {
    # Touches (creates or overwrites) $aliveMarker with the current
    # timestamp. Called from the `finally` block at the bottom of this
    # script so it runs on EVERY pass regardless of which branch the try
    # block took -- see the "who watches the watchdog" note in the header
    # comment above for why that matters (a marker only written on the
    # relaunch path would be indistinguishable from a dead watchdog).
    #
    # Same no-BOM-UTF8 + FileShare.ReadWrite approach as Write-WatchdogLog
    # above, though there is no known reader holding this file open the way
    # Howl holds howl.log open -- ReadWrite here is just cheap insurance in
    # case main.py's read (core.lifecycle.watchdog_marker_age_seconds, a
    # plain stat() call) or another watchdog run overlaps in the same
    # instant.
    try {
        if (-not (Test-Path $howlDir)) {
            New-Item -ItemType Directory -Path $howlDir -Force | Out-Null
        }
        $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        $bytes = $utf8NoBom.GetBytes($timestamp)
        $fs = [System.IO.File]::Open($aliveMarker, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)
        try {
            $fs.Write($bytes, 0, $bytes.Length)
            $fs.Flush()
        } finally {
            $fs.Close()
        }
    } catch {
        # Marking aliveness must never be why the watchdog itself fails --
        # worst case main.py sees a stale marker and warns one episode late.
    }
}

try {
    if (Test-Path $stopMarker) {
        # User stopped Howl on purpose — do not resurrect it.
        exit 0
    }

    $aliveProcesses = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and ($_.ExecutablePath -eq $pythonwExe) }

    if ($aliveProcesses) {
        # This repo's venv pythonw.exe (the launcher — see .DESCRIPTION for
        # why matching just this one process is enough) is running —
        # nothing to do.
        exit 0
    }

    if (-not (Test-Path $pythonwExe)) {
        Write-WatchdogLog "ERROR: no se encuentra $pythonwExe -- no se puede relanzar"
        exit 1
    }

    # Same launch as Howl.bat: venv's pythonw.exe, headless, no console.
    Start-Process -FilePath $pythonwExe -ArgumentList "`"$mainPyPath`"" -WorkingDirectory $repoRoot -WindowStyle Hidden
    Write-WatchdogLog "Howl no estaba vivo - relanzado"
} catch {
    Write-WatchdogLog "ERROR inesperado en el watchdog: $($_.Exception.Message)"
    exit 1
} finally {
    Update-AliveMarker
}

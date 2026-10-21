"""lifecycle.py — startup/shutdown bookkeeping shared by main.py and the tray
icon.

Owns the on-disk marker howl-watchdog.ps1 reads to tell an intentional stop
(tray "Salir", howl-stop.bat) apart from a crash or an external kill — see
howl-watchdog.ps1. The marker's path is duplicated (not imported)
in Howl.bat, howl-stop.bat and howl-watchdog.ps1 because those aren't Python;
%LOCALAPPDATA%\\Howl\\stopped-by-user is the one fixed path all of them must
agree on.

Every function here is defensive (never raises) — a failure to write/clear
the marker must not block starting or stopping Howl itself.
"""

import os
import sys
import time
from pathlib import Path


def howl_appdata_dir() -> Path:
    """Same per-OS base directory as core/recordings.py's recordings_dir()
    and main.py's log dir — %LOCALAPPDATA%\\Howl on Windows, ~/.config/Howl
    elsewhere. Does NOT create the directory; callers that need it to exist
    create it themselves, so a read-only check (does the marker exist?)
    never has the side effect of creating the folder.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path.home() / ".config"
    return base / "Howl"


def stopped_marker_path() -> Path:
    """The file howl-watchdog.ps1 checks before relaunching a dead Howl.
    Its presence means the user stopped Howl on purpose — see
    mark_stopped_by_user() / clear_stopped_marker() below."""
    return howl_appdata_dir() / "stopped-by-user"


def mark_stopped_by_user() -> None:
    """Create the marker — call right before an intentional shutdown (tray
    "Salir"). howl-stop.bat does the batch equivalent. Never raises: a
    failure here just means the watchdog might relaunch Howl a bit too
    eagerly later, which is the safe direction to fail in."""
    try:
        d = howl_appdata_dir()
        d.mkdir(parents=True, exist_ok=True)
        stopped_marker_path().touch()
    except OSError as e:
        print(f"[lifecycle] could not write stopped-by-user marker: {e}")


def clear_stopped_marker() -> None:
    """Remove the marker on a normal start (manual, Howl.bat, or Windows
    autostart) — starting Howl again IS the signal that the user wants it
    running, so any earlier intentional stop no longer applies. Never
    raises."""
    try:
        stopped_marker_path().unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"[lifecycle] could not clear stopped-by-user marker: {e}")


# ---------------------------------------------------------------------------
# In-process shutdown reason (2026-09-05) — separate concern from the
# on-disk stopped-by-user marker above. That marker tells the WATCHDOG
# whether to relaunch; this in-memory flag tells the LOG, via main.py's
# atexit hook, why THIS process is exiting. atexit fires on every orderly
# interpreter shutdown, including right after sys.excepthook has already
# printed "HOWL CRASH" for an unhandled exception — so the atexit line must
# not claim "clean exit" unless something that actually knows the reason
# said so first. Whoever knows the reason calls mark_shutdown_reason():
# TrayIcon._quit() with "tray", main.py's sys.excepthook with
# "unhandled-exception". First reason wins — an unhandled exception is the
# more informative fact, and nothing should overwrite it on the way out.
# ---------------------------------------------------------------------------
_shutdown_reason: str | None = None


def mark_shutdown_reason(reason: str) -> None:
    """Record why the process is exiting, for the atexit log line in
    main.py. Never raises. First call wins (see module note above)."""
    global _shutdown_reason
    try:
        if _shutdown_reason is None:
            _shutdown_reason = reason
    except Exception:
        pass


def get_shutdown_reason() -> str | None:
    """None means nothing in this process ever identified why it's exiting
    — main.py's atexit hook must report that as unknown, never as clean."""
    return _shutdown_reason


# ---------------------------------------------------------------------------
# Watchdog health marker (2026-09-05) — "nobody watches the watchdog" fix.
# howl-watchdog.ps1's Update-AliveMarker touches this exact file on EVERY
# pass of the scheduled task (every 5 min), whether or not it had to
# relaunch Howl — a marker that only updates when something was wrong would
# be indistinguishable from a watchdog that stopped running entirely (a
# disabled task, a broken wscript hop, etc. all report LastTaskResult 0
# regardless). main.py's existing heartbeat QTimer checks this marker's age
# on every tick (no new timer) via watchdog_marker_age_seconds() below.
#
# The path is duplicated (not imported), same reasoning as stopped_marker_path()
# above: howl-watchdog.ps1 and install-watchdog-task.ps1 aren't Python, so
# they hardcode "watchdog-alive" under howl_appdata_dir() themselves.
# ---------------------------------------------------------------------------
def watchdog_marker_path() -> Path:
    """The file howl-watchdog.ps1 touches on every pass. Reading its mtime
    is how Howl tells "the watchdog task isn't running at all" apart from
    "the watchdog ran and found nothing to do" — both look identical from
    Howl's side unless something is watching this marker's age."""
    return howl_appdata_dir() / "watchdog-alive"


def watchdog_marker_age_seconds() -> float | None:
    """Seconds since the watchdog last touched its alive marker, or None if
    the marker has never existed on this machine.

    None is deliberately NOT the same as "very old" — it means no watchdog
    was ever installed, and main.py must stay silent in that case (Howl
    works fine standalone; plenty of users won't have the watchdog task at
    all). Only an existing-but-aging marker should ever trigger a warning.
    Never raises."""
    try:
        mtime = watchdog_marker_path().stat().st_mtime
    except OSError:
        return None
    return time.time() - mtime

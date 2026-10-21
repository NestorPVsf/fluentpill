"""lifecycle.py — startup/shutdown bookkeeping shared by main.py and the tray
icon.

Owns the on-disk marker fluentpill-watchdog.ps1 reads to tell an intentional stop
(tray "Salir", fluentpill-stop.bat) apart from a crash or an external kill — see
fluentpill-watchdog.ps1. The marker's path is duplicated (not imported)
in fluentpill.bat, fluentpill-stop.bat and fluentpill-watchdog.ps1 because those aren't Python;
%LOCALAPPDATA%\\fluentpill\\stopped-by-user is the one fixed path all of them must
agree on.

Every function here is defensive (never raises) — a failure to write/clear
the marker must not block starting or stopping fluentpill itself.
"""

import os
import sys
import time
from pathlib import Path


APP_DIR_NAME = "fluentpill"
LEGACY_APP_DIR_NAME = "Howl"  # pre-rename folder, see migrate_legacy_appdata()

_resolved_dir: Path | None = None
_migration_note: str | None = None


def _appdata_base() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return Path.home() / ".config"


def migrate_legacy_appdata(base: Path) -> "tuple[Path, str | None]":
    """One-time rename of <base>/Howl -> <base>/fluentpill. Returns
    (directory to use, note for the log or None). Never raises.

    - old exists, new doesn't -> os.replace(); on failure (a locked file)
      the OLD folder is used for this run and the note says why.
    - both exist -> nothing is touched, the new one wins.
    - neither exists -> the new one is created.
    """
    new, old = base / APP_DIR_NAME, base / LEGACY_APP_DIR_NAME
    try:
        if new.exists():
            return new, None
        if old.is_dir():
            try:
                os.replace(old, new)
                return new, f"[lifecycle] migrated {old} -> {new}"
            except OSError as e:
                return old, f"[lifecycle] could not rename {old} -> {new} ({e}); using the old folder"
        new.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return new, f"[lifecycle] appdata folder setup failed for {new}: {e}"
    return new, None


def appdata_dir() -> Path:
    """The app's per-OS data folder — %LOCALAPPDATA%\\fluentpill on Windows,
    ~/.config/fluentpill elsewhere. Shared by main.py's log, recordings,
    sounds and the TTS cache. The first call per process runs the legacy
    Howl -> fluentpill migration (cached afterwards); never raises."""
    global _resolved_dir, _migration_note
    if _resolved_dir is None:
        _resolved_dir, _migration_note = migrate_legacy_appdata(_appdata_base())
    return _resolved_dir


def migration_note() -> str | None:
    """What the migration did on this run (print it once the log is open)."""
    return _migration_note


def stopped_marker_path() -> Path:
    """The file fluentpill-watchdog.ps1 checks before relaunching a dead fluentpill.
    Its presence means the user stopped fluentpill on purpose — see
    mark_stopped_by_user() / clear_stopped_marker() below."""
    return appdata_dir() / "stopped-by-user"


def mark_stopped_by_user() -> None:
    """Create the marker — call right before an intentional shutdown (tray
    "Salir"). fluentpill-stop.bat does the batch equivalent. Never raises: a
    failure here just means the watchdog might relaunch fluentpill a bit too
    eagerly later, which is the safe direction to fail in."""
    try:
        d = appdata_dir()
        d.mkdir(parents=True, exist_ok=True)
        stopped_marker_path().touch()
    except OSError as e:
        print(f"[lifecycle] could not write stopped-by-user marker: {e}")


def clear_stopped_marker() -> None:
    """Remove the marker on a normal start (manual, fluentpill.bat, or Windows
    autostart) — starting fluentpill again IS the signal that the user wants it
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
# printed "FLUENTPILL CRASH" for an unhandled exception — so the atexit line must
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
# fluentpill-watchdog.ps1's Update-AliveMarker touches this exact file on EVERY
# pass of the scheduled task (every 5 min), whether or not it had to
# relaunch fluentpill — a marker that only updates when something was wrong would
# be indistinguishable from a watchdog that stopped running entirely (a
# disabled task, a broken wscript hop, etc. all report LastTaskResult 0
# regardless). main.py's existing heartbeat QTimer checks this marker's age
# on every tick (no new timer) via watchdog_marker_age_seconds() below.
#
# The path is duplicated (not imported), same reasoning as stopped_marker_path()
# above: fluentpill-watchdog.ps1 and install-watchdog-task.ps1 aren't Python, so
# they hardcode "watchdog-alive" under appdata_dir() themselves.
# ---------------------------------------------------------------------------
def watchdog_marker_path() -> Path:
    """The file fluentpill-watchdog.ps1 touches on every pass. Reading its mtime
    is how fluentpill tells "the watchdog task isn't running at all" apart from
    "the watchdog ran and found nothing to do" — both look identical from
    fluentpill's side unless something is watching this marker's age."""
    return appdata_dir() / "watchdog-alive"


def watchdog_marker_age_seconds() -> float | None:
    """Seconds since the watchdog last touched its alive marker, or None if
    the marker has never existed on this machine.

    None is deliberately NOT the same as "very old" — it means no watchdog
    was ever installed, and main.py must stay silent in that case (fluentpill
    works fine standalone; plenty of users won't have the watchdog task at
    all). Only an existing-but-aging marker should ever trigger a warning.
    Never raises."""
    try:
        mtime = watchdog_marker_path().stat().st_mtime
    except OSError:
        return None
    return time.time() - mtime

"""recordings.py — Optional debug WAV saver for fluentpill dictations.

Saves a copy of each recorded WAV to a per-OS directory so the user can review
whether a missed word was caused by the microphone or by Whisper.  All functions
are fully defensive: they catch every exception and never raise, so a disk error
cannot block the transcription pipeline.

Enable/disable via SAVE_RECORDINGS env var (default: true).
Retention is controlled by RECORDINGS_RETENTION_HOURS (default: 24).
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path

from core.lifecycle import appdata_dir


def recordings_dir() -> Path:
    """Return (and create) the platform-appropriate recordings directory.

    Windows : %LOCALAPPDATA%\\fluentpill\\recordings
    Others  : ~/.config/fluentpill/recordings

    Returns the directory Path, or a fallback temp path if creation fails.
    """
    try:
        rec_dir = appdata_dir() / "recordings"
        rec_dir.mkdir(parents=True, exist_ok=True)
        return rec_dir
    except Exception as e:
        print(f"[recordings] Could not create recordings dir: {e}")
        # Fallback to a temp subdirectory so callers still get a Path object
        fallback = Path(os.environ.get("TEMP", "/tmp")) / "fluentpill" / "recordings"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return fallback


def save_recording(wav_bytes: bytes) -> "Path | None":
    """Write *wav_bytes* to a timestamped file in recordings_dir().

    The filename uses a sortable, human-readable timestamp:
        YYYY-MM-DD_HH-MM-SS_microseconds.wav

    Returns the saved Path, or None if any error occurred. Callers that only
    need the filename (e.g. to store alongside a DB row) should use
    `dest.name` — never store the full path (machine-local, and the dir
    layout is an implementation detail of this module).
    """
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
        dest = recordings_dir() / f"{timestamp}.wav"
        dest.write_bytes(wav_bytes)
        print(f"[recordings] Saved {dest.name} ({len(wav_bytes):,} bytes)")
        return dest
    except Exception as e:
        print(f"[recordings] save_recording failed: {e}")
        return None


def cleanup_old_recordings(retention_hours: float) -> int:
    """Delete *.wav files in recordings_dir() older than *retention_hours*.

    Uses file mtime (seconds since epoch) for the age check.  A file that
    cannot be deleted is skipped so the rest of the cleanup still runs.

    Returns the number of files actually deleted.
    """
    deleted = 0
    try:
        cutoff = time.time() - retention_hours * 3600
        rec_dir = recordings_dir()
        for wav_file in rec_dir.glob("*.wav"):
            try:
                if wav_file.stat().st_mtime < cutoff:
                    wav_file.unlink()
                    deleted += 1
            except Exception as e:
                print(f"[recordings] Could not delete {wav_file.name}: {e}")
    except Exception as e:
        print(f"[recordings] cleanup_old_recordings failed: {e}")
    if deleted:
        print(f"[recordings] Cleaned up {deleted} old recording(s)")
    return deleted

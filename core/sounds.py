"""Audio cues for recording start/stop.

Motivation (2026-08-24): the floating pill can end up hidden/covered by
another window, and howl.log showed two dictation sessions of 718s and 720s
that recorded the whole time without the user noticing. A sound is the one
piece of feedback that doesn't depend on looking at the screen.

Windows implementation: two short WAV tones generated once in memory and
cached under %LOCALAPPDATA%\\Howl\\sounds\\, played with winsound.PlaySound
using SND_ASYNC. winsound.Beep() is deliberately NOT used — it BLOCKS the
calling thread for the full tone duration, and these calls happen on the UI
thread right as recording starts/stops.

macOS/Linux: no-op (the active deployment target is Windows).

This module must never raise — a sound-cue failure can never be allowed to
affect recording.
"""

import io
import math
import os
import struct
import sys
import wave
from pathlib import Path

import config

# Internal switch (in addition to config.SOUND_CUES_ENABLED) — flip to False
# here to silence cues unconditionally regardless of env/config, e.g. for
# local debugging without touching .env.
_ENABLED = True

_SAMPLE_RATE = 44100
_DURATION_S = 0.05  # 50ms — short and discrete, not an alarm
_FADE_S = 0.006      # 6ms linear fade in/out so the tone doesn't click

_START_FREQ_HZ = 880.0  # higher/ascending-feeling tone — recording started
_STOP_FREQ_HZ = 523.0   # lower tone — recording stopped

_START_FILENAME = "start.wav"
_STOP_FILENAME = "stop.wav"

_sounds_dir_cache: Path | None = None


def _sounds_dir() -> Path:
    global _sounds_dir_cache
    if _sounds_dir_cache is not None:
        return _sounds_dir_cache
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path.home() / ".config"
    d = base / "Howl" / "sounds"
    d.mkdir(parents=True, exist_ok=True)
    _sounds_dir_cache = d
    return d


def _generate_wav_bytes(freq_hz: float) -> bytes:
    """Short sine-wave tone as 16-bit PCM mono WAV bytes, with a linear
    fade-in/out so playback doesn't click at the boundaries."""
    n_samples = int(_SAMPLE_RATE * _DURATION_S)
    fade_samples = max(1, int(_SAMPLE_RATE * _FADE_S))
    samples = []
    for i in range(n_samples):
        t = i / _SAMPLE_RATE
        amp = 1.0
        if i < fade_samples:
            amp = i / fade_samples
        elif i > n_samples - fade_samples:
            amp = (n_samples - i) / fade_samples
        value = amp * 0.5 * math.sin(2 * math.pi * freq_hz * t)
        samples.append(int(value * 32767))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(struct.pack(f"<{n_samples}h", *samples))
    return buf.getvalue()


def _ensure_wav_file(filename: str, freq_hz: float) -> Path | None:
    """Write the tone to disk the first time it's needed; reuse it after.

    Writes to a sibling temp file and renames it into place (Path.replace ->
    os.replace, atomic on both POSIX and Windows for same-volume renames) so
    a crash/interruption mid-write can never leave a half-written WAV that a
    later run would try to play.
    """
    path = _sounds_dir() / filename
    if path.exists():
        return path
    try:
        data = _generate_wav_bytes(freq_hz)
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_bytes(data)
        tmp_path.replace(path)
        return path
    except OSError:
        return None


def _play(filename: str, freq_hz: float) -> None:
    if sys.platform != "win32":
        return  # no-op on macOS/Linux
    try:
        # config access lives INSIDE the try too — "never raises" has to be
        # literal, and a broken/absent config attribute must not propagate.
        if not _ENABLED or not config.SOUND_CUES_ENABLED:
            return
        import winsound
        path = _ensure_wav_file(filename, freq_hz)
        if path is None:
            return
        winsound.PlaySound(
            str(path),
            winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
        )
    except Exception:
        pass  # never let a sound-cue failure affect recording


def play_start() -> None:
    """Short high tone — recording has started."""
    _play(_START_FILENAME, _START_FREQ_HZ)


def play_stop() -> None:
    """Short lower tone — recording has stopped."""
    _play(_STOP_FILENAME, _STOP_FREQ_HZ)


def prewarm() -> None:
    """Pre-generate both cue WAV files up front (call once at app startup,
    ideally off the UI thread).

    Without this, generating the samples and writing the WAV happens lazily
    inside the first play_start() — synchronously, on the UI thread, right
    as recording starts. SND_ASYNC only makes *playback* non-blocking; disk
    I/O on that first call is not covered by it. Usually a few ms, but an AV
    scanner or a redirected profile can make it noticeable on the very first
    dictation after each launch.

    Idempotent (safe to call more than once — _ensure_wav_file reuses an
    existing file) and never raises.
    """
    try:
        if not _ENABLED or not config.SOUND_CUES_ENABLED:
            return
        if sys.platform != "win32":
            return
        _ensure_wav_file(_START_FILENAME, _START_FREQ_HZ)
        _ensure_wav_file(_STOP_FILENAME, _STOP_FREQ_HZ)
    except Exception:
        pass

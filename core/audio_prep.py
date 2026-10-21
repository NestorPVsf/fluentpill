"""Energy-based silence trimming and pause-splitting for dictation audio.

No VAD model (torch/onnx/silero) — plain per-window RMS on the same int16
scale as config.SILENCE_PEAK_RMS. Two problems this fixes:
  - Whisper hallucinates over leading/trailing silence in a recording.
  - Whisper accuracy degrades (garbled words, dropped accents) on long,
    continuous audio, so dictations over CHUNK_SECONDS are split at natural
    pauses before being sent — never mid-word.
"""

import numpy as np

from config import SAMPLE_RATE, SILENCE_PEAK_RMS

_WINDOW_MS = 30
_PAD_MS = 200
_MIN_PAUSE_MS = 600


def _samples(ms: float) -> int:
    return max(1, int(SAMPLE_RATE * ms / 1000))


def _window_rms(audio: np.ndarray, window: int) -> np.ndarray:
    """Per-window RMS, one value per full window (trailing partial window dropped)."""
    n_windows = len(audio) // window
    if n_windows == 0:
        return np.empty((0,), dtype=np.float32)
    a = audio[: n_windows * window].astype(np.float32).reshape(n_windows, window)
    return np.sqrt(np.mean(a * a, axis=1))


def trim_silence(audio: np.ndarray, threshold: float = SILENCE_PEAK_RMS) -> np.ndarray:
    """Trim leading/trailing silence, keeping ~200ms padding around speech."""
    if audio.size == 0:
        return audio
    window = _samples(_WINDOW_MS)
    rms = _window_rms(audio, window)
    speech = np.flatnonzero(rms >= threshold)
    if speech.size == 0:
        return audio  # all silence — let the existing silence gate handle it
    pad = _samples(_PAD_MS)
    start = max(0, speech[0] * window - pad)
    end = min(audio.size, (speech[-1] + 1) * window + pad)
    return audio[start:end]


def split_at_pauses(audio: np.ndarray, max_chunk_seconds: float, threshold: float = SILENCE_PEAK_RMS) -> list[np.ndarray]:
    """Split into chunks of at most max_chunk_seconds, cutting at the natural
    pause nearest each target boundary (never mid-word). Chunks are
    contiguous and cover the whole input exactly."""
    if max_chunk_seconds <= 0 or audio.size == 0:
        return [audio]
    max_samples = int(max_chunk_seconds * SAMPLE_RATE)
    if max_samples < SAMPLE_RATE:
        return [audio]  # sub-second chunk size is a misconfiguration — don't split
    if audio.size <= max_samples:
        return [audio]

    window = _samples(_WINDOW_MS)
    rms = _window_rms(audio, window)
    pause_windows = max(1, int(_MIN_PAUSE_MS / _WINDOW_MS))
    silent = rms < threshold
    n_windows = len(silent)

    def nearest_pause_cut(min_sample: int, target_sample: int) -> int | None:
        target_window = target_sample // window
        # A pause far past the target would silently produce an oversized
        # chunk — cap the overshoot at 10% of the chunk size.
        max_cut = target_sample + max_samples // 10
        seen = set()
        for offset in range(0, n_windows + 1):
            for idx in (target_window - offset, target_window + offset):
                if idx in seen or idx < 0 or idx + pause_windows > n_windows:
                    continue
                seen.add(idx)
                cut = (idx + pause_windows // 2) * window
                if cut <= min_sample or cut > max_cut:
                    continue
                if silent[idx: idx + pause_windows].all():
                    return cut
        return None

    chunks: list[np.ndarray] = []
    start = 0
    while audio.size - start > max_samples:
        target = start + max_samples
        cut = nearest_pause_cut(start, target)
        if cut is None:
            cut = target  # no natural pause found — hard cut
        cut = max(start + 1, min(cut, audio.size))
        chunks.append(audio[start:cut])
        start = cut
    chunks.append(audio[start:])
    return chunks

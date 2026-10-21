import io
import wave
import queue
import time
import numpy as np
import sounddevice as sd
from config import SAMPLE_RATE, CHANNELS, AUDIO_DTYPE, BLOCK_SIZE


def to_wav_buffer(audio: np.ndarray) -> io.BytesIO:
    """Wrap a mono int16 numpy array into an in-memory WAV buffer."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)  # 16-bit = 2 bytes
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(np.ascontiguousarray(audio, dtype=np.int16).tobytes())
    buf.seek(0)
    return buf


class AudioRecorder:
    def __init__(self):
        self.audio_queue = queue.Queue()  # For UI visualization
        self.frames: list[np.ndarray] = []
        self.stream: sd.InputStream | None = None
        self.is_recording = False
        self._start_time = 0.0

    def _callback(self, indata: np.ndarray, frames: int, time_info, status):
        if status:
            print(f"Audio status: {status}")
        self.audio_queue.put(indata.copy())
        self.frames.append(indata.copy())

    def start(self):
        self.frames.clear()
        # Drain any old data from the queue
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break
        self.is_recording = True
        self._start_time = time.time()
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=AUDIO_DTYPE,
            blocksize=BLOCK_SIZE,
            callback=self._callback,
        )
        self.stream.start()

    def stop(self) -> float:
        """Stop recording and return duration in seconds."""
        self.is_recording = False
        duration = time.time() - self._start_time
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        return duration

    def get_audio_array(self) -> np.ndarray:
        """Return recorded frames concatenated into a single mono int16 array."""
        if not self.frames:
            return np.empty((0,), dtype=np.int16)
        return np.concatenate(self.frames, axis=0).reshape(-1)

    def get_wav_buffer(self) -> io.BytesIO:
        """Convert recorded frames to in-memory WAV buffer."""
        if not self.frames:
            return io.BytesIO()
        return to_wav_buffer(self.get_audio_array())

    def get_duration(self) -> float:
        if not self.frames:
            return 0.0
        total_samples = sum(f.shape[0] for f in self.frames)
        return total_samples / SAMPLE_RATE

    def peak_rms(self, skip_initial_seconds: float = 0.0) -> float:
        """Return the highest per-block RMS across recorded frames.

        Frames are int16 (range ±32768). Silence / near-silence yields values
        well below 60; normal speech is typically above 500-2000. Call this
        *after* stop() — frames are preserved until the next start().

        skip_initial_seconds: ignore this many leading seconds before
        computing the peak. Exists because Howl's own start-of-recording beep
        (core/sounds.py) is picked up by the mic — measured on the real
        hardware (Microphone Array): ambient peak_rms=421, peak_rms=756
        during the ~50ms tone (~1.8x ambient). Without this, a truly silent
        recording (mic beep + nothing else) could clear a low
        SILENCE_PEAK_RMS threshold and still reach Whisper — exactly the
        hallucination scenario the silence gate exists to prevent. This does
        NOT change what gets transcribed — only what this function measures
        for the gate decision; callers still send the full, untrimmed audio.

        Safeguard: if skipping would discard every block (a very short
        recording), falls back to using ALL blocks instead of returning 0.0
        — 0.0 would silently drop the whole recording, the opposite of what
        this parameter is for.
        """
        if not self.frames:
            return 0.0
        skip_blocks = 0
        if skip_initial_seconds > 0:
            block_seconds = BLOCK_SIZE / SAMPLE_RATE
            skip_blocks = int(round(skip_initial_seconds / block_seconds))
        blocks = self.frames[skip_blocks:] if skip_blocks < len(self.frames) else self.frames
        peak = 0.0
        for block in blocks:
            a = block.astype(np.float32)
            r = float(np.sqrt(np.mean(a * a))) if a.size else 0.0
            if r > peak:
                peak = r
        return peak

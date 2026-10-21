"""Groq/OpenAI Whisper client with cross-vendor resilience.

Resilience lesson (from studying Wispr Flow): a single ASR
provider outage must not take dictation down — Flow logged 75+ cloud
incidents in 6 months and mitigates with multi-vendor failover.

Provider order is configurable via ASR_PRIMARY (config.py):
- "openai" (default, when OPENAI_API_KEY is set): OpenAI gpt-4o-transcribe
  once, then Groq whisper-large-v3 × N attempts as fallback. A/B testing
  on real dictation WAVs showed gpt-4o-transcribe noticeably more accurate
  than Groq's whisper-large-v3 on fast conversational Spanish.
- "groq" (or no OpenAI key): Groq × N attempts first, OpenAI as the
  cross-vendor fallback (original resilience design).

Either way, transcribe() raises only if the whole chain failed — the caller
shows the error state and the debug WAV (SAVE_RECORDINGS) preserves the audio
for post-mortem.
"""

import io
import time

from groq import Groq
from config import (
    GROQ_API_KEY,
    GROQ_MODEL,
    WHISPER_LANGUAGE,
    TRANSCRIBE_TIMEOUT_SECONDS,
    TRANSCRIBE_GROQ_ATTEMPTS,
    OPENAI_API_KEY,
    OPENAI_TRANSCRIBE_MODEL,
    OPENAI_TRANSCRIBE_FALLBACK,
    ASR_PRIMARY,
)


class Transcriber:
    def __init__(self):
        self.client = Groq(api_key=GROQ_API_KEY, timeout=TRANSCRIBE_TIMEOUT_SECONDS)
        self._whisper_prompt: str = ""

    def set_whisper_prompt(self, prompt: str):
        """Set vocabulary prompt to bias Whisper toward known words."""
        self._whisper_prompt = prompt

    def transcribe(self, wav_buffer: io.BytesIO, english: bool = False) -> str:
        """Transcribe WAV audio through the resilience chain.

        Order depends on ASR_PRIMARY (only "openai" if a key is configured):
        - openai-primary: OpenAI once, then Groq × TRANSCRIBE_GROQ_ATTEMPTS.
        - groq-primary (default order): Groq × TRANSCRIBE_GROQ_ATTEMPTS
          (backoff 0.5s·attempt between tries), then OpenAI once if
          OPENAI_TRANSCRIBE_FALLBACK and a key is present.
        """
        wav_buffer.seek(0)
        data = wav_buffer.read()
        if len(data) < 100:
            return ""

        if ASR_PRIMARY == "openai" and OPENAI_API_KEY:
            return self._transcribe_openai_primary(data, english)
        return self._transcribe_groq_primary(data, english)

    def _transcribe_groq_primary(self, data: bytes, english: bool = False) -> str:
        last_error: Exception | None = None
        attempts = max(1, TRANSCRIBE_GROQ_ATTEMPTS)
        for attempt in range(1, attempts + 1):
            try:
                return self._transcribe_groq(data, english)
            except Exception as e:
                last_error = e
                print(f"[transcriber] provider=groq attempt {attempt}/{attempts} FAILED: {e}")
                if attempt < attempts:
                    time.sleep(0.5 * attempt)

        if OPENAI_TRANSCRIBE_FALLBACK and OPENAI_API_KEY:
            try:
                return self._transcribe_openai(data, english)
            except Exception as e:
                print(f"[transcriber] provider=openai fallback FAILED: {e}")
                last_error = e

        raise last_error if last_error else RuntimeError("transcription failed")

    def _transcribe_openai_primary(self, data: bytes, english: bool = False) -> str:
        try:
            return self._transcribe_openai(data, english)
        except Exception as e:
            print(f"[transcriber] provider=openai (primary) FAILED: {e}")

        last_error: Exception | None = None
        attempts = max(1, TRANSCRIBE_GROQ_ATTEMPTS)
        for attempt in range(1, attempts + 1):
            try:
                return self._transcribe_groq(data, english)
            except Exception as e:
                last_error = e
                print(f"[transcriber] provider=groq attempt {attempt}/{attempts} FAILED: {e}")
                if attempt < attempts:
                    time.sleep(0.5 * attempt)

        raise last_error if last_error else RuntimeError("transcription failed")

    def _common_kwargs(self, english: bool = False) -> dict:
        kwargs: dict = {
            "file": ("recording.wav", None),  # data filled by caller
            "response_format": "text",
            "temperature": 0.0,
        }
        # "es" anchors Spanish (English tech words come via prompt). Empty
        # WHISPER_LANGUAGE omits the param → the model auto-detects.
        # English practice mode: force "en" and skip the Spanish vocabulary prompt.
        if english:
            kwargs["language"] = "en"
        elif WHISPER_LANGUAGE:
            kwargs["language"] = WHISPER_LANGUAGE
        if self._whisper_prompt and not english:
            kwargs["prompt"] = self._whisper_prompt
        return kwargs

    def _transcribe_groq(self, data: bytes, english: bool = False) -> str:
        t0 = time.monotonic()
        kwargs = self._common_kwargs(english)
        kwargs["file"] = ("recording.wav", data)
        kwargs["model"] = GROQ_MODEL
        transcription = self.client.audio.transcriptions.create(**kwargs)
        text = transcription.strip() if isinstance(transcription, str) else str(transcription).strip()
        duration_ms = int((time.monotonic() - t0) * 1000)
        print(f"[transcriber] provider=groq model={GROQ_MODEL} duration={duration_ms}ms chars={len(text)}")
        return text

    def _transcribe_openai(self, data: bytes, english: bool = False) -> str:
        t0 = time.monotonic()
        from openai import OpenAI

        client = OpenAI(api_key=OPENAI_API_KEY, timeout=TRANSCRIBE_TIMEOUT_SECONDS)
        kwargs = self._common_kwargs(english)
        kwargs["file"] = ("recording.wav", data)
        kwargs["model"] = OPENAI_TRANSCRIBE_MODEL
        transcription = client.audio.transcriptions.create(**kwargs)
        text = transcription.strip() if isinstance(transcription, str) else getattr(transcription, "text", str(transcription)).strip()
        duration_ms = int((time.monotonic() - t0) * 1000)
        print(f"[transcriber] provider=openai model={OPENAI_TRANSCRIBE_MODEL} duration={duration_ms}ms chars={len(text)} (fallback)")
        return text

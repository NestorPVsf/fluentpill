"""english_tts.py — native-voice audio for the English corrections panel.

Blocking and Qt-free: synthesize(text) returns the path of a cached WAV,
calling Gemini's speech generation only when the file is not on disk yet.
The panel runs it in a worker thread. Raises TtsError (with a short Spanish
`hint` for the button tooltip); callers log it and carry on.
"""

import base64
import hashlib
import io
import os
import uuid
import wave
from pathlib import Path

from core.lifecycle import howl_appdata_dir

SAMPLE_RATE = 24000          # Gemini speech output: 24 kHz, 16-bit, mono PCM
MAX_CHARS = 1000


class TtsError(Exception):
    def __init__(self, message: str, hint: str = "No se pudo generar el audio"):
        super().__init__(message)
        self.hint = hint


def _settings() -> dict:
    import config
    return {
        "provider": config.ENGLISH_TTS_PROVIDER,
        "model": config.ENGLISH_TTS_MODEL,
        "voice": config.ENGLISH_TTS_VOICE,
        "style": config.ENGLISH_TTS_STYLE,
        "timeout": config.ENGLISH_TTS_TIMEOUT_SECONDS,
        "api_key": config.GEMINI_API_KEY,
    }


def cache_dir() -> Path:
    return howl_appdata_dir() / "tts"


def cache_key(text: str, model: str, voice: str, style: str = "") -> str:
    raw = "\x1f".join((model, voice, style, text))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def cache_path(text: str, model: str, voice: str, style: str = "") -> Path:
    return cache_dir() / f"{cache_key(text, model, voice, style)}.wav"


_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"


def _post(url: str, headers: dict, payload: dict, timeout: float):
    """Single seam for the network (tests replace it)."""
    import httpx
    return httpx.post(url, headers=headers, json=payload, timeout=timeout)


def _extract_audio(body: dict) -> bytes:
    for step in body.get("steps") or []:
        for item in step.get("content") or []:
            if item.get("type") == "audio" and item.get("data"):
                return base64.b64decode(item["data"])
    raise TtsError("no audio in response")


def _request_audio(text: str, cfg: dict) -> bytes:
    """One Gemini speech call. Returns the audio bytes (WAV, or raw PCM).

    Plain HTTP on the Interactions endpoint: the installed google-genai SDK
    drops the `speech_metadata` annotation that carries the speaking style,
    and putting the style inside the text makes the model read it aloud.
    """
    if not cfg["api_key"]:
        raise TtsError("GEMINI_API_KEY missing", "Falta la clave de Gemini")
    payload = {
        "model": cfg["model"],
        "input": [{"type": "user_input", "content": [{
            "type": "text", "text": text,
            "annotations": [{"type": "speech_metadata", "style": cfg["style"]}],
        }]}],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": cfg["voice"]}]},
    }
    try:
        resp = _post(_ENDPOINT, {"x-goog-api-key": cfg["api_key"]}, payload, cfg["timeout"])
    except Exception as e:
        name = type(e).__name__.lower()
        offline = any(k in name for k in ("connect", "timeout", "network"))
        raise TtsError(f"{type(e).__name__}: {e}",
                       "Sin conexión" if offline else "No se pudo generar el audio") from e
    if resp.status_code != 200:
        hint = {400: "Revisa la clave de Gemini", 401: "Revisa la clave de Gemini",
                403: "Revisa la clave de Gemini", 429: "Límite de Gemini alcanzado"
                }.get(resp.status_code, "No se pudo generar el audio")
        raise TtsError(f"HTTP {resp.status_code}: {str(resp.text)[:200]}", hint)
    try:
        return _extract_audio(resp.json())
    except TtsError:
        raise
    except Exception as e:
        raise TtsError(f"bad response: {type(e).__name__}: {e}") from e


def _to_wav(audio: bytes) -> bytes:
    """Gemini may return raw PCM; wrap it. Already-WAV data passes through."""
    if audio[:4] == b"RIFF":
        return audio
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(audio)
    return buf.getvalue()


def synthesize(text: str) -> Path:
    """Path of the cached WAV for `text`, generating it when missing."""
    text = (text or "").strip()[:MAX_CHARS]
    if not text:
        raise TtsError("empty text")
    cfg = _settings()
    if cfg["provider"] != "gemini":
        raise TtsError(f"unsupported provider {cfg['provider']!r}")
    path = cache_path(text, cfg["model"], cfg["voice"], cfg["style"])
    if path.is_file() and path.stat().st_size > 44:
        return path
    wav = _to_wav(_request_audio(text, cfg))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.part")
        tmp.write_bytes(wav)
        os.replace(tmp, path)
    except OSError as e:
        raise TtsError(f"cache write failed: {e}") from e
    return path

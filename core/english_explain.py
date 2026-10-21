"""english_explain.py - "Explícame": a short Spanish mini-lesson about ONE dictation.

Blocking and Qt-free (the popup runs it in a worker thread). Reuses the
provider chain and clients of core/english.py. Raises ExplainError with a short
Spanish `hint` for the popup; successful lessons are cached in memory so a
second press on the same dictation costs nothing (failures are never cached).
"""

import json
from pathlib import Path

from core.english import _call_groq, _call_openai

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "english_explain.md"
_MAX_TOKENS = 3000            # reasoning models spend part of this before the text
_cache: dict[str, str] = {}


class ExplainError(Exception):
    def __init__(self, message: str, hint: str = "No se pudo generar la explicación"):
        super().__init__(message)
        self.hint = hint


def build_user_message(original: str, corrected: str, corrections, upgrade: str) -> str:
    lines = [f"Lo que dijo (original): {original}"] if (original or "").strip() else []
    lines += [f"Versión corregida: {corrected}", "Cambios:"]
    for c in corrections or []:
        if isinstance(c, dict):
            lines.append(f"- {c.get('original', '')} -> {c.get('better', '')} "
                         f"({c.get('type', '')}; {c.get('why', '')})")
    if (upgrade or "").strip():
        lines.append(f"Versión más avanzada: {upgrade}")
    return "\n".join(lines)


def _key(user: str) -> str:
    return json.dumps(user, ensure_ascii=False)


def explain(original: str, corrected: str, corrections, upgrade: str = "") -> str:
    """The lesson text for this dictation (cached). Raises ExplainError."""
    user = build_user_message(original or "", corrected or "", corrections, upgrade or "")
    key = _key(user)
    if key in _cache:
        return _cache[key]
    import config
    system = _PROMPT_PATH.read_text(encoding="utf-8")
    timeout = config.CLEANER_TIMEOUT_SECONDS * 4
    chain = [
        (config.OPENAI_API_KEY, config.OPENAI_CLEANER_MODEL_LONG, _call_openai),
        (config.GROQ_API_KEY, config.GROQ_CLEANER_MODEL, _call_groq),
    ]
    if not any(k for k, _, _ in chain):
        raise ExplainError("no API key", "Falta la clave de OpenAI o Groq")
    for api_key, model, call in chain:
        if not api_key:
            continue
        text = call(system, user, api_key, model, timeout, json_mode=False, max_tokens=_MAX_TOKENS)
        if text and text.strip():
            _cache[key] = text.strip()
            return _cache[key]
    raise ExplainError("all providers failed", "No se pudo generar la explicación. Reintenta.")

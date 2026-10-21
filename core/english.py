"""English practice mode: correct an English dictation and explain the fixes.

Flow: Whisper (language="en") -> correct_english() -> paste `corrected`, store
the `corrections` for later study. NEVER raises and NEVER loses the dictation:
broken JSON -> rescue the "corrected" field, else paste the original; a reply
more than twice as long as the original is discarded (the model started
chatting) and the original is pasted.
"""

import json
import re
import time
from pathlib import Path

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "english_corrector.md"
_TYPES = {"grammar", "vocabulary", "naturalness", "pronunciation-guess"}
_MAX_CORRECTIONS = 6


def _system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _json_candidate(raw: str) -> str:
    """Strip code fences / chatter around the first {...} block."""
    raw = raw.strip()
    start, end = raw.find("{"), raw.rfind("}")
    return raw[start:end + 1] if start != -1 and end > start else raw


def _rescue_corrected(raw: str) -> str:
    """Pull the "corrected" string out of malformed JSON (e.g. truncated)."""
    m = re.search(r'"corrected"\s*:\s*"((?:[^"\\]|\\.)*)"', raw, re.DOTALL)
    if not m:
        return ""
    try:
        return json.loads('"' + m.group(1) + '"').strip()
    except ValueError:
        return m.group(1).strip()


def _clean_corrections(items) -> list[dict]:
    out = []
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        original = str(it.get("original", "")).strip()
        better = str(it.get("better", "")).strip()
        if not original or not better or original == better:
            continue
        ctype = str(it.get("type", "")).strip().lower()
        out.append({
            "original": original,
            "better": better,
            "why": " ".join(str(it.get("why", "")).split()),
            "type": ctype if ctype in _TYPES else "grammar",
        })
        if len(out) >= _MAX_CORRECTIONS:
            break
    return out


def parse_response(raw: str, original: str) -> dict:
    """Turn the LLM reply into {corrected, corrections, upgrade, ok}.

    ok=False means we fell back to the original transcript.
    """
    fallback = {"corrected": original, "corrections": [], "upgrade": "", "ok": False}
    if not raw or not raw.strip():
        return fallback
    try:
        data = json.loads(_json_candidate(raw))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except ValueError:
        rescued = _rescue_corrected(raw)
        if rescued and len(rescued) <= 2 * len(original):
            return {"corrected": rescued, "corrections": [], "upgrade": "", "ok": True}
        return fallback

    corrected = str(data.get("corrected", "")).strip()
    if not corrected or len(corrected) > 2 * len(original):
        return fallback
    return {
        "corrected": corrected,
        "corrections": _clean_corrections(data.get("corrections")),
        "upgrade": str(data.get("upgrade", "") or "").strip(),
        "ok": True,
    }


def _call_openai(system: str, user: str, api_key: str, model: str, timeout: float,
                 json_mode: bool = True, max_tokens: int = 1500) -> str | None:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, timeout=timeout)
        kwargs = dict(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_completion_tokens=max_tokens,
        )
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        # Some models reject reasoning_effort; try with it, then without.
        try:
            resp = client.chat.completions.create(reasoning_effort="low", **kwargs)
        except Exception:
            resp = client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip() or None
    except Exception as e:
        print(f"[english] provider=openai FAILED: {e}")
        return None


def _call_groq(system: str, user: str, api_key: str, model: str, timeout: float,
               json_mode: bool = True, max_tokens: int = 1500) -> str | None:
    try:
        from groq import Groq
        client = Groq(api_key=api_key, timeout=timeout)
        kwargs = dict(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.2 if json_mode else 0.5,
        )
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        else:
            kwargs["max_tokens"] = max_tokens   # corrector behaviour stays unchanged
        resp = client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip() or None
    except Exception as e:
        print(f"[english] provider=groq FAILED: {e}")
        return None


def correct_english(text: str) -> dict:
    """Correct an English transcript. Returns the parse_response() dict."""
    fallback = {"corrected": text, "corrections": [], "upgrade": "", "ok": False}
    if not text or not text.strip():
        return fallback
    try:
        from config import (
            OPENAI_API_KEY, OPENAI_CLEANER_MODEL_LONG,
            GROQ_API_KEY, GROQ_CLEANER_MODEL, CLEANER_TIMEOUT_SECONDS,
        )
        system = _system_prompt()
        timeout = CLEANER_TIMEOUT_SECONDS * 2
        chain = [
            (OPENAI_API_KEY, OPENAI_CLEANER_MODEL_LONG, _call_openai),
            (GROQ_API_KEY, GROQ_CLEANER_MODEL, _call_groq),
        ]
        for key, model, call in chain:
            if not key:
                continue
            t0 = time.monotonic()
            raw = call(system, text, key, model, timeout)
            if raw is None:
                continue
            result = parse_response(raw, text)
            print(f"[english] model={model} {int((time.monotonic() - t0) * 1000)}ms ok={result['ok']}")
            if result["ok"]:
                return result
        print("[english] no usable correction — pasting the original")
    except Exception as e:  # never lose the dictation
        print(f"[english] correction raised: {e!r}")
    return fallback

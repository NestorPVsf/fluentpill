"""Post-processing LLM cleaner for dictated text.

Pipeline position: Whisper transcript → dictionary.apply() → cleaner.clean() → paste.
Provider chains are tiered by dictation length (first success wins):
  SHORT: GPT-5.6 Luna → Gemini Flash-Lite → Groq gpt-oss-120b (speed, ~2s)
  LONG:  GPT-5.6 Terra → GPT-5.6 Luna → Groq gpt-oss-120b (synthesis of
         rambling monologues needs comprehension). Correction/backtrack/quote
         markers escalate short dictations to the LONG chain.
All have hard timeout. If everything fails, returns the input text unchanged
(NEVER raises — failure must NEVER block the paste).

PROMPT CACHING STRATEGY
-----------------------
The system prompt lives in `prompts/dictation_cleaner.md` and is loaded once
at import time. Keeping the prompt in a standalone file guarantees the prefix
is BYTE-IDENTICAL across calls — any whitespace drift would invalidate the
provider cache. The cleaner intentionally puts the system prompt FIRST so the
whole static block can be cached and only the user text is billed at full rate.

Cache hit stats are logged per call (cached_tokens / total_tokens) so the user
can confirm caching is working. Expected behavior after warm-up:
- First call of a session: cached_tokens=0 (cache miss, full bill)
- Subsequent calls within 5-10 min: cached_tokens≈prompt_tokens (50-90% cheaper)
"""

import os
import re
import time
from difflib import SequenceMatcher

# --- System prompt loaded once at import time ---
_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "prompts",
    "dictation_cleaner.md",
)

try:
    with open(_PROMPT_PATH, "r", encoding="utf-8") as _f:
        CLEANER_SYSTEM_PROMPT_BASE = _f.read().rstrip() + "\n"
except FileNotFoundError as _e:
    raise RuntimeError(
        f"Cleaner system prompt not found at {_PROMPT_PATH}. "
        "Did you delete prompts/dictation_cleaner.md?"
    ) from _e


def _build_tech_vocab_block() -> str:
    """Append the static dev vocabulary so the LLM treats these terms as
    first-class and never normalizes their spelling. Byte-identical across
    calls — keeps prompt-prefix caching intact on Gemini/OpenAI/Groq."""
    from config import TECH_VOCABULARY
    vocab = ", ".join(TECH_VOCABULARY)
    return (
        "\n\nVOCABULARIO TECNICO DEL HABLANTE (es desarrollador):\n"
        f"{vocab}.\n\n"
        "Estos terminos son CORRECTOS por defecto. NUNCA los traduzcas al "
        "espanol, NUNCA cambies su grafia (GitHub no es 'github' ni 'GitHub.com'), "
        "NUNCA expandas siglas (PR se queda como PR, API como API, RLS como RLS, "
        "salvo que el hablante diga explicitamente la forma larga)."
    )


# Static, full system prompt. Built once at import time so the prefix that the
# providers cache is byte-identical across every call of this app's lifetime.
CLEANER_SYSTEM_PROMPT = CLEANER_SYSTEM_PROMPT_BASE + _build_tech_vocab_block()

# Stable user identifier for OpenAI cache routing — keeps this app's prompts
# pinned to the same cache shard across calls.
_OPENAI_USER_ID = "fluentpill-cleaner-v1"

# Params each OpenAI model rejected with a 400 this session (e.g. gpt-5.6
# rejects `temperature`). Remembered so the drop-and-retry round-trip is paid
# once per model, not once per dictation.
_openai_rejected_params: dict[str, set[str]] = {}


# Matches the <USER_VOCAB> / <APP_CONTEXT> metadata blocks — used as a
# defensive server-side strip in case the LLM echoes them back into its
# output (small models like Gemini Flash-Lite sometimes ignore the
# system-prompt rule about treating them as metadata).
_METADATA_BLOCK_RE = re.compile(
    r"<(USER_VOCAB|APP_CONTEXT|AVISO_EDITOR)>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)


def _strip_metadata_echo(cleaned: str) -> str:
    """Remove any <USER_VOCAB>/<APP_CONTEXT> block the LLM may have echoed,
    plus any leading whitespace left behind. NEVER raises."""
    low = cleaned.lower()
    if "<user_vocab>" not in low and "<app_context>" not in low and "<aviso_editor>" not in low:
        return cleaned
    stripped = _METADATA_BLOCK_RE.sub("", cleaned).strip()
    if stripped != cleaned:
        print("[cleaner] stripped echoed metadata block from LLM response")
    return stripped


# Markdown link: [display](url). Dictation NEVER contains this syntax — every
# occurrence in the LLM output is a hallucination (e.g. the model interpreting
# trailing sounds as a URL fragment and "formatting" it). Strip aggressively.
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]*)\]\(([^)\n]*)\)")


def _strip_fabricated_markdown(cleaned: str) -> str:
    """Remove any Markdown link the LLM may have invented in the output.

    Policy:
    - If the display text looks like a URL or domain (or is empty), drop the
      whole `[X](Y)` block (likely fully hallucinated, e.g. the "coño →
      www.mr.com.au" case from 2026-05-27).
    - Otherwise keep the display text and drop the parenthesized URL —
      preserves any meaning the display text carried.
    NEVER raises.
    """
    if "](" not in cleaned:
        return cleaned

    def _replace(match: "re.Match[str]") -> str:
        display = (match.group(1) or "").strip()
        if not display:
            return ""
        # display looks URL-like → drop entirely
        low = display.lower()
        if (
            low.startswith(("http://", "https://", "www."))
            or ("." in display and " " not in display and len(display) < 80)
        ):
            return ""
        return display

    stripped = _MARKDOWN_LINK_RE.sub(_replace, cleaned)
    # Collapse any double-spaces / leading-trailing whitespace introduced by removals
    stripped = re.sub(r"[ \t]{2,}", " ", stripped).strip()
    if stripped != cleaned:
        print("[cleaner] stripped fabricated Markdown link(s) from LLM response")
    return stripped


def _post_process_llm_output(cleaned: str) -> str:
    """All defensive post-processing the LLM output goes through."""
    cleaned = _strip_metadata_echo(cleaned)
    cleaned = _strip_fabricated_markdown(cleaned)
    return cleaned


def _looks_like_chatbot_response(
    input_text: str,
    cleaned: str,
    user_vocab: list[str] | None,
) -> bool:
    """Detect when the LLM ignored its editor role and generated a chatbot-style
    response to the dictation instead of just cleaning it.

    Two strong signals combined (real failure case 2026-05-27):
    1. Massive length expansion (cleaned > 2× input AND > 200 chars).
    2. The cleaned output contains 2+ user-vocab terms that are NOT present
       in the input — meaning the LLM raided the <USER_VOCAB> metadata for
       words to invent bullet points with.

    Also flags pure runaway hallucination via length alone if very extreme.
    """
    if not cleaned:
        return False
    in_len = max(len(input_text), 1)
    out_len = len(cleaned)
    expansion = out_len / in_len
    big_expansion = out_len >= 200 and expansion >= 2.0
    extreme_expansion = out_len >= 500 and expansion >= 3.0

    leaked_vocab = 0
    if user_vocab:
        in_lower = input_text.lower()
        out_lower = cleaned.lower()
        for word in user_vocab:
            wl = word.lower().strip()
            if not wl or len(wl) < 3:
                continue
            if wl in out_lower and wl not in in_lower:
                leaked_vocab += 1

    if big_expansion and leaked_vocab >= 2:
        return True
    if leaked_vocab >= 3:
        return True
    if extreme_expansion:
        return True
    return False


def _accept_or_reject(
    input_text: str,
    raw_result: str,
    user_vocab: list[str] | None,
) -> tuple[str, bool]:
    """Run post-processing on the LLM result; if the result still looks like a
    hallucinated chatbot response, drop it and fall back to the input text."""
    polished = _post_process_llm_output(raw_result)
    if _looks_like_chatbot_response(input_text, polished, user_vocab):
        print(
            f"[cleaner] LLM looked like a chatbot response "
            f"(in={len(input_text)}, out={len(polished)}) — falling back to raw text"
        )
        return input_text, False
    return polished, True


# ---------------------------------------------------------------------------
# Length guard (anti-summary)
# ---------------------------------------------------------------------------
# The cleaner must CLEAN, not SUMMARIZE. The LLM sometimes still condenses a
# long dictation, dropping doubts, reasons or "y también X" requests. Spanish
# filler is a short, closed list, so the expected clean length can be computed
# in code: raw minus filler. If the output is well below that, it summarized.
GUARD_MIN_WORDS = 25        # same boundary as the prompt's short/long rule
GUARD_MIN_RATIO = 0.75      # output / (raw without filler), in word chars (0.80 = same precision, fires twice as often: see tools/eval)
GUARD_FALLBACK_RATIO = 0.60  # below this after the retry: it is a summary, use the light cleanup
GUARD_MIN_RATIO_CORRECTED = 0.60  # dictations with "perdón / mejor dicho" shrink on purpose

# Pure filler only: nothing here can carry information. Deliberately NOT in the
# list: "creo que", "no sé si", "quizás", "también", "porque" (doubts/reasons).
_FILLER_RE = re.compile(
    r"(?<!\w)(?:"
    r"eh+m*|mm+|hmm+|ajá|"
    r"o sea|pues nada|pero bueno|a ver|pues mira|mira|en plan|me explico|"
    r"¿vale\?|¿sabes\?|¿no\?|¿verdad\?|¿ok\?|"
    r"vale|bueno|pues|vaya"
    r")(?!\w)[\s,;.]*",
    re.IGNORECASE,
)
# Stricter version for the deterministic fallback, which REWRITES text: only an
# interjection standing alone (followed by punctuation) goes, so "mira este
# fichero" or "pues no hay" keep their verb/cause. Tag questions go anywhere.
_TAG_QUESTION_RE = re.compile(r"\s*,?\s*¿(?:vale|sabes|no|verdad|ok)\?", re.IGNORECASE)
_FILLER_STRICT_RE = re.compile(
    r"(?<!\w)(?:"
    r"(?:eh+m*|mm+|hmm+|ajá|o sea|pues nada|pero bueno|a ver|pues mira|mira|en plan|me explico|vale|bueno|pues|vaya)"
    r"(?=\s*[,.;?!])"
    r")[\s,;.]*",
    re.IGNORECASE,
)
# Immediate stutter of function words only ("yo yo creo", "que que"): "muy, muy
# rápido" or "no, no" are emphasis and stay.
_STUTTER_RE = re.compile(r"\b(yo|el|la|los|las|un|una|que|de|en|y|a|se|lo|es|me|te)(?:\s+\1\b)+", re.IGNORECASE)


def _word_chars(text: str) -> int:
    return len(re.findall(r"\w", text, flags=re.UNICODE))


def _light_clean(text: str) -> str:
    """Deterministic minimal cleanup: filler words and immediate stutters only.
    Used as the fallback when the LLM keeps summarizing — it can lose nothing."""
    out = _STUTTER_RE.sub(r"\1", text)
    # A tag question ("..., ¿vale?") closes its sentence: turn it into a full stop.
    out = _TAG_QUESTION_RE.sub(". ", out)
    out = _FILLER_STRICT_RE.sub("", out)
    out = re.sub(r"[,;]\s*\.(?!\.)", ".", out)
    out = re.sub(r"(?<!\.)\.\s*\.(?!\.)", ".", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r"([,;])\s*([.!?])", r"\2", out)
    out = re.sub(r"[ \t]{2,}", " ", out).strip(" ,;")
    # Capitalise the first letter and any letter that starts a sentence
    out = re.sub(
        r"(^|[.!?¿¡]\s+)([a-záéíóúñ])",
        lambda m: m.group(1) + m.group(2).upper(),
        out,
    )
    return out or text


def _expected_clean_chars(raw: str) -> int:
    return _word_chars(_FILLER_RE.sub("", _STUTTER_RE.sub(r"\1", raw)))


_RETRY_NOTICE = (
    "<AVISO_EDITOR>\n"
    "Tu primer intento quedó demasiado corto: omitía información que el hablante sí dijo. "
    "Repite la limpieza conservando TODAS las frases con información: datos y cifras, dudas "
    "('no sé si', 'creo que' siguen siendo dudas), razones ('porque', 'por lo que'), incisos, "
    "peticiones y lo que añade con 'y también'. Solo quita muletillas, repeticiones literales "
    "y arranques fallidos que él mismo corrige. Si cambió de decisión, vale la última. No resumas; el resultado puede quedar menos "
    "elegante, pero completo.\n"
    "</AVISO_EDITOR>"
)


def _apply_length_guard(
    raw: str,
    polished: str,
    retry,
    *,
    has_correction: bool = False,
    is_translation: bool = False,
) -> tuple[str, dict]:
    """Detect a summarized output, retry once, then fall back to a light cleanup.

    `retry(extra_notice) -> str | None` re-runs the SAME provider/model with the
    notice appended to the user message and returns the post-processed text.
    Returns (text, info); info keys: ratio, retried, fallback (bools/float) for
    logging and for the eval harness to count how often the guard fires.
    """
    info = {"ratio": None, "retried": False, "fallback": False}
    if is_translation or len(raw.split()) < GUARD_MIN_WORDS:
        return polished, info
    expected = max(_expected_clean_chars(raw), 1)
    threshold = GUARD_MIN_RATIO_CORRECTED if has_correction else GUARD_MIN_RATIO
    ratio = _word_chars(polished) / expected
    info["ratio"] = round(ratio, 3)
    if ratio >= threshold:
        return polished, info

    info["retried"] = True
    print(f"[cleaner] length-guard: output {ratio:.2f} of expected (<{threshold:.2f}) — retrying once")
    second = None
    try:
        second = retry(_RETRY_NOTICE)
    except Exception as e:  # the guard must never block the paste
        print(f"[cleaner] length-guard retry FAILED: {e}")
    best, best_ratio = polished, ratio
    if second:
        r2 = _word_chars(second) / expected
        info["ratio2"] = round(r2, 3)
        if r2 >= threshold:
            print(f"[cleaner] length-guard: retry accepted ({r2:.2f})")
            return second, info
        if r2 > ratio:
            best, best_ratio = second, r2
    # Still short. A moderately short output is often legitimate (the speaker changed
    # their mind without a marker: "sí, misión facturación... no, mejor solo facturación"),
    # and the light fallback would bring BOTH versions back. Only a clear summary
    # (below GUARD_FALLBACK_RATIO) is replaced by the deterministic cleanup.
    if has_correction or best_ratio >= GUARD_FALLBACK_RATIO:
        info["kept_short"] = True
        print(f"[cleaner] length-guard: still short ({best_ratio:.2f}) — keeping the longer LLM output")
        return best, info
    info["fallback"] = True
    print(f"[cleaner] length-guard: still very short ({best_ratio:.2f}) after retry — light cleanup (fillers/stutters only)")
    return _light_clean(raw), info


def _build_user_message(
    text: str,
    user_vocab: list[str] | None,
    app_context=None,
    include_title: bool = False,
) -> str:
    """Prepend the per-call metadata blocks to the user message. They go in
    the USER message (not system) because they change over time / per call —
    putting them in system would invalidate the cache prefix.

    - <USER_VOCAB>: user-learned vocabulary so the LLM respects
      project-specific terms. Capped at 50 terms to avoid bloating the prompt.
    - <APP_CONTEXT>: foreground app + category (core.app_context.AppContext)
      so the LLM adapts register/format to where the text will be pasted.
      The window title is only included when the user opted in
      (APP_CONTEXT_SEND_TITLE).

    Both are wrapped in tags so the system prompt can instruct the LLM to
    treat them as metadata and NEVER echo them back in the cleaned output.
    """
    blocks: list[str] = []
    if user_vocab:
        capped = user_vocab[:50]
        blocks.append("<USER_VOCAB>\n" + ", ".join(capped) + "\n</USER_VOCAB>")
    if app_context is not None:
        lines = [f"app: {app_context.app_name}", f"categoria: {app_context.category}"]
        if include_title and app_context.title:
            lines.append(f"titulo: {app_context.title[:120]}")
        blocks.append("<APP_CONTEXT>\n" + "\n".join(lines) + "\n</APP_CONTEXT>")
    if not blocks:
        return text
    return "\n\n".join(blocks) + f"\n\n{text}"


# Spoken translation directive ("traduce al inglés…"). Imperative forms only —
# unambiguous. Used to bypass the CLEANER_MIN_WORDS gate so short dictations
# that open with the directive still reach the LLM, which applies the
# REGLA DE TRADUCCIÓN of the system prompt.
_TRANSLATE_DIRECTIVE_RE = re.compile(
    r"^\s*(traduce|tradúcelo|traducelo|traducir|translate)\b",
    re.IGNORECASE,
)


# Spoken self-correction / backtrack / quote-directive markers. When present,
# the dictation needs actual editing intelligence, not just filler-stripping:
# (a) it must reach the LLM even below CLEANER_MIN_WORDS ("el martes, no,
#     perdón, el miércoles" is 6 words and used to paste raw), and
# (b) it gets the LONG chain (strong model) — the small tier reads the
#     "limpieza mínima y literal" rule as a license to keep both versions.
# False positives are cheap: worst case a short dictation gets a smarter model.
_CORRECTION_MARKER_RE = re.compile(
    r"\b("
    r"perd[oó]n|mejor dicho|quiero decir|mentira|corrijo|"
    r"no,? en realidad|"
    r"borra (eso|lo [uú]ltimo)|t[aá]ch(alo|a eso)|olvida eso|scratch that|"
    r"entre comillas|abro comillas"
    r")\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Phantom-phrase filter (Capa B)
# ---------------------------------------------------------------------------
# Phrases Whisper-large-v3 hallucinates on near-silence / background-noise
# audio in Spanish. Stored normalised (lower, no accents/punctuation, stripped)
# for comparison.  The list is order-independent — each entry is tested as a
# standalone segment match, never a substring-in-the-middle match.
_PHANTOM_PHRASES: list[str] = [
    "gracias",
    "gracias por ver el video",
    "gracias por ver el video",
    "gracias por ver el vídeo",
    "gracias por ver",
    "subtitulos realizados por la comunidad de amara org",
    "subtitulos por la comunidad de amara org",
    "subtitulos en espanol por la comunidad de amara org",
    "amara org",
    "suscribete",
    "suscribete al canal",
    "no olvides suscribirte",
    "hasta la proxima",
    "hasta el proximo video",
    "nos vemos en el proximo video",
    "musica",
    "aplausos",
]

# Prefix stems for YouTube-outro hallucinations. Whisper varies the tail
# ("Gracias por ver ESTE video", "Suscríbete al canal PARA MÁS VIDEOS") so an
# exact-sentence list can't keep up. A sentence starting with any of these is
# never real dictation.
_PHANTOM_PREFIXES: list[str] = [
    "gracias por ver el video",
    "gracias por ver este video",
    "suscribete al canal",
    "no olvides suscribirte",
    "subtitulos realizados por",
    "subtitulos por la comunidad",
    "subtitulos en espanol por",
    "nos vemos en el proximo",
    "hasta el proximo video",
]


def _is_phantom(normalized: str) -> bool:
    """True if a normalised sentence is a known Whisper phantom phrase."""
    if not normalized:
        return False
    if normalized in _PHANTOM_PHRASES:
        return True
    return any(normalized.startswith(p) for p in _PHANTOM_PREFIXES)


def _normalize_for_phantom(s: str) -> str:
    """Lower-case, strip accents/punctuation, collapse spaces. For comparison only.

    Punctuation between word characters (e.g. the dot in "Amara.org") is
    converted to a space so "amara org" matches the list entry; leading/trailing
    punctuation is simply dropped.
    """
    # Replace punctuation that sits between two word chars with a space
    # so "Amara.org" → "Amara org". Other punctuation (leading/trailing) is dropped.
    s = re.sub(r"(?<=\w)[^\w\s](?=\w)", " ", s, flags=re.UNICODE)
    # Remove remaining non-word, non-space characters
    s = re.sub(r"[^\w\s]", "", s, flags=re.UNICODE)
    # Normalise accented vowels → plain ASCII equivalents (Spanish)
    _accent_map = str.maketrans("áéíóúàèìòùäëïöüâêîôûñÁÉÍÓÚÀÈÌÒÙÄËÏÖÜÂÊÎÔÛÑ",
                                "aeiouaeiouaeiouaeiounaeiouaeiouaeiouaeioun")
    s = s.translate(_accent_map)
    return re.sub(r"\s+", " ", s).strip().lower()


def _strip_phantom_phrases(text: str) -> str:
    """Remove phantom Whisper phrases from ``text``.

    Two passes:
    1. Whole-text match: if the entire normalised transcript is (only) a
       phantom phrase, return "".
    2. Segment trim: if a phantom phrase appears as an isolated sentence at
       the START or END of a longer transcript, remove just that sentence
       and preserve the rest.

    "Isolated sentence" = bounded by start/end of string or a sentence-ending
    punctuation mark (. ! ?), preventing a match inside the middle of legit
    speech like "Muchas gracias por revisar el repo".
    """
    if not text:
        return text

    norm_full = _normalize_for_phantom(text)

    # Pass 1 — whole-text phantom
    if _is_phantom(norm_full):
        print(f"[cleaner] pre-clean stripped whole-text phantom phrase: {text!r}")
        return ""

    # Pass 2 — leading/trailing sentence phantom.
    # Split on sentence boundaries to get candidate segments.
    # We work on the original text to preserve capitalisation/punctuation in
    # what we keep, but compare against normalised phantoms.
    sentence_split_re = re.compile(r"(?<=[.!?¿¡])\s+")
    sentences = sentence_split_re.split(text)
    if len(sentences) < 2:
        return text  # single sentence, already handled by pass 1

    changed = False

    # Trim leading sentences that are phantoms
    while sentences:
        if _is_phantom(_normalize_for_phantom(sentences[0])):
            print(f"[cleaner] pre-clean stripped leading phantom phrase: {sentences[0]!r}")
            sentences.pop(0)
            changed = True
        else:
            break

    # Trim trailing sentences that are phantoms
    while sentences:
        if _is_phantom(_normalize_for_phantom(sentences[-1])):
            print(f"[cleaner] pre-clean stripped trailing phantom phrase: {sentences[-1]!r}")
            sentences.pop()
            changed = True
        else:
            break

    if not sentences:
        return ""

    return " ".join(sentences).strip() if changed else text


def strip_whisper_hallucinations(text: str) -> str:
    """Deterministic pre-clean for known Whisper failure modes.

    Whisper hallucinates several patterns on long/noisy/silent audio that the
    downstream LLM cleaner often misses (and that bypass the cleaner entirely
    when it is disabled or fails):

    1. Runs of repeated single characters ("HHHHHH", "H H H H H") — produced
       during silences or non-speech segments.
    2. Verbatim duplication of the whole transcript (loop-hallucination on
       recordings >30s where the decoder re-emits prior context as new).
    3. Known phantom phrases ("Gracias.", "¡Suscríbete al canal!", etc.) —
       Whisper-large-v3 semantic hallucinations on near-silence audio in Spanish.

    Conservative thresholds: a 4+ identical-letter run, or 0.85+ similarity
    between the two halves of a long (≥200 char) transcript.
    """
    original = text
    # "HHHHHH" — 4+ identical letters with no diversity
    text = re.sub(r"([A-Za-zñÑ])\1{3,}", "", text)
    # "H H H H H" — single letter repeated separated by whitespace
    text = re.sub(r"\b([A-Za-zñÑ])(?:\s+\1){2,}\b", "", text)
    # Whisper loop: text ≈ A + A
    if len(text) >= 200:
        half = len(text) // 2
        if SequenceMatcher(None, text[:half], text[half:]).ratio() >= 0.85:
            text = text[:half]
    text = re.sub(r"\s{2,}", " ", text).strip()
    # Phantom semantic phrases hallucinated on near-silence audio
    text = _strip_phantom_phrases(text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if text != original:
        print(f"[cleaner] pre-clean stripped Whisper hallucinations ({len(original)} -> {len(text)} chars)")
    return text


def clean_dictation(
    text: str,
    user_vocab: list[str] | None = None,
    app_context=None,
) -> tuple[str, bool]:
    """Clean a raw Whisper transcription with an LLM.

    Args:
        text: raw transcription (after Whisper + builtin corrections).
        user_vocab: optional list of project-specific terms the user has
            taught the dictionary. Prepended to the user message so the LLM
            respects them. None = no extra vocabulary.
        app_context: optional core.app_context.AppContext with the foreground
            app where the text will be pasted. The LLM adapts register/format
            per category (email/chat/code/...). None = default behavior.

    Returns (cleaned_text, was_cleaned):
    - was_cleaned=True  → LLM processed the text
    - was_cleaned=False → bypass (short text, disabled, or all providers failed)

    NEVER raises. On any failure, returns (original_text, False).
    """
    # Lazy import config to avoid circular issues at module load time
    from config import (
        CLEANER_ENABLED,
        CLEANER_MIN_WORDS,
        CLEANER_LONG_WORDS,
        CLEANER_TIMEOUT_SECONDS,
        APP_CONTEXT_SEND_TITLE,
        GEMINI_API_KEY,
        GEMINI_CLEANER_MODEL,
        GEMINI_CLEANER_MODEL_LONG,
        OPENAI_API_KEY,
        OPENAI_CLEANER_MODEL,
        OPENAI_CLEANER_MODEL_LONG,
        GROQ_API_KEY,
        GROQ_CLEANER_MODEL,
    )

    # Deterministic pre-clean runs even if LLM cleaner is disabled/bypassed
    text = strip_whisper_hallucinations(text)

    # --- Bypass checks ---
    if not CLEANER_ENABLED:
        return text, False

    word_count = len(text.split())
    has_correction = bool(_CORRECTION_MARKER_RE.search(text))
    if (
        word_count < CLEANER_MIN_WORDS
        and not _TRANSLATE_DIRECTIVE_RE.match(text)
        and not has_correction
    ):
        return text, False

    # Build the user message — text alone, or prefixed with the per-call
    # metadata blocks (user vocabulary, foreground-app context).
    user_message = _build_user_message(
        text, user_vocab, app_context=app_context, include_title=APP_CONTEXT_SEND_TITLE
    )

    # Estimate max_tokens: 1.5× input tokens + margin. The +250 covers the
    # gpt-5.6 family's reasoning tokens, which draw from the same
    # max_completion_tokens budget — too tight a cap yields EMPTY content
    # (verified 2026-08-13: cap=20 → '' with completion_tokens=20).
    # Rough heuristic: 1 word ≈ 1.3 tokens in Spanish.
    estimated_input_tokens = int(word_count * 1.3)
    max_tokens = int(estimated_input_tokens * 1.5) + 250

    # Tier selection — long dictations need synthesis (strong model, more time);
    # short ones need speed. Boundary mirrors the 25-word rule in the prompt.
    # Correction/backtrack/quote markers force the strong tier at any length:
    # applying "X, perdón, Y" correctly is an editing decision, not filler-stripping.
    is_long = word_count >= CLEANER_LONG_WORDS or has_correction
    if has_correction and word_count < CLEANER_LONG_WORDS:
        print("[cleaner] correction/quote marker detected — escalating short dictation to strong tier")
    timeout = CLEANER_TIMEOUT_SECONDS * (2 if is_long else 1)
    if is_long:
        # Terra synthesizes rambling dictations better than the small tier.
        # Fallback: Luna (reliable, faithful) → Groq → Gemini (last resort
        # only: Gemini drifts badly on long dictations — translates to English, invents
        # mappings, ignores the no-chatbot rule; verified 2026-06-15 on real audio).
        # Headroom for the longer output:
        max_tokens += 2000
        chain = [
            (OPENAI_API_KEY, OPENAI_CLEANER_MODEL_LONG, _call_openai),
            (OPENAI_API_KEY, OPENAI_CLEANER_MODEL, _call_openai),
            (GROQ_API_KEY, GROQ_CLEANER_MODEL, _call_groq),
            (GEMINI_API_KEY, GEMINI_CLEANER_MODEL_LONG, _call_gemini),
        ]
    else:
        chain = [
            (OPENAI_API_KEY, OPENAI_CLEANER_MODEL, _call_openai),
            (GEMINI_API_KEY, GEMINI_CLEANER_MODEL, _call_gemini),
            (GROQ_API_KEY, GROQ_CLEANER_MODEL, _call_groq),
        ]

    for api_key, model, call in chain:
        if not api_key:
            continue
        result = call(
            user_message=user_message,
            api_key=api_key,
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        if result is not None:
            polished, accepted = _accept_or_reject(text, result, user_vocab)
            if not accepted:
                return polished, accepted

            def _retry(notice: str, _call=call, _key=api_key, _model=model) -> str | None:
                again = _call(
                    user_message=user_message + "\n\n" + notice,
                    api_key=_key,
                    model=_model,
                    max_tokens=max_tokens,
                    # Half the budget: a stuck retry must not double the wait before paste.
                    timeout=max(timeout / 2, 8.0),
                )
                return _post_process_llm_output(again) if again else None

            polished, _info = _apply_length_guard(
                text,
                polished,
                _retry,
                has_correction=has_correction,
                is_translation=bool(_TRANSLATE_DIRECTIVE_RE.match(text)),
            )
            return polished, True

    # --- All providers failed or no keys configured ---
    print("[cleaner] provider=bypass (no keys or all failed) — returning raw text")
    return text, False


def _format_cache_stats(prompt_tokens: int | None, cached_tokens: int | None) -> str:
    """Pretty cache hit ratio for logs."""
    if not prompt_tokens:
        return "cache=n/a"
    cached = cached_tokens or 0
    pct = int((cached / prompt_tokens) * 100) if prompt_tokens else 0
    return f"cache={cached}/{prompt_tokens}({pct}%)"


def _call_openai(
    user_message: str,
    api_key: str,
    model: str,
    max_tokens: int,
    timeout: float,
) -> str | None:
    """Call OpenAI API. Returns cleaned text or None on any error.

    OpenAI: automatic prefix caching for prompts >1024 tokens. The system prompt
    must arrive byte-identical (we keep it in a file to enforce that). Passing
    a stable `user` improves cache routing.
    """
    t0 = time.monotonic()
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, timeout=timeout)
        # Newer OpenAI models (gpt-5.x family) reject `max_tokens` and require
        # `max_completion_tokens`. Try the new param first, fall back to the
        # old one if the SDK / model still expects it.
        # Param support varies per model: some reject `temperature`
        # (gpt-5.5+), others reject `reasoning_effort` values (gpt-4o). On a
        # 400 naming one of these params, drop it and retry.
        def _create(optional_params: dict) -> object:
            kwargs: dict = dict(
                model=model,
                messages=[
                    {"role": "system", "content": CLEANER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                user=_OPENAI_USER_ID,
                **optional_params,
            )
            try:
                return client.chat.completions.create(
                    **kwargs,
                    max_completion_tokens=max_tokens,
                )
            except TypeError:
                # Old SDK without max_completion_tokens — fall back
                return client.chat.completions.create(
                    **kwargs,
                    max_tokens=max_tokens,
                )

        # reasoning_effort="low" keeps the gpt-5.6 family fast: the cleaner
        # needs editing judgment, not deep chains of thought, and reasoning
        # tokens eat the max_completion_tokens budget.
        optional = {"temperature": 0.1, "reasoning_effort": "low"}
        for param in _openai_rejected_params.get(model, ()):
            optional.pop(param, None)
        while True:
            try:
                response = _create(optional)
                break
            except Exception as param_err:
                err_str = str(param_err)
                dropped = None
                if "400" in err_str:
                    for param in list(optional):
                        if param in err_str.lower():
                            dropped = param
                            break
                if dropped is None:
                    raise
                del optional[dropped]
                _openai_rejected_params.setdefault(model, set()).add(dropped)
                print(f"[cleaner] provider=openai model={model} rejected {dropped} — retrying without")

        cleaned = (response.choices[0].message.content or "").strip()
        if not cleaned:
            # gpt-5.x can burn the whole completion budget on reasoning and
            # return empty content. Treat as failure so the chain falls
            # through to the next provider instead of pasting nothing.
            duration_ms = int((time.monotonic() - t0) * 1000)
            print(f"[cleaner] provider=openai model={model} returned EMPTY content ({duration_ms}ms) — treating as failure")
            return None
        duration_ms = int((time.monotonic() - t0) * 1000)

        # Extract cache stats — keys may not exist on older SDK versions or models
        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        cached_tokens = None
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached_tokens = getattr(details, "cached_tokens", None)

        stats = _format_cache_stats(prompt_tokens, cached_tokens)
        print(f"[cleaner] provider=openai duration={duration_ms}ms {stats}")
        return cleaned
    except Exception as e:
        duration_ms = int((time.monotonic() - t0) * 1000)
        print(f"[cleaner] provider=openai FAILED ({duration_ms}ms): {e}")
        return None


def _call_gemini(
    user_message: str,
    api_key: str,
    model: str,
    max_tokens: int,
    timeout: float,
) -> str | None:
    """Call Gemini API as first fallback. Returns cleaned text or None on any error.

    Gemini: implicit caching is auto-enabled in 2.5+ when system_instruction
    repeats. No flag needed. We log `cached_content_token_count` to verify.
    """
    t0 = time.monotonic()
    try:
        from google import genai
        from google.genai import types as genai_types

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model,
            contents=user_message,
            config=genai_types.GenerateContentConfig(
                system_instruction=CLEANER_SYSTEM_PROMPT,
                temperature=0.1,
                max_output_tokens=max_tokens,
                http_options=genai_types.HttpOptions(timeout=int(timeout * 1000)),
            ),
        )
        cleaned = (response.text or user_message).strip()
        duration_ms = int((time.monotonic() - t0) * 1000)

        usage = getattr(response, "usage_metadata", None)
        prompt_tokens = getattr(usage, "prompt_token_count", None)
        cached_tokens = getattr(usage, "cached_content_token_count", None)

        stats = _format_cache_stats(prompt_tokens, cached_tokens)
        print(f"[cleaner] provider=gemini duration={duration_ms}ms {stats}")
        return cleaned
    except Exception as e:
        duration_ms = int((time.monotonic() - t0) * 1000)
        print(f"[cleaner] provider=gemini FAILED ({duration_ms}ms): {e}")
        return None


# Set to True the first time Groq rejects the cleaner payload with 413
# (tokens-per-minute limit below our ~8k-token system prompt on free tier).
# The limit is per-organization, so retrying every dictation is pure latency.
_groq_payload_too_large = False


def _call_groq(
    user_message: str,
    api_key: str,
    model: str,
    max_tokens: int,
    timeout: float,
) -> str | None:
    """Call Groq API as fallback. Returns cleaned text or None on any error.

    Groq: automatic prefix caching for production models (llama-3.3-70b et al).
    Stats exposed via `prompt_tokens_details.cached_tokens` when available.
    """
    global _groq_payload_too_large
    if _groq_payload_too_large:
        return None
    t0 = time.monotonic()
    try:
        from groq import Groq

        client = Groq(api_key=api_key, timeout=timeout)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": CLEANER_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.1,
            max_tokens=max_tokens,
        )
        cleaned = response.choices[0].message.content or user_message
        cleaned = cleaned.strip()
        duration_ms = int((time.monotonic() - t0) * 1000)

        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        cached_tokens = None
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached_tokens = getattr(details, "cached_tokens", None)

        stats = _format_cache_stats(prompt_tokens, cached_tokens)
        print(f"[cleaner] provider=groq duration={duration_ms}ms {stats}")
        return cleaned
    except Exception as e:
        duration_ms = int((time.monotonic() - t0) * 1000)
        err = str(e)
        if "413" in err and "tokens per minute" in err:
            _groq_payload_too_large = True
            print(
                "[cleaner] provider=groq disabled for this session: account TPM "
                "limit is below the cleaner prompt size (upgrade tier to re-enable)"
            )
        print(f"[cleaner] provider=groq FAILED ({duration_ms}ms): {e}")
        return None

"""Vocabulary + safe builtin corrections for Whisper transcription.

Design notes
------------
Previously this module "learned" wrong→right regex mappings from any user edit
and re-applied them globally. That is unsafe: a single mis-aligned diff leaked
destructive rules like "está" → "parte" into every future transcription.

The new design only does two things:

1) Apply a curated set of builtin substitutions (BUILTIN_CORRECTIONS) for
   common Whisper mishearings of tech terms. These use word boundaries and
   skip very short keys, so they cannot corrupt unrelated words.
2) Learn *vocabulary words* (not mappings) from user corrections. When the
   user edits a transcription and introduces a new word, that word is added
   to a vocabulary table and fed into Whisper's `prompt` parameter next time.
   This biases Whisper without ever rewriting its output.
"""

import re
from db.database import TranscriptionDB
from config import (
    TECH_VOCABULARY,
    BUILTIN_CORRECTIONS,
    VOCAB_PROMOTE_AFTER,
    VOCAB_MAX_CANDIDATES_PER_DICTATION,
)


# Words we never treat as "learnable vocabulary". Keeps common Spanish
# function words and filler out of the Whisper prompt when the user makes
# normal sentence-level edits.
_SPANISH_STOPWORDS = frozenset(
    w.lower() for w in [
        "está", "esta", "este", "estos", "estas", "estoy", "estás", "estamos", "están",
        "estaba", "estaban", "estuvo", "estuve", "estuviste", "estuvimos",
        "ser", "eres", "soy", "somos", "sois", "son", "era", "eras", "eran",
        "fue", "fueron", "fui", "fuiste", "fuimos", "sea", "sean", "sido",
        "para", "pero", "porque", "pues", "entonces", "aunque", "mientras",
        "cuando", "cuándo", "donde", "dónde", "como", "cómo", "cuanto", "cuánto",
        "cual", "cuál", "quien", "quién", "que", "qué",
        "muy", "mucho", "mucha", "muchos", "muchas", "poco", "poca", "pocos", "pocas",
        "hay", "haber", "tengo", "tienes", "tiene", "tenemos", "tenéis", "tienen",
        "tenía", "tenían", "tuvo", "tuvieron",
        "nosotros", "vosotros", "ellos", "ellas", "usted", "ustedes",
        "hola", "gracias", "buenos", "buenas",
        "desde", "hasta", "entre", "sobre", "bajo", "hacia", "tras", "contra",
        "ante", "según", "durante", "mediante",
        "porque", "porqué", "acerca", "incluso", "además", "también", "tampoco",
        "ahora", "luego", "antes", "después", "siempre", "nunca", "jamás",
        "aún", "todavía", "aquí", "allí", "allá", "ahí",
        "cosa", "cosas", "vez", "veces", "parte", "partes", "lado", "lados",
        "tipo", "tipos", "forma", "formas",
        "bien", "bueno", "buena", "mal", "malo", "mala",
        "algo", "nada", "todo", "toda", "todos", "todas",
        "nadie", "alguien", "alguno", "alguna", "algunos", "algunas",
        "ninguno", "ninguna", "ningún",
        "hacer", "hace", "hizo", "hago", "haces", "hacen", "hecho",
        "decir", "dice", "dije", "dijo", "digo", "dices", "dicho",
        "puedes", "puede", "podemos", "pueden", "podría", "podrías", "podemos",
        "debe", "debes", "debemos", "deben", "debería",
        "quiero", "quieres", "quiere", "queremos", "quieren", "querer",
        "voy", "vas", "vamos", "van", "iba", "iban",
        "sería", "serían", "será", "serán", "fuera", "fueran",
        "claro", "vale", "venga", "oye", "mira",
        "también", "solo", "sólo", "casi", "quizá", "quizás", "tal", "vez",
        "más", "menos", "tan", "tanto", "tanta", "tantos", "tantas",
        "con", "sin", "por", "para", "del", "las", "los", "una", "unos", "unas",
    ]
)

# Builtin keys below this length are rejected to prevent accidental substring
# mangling (e.g. "prom" → "prompt" would eat "promedio", "promo", etc.).
_MIN_BUILTIN_KEY_LEN = 4


class Dictionary:
    """Applies safe builtin corrections and tracks a user vocabulary."""

    def __init__(self, db: TranscriptionDB):
        self._db = db
        self._vocabulary_words: list[str] = []
        # Pre-compile builtin corrections with word boundaries once.
        self._builtin_patterns = self._compile_builtins()
        self.reload()

    @staticmethod
    def _compile_builtins() -> list[tuple[re.Pattern, str]]:
        compiled: list[tuple[re.Pattern, str]] = []
        # Sort by key length descending so multi-word patterns ("claud code")
        # fire BEFORE their single-word substrings ("claud"). Otherwise the
        # short rule rewrites the longer phrase first and the multi-word one
        # never matches.
        items = sorted(
            BUILTIN_CORRECTIONS.items(),
            key=lambda kv: len(kv[0].strip()),
            reverse=True,
        )
        for wrong, correct in items:
            key = wrong.strip()
            if len(key) < _MIN_BUILTIN_KEY_LEN:
                continue
            # Word boundaries on each side — keeps "jihab" from touching
            # random substrings. `\b` won't match before/after non-word chars,
            # so for multi-word keys ("git hub") we only anchor the outer edges.
            pattern = re.compile(rf"\b{re.escape(key)}\b", re.IGNORECASE)
            compiled.append((pattern, correct))
        return compiled

    def reload(self):
        """Reload vocabulary from DB."""
        self._vocabulary_words = self._db.get_vocabulary_words()

    def user_vocabulary_words(self) -> list[str]:
        """Return the user-learned vocabulary words.

        These are project-specific terms taught via corrections. The cleaner
        LLM uses them to know which proper-noun-like tokens to respect even
        when they look unusual.
        """
        return list(self._vocabulary_words)

    def apply(self, text: str) -> str:
        """Apply safe builtin corrections only.

        No user-learned regex mappings are ever applied here. User vocabulary
        only influences the Whisper prompt — never post-processing.
        """
        result = text
        for pattern, replacement in self._builtin_patterns:
            result = pattern.sub(replacement, result)
        return result

    # --- Learning: words only, never mappings ---

    _WORD_RE = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9.\-/']*")

    # Punctuation stripped from token edges. Internal '.', '-', '/' are kept
    # (Claude.md, example.com, cherry-pick) — only leading/trailing junk goes.
    _EDGE_PUNCT = ".,;:!?…\"'()"

    @classmethod
    def _normalize_token(cls, token: str) -> str:
        return token.strip(cls._EDGE_PUNCT)

    @staticmethod
    def _looks_like_proper_or_technical(token: str) -> bool:
        """Reject plain-lowercase-alphabetic words (Whisper edit noise).

        A learned word only earns a spot in the Whisper prompt if it "looks"
        like a proper noun or technical term: it has an uppercase letter, a
        digit, or an internal separator (Claude.md, example.com, cherry-pick).
        Otherwise any lowercase Spanish word the user happened to retype
        ("esto", "pulir", "escuche") would poison the prompt just as badly as
        the old wrong→right mappings did.
        """
        if any(c.isupper() or c.isdigit() for c in token):
            return True
        return any(c in ".-/" for c in token[1:-1])

    @classmethod
    def _is_candidate_token(cls, token: str, orig_tokens: set[str], tech_lower: set[str]) -> bool:
        """Shared content-word filter used by both `learn()` and `observe()`.

        True when `token` (already edge-trimmed) is worth learning: new
        versus `orig_tokens`, long enough (≥4 chars) to be a meaningful term,
        contains at least one letter, isn't a Spanish stopword/filler, isn't
        already covered by the static tech vocabulary, AND looks like a
        proper noun or technical term (uppercase/digit/internal separator).
        Plain lowercase-alphabetic tokens are rejected — those are exactly
        the kind of noise ("esto", "pulir", "invente...") that used to
        poison the Whisper prompt via loosely-learned vocabulary.
        """
        if not token:
            return False
        low = token.lower()
        if low in orig_tokens:
            return False
        if len(token) < 4:
            return False
        if not any(c.isalpha() for c in token):
            return False
        if low in _SPANISH_STOPWORDS:
            return False
        if low in tech_lower:
            return False
        if not cls._looks_like_proper_or_technical(token):
            return False
        return True

    def learn(self, original: str, corrected: str):
        """Learn vocabulary from a user correction.

        Adds words that appear in `corrected` but not in `original` and pass
        `_is_candidate_token` (real content word, not stopword/tech-vocab
        noise, looks like a proper noun or technical term).

        Crucially, this never stores a wrong→right mapping.
        """
        orig_tokens = {
            self._normalize_token(t).lower() for t in self._WORD_RE.findall(original)
        }
        tech_lower = {t.lower() for t in TECH_VOCABULARY}

        seen: set[str] = set()
        for raw_token in self._WORD_RE.findall(corrected):
            token = self._normalize_token(raw_token)
            if not token:
                continue
            low = token.lower()
            if low in seen:
                continue
            seen.add(low)

            if not self._is_candidate_token(token, orig_tokens, tech_lower):
                continue

            self._db.add_vocabulary_word(token)

        self.reload()

    # --- Passive learning: raw-vs-cleaned diff, no UI involved ---
    #
    # `observe()` needs a STRICTER proper-noun signal than `learn()`.
    # `_looks_like_proper_or_technical` (above) treats any uppercase letter
    # as evidence — correct for `learn()`, where a user retypes a word
    # mid-sentence by hand, so a capital there really does mean something.
    # But `cleaned_text` here is LLM-rewritten prose: every sentence the
    # cleaner starts is capitalized regardless of whether the word is a
    # proper noun, so on a real dry run (2026-08-24, 1634 raw/cleaned pairs)
    # 8 of the 10 words that would have auto-promoted were plain Spanish
    # imperatives ("Revisa", "Actualiza", "Arregla", "Deja", "Échale") that
    # only looked capitalized because the cleaner happened to start a
    # sentence with them — never proper nouns. A bare capital is no longer
    # enough here: a token needs either a technical signal that doesn't
    # depend on position, or a capital letter somewhere that ISN'T at the
    # start of a sentence/line/list item (real mid-sentence capitalization).

    # Sentence-ending punctuation after which the next word is expected to
    # be capitalized anyway — no positional evidence there.
    _SENTENCE_END_CHARS = ".!?:¡¿"
    # A line that, once a token is removed, is ONLY a bullet/number marker
    # ("-", "*", "•", "–", "1.", "2)") — the token is a list item's first
    # word, same "no evidence" bucket as a fresh sentence.
    _BULLET_LINE_RE = re.compile(r"^[ \t]*(?:[-*•–]|\d+[.)])[ \t]*$")

    @classmethod
    def _is_sentence_initial(cls, text: str, start: int) -> bool:
        """True if the token starting at `start` in `text` opens a
        sentence, a line, or a list item — i.e. carries NO positional
        evidence of being a proper noun on its own."""
        line_start = text.rfind("\n", 0, start) + 1
        line_prefix = text[line_start:start]
        stripped = line_prefix.rstrip()
        if not stripped:
            return True  # start of text or start of line
        if cls._BULLET_LINE_RE.match(line_prefix):
            return True  # only a bullet/number marker precedes it on this line
        return stripped[-1] in cls._SENTENCE_END_CHARS

    @staticmethod
    def _has_strong_technical_signal(token: str) -> bool:
        """True when `token` looks technical regardless of where it sits:
        a digit anywhere (n8n, gpt5), an internal '.', '/', '-' or '_'
        (env.local, example.com, Claude.md, cherry-pick), or an uppercase
        letter after the first character — camelCase / internal caps
        (PostgreSQL, WhatsApp). These don't need positional evidence
        because the cleaner has no reason to invent them mid-rewrite.
        """
        if any(c.isdigit() for c in token):
            return True
        if any(c in "./-_" for c in token[1:-1]):
            return True
        if any(c.isupper() for c in token[1:]):
            return True
        return False

    @classmethod
    def _has_non_initial_occurrence(cls, token: str, cleaned_text: str) -> bool:
        """True if `token` shows up anywhere in `cleaned_text` NOT at the
        start of a sentence/line/list item — real mid-sentence
        capitalization, the strong positional evidence of an actual proper
        noun (vs. a verb the cleaner capitalized only by starting a new
        sentence with it)."""
        pattern = re.compile(r"\b" + re.escape(token) + r"\b")
        for m in pattern.finditer(cleaned_text):
            if not cls._is_sentence_initial(cleaned_text, m.start()):
                return True
        return False

    @classmethod
    def _passes_observe_filter(cls, token: str, cleaned_text: str) -> bool:
        """Observe()-only gate layered on top of `_is_candidate_token`.

        Accepts a token when either (a) it carries a technical signal that
        doesn't depend on position, or (b) its capital letter shows up
        somewhere that isn't the start of a sentence/line/list item. A
        token whose only "evidence" is sitting first in a cleaner-rewritten
        sentence is rejected — that capital means nothing here.
        """
        if cls._has_strong_technical_signal(token):
            return True
        if not token[:1].isupper():
            return False
        return cls._has_non_initial_occurrence(token, cleaned_text)

    @staticmethod
    def _is_fragment_of_known_multi_word_entry(
        token_low: str, vocab_lower: set[str], tech_lower: set[str]
    ) -> bool:
        """True when `token_low` (already lowercased, same normalization as
        the rest of this module) is a word INSIDE a multi-word entry the
        system already knows in full — e.g. "code" inside vocabulary/tech
        entry "claude code", or "corp" inside vocabulary entry
        "acme corp". Learning the bare fragment adds nothing to the
        Whisper prompt beyond what the full term already provides, and for
        something like "code" it can even bias toward the isolated word
        instead of the complete term.

        Exemption: a token that ALSO has its own standalone (single-word)
        entry — in `vocab_lower` or `tech_lower` — is never rejected here;
        it earned that entry independently of any multi-word phrase.
        """
        if token_low in vocab_lower or token_low in tech_lower:
            return False
        for entry_low in vocab_lower | tech_lower:
            words = entry_low.split()
            if len(words) < 2:
                continue  # single-word entries aren't "fragments" of anything
            if token_low in words:
                return True
        return False

    @staticmethod
    def _sample_snippet(text: str, limit: int = 120) -> str:
        collapsed = " ".join(text.split())
        if len(collapsed) <= limit:
            return collapsed
        return collapsed[:limit].rstrip() + "…"

    def observe(self, raw_text: str, cleaned_text: str) -> list[str]:
        """Passive vocabulary learning from the raw-vs-cleaned diff.

        Every dictation already stores `raw_text` (post-ASR, post-builtin
        corrections) and `text` (post-cleaner) — no correction toast needed.
        A word that shows up in `cleaned_text` but not in `raw_text`, passes
        the same content-word filter as `learn()`, AND passes
        `_passes_observe_filter` (stricter positional check — see comment
        above `_is_sentence_initial`), is a *candidate*: it is bumped in
        `vocab_candidates` (never straight into the real
        vocabulary) and only promoted — added to `vocabulary` and fed into
        the Whisper prompt — once it has been seen `VOCAB_PROMOTE_AFTER`
        times. A single aggressive cleaner rewrite must never poison the
        prompt on its own. At most `VOCAB_MAX_CANDIDATES_PER_DICTATION` new
        candidates are registered per dictation, so one rewrite can't flood
        the dashboard's suggestions tray.

        Returns the list of words promoted by this call (for logging).
        Never raises — a failure here must not block a paste.
        """
        try:
            raw_text = (raw_text or "").strip()
            cleaned_text = (cleaned_text or "").strip()
            if not raw_text or not cleaned_text or raw_text == cleaned_text:
                return []

            orig_tokens = {
                self._normalize_token(t).lower() for t in self._WORD_RE.findall(raw_text)
            }
            tech_lower = {t.lower() for t in TECH_VOCABULARY}
            vocab_lower = {w.lower() for w in self._vocabulary_words}

            sample_raw = self._sample_snippet(raw_text)
            sample_clean = self._sample_snippet(cleaned_text)

            promoted: list[str] = []
            seen: set[str] = set()
            candidates_found = 0
            for raw_token in self._WORD_RE.findall(cleaned_text):
                if candidates_found >= VOCAB_MAX_CANDIDATES_PER_DICTATION:
                    break
                token = self._normalize_token(raw_token)
                if not token:
                    continue
                low = token.lower()
                if low in seen:
                    continue
                seen.add(low)

                # Already part of the user's learned vocabulary → nothing new.
                if low in vocab_lower:
                    continue
                # A word inside an already-known multi-word entry ("code" in
                # "Claude Code", "corp" in "Acme Corp") adds nothing on
                # its own — skip it unless it also has its own standalone
                # entry (checked inside the helper).
                if self._is_fragment_of_known_multi_word_entry(low, vocab_lower, tech_lower):
                    continue
                if not self._is_candidate_token(token, orig_tokens, tech_lower):
                    continue
                # Stricter gate than learn(): a bare sentence-initial capital
                # is not evidence in LLM-rewritten prose (see comment above).
                if not self._passes_observe_filter(token, cleaned_text):
                    continue

                candidates_found += 1
                result = self._db.bump_vocab_candidate(
                    token, sample_raw=sample_raw, sample_clean=sample_clean
                )
                if (
                    result.get("status") == "pending"
                    and result.get("times_seen", 0) >= VOCAB_PROMOTE_AFTER
                ):
                    self._db.add_vocabulary_word(token)
                    self._db.set_vocab_candidate_status(token, "approved")
                    promoted.append(token)

            if promoted:
                self.reload()
            return promoted
        except Exception:
            return []

    # --- Whisper prompt ---

    # Groq Whisper limit on the `prompt` parameter.
    MAX_PROMPT_CHARS = 890

    def whisper_prompt(self) -> str:
        """Build a natural-sounding prompt that biases Whisper toward our vocabulary.

        Whisper treats `prompt` as a previous-transcript context. A realistic
        Spanish sentence with tech terms in their correct spelling works much
        better than "Spanglish técnico" + comma-separated lists, and does not
        push Whisper to invent English where none was spoken.
        """
        # User-learned vocabulary takes priority (most recently used first),
        # then the curated static tech vocabulary.
        ordered: list[str] = []
        seen_lower: set[str] = set()
        for word in list(self._vocabulary_words) + list(TECH_VOCABULARY):
            low = word.lower()
            if low in seen_lower:
                continue
            seen_lower.add(low)
            ordered.append(word)

        lead = (
            "Transcripción en español natural. Puede incluir nombres propios "
            "y términos técnicos en inglés con su grafía correcta, por ejemplo: "
        )
        tail = "."

        budget = self.MAX_PROMPT_CHARS - len(lead) - len(tail)
        parts: list[str] = []
        length = 0
        for word in ordered:
            addition = len(word) + 2  # ", "
            if length + addition > budget:
                break
            parts.append(word)
            length += addition

        return lead + ", ".join(parts) + tail

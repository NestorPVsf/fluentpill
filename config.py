import os
from dotenv import load_dotenv

# Load .env first, then .env.local on top (Next.js convention — .env.local wins).
_BASE_DIR = os.path.dirname(__file__)
load_dotenv(os.path.join(_BASE_DIR, ".env"))
load_dotenv(os.path.join(_BASE_DIR, ".env.local"), override=True)

# Groq API
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
# whisper-large-v3 (full) over -turbo: noticeably better accuracy on fast,
# rambling Spanish — turbo produced garbled fragments that no cleaner can fix.
GROQ_MODEL = os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3")
# Transcription language sent to Whisper. "es" anchors Spanish (fewer
# hallucinations on Spanglish dictation). Set WHISPER_LANGUAGE= (empty) to let
# Whisper auto-detect per dictation (multi-language / code-switching).
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "es")

# Transcription resilience (Wispr Flow lesson: a single-provider outage must
# not take dictation down). Groq is retried with short backoff; if every
# attempt fails, one cross-vendor fallback to OpenAI's transcription API.
TRANSCRIBE_TIMEOUT_SECONDS = float(os.getenv("TRANSCRIBE_TIMEOUT_SECONDS", "45"))
TRANSCRIBE_GROQ_ATTEMPTS = int(os.getenv("TRANSCRIBE_GROQ_ATTEMPTS", "2"))
OPENAI_TRANSCRIBE_FALLBACK = os.getenv("OPENAI_TRANSCRIBE_FALLBACK", "true").lower() == "true"
# Verified current audio-transcription model ids (ago 2026): gpt-transcribe
# (jul 2026 — 3.3% WER vs 4.0% de gpt-4o-transcribe, y más barato:
# $0.0045/min vs $0.006), gpt-4o-transcribe, gpt-4o-mini-transcribe,
# whisper-1 (legacy).
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-transcribe")
# Which provider tries first. "openai" (default) tries OpenAI once, then falls
# back to the Groq×N chain on failure. "groq" keeps the original order (Groq×N
# first, OpenAI as cross-vendor fallback). Only takes effect if OPENAI_API_KEY
# is set — otherwise Groq always goes first regardless of this setting.
ASR_PRIMARY = os.getenv("ASR_PRIMARY", "openai").lower()

# Gemini API
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

# Audio
SAMPLE_RATE = 16000
CHANNELS = 1
AUDIO_DTYPE = "int16"
BLOCK_SIZE = 1024

# Session cap: auto-stop and transcribe after N seconds of continuous
# recording (0 disables). Wispr Flow caps at 20 min; Groq's 25MB WAV limit is
# ~13 min at 16kHz/16-bit mono, so 12 min keeps every dictation transcribable
# instead of failing at the API after a long monologue.
MAX_RECORDING_SECONDS = float(os.getenv("MAX_RECORDING_SECONDS", "720"))

# Sound cues: short beep on recording start/stop. Added 2026-08-24 because the
# pill kept ending up buried behind other windows — fluentpill.log showed two
# dictation sessions of 718s and 720s recording the whole time unnoticed, and a
# sound was the only feedback that didn't depend on seeing the pill.
# OFF by default since the same day: once the pill reliably sits on top while
# recording (core/win_topmost.py) the beep became redundant, and it is actively
# unwanted in a cafe or over headphones. Kept as a one-flag fallback — if the
# always-on-top ever regresses, SOUND_CUES_ENABLED=true restores the audible
# cue with no code change. Windows: two short WAV tones (core/sounds.py),
# played non-blocking (winsound.Beep() would freeze the calling thread for the
# tone's duration). No-op on macOS/Linux.
SOUND_CUES_ENABLED = os.getenv("SOUND_CUES_ENABLED", "false").lower() == "true"

# UI
PILL_WIDTH_IDLE = 52
PILL_WIDTH_RECORDING = 210  # +26px vs the original 184 to fit the M:SS elapsed counter
PILL_WIDTH_STATUS = 80
PILL_HEIGHT = 52
PILL_OPACITY = 1.0
PILL_CORNER_RADIUS = 26
PILL_MARGIN_BOTTOM = 14
LOGO_SIZE = 26

# Logo path
LOGO_PATH = os.path.join(os.path.dirname(__file__), "logo_small.png")

# Tray icons (fluentpill brand). Multi-size .ico: the 16 px entry is the
# pixel-grid master, so it stays crisp on the taskbar. Cyan reads on the dark
# taskbar (the default); teal is the same mark for a light taskbar. See
# ui/tray_icon.py for how one is picked.
_BRAND_DIR = os.path.join(os.path.dirname(__file__), "assets", "brand")
TRAY_ICON_DARK_TASKBAR = os.path.join(_BRAND_DIR, "fluentpill.ico")
TRAY_ICON_LIGHT_TASKBAR = os.path.join(_BRAND_DIR, "fluentpill-teal.ico")

# Pill visibility model (2026-08-24): by default the pill is hidden at rest
# and only appears while a dictation is actually in flight (RECORDING /
# PROCESSING / DONE / ERROR) — the system tray icon (ui/tray_icon.py) is now
# the permanent presence indicator. PILL_ALWAYS_VISIBLE=true restores the
# pre-2026-08-24 behavior (pill permanently on screen, shrunk to
# PILL_WIDTH_IDLE at rest) as a one-flag safety net if the new model doesn't
# work out — same boolean-from-env pattern as SOUND_CUES_ENABLED above.
PILL_ALWAYS_VISIBLE = os.getenv("PILL_ALWAYS_VISIBLE", "false").lower() == "true"

# Canonical reveal position (2026-08-25): the pill always reappears centered
# horizontally and pinned at this fraction of the screen's FULL height
# (taskbar included in the fraction's reference, but see pill_widget.py's
# _canonical_position() for the clamp against the taskbar-free available
# area). 0.72 * 1080 ≈ 778px on a 1080p screen — inside the bottom third,
# with breathing room above the taskbar. No longer reads a dragged/saved
# position on reveal — dragging
# during a dictation still works, it just doesn't persist to the next one.
PILL_VERTICAL_ANCHOR = float(os.getenv("PILL_VERTICAL_ANCHOR", "0.72"))

# Reveal/dismiss animation durations (2026-08-25, "Apple-style" — fast start,
# long settle, opacity + a small vertical pose move together). Dismiss is
# deliberately shorter than reveal — that asymmetry is part of the feel.
PILL_REVEAL_MS = float(os.getenv("PILL_REVEAL_MS", "260"))
PILL_DISMISS_MS = float(os.getenv("PILL_DISMISS_MS", "180"))

# Audio Visualizer
NUM_BARS = 12
VIZ_FPS = 30
BAR_DECAY = 0.80
BAR_GAIN = 6.0

# Hotkey
DOUBLE_TAP_INTERVAL = 0.4  # seconds for double-tap detection

# Database
DB_PATH = os.path.join(os.path.dirname(__file__), "transcriptions.db")

# Common Whisper misheard tech words (Spanglish → correct)
# These are applied as post-processing after transcription
BUILTIN_CORRECTIONS = {
    # GitHub variations
    "yihabb": "GitHub", "yijab": "GitHub", "jihab": "GitHub", "gitjab": "GitHub",
    "guithub": "GitHub", "guitjab": "GitHub", "git hub": "GitHub", "guitab": "GitHub",
    "guit hab": "GitHub", "yitjab": "GitHub", "jihub": "GitHub",
    # JavaScript variations
    "jascript": "JavaScript", "yavascript": "JavaScript", "javacript": "JavaScript",
    "java script": "JavaScript",
    # TypeScript variations
    "taiscript": "TypeScript", "taipscript": "TypeScript", "type script": "TypeScript",
    # Next.js
    "nextjes": "Next.js", "nex js": "Next.js", "nexyes": "Next.js",
    # React
    "riact": "React", "reac": "React",
    # Docker
    "doquer": "Docker", "doker": "Docker",
    # Kubernetes
    "cubernetes": "Kubernetes", "cubernétes": "Kubernetes", "kubernetis": "Kubernetes",
    # Vercel — pronunciación española típica (/θ/ o /s/) confunde Whisper
    "bercel": "Vercel", "versél": "Vercel", "versel": "Vercel",
    "verzel": "Vercel", "berzel": "Vercel", "verzell": "Vercel",
    "vercell": "Vercel", "bercell": "Vercel", "versell": "Vercel",
    "berzell": "Vercel", "berseil": "Vercel", "verseil": "Vercel",
    # Supabase
    "superbase": "Supabase", "superbeis": "Supabase", "supa beis": "Supabase",
    "supabase": "Supabase", "sipábase": "Supabase", "supabeiss": "Supabase",
    "supabase ": "Supabase", "súpabase": "Supabase",
    # Prisma
    "prisma": "Prisma", "prizma": "Prisma",
    # Stripe
    "estripe": "Stripe", "estráip": "Stripe",
    # Node
    "noudyeys": "Node.js", "noud js": "Node.js",
    # Claude / Claude Code (multi-word mishearings: Whisper bias not enough
    # because "Claude" ≈ "Cloud" phonetically and "Code" picks up trailing "c")
    "clod": "Claude", "claud": "Claude",
    "cloud code": "Claude Code", "cloud codec": "Claude Code",
    "claud code": "Claude Code", "claud codec": "Claude Code",
    "clod code": "Claude Code", "clod codec": "Claude Code",
    "cloud kode": "Claude Code", "cloude code": "Claude Code",
    # Deploy/deployment
    "deploi": "deploy", "deploiment": "deployment", "diploi": "deploy",
    # Webhook
    "webjuk": "webhook", "güebjuk": "webhook",
    # Middleware
    "middelwer": "middleware", "midelwer": "middleware",
    # Backend/Frontend
    "backen": "backend", "fronten": "frontend",
    # Database
    "databeis": "database", "data beis": "database",
    # Endpoint
    "endpoin": "endpoint", "enpoin": "endpoint",
    # Debugging
    "debuguin": "debugging", "debugin": "debugging",
    # Pipeline
    "paiplain": "pipeline", "payplain": "pipeline",
    # Refactor
    "refactorin": "refactoring", "rifactor": "refactor",
    # Streaming
    "estrimin": "streaming", "estriming": "streaming",
    # Feature
    "fichar": "feature", "ficher": "feature",
    # Sprint
    "esprin": "sprint",
    # Workflow
    "worflo": "workflow", "guorflo": "workflow",
    # Dashboard
    "dashbor": "dashboard", "dasbor": "dashboard",
    # Template
    "templeit": "template", "templei": "template",
    # Snippet
    "esnipet": "snippet", "snippe": "snippet",
    # Serverless
    "serverles": "serverless", "serberles": "serverless",
}

# Cleaner provider chains, tiered by dictation length (first success wins):
#   SHORT (< CLEANER_LONG_WORDS): GPT-5.6 Luna → Gemini Flash-Lite → Groq.
#     Filler-stripping needs speed, not depth (~2s). Luna (jul 2026) supera a
#     gpt-5.4-mini en calidad y cuesta una fracción ($0.20/$1.20 por M tokens).
#   LONG  (>= CLEANER_LONG_WORDS): GPT-5.6 Terra → GPT-5.6 Luna → Groq.
#     Terra (nivel GPT-5.5) synthesizes rambling monologues better than the
#     small tier. Sol descartado a propósito: 2.5× el precio de Terra por
#     inteligencia que limpiar dictados no necesita.
#     Gemini 3.5 Flash REMOVED from LONG chain — drifts badly in practice
#     (translates to English, invents mappings, ignores no-chatbot rule).
GEMINI_CLEANER_MODEL = os.getenv("GEMINI_CLEANER_MODEL", "gemini-3.1-flash-lite")
GEMINI_CLEANER_MODEL_LONG = os.getenv("GEMINI_CLEANER_MODEL_LONG", "gemini-3.5-flash")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_CLEANER_MODEL = os.getenv("OPENAI_CLEANER_MODEL", "gpt-5.6-luna")
OPENAI_CLEANER_MODEL_LONG = os.getenv("OPENAI_CLEANER_MODEL_LONG", "gpt-6.1-sol")
# English mode (corrections + Explicame) stays on terra: gpt-6.1-sol leaves "upgrade" empty and takes ~9 s (2026-10-21).
ENGLISH_MODEL = os.getenv("ENGLISH_MODEL", "gpt-5.6-terra")
GROQ_CLEANER_MODEL = os.getenv("GROQ_CLEANER_MODEL", "openai/gpt-oss-120b")

# English corrections panel: "listen" buttons (core/english_tts.py). Native speech
# generation of Gemini; model and voice names checked 2026-10-20 at
# ai.google.dev/gemini-api/docs/speech-generation ("Iapetus" is tagged Clear).
ENGLISH_TTS_PROVIDER = os.getenv("ENGLISH_TTS_PROVIDER", "gemini").lower()
ENGLISH_TTS_MODEL = os.getenv("ENGLISH_TTS_MODEL", "gemini-3.8-flash-tts")
ENGLISH_TTS_VOICE = os.getenv("ENGLISH_TTS_VOICE", "Iapetus")
ENGLISH_TTS_STYLE = os.getenv(
    "ENGLISH_TTS_STYLE", "natural native American English, clear, conversational pace")
ENGLISH_TTS_TIMEOUT_SECONDS = float(os.getenv("ENGLISH_TTS_TIMEOUT_SECONDS", "20"))
# Boundary between tiers — keep in sync with the 25-word rule in prompts/dictation_cleaner.md
CLEANER_LONG_WORDS = int(os.getenv("CLEANER_LONG_WORDS", "26"))

# Cleaner behavior
CLEANER_ENABLED = os.getenv("CLEANER_ENABLED", "true").lower() == "true"
CLEANER_MIN_WORDS = int(os.getenv("CLEANER_MIN_WORDS", "8"))
CLEANER_TIMEOUT_SECONDS = float(os.getenv("CLEANER_TIMEOUT_SECONDS", "10.0"))

# App-context aware cleaning: capture the foreground app at record time and
# let the cleaner adapt register/format per category (email/chat/code/ai/
# notes/docs). Only the app name + derived category are sent to the LLM; the
# window title is used LOCALLY for browser-tab detection (Gmail vs WhatsApp
# Web) and only included in the LLM message when APP_CONTEXT_SEND_TITLE=true.
APP_CONTEXT_ENABLED = os.getenv("APP_CONTEXT_ENABLED", "true").lower() == "true"
APP_CONTEXT_SEND_TITLE = os.getenv("APP_CONTEXT_SEND_TITLE", "false").lower() == "true"

# Correction toast (the dark editable box shown ~800ms after paste, letting
# you fix the transcript and teach the dictionary): OFF by default since
# 2026-08-24. In a terminal (core/app_context.py's is_terminal) it can't
# replace the pasted text by design — undo_and_paste_text() takes the
# clipboard-only path there — so it only got in the way there. Kept behind
# this flag rather than removed: ui/correction_toast.py and the
# correction_submitted signal still work when re-enabled.
CORRECTION_TOAST_ENABLED = os.getenv("CORRECTION_TOAST_ENABLED", "false").lower() == "true"
# Floating panel with the corrections of an English dictation (ui/english_popup.py)
ENGLISH_POPUP_ENABLED = os.getenv("ENGLISH_POPUP_ENABLED", "true").lower() == "true"
# Shadowing in that panel: a mic button next to each play button, repeat aloud and get a 0-100 score
ENGLISH_SHADOWING_ENABLED = os.getenv("ENGLISH_SHADOWING_ENABLED", "true").lower() == "true"
# "Explícame" button in that panel: a short Spanish mini-lesson about the dictation (core/english_explain.py)
ENGLISH_EXPLAIN_ENABLED = os.getenv("ENGLISH_EXPLAIN_ENABLED", "true").lower() == "true"

# Refine ("✨ Refinar Prompt") widget + its preview/compare window: OFF by
# default since 2026-08-25 — the author doesn't use the button. Same policy as
# CORRECTION_TOAST_ENABLED above: disabled behind a flag, not deleted.
# ui/refine_widget.py, ui/preview_widget.py, core/refiner.py and the
# refine_requested/refinement_done/refinement_error/choice_made signal
# wiring in main.py all still work the moment this is flipped back to true
# — no code was removed to turn this off, see main.py's FluentpillApp.__init__.
REFINE_ENABLED = os.getenv("REFINE_ENABLED", "false").lower() == "true"

# Paste queue (2026-08-24): golden rule is "never paste while the user is
# actively recording the next dictation" — a finished transcription that
# lands mid-recording is held and pasted together with the new one, combined
# with "\n\n", the moment the CURRENT recording actually stops. See main.py
# _queue_or_paste()/_drain_pending(). MAX_PENDING_PASTES is a safety valve
# for the pathological case (transcription latency spikes badly while the
# user keeps dictating) — past this many held items, flush immediately
# rather than grow unbounded or risk losing text the user already said, even
# though that flush can land mid-recording (deliberate, logged trade-off).
MAX_PENDING_PASTES = int(os.getenv("MAX_PENDING_PASTES", "10"))

# Game mute (2026-09-05): Ctrl and Shift are pressed constantly in RTS games
# (Ctrl+1..9 are control groups, Shift queues orders), so fluentpill kept starting
# recordings while playing — 8478 app-context captures with StarCraft in the
# foreground in one log. While a fullscreen app is focused, the keyboard
# shortcuts stop responding; the tray click stays live as a deliberate escape
# hatch (unreachable by accident from inside a game). Set to false to restore
# the pre-2026-09-05 behavior, same policy as PILL_ALWAYS_VISIBLE / REFINE_ENABLED.
#
# Honest about the false positives, because "my hotkeys are dead" is the most
# likely confusion this creates: a FULLSCREEN VIDEO and a SLIDESHOW
# PRESENTATION also mute the shortcuts. That is inherent to the detector
# (core/win_topmost.py) — for the pill it was merely cosmetic, here it gates
# input. The tray icon always works.
GAME_MUTE_ENABLED = os.getenv("GAME_MUTE_ENABLED", "true").lower() == "true"

# Ctrl+Space through Win32 RegisterHotKey on a dedicated high-priority thread
# (2026-10-13), so the pill opens fast even on a saturated machine. If another
# app already owns the chord it logs a WARNING and falls back to the pynput
# hook. Set HOTKEY_REGISTERHOTKEY=false to go back to the pynput-only path.
HOTKEY_REGISTERHOTKEY = os.getenv("HOTKEY_REGISTERHOTKEY", "true").lower() == "true"

# Passive vocabulary auto-learn (2026-08-24): with the correction toast OFF,
# the dictionary needs another way to grow. Every dictation already stores
# raw_text (post-ASR, post-builtin-corrections) and text (post-cleaner) — any
# word that shows up in the cleaned transcript but not the raw one is a
# candidate. Dictionary.observe() bumps it in vocab_candidates and only
# promotes it into the real vocabulary (and the Whisper prompt) after
# VOCAB_PROMOTE_AFTER sightings — a single cleaner rewrite must never poison
# the prompt on its own. VOCAB_MAX_CANDIDATES_PER_DICTATION caps how many new
# candidates one dictation can register, so an aggressive rewrite can't flood
# the dashboard's "sugerencias de vocabulario" tray. Candidates that never
# reach the threshold wait there; rejecting one is permanent — it is never
# re-proposed.
VOCAB_AUTOLEARN_ENABLED = os.getenv("VOCAB_AUTOLEARN_ENABLED", "true").lower() == "true"
VOCAB_PROMOTE_AFTER = int(os.getenv("VOCAB_PROMOTE_AFTER", "3"))
VOCAB_MAX_CANDIDATES_PER_DICTATION = int(os.getenv("VOCAB_MAX_CANDIDATES_PER_DICTATION", "5"))

# Silence gate: recordings whose peak per-block RMS (int16 scale, ±32768) is
# below this threshold are considered silent and skipped before calling Whisper.
# Lowered 180 -> 60 on 2026-08-24, measured from the user's own log: real
# silences gave peak_rms of 0, 2.6, 41.9 and 53.7, while a REAL dictation was
# dropped at peak_rms=179.1 < 180.0, and several others in the 82-142 range
# were almost certainly quiet/distant speech rather than silence. 60 stays
# above every measured silence without swallowing real speech. The
# deterministic anti-hallucination filter (strip_whisper_hallucinations)
# still covers Whisper's phantom phrases regardless of this threshold.
# Tune via env var SILENCE_PEAK_RMS if the mic is unusually quiet or noisy.
SILENCE_PEAK_RMS = float(os.getenv("SILENCE_PEAK_RMS", "60"))

# Interaction between the two features above: the start-of-recording sound
# cue (SOUND_CUES_ENABLED) is itself picked up by the mic. Measured on real
# hardware (Microphone Array): ambient peak_rms=421, peak_rms=756 during the
# ~50ms tone — enough to nudge a truly silent recording over a low
# SILENCE_PEAK_RMS threshold and let it reach Whisper. main.py passes this to
# recorder.peak_rms(skip_initial_seconds=...) — but ONLY when
# SOUND_CUES_ENABLED is on; with cues off there is nothing to skip and the
# gate should look at the whole recording. See core/recorder.py peak_rms().
SILENCE_GATE_SKIP_INITIAL_SECONDS = float(os.getenv("SILENCE_GATE_SKIP_INITIAL_SECONDS", "0.25"))

# Audio prep before transcription: energy-based silence trim (no VAD model —
# numpy RMS windows, same threshold family as SILENCE_PEAK_RMS) fixes Whisper
# hallucinating over leading/trailing silence; splitting long dictations at
# natural pauses fixes accuracy degrading past a few minutes of continuous
# audio. CHUNK_SECONDS=0 disables splitting (still trims).
SILENCE_TRIM_ENABLED = os.getenv("SILENCE_TRIM_ENABLED", "true").lower() == "true"
CHUNK_SECONDS = float(os.getenv("CHUNK_SECONDS", "60"))

# Lifecycle log lines (2026-09-05): fluentpill.log has no timestamps on any line by
# default (see main.py's _setup_logging, left untouched on purpose). These are
# the deliberate exception — a startup line, a heartbeat every N seconds, and
# a process-exit line via atexit — just enough to answer "when did fluentpill die,
# and was it a confirmed stop or an external kill" (see fluentpill-watchdog.ps1,
# which reads the same log for forensics). ~8 KB/day at the default interval,
# negligible next to the 5 MB truncation _setup_logging already does.
def _validate_heartbeat_interval(raw: str) -> float:
    """QTimer.start() takes an int milliseconds arg, so 0, negative, NaN,
    inf, or anything past Qt's ~24.8-day 32-bit ms range would either spin
    the timer instantly or crash the int(...) conversion in main.py. Fall
    back to the 300s default rather than let a bad env value wedge the
    heartbeat (or the app) at startup."""
    default = 300.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    max_seconds = 2_147_483_647 / 1000.0  # QTimer interval is a 32-bit ms int
    if not (0 < value <= max_seconds):
        return default
    return value


HEARTBEAT_INTERVAL_SECONDS = _validate_heartbeat_interval(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "300"))

# Watchdog health check (2026-09-05) — "nobody watches the watchdog" fix.
# fluentpill-watchdog.ps1 touches %LOCALAPPDATA%\fluentpill\watchdog-alive on EVERY
# pass (not just when it relaunches fluentpill); the heartbeat timer above checks
# that marker's age each tick and warns (tray tooltip + one fluentpill.log line
# per episode) once it's older than this many seconds. A marker that has
# NEVER existed means no watchdog task is installed — silence, not a
# warning, since fluentpill works fine standalone.
WATCHDOG_STALE_SECONDS = float(os.getenv("WATCHDOG_STALE_SECONDS", str(15 * 60)))

# Recording debug saves — optional feature to persist WAV files for diagnosis.
# Set SAVE_RECORDINGS=false to disable; retention auto-cleans files older than N hours.
SAVE_RECORDINGS = os.getenv("SAVE_RECORDINGS", "true").lower() == "true"
RECORDINGS_RETENTION_HOURS = float(os.getenv("RECORDINGS_RETENTION_HOURS", "24"))

# Base tech vocabulary for Whisper prompt (Spanglish support)
# These words bias Whisper toward correct English spellings when spoken in Spanish context
TECH_VOCABULARY = [
    # Git & Version Control
    "GitHub", "GitLab", "Bitbucket", "git", "commit", "push", "pull", "merge",
    "branch", "rebase", "cherry-pick", "stash", "checkout", "clone", "fork",
    "pull request", "PR", "diff", "staging",
    # Development
    "deploy", "deployment", "localhost", "backend", "frontend", "fullstack",
    "API", "REST", "GraphQL", "endpoint", "webhook", "middleware", "runtime",
    "framework", "library", "package", "module", "component", "plugin",
    # Languages & Tools
    "TypeScript", "JavaScript", "Python", "Node.js", "npm", "React", "Next.js",
    "Vue", "Svelte", "Angular", "Tailwind", "CSS", "HTML", "JSON", "YAML",
    "Docker", "Kubernetes", "Terraform", "Vercel", "AWS", "Azure",
    # AI & ML
    "Claude", "Claude Code", "GPT", "LLM", "Whisper", "Groq", "OpenAI",
    "Anthropic", "Gemini", "prompt", "token", "embedding", "fine-tuning",
    "RAG", "vector", "AI SDK", "MCP",
    # Concepts
    "refactor", "debug", "debugging", "pipeline", "CI/CD", "DevOps",
    "sprint", "backlog", "roadmap", "feature", "hotfix", "release",
    "database", "schema", "migration", "query", "index",
    "cache", "Redis", "PostgreSQL", "Postgres", "SQLite", "MongoDB",
    # Databases & SaaS
    "Supabase", "Neon", "Prisma", "Drizzle", "PlanetScale", "MongoDB",
    "Stripe", "Resend", "Clerk", "Upstash",
    # Common Spanglish
    "link", "feedback", "setup", "update", "upgrade", "workflow",
    "dashboard", "layout", "responsive", "template", "snippet",
    "bug", "fix", "issue", "ticket", "milestone", "repo", "README",
    "config", "settings", "env", "environment", "production", "staging",
    "server", "serverless", "cloud", "edge", "stream", "streaming",
]

# English practice mode (2026-10-20): a second chord that records like Ctrl+Space
# but transcribes in English (Whisper language="en", no Spanish vocabulary
# prompt), corrects grammar/vocabulary with an LLM and stores each correction in
# SQLite so the user can study his own mistakes. Empty value = feature off.
# Format: "ctrl+alt+space" (modifiers ctrl/alt/shift/win + space). Windows only
# (RegisterHotKey); if the chord is taken the mode is disabled with a log line.
ENGLISH_MODE_HOTKEY = os.getenv("ENGLISH_MODE_HOTKEY", "ctrl+alt+space").strip().lower()

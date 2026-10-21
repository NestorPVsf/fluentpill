"""Howl - Voice-to-text desktop tool powered by Groq Whisper."""

import os
import sys
import signal
import threading
import faulthandler
import traceback
import atexit
from datetime import datetime, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# Headless launch (pythonw.exe) doesn't connect stdout/stderr to anything, so
# print() statements would either crash or vanish. Redirect both to a rolling
# log file under the user's local app data so we keep the diagnostic trail.
# When launched via `python main.py` directly (visible console), keep behavior
# as-is so live logs work in development.
# ---------------------------------------------------------------------------
def _setup_logging() -> Path | None:
    """Route stdout/stderr to a log file when running without a console.

    Returns the log path if a file was opened, else None.
    """
    has_console = sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)()
    if has_console:
        return None  # interactive run — leave streams alone

    # Choose a per-OS sensible spot
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path.home() / ".config"
    log_dir = base / "Howl"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    log_path = log_dir / "howl.log"

    # Truncate if it grew over 5 MB so we don't accumulate forever
    try:
        if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
            log_path.unlink()
    except OSError:
        pass

    try:
        log_fp = open(log_path, "a", encoding="utf-8", buffering=1)  # line-buffered
    except OSError:
        return None
    sys.stdout = log_fp
    sys.stderr = log_fp
    return log_path


_log_path = _setup_logging()

# Enable crash diagnostics — prints C-level traceback on segfault
faulthandler.enable()


def _ts() -> str:
    """Timestamp for the log's lifecycle lines (startup/heartbeat/shutdown).
    Every other line in howl.log has none, by design (_setup_logging above
    is deliberately not touched) — these three are the exception, just
    enough to date "when did Howl die" without rewriting the logging setup.
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal, pyqtSlot


def _global_exception_handler(exc_type, exc_value, exc_tb):
    """Catch unhandled exceptions before they kill the process.

    This does NOT stop the interpreter from exiting — sys.excepthook only
    runs on the way out, right before Python's normal shutdown sequence
    (which still fires atexit hooks) continues. So _on_shutdown() below
    would otherwise print a "clean exit" line immediately after this one,
    which is exactly backwards. Record the real reason here so it wins.
    """
    try:
        from core.lifecycle import mark_shutdown_reason
        mark_shutdown_reason("unhandled-exception")
    except Exception:
        pass
    print("=" * 60)
    print("HOWL CRASH — unhandled exception:")
    traceback.print_exception(exc_type, exc_value, exc_tb)
    print("=" * 60)

sys.excepthook = _global_exception_handler

from ui.pill_widget import PillWidget
from ui.refine_widget import RefineWidget
from ui.preview_widget import PreviewWidget
from ui.correction_toast import CorrectionToast
from ui.tray_icon import TrayIcon

# Delay heavy module loading to speed up UI startup
# They will be imported inside _deferred_setup


class HowlApp(QObject):
    """Main application controller. Wires hotkey -> recorder -> transcriber -> clipboard."""

    # Signal to handle transcription result on the main thread.
    # Third arg: debug-WAV filename for this session (str | None) — carried
    # through the signal (not instance state) so overlapping sessions can't
    # link the wrong audio to a transcription row.
    transcription_done = pyqtSignal(str, float, object)
    transcription_error = pyqtSignal(str)
    
    # Signals for refinement
    refinement_done = pyqtSignal(str, str)
    refinement_error = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        # Fast UI initialization
        self.pill = PillWidget()
        from config import REFINE_ENABLED
        # PreviewWidget only ever opens after a refine completes, so it's
        # unreachable with refine off — one flag gates both (2026-08-25),
        # not two. Left as None (not just unconstructed-and-forgotten) so
        # every use site below can do an explicit `if self.refine_widget:`
        # instead of an AttributeError waiting to happen.
        self.refine_widget = RefineWidget() if REFINE_ENABLED else None
        self.preview_widget = PreviewWidget() if REFINE_ENABLED else None
        if self.refine_widget is not None:
            self.refine_widget.refine_requested.connect(self._on_refine_requested, Qt.ConnectionType.QueuedConnection)
        if self.preview_widget is not None:
            self.preview_widget.choice_made.connect(self._on_preview_choice, Qt.ConnectionType.QueuedConnection)
        from config import CORRECTION_TOAST_ENABLED
        self.correction_toast = CorrectionToast() if CORRECTION_TOAST_ENABLED else None
        if self.correction_toast is not None:
            self.correction_toast.correction_submitted.connect(self._on_correction, Qt.ConnectionType.QueuedConnection)

        # System tray icon
        self.tray = TrayIcon(self)
        self.tray.show()
        self.tray.toggle_recording.connect(self._on_tray_toggle, Qt.ConnectionType.QueuedConnection)
        self.pill.state_changed.connect(self.tray.update_state)

        # Placeholders for deferred components
        self.recorder = None
        self.transcriber = None
        self.db = None
        self.hotkey = None
        self.dictionary = None
        self._last_transcription = ""      # Cleaned text (for repaste / correction)
        self._last_raw_transcription = ""  # Pre-cleaner text (for raw_repaste)
        self._session_english = False      # English practice mode of the recording in progress
        self._pending_english = None       # corrections of the dictation about to be saved
        self.english_popup = None          # lazily created floating corrections panel
        self._recording_session = 0        # Guards stale session-cap timers
        # Golden rule: never paste while the user is actively recording the
        # next dictation. Finished dictations that land mid-recording queue
        # here instead, and get pasted together (see _queue_or_paste /
        # _drain_pending) the moment the current recording actually stops.
        self._pending_pastes: list[str] = []

        # Heartbeat (2026-09-05): a short timestamped line every
        # HEARTBEAT_INTERVAL_SECONDS so a dead process leaves a "last known
        # alive" mark in the log even on a night nobody dictates — otherwise
        # the log's last line is whatever the last dictation was, hours
        # before the actual death. QTimer (not a background thread) because
        # this only ever needs to run on the main thread, same reasoning as
        # every other cross-thread concern in this app.
        from config import HEARTBEAT_INTERVAL_SECONDS
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.timeout.connect(self._on_heartbeat)
        self._heartbeat_timer.start(int(HEARTBEAT_INTERVAL_SECONDS * 1000))

        # Watchdog health (2026-09-05): True once we've logged a
        # [watchdog-stale] line for the CURRENT episode, so repeated
        # heartbeat ticks while the watchdog stays dead don't spam the log
        # — see _check_watchdog_health(). Reset back to False the moment
        # the marker looks fresh again, so a later episode can warn again.
        self._watchdog_stale = False

    @pyqtSlot()
    def _on_heartbeat(self):
        print(f"[heartbeat] {_ts()} pid={os.getpid()}")
        self._check_watchdog_health()

    def _check_watchdog_health(self):
        """Piggybacks on the heartbeat timer above (no new QTimer, per the
        brief) to notice a dead watchdog. howl-watchdog.ps1 touches
        core.lifecycle's watchdog-alive marker on EVERY pass, whether or
        not it had to relaunch Howl — a monitor that only writes when it
        finds a problem is indistinguishable from a monitor that stopped
        running at all, which is exactly the failure mode this exists to
        catch (a scheduled task can report LastTaskResult 0 while the
        wscript/powershell hop underneath it never actually ran).

        None age (marker has never existed) means no watchdog is installed
        on this machine — stay silent, never warn, since Howl works fine
        standalone. Only an existing-but-aging marker crosses into a
        warning, and the [watchdog-stale] log line fires exactly once per
        stale episode (guarded by self._watchdog_stale) rather than once
        per heartbeat tick. The tray tooltip (Howl's permanent on-screen
        presence) is updated on every tick regardless, so
        it also clears itself the moment the watchdog catches up.
        """
        from core.lifecycle import watchdog_marker_age_seconds
        from config import WATCHDOG_STALE_SECONDS
        age = watchdog_marker_age_seconds()
        if age is None:
            return  # watchdog never installed — nothing to report, ever
        is_stale = age > WATCHDOG_STALE_SECONDS
        if is_stale and not self._watchdog_stale:
            self._watchdog_stale = True
            since = (datetime.now() - timedelta(seconds=age)).strftime("%Y-%m-%d %H:%M:%S")
            print(f"[watchdog-stale] {_ts()} el watchdog no pasa desde {since}")
        elif not is_stale:
            self._watchdog_stale = False
        self.tray.set_watchdog_stale(self._watchdog_stale)

    def start(self):
        # 2026-08-24: the pill is hidden at rest by default (tray icon is
        # now the permanent presence indicator) — its native HWND/styles
        # already exist from PillWidget.__init__'s _ensure_native_handle(),
        # so there's nothing left to do here for the default case; no
        # "loading" spinner flash during the ~100ms-1s deferred setup below.
        # PILL_ALWAYS_VISIBLE=true is the explicit exception: it restores
        # the old behavior 1:1, loading spinner included.
        from config import PILL_ALWAYS_VISIBLE
        if PILL_ALWAYS_VISIBLE:
            self.pill.set_state(PillWidget.STATE_PROCESSING)  # Show loading

        # Schedule heavy setup immediately after event loop starts
        QTimer.singleShot(100, self._deferred_setup)

    @pyqtSlot()
    def _deferred_setup(self):
        # Lazy load heavy modules
        from core.recorder import AudioRecorder
        from core.transcriber import Transcriber
        from core.hotkey import HotkeyListener
        from core.dictionary import Dictionary
        from db.database import TranscriptionDB
        from web.server import start_web_server

        self.db = TranscriptionDB()
        self.dictionary = Dictionary(self.db)
        self.recorder = AudioRecorder()
        self.transcriber = Transcriber()
        # Feed learned vocabulary to Whisper for better accuracy
        self.transcriber.set_whisper_prompt(self.dictionary.whisper_prompt())
        self.hotkey = HotkeyListener()

        # Connect visualizer to recorder's audio queue
        self.pill.visualizer.set_audio_queue(self.recorder.audio_queue)

        # Connect hotkey signals
        self.hotkey.pressed.connect(self._on_hotkey_pressed, Qt.ConnectionType.QueuedConnection)
        self.hotkey.released.connect(self._on_hotkey_released, Qt.ConnectionType.QueuedConnection)
        self.hotkey.repaste.connect(self._on_repaste, Qt.ConnectionType.QueuedConnection)
        self.hotkey.raw_repaste.connect(self._on_raw_repaste, Qt.ConnectionType.QueuedConnection)

        # Connect transcription signals
        self.transcription_done.connect(self._on_transcription_done, Qt.ConnectionType.QueuedConnection)
        self.transcription_error.connect(self._on_transcription_error, Qt.ConnectionType.QueuedConnection)
        self.refinement_done.connect(self._on_refinement_done, Qt.ConnectionType.QueuedConnection)
        self.refinement_error.connect(self._on_refinement_error, Qt.ConnectionType.QueuedConnection)

        # Start listening
        self.hotkey.start()

        # Pre-generate the sound-cue WAV files off the UI thread so the very
        # first play_start() (at the next recording) never has to touch disk
        # — see core/sounds.py prewarm(). Never raises; fire-and-forget.
        from core.sounds import prewarm as _prewarm_sounds
        threading.Thread(target=_prewarm_sounds, daemon=True).start()

        # Prune stale debug recordings at startup (defensive — never raises)
        try:
            from config import SAVE_RECORDINGS, RECORDINGS_RETENTION_HOURS
            if SAVE_RECORDINGS:
                from core.recordings import cleanup_old_recordings
                cleanup_old_recordings(RECORDINGS_RETENTION_HOURS)
        except Exception as e:
            print(f"[recordings] Startup cleanup skipped: {e}")

        # Start web dashboard with dictionary + transcriber access (the
        # latter powers the history's re-transcribe action).
        from web.server import set_dictionary, set_transcriber
        set_dictionary(self.dictionary)
        set_transcriber(self.transcriber)
        port = start_web_server(5000)
        print(f"Dashboard: http://localhost:{port}")

        self.pill.set_state(PillWidget.STATE_IDLE)
        print(f"[startup] {_ts()} pid={os.getpid()}")
        print("Howl running. Ctrl+Shift (hold) o Ctrl+Space (hands-free). Ctrl+C to quit.")

    @pyqtSlot()
    def _on_hotkey_pressed(self):
        # Show the pill FIRST (measured 2026-10-13: the context capture + mic
        # open below cost 85-230 ms under CPU load, all of it before the user
        # saw anything). The pill is a no-activate window, so the app snapshot
        # taken next still sees the real frontmost app. A mic failure below
        # flips it to ERROR.
        self.pill.set_state(PillWidget.STATE_RECORDING)
        self._session_english = bool(getattr(self.hotkey, "english_mode", False))
        from core.clipboard import save_frontmost_app
        from core.app_context import capture_app_context
        save_frontmost_app()
        # Fresh snapshot of the app that will receive the paste (never raises)
        capture_app_context(reset=True)
        if self.english_popup is not None:
            self.english_popup.cancel_shadowing()   # a dictation owns the mic now
        try:
            self.recorder.start()
        except Exception as e:
            # Mic unavailable: unplugged, busy in another app, or permission
            # blocked. Specific log + ERROR pill + clean hotkey state so the
            # next toggle starts fresh instead of eating a press.
            print(f"ERROR: could not start recording — mic unavailable/busy/blocked?: {e}")
            self.hotkey.abort_recording()
            self.pill.set_state(PillWidget.STATE_ERROR)
            return

        from core.sounds import play_start
        play_start()

        # Session cap: auto-stop and transcribe what was recorded so far
        # instead of failing later at the API (Groq WAV limit ≈13 min).
        from config import MAX_RECORDING_SECONDS
        self._recording_session += 1
        if MAX_RECORDING_SECONDS > 0:
            session = self._recording_session
            QTimer.singleShot(
                int(MAX_RECORDING_SECONDS * 1000),
                lambda: self._auto_stop(session),
            )

    def _auto_stop(self, session: int):
        """Session-cap timeout: stop + transcribe a too-long dictation."""
        if session != self._recording_session:
            return  # a newer recording owns the mic — stale timer
        if not self.recorder or not self.recorder.is_recording:
            return  # already stopped normally
        print("[recorder] Max recording duration reached — auto-stopping and transcribing")
        if not self.hotkey.force_stop_recording():
            # Defensive: the listener's state should already say "recording"
            # (the tray syncs it since 2026-09-05), but if anything ever left
            # them out of step, fall through and stop by hand.
            self._on_hotkey_released()

    @pyqtSlot()
    def _on_hotkey_released(self):
        # Guard against double-stop (auto-stop + manual release, tray + hotkey,
        # stale hotkey state): a stop with no active recording would re-process
        # the previous dictation's frames. No-op instead.
        if not self.recorder or not self.recorder.is_recording:
            return

        from core.sounds import play_stop
        play_stop()

        try:
            duration = self.recorder.stop()
        except Exception as e:
            # Device-level failure on stop (rare, but a stuck pill + a
            # runaway reassert timer are worse than an ERROR state). Specific
            # log + ERROR pill + clean hotkey state so the next press starts
            # fresh instead of eating one, mirroring _on_hotkey_pressed's
            # mic-failure handling above.
            print(f"ERROR: could not stop recording — device error?: {e}")
            self.hotkey.abort_recording()
            self.pill.set_state(PillWidget.STATE_ERROR)
            return
        # Refresh the app snapshot — closest to paste time. A failed/shell-y
        # capture (e.g. tray click puts the taskbar in foreground) keeps the
        # start-time snapshot instead.
        from core.app_context import capture_app_context
        capture_app_context()
        self.pill.set_state(PillWidget.STATE_PROCESSING)

        # Don't transcribe very short recordings (accidental taps)
        if duration < 1.0:
            self._settle_pill_after_no_paste(PillWidget.STATE_IDLE)
            return

        # Skip recordings that are silent / near-silent to prevent Whisper
        # from hallucinating phrases ("Gracias.", "¡Suscríbete al canal!", etc.)
        # skip_initial_seconds only applies when the start-of-recording sound
        # cue is on — that tone is picked up by the mic (measured: ambient
        # peak_rms=421, ~756 during the tone) and would otherwise let a truly
        # silent recording clear a low SILENCE_PEAK_RMS. With cues off there
        # is nothing to skip, so the gate looks at the full recording.
        from config import SILENCE_PEAK_RMS, SOUND_CUES_ENABLED, SILENCE_GATE_SKIP_INITIAL_SECONDS
        skip_seconds = SILENCE_GATE_SKIP_INITIAL_SECONDS if SOUND_CUES_ENABLED else 0.0
        peak = self.recorder.peak_rms(skip_initial_seconds=skip_seconds)
        if peak < SILENCE_PEAK_RMS:
            print(f"Skipped: silence detected (peak_rms={peak:.1f} < {SILENCE_PEAK_RMS})")
            self._settle_pill_after_no_paste(PillWidget.STATE_IDLE)
            return

        # Transcribe in background thread to avoid blocking UI. The debug WAV
        # (below) is saved from the ORIGINAL untrimmed audio; the raw numpy
        # array handed to the worker gets trimmed/chunked before transcription.
        wav_buffer = self.recorder.get_wav_buffer()
        raw_audio = self.recorder.get_audio_array()
        recording_duration = self.recorder.get_duration()

        # Optionally save a debug copy of the WAV for post-mortem diagnosis.
        # Uses .getvalue() (no seek side-effects) so wav_buffer stays intact.
        # The filename (not the full path — machine-local) travels with this
        # session (thread arg → signal) so the DB insert can link the
        # transcription to its audio for the dashboard's playback/re-transcribe
        # feature without racing a newer session.
        audio_filename = None
        try:
            from config import SAVE_RECORDINGS, RECORDINGS_RETENTION_HOURS
            if SAVE_RECORDINGS:
                from core.recordings import save_recording, cleanup_old_recordings
                wav_data = wav_buffer.getvalue()  # does NOT move the read pointer
                saved_path = save_recording(wav_data)
                if saved_path:
                    audio_filename = saved_path.name
                cleanup_old_recordings(RECORDINGS_RETENTION_HOURS)
        except Exception as e:
            print(f"[recordings] Save skipped (transcription continues): {e}")

        thread = threading.Thread(
            target=self._transcribe_worker,
            args=(raw_audio, recording_duration, audio_filename, self._session_english),
            daemon=True,
        )
        thread.start()

    def _transcribe_worker(self, raw_audio, duration, audio_filename=None, english=False):
        try:
            # Refresh dictionary + Whisper prompt before each transcription
            self.dictionary.reload()
            self.transcriber.set_whisper_prompt(self.dictionary.whisper_prompt())

            from config import SAMPLE_RATE, SILENCE_TRIM_ENABLED, CHUNK_SECONDS
            from core.audio_prep import trim_silence, split_at_pauses
            from core.recorder import to_wav_buffer

            audio = raw_audio
            orig_seconds = audio.size / SAMPLE_RATE if audio.size else 0.0
            if SILENCE_TRIM_ENABLED and audio.size:
                audio = trim_silence(audio)
            trimmed_seconds = audio.size / SAMPLE_RATE if audio.size else 0.0

            chunks = split_at_pauses(audio, CHUNK_SECONDS) if CHUNK_SECONDS > 0 else [audio]
            print(f"[audio-prep] trimmed {orig_seconds:.1f}s -> {trimmed_seconds:.1f}s, chunks={len(chunks)}")

            parts = [
                self.transcriber.transcribe(to_wav_buffer(chunk), english=english)
                for chunk in chunks
                if chunk.size
            ]
            text = " ".join(p for p in parts if p).strip()
            if text and english:
                # English practice mode: no Spanish dictionary / cleaner. Never raises.
                from core.english import correct_english
                result = correct_english(text)
                self._last_raw_transcription = text
                self._pending_english = result if result["ok"] else {"corrections": [], "upgrade": ""}
                self._pending_english["english"] = True
                self._pending_english["original"] = text
                self.transcription_done.emit(result["corrected"], duration, audio_filename)
            elif text:
                # Apply learned vocabulary corrections
                text = self.dictionary.apply(text)
                # LLM post-processing: clean muletillas, lateral speech, formatting
                from core.cleaner import clean_dictation
                from core.app_context import get_captured_context
                raw_text = text
                user_vocab = self.dictionary.user_vocabulary_words()
                cleaned, was_cleaned = clean_dictation(
                    text,
                    user_vocab=user_vocab,
                    app_context=get_captured_context(),
                )
                self._last_raw_transcription = raw_text
                text = cleaned
                # Guard: phantom-phrase filter may have reduced text to empty —
                # treat that as no speech rather than pasting an empty string.
                if not text.strip():
                    self.transcription_error.emit("No speech detected")
                    return
                self.transcription_done.emit(text, duration, audio_filename)
            else:
                self.transcription_error.emit("No speech detected")
        except Exception as e:
            # All transcription providers failed. The dictation audio is NOT
            # lost if SAVE_RECORDINGS is on — point the user at it.
            try:
                from config import SAVE_RECORDINGS
                hint = " — audio conservado en recordings/" if SAVE_RECORDINGS else ""
            except Exception:
                hint = ""
            self.transcription_error.emit(f"{e}{hint}")

    def _settle_pill_after_no_paste(self, fallback_state: str):
        """Common tail for 'this dictation ended with nothing new to paste'
        (too short, silence-gated, empty after cleaning, transcription
        failed entirely). Golden rule (point 4): if a recording is already
        under way, the pill must keep lying about nothing — leave it showing
        RECORDING, untouched. Otherwise, drain whatever text is pending from
        an EARLIER held dictation (that sets DONE/ERROR itself); only if
        there's nothing to drain do we fall back to `fallback_state`.
        """
        if self.recorder and self.recorder.is_recording:
            return
        if self._pending_pastes:
            self._drain_pending()
            return
        self.pill.set_state(fallback_state)

    def _queue_or_paste(self, new_text: str):
        """Route a finished dictation's text: queue it, then either flush
        immediately (no recording in progress right now) or hold everything
        for the next drain. Golden rule: never paste while the user is
        actively recording the next dictation — with one safety-valve
        exception, see MAX_PENDING_PASTES in config.py.
        """
        self._pending_pastes.append(new_text)

        from config import MAX_PENDING_PASTES
        if len(self._pending_pastes) >= MAX_PENDING_PASTES:
            print(f"[queue] MAX_PENDING_PASTES ({MAX_PENDING_PASTES}) reached — "
                  f"flushing {len(self._pending_pastes)} pending paste(s) now "
                  f"(safety valve; may land mid-recording)")
            self._drain_pending()
            return

        if self.recorder and self.recorder.is_recording:
            print(f"[queue] held — recording in progress ({len(self._pending_pastes)} pending)")
            return

        self._drain_pending()

    def _drain_pending(self):
        """Paste everything queued as ONE combined clipboard/paste operation
        — pending texts in arrival order, joined by a blank line — then show
        the toast/refine affordances for what actually landed. Caller is
        responsible for only invoking this when pasting is actually safe
        (checked recording state itself, or hit the MAX_PENDING_PASTES
        safety valve).
        """
        if not self._pending_pastes:
            return
        combined = "\n\n".join(self._pending_pastes)
        self._pending_pastes = []
        try:
            from core.clipboard import paste_text
            self._last_transcription = combined
            with self.hotkey.suspended():
                paste_text(combined)
            self.pill.set_state(PillWidget.STATE_DONE)
            print(f"Pasted ({len(combined)} chars): {combined[:80]}{'...' if len(combined) > 80 else ''}")
        except Exception as e:
            print(f"ERROR in paste: {e}")
            traceback.print_exc()
            self.pill.set_state(PillWidget.STATE_ERROR)
            return

        # Toast and refine — isolated so they can't kill the app. Shown for
        # the COMBINED text, since that's what's actually on screen now.
        if self.correction_toast is not None:
            try:
                self.correction_toast.show_transcription(combined)
            except Exception as e:
                print(f"Correction toast error: {e}")
        if self.refine_widget is not None:
            try:
                self.refine_widget.show_for_text(combined)
            except Exception as e:
                print(f"Refine widget error: {e}")

    @pyqtSlot(str, float, object)
    def _on_transcription_done(self, text: str, duration: float, audio_filename=None):
        # DB insert and vocab-autolearn always run, per-dictation, regardless
        # of whether pasting ends up held for a concurrent recording — only
        # the PASTE is gated by the golden rule (point 1).
        raw = self._last_raw_transcription or text
        english = self._pending_english  # set by the worker in English mode
        self._pending_english = None
        try:
            from core.app_context import get_captured_context
            ctx = get_captured_context()
            row_id = self.db.insert(
                text=text,
                duration_seconds=duration,
                raw_text=raw,
                was_cleaned=(text != raw),
                app_name=ctx.app_name if ctx else None,
                app_category=ctx.category if ctx else None,
                audio_path=audio_filename,
                language="en" if english else None,
            )
            if english:
                n = self.db.insert_english_corrections(
                    row_id, english["corrections"], english.get("upgrade", ""))
                print(f"[english] {n} correcciones")
            print(f"Transcribed ({duration:.1f}s): {text[:80]}{'...' if len(text) > 80 else ''}")
        except Exception as e:
            print(f"ERROR saving transcription: {e}")
            traceback.print_exc()

        # Passive vocabulary learning — isolated so a failure here can't
        # affect the save above or the paste/toast/refine below.
        try:
            from config import VOCAB_AUTOLEARN_ENABLED
            if VOCAB_AUTOLEARN_ENABLED and self.dictionary is not None and not english:
                learned = self.dictionary.observe(raw, text)
                if learned:
                    print(f"[vocab] learned: {', '.join(learned)}")
        except Exception as e:
            print(f"Vocab autolearn error: {e}")

        self._queue_or_paste(text)

        # English practice: show the corrections in a floating panel. This slot
        # already runs on the Qt main thread (transcription_done signal). Isolated
        # so a failure here can never affect the dictation that was just pasted.
        if english and english.get("corrections"):
            try:
                from config import ENGLISH_POPUP_ENABLED
                if ENGLISH_POPUP_ENABLED:
                    if self.english_popup is None:
                        from ui.english_popup import EnglishPopup
                        self.english_popup = EnglishPopup()
                        self.english_popup.configure_shadowing(
                            transcribe=lambda wav: self.transcriber.transcribe(wav, english=True),
                            mic_busy=lambda: bool(self.recorder and self.recorder.is_recording),
                            on_attempt=self._save_shadowing)
                    self.english_popup.show_result(
                        english["corrections"], text, english.get("upgrade", ""),
                        original=english.get("original", ""))
            except Exception as e:
                print(f"English popup error (ignored): {e}")
                traceback.print_exc()

    def _save_shadowing(self, target: str, heard: str, score: int):
        try:
            self.db.insert_english_shadowing(target, heard, score)
        except Exception as e:
            print(f"ERROR saving shadowing attempt (ignored): {e}")

    @pyqtSlot()
    def _on_tray_toggle(self):
        """Left-click on tray icon toggles hands-free recording.

        The listener never sees a key event for this, so its state has to be
        told explicitly — otherwise a tray-started recording leaves
        hotkey._recording False and the next Ctrl+Space reads as "start"
        instead of "stop". With the game mute that start is blocked and the
        recording becomes unstoppable from the keyboard. self.hotkey is None
        until _deferred_setup() builds it, hence the guard.
        """
        if self.recorder and self.recorder.is_recording:
            if self.hotkey:
                self.hotkey.sync_external_stop()
            self._on_hotkey_released()
        else:
            if self.hotkey:
                self.hotkey.sync_external_start()
            self._on_hotkey_pressed()

    @pyqtSlot()
    def _on_repaste(self):
        """Re-paste the last transcription (Ctrl+Shift+V)."""
        if self._last_transcription:
            from core.clipboard import paste_text
            with self.hotkey.suspended():
                paste_text(self._last_transcription)
            self.pill.set_state(PillWidget.STATE_DONE)
            print(f"Re-pasted: {self._last_transcription[:80]}")

    @pyqtSlot()
    def _on_raw_repaste(self):
        """Re-paste the last RAW (pre-cleaner) transcription (Ctrl+Shift+R)."""
        if self._last_raw_transcription:
            from core.clipboard import paste_text
            with self.hotkey.suspended():
                paste_text(self._last_raw_transcription)
            self.pill.set_state(PillWidget.STATE_DONE)
            print(f"Re-pasted RAW: {self._last_raw_transcription[:80]}")

    @pyqtSlot(str, str)
    def _on_correction(self, original: str, corrected: str):
        """User corrected a transcription — replace the pasted text and teach the dictionary."""
        if corrected and corrected != original:
            # Replace what was already pasted with the corrected version
            from core.clipboard import undo_and_paste_text
            with self.hotkey.suspended():
                replaced = undo_and_paste_text(corrected)
            self._last_transcription = corrected
            self.pill.set_state(PillWidget.STATE_DONE)
            if not replaced:
                print("Correction NOT applied in place (terminal target) — "
                      "corrected text is on the clipboard, paste it yourself.")
        if self.dictionary and corrected != original:
            before = set(self.dictionary._vocabulary_words)
            self.dictionary.learn(original, corrected)
            added = [w for w in self.dictionary._vocabulary_words if w not in before]
            if added:
                print(f"Vocabulary learned: {added}")

    @pyqtSlot(str)
    def _on_transcription_error(self, error: str):
        # Covers both "No speech detected" (empty after cleaning) and the
        # whole transcription chain failing. This dictation produced no text
        # to paste — but an EARLIER held dictation might still be waiting
        # (point 3: it must not be lost because this one didn't pan out).
        print(f"Transcription error: {error}")
        self._settle_pill_after_no_paste(PillWidget.STATE_ERROR)

    @pyqtSlot(str)
    def _on_refine_requested(self, text: str):
        thread = threading.Thread(
            target=self._refine_worker,
            args=(text,),
            daemon=True,
        )
        thread.start()

    def _refine_worker(self, text: str):
        from core.refiner import refine_prompt
        try:
            refined_text = refine_prompt(text)
            self.refinement_done.emit(text, refined_text)
        except Exception as e:
            self.refinement_error.emit(str(e))

    @pyqtSlot(str, str)
    def _on_refinement_done(self, original: str, refined: str):
        # Unreachable while REFINE_ENABLED=false — nothing ever emits
        # refinement_done (only _refine_worker does, only ever started from
        # _on_refine_requested, only ever wired to a live refine_widget in
        # __init__). Guarded anyway, cheap insurance against exactly the
        # AttributeError-on-None-right-after-paste this flag was meant to
        # rule out.
        if self.refine_widget is not None:
            self.refine_widget.hide()
        self.pill.set_state(PillWidget.STATE_DONE)
        if self.preview_widget is not None:
            self.preview_widget.show_preview(original, refined)

    @pyqtSlot(str)
    def _on_preview_choice(self, chosen_text: str):
        if chosen_text:
            from core.clipboard import undo_and_paste_text
            with self.hotkey.suspended():
                replaced = undo_and_paste_text(chosen_text)
            if replaced:
                print("Refined text manually accepted and applied.")
            else:
                print("Refined text accepted but NOT applied in place (terminal "
                      "target) — it is on the clipboard, paste it yourself.")
        else:
            print("Original text kept. No changes applied.")

    @pyqtSlot(str)
    def _on_refinement_error(self, error: str):
        # Same unreachable-while-disabled reasoning as _on_refinement_done()
        # above — guarded anyway.
        if self.refine_widget is not None:
            self.refine_widget.hide()
        self.pill.set_state(PillWidget.STATE_ERROR)
        print(f"Refinement error: {error}")


def _acquire_single_instance_lock() -> bool:
    """One Howl at a time — a second instance would double-register the global
    hotkeys and produce double pastes. Windows named mutex: auto-released by
    the OS on process death, so there is no stale-lock risk. The handle stays
    open for the process lifetime on purpose. Non-Windows: no lock (dev-only
    platform)."""
    if sys.platform != "win32":
        return True
    import ctypes
    ERROR_ALREADY_EXISTS = 183
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\HowlSingleInstance")
    return ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def _on_shutdown():
    """atexit hook — fires on any ORDERLY interpreter shutdown: tray "Salir"
    -> QApplication.quit() -> app.exec() returns -> sys.exit() below, a
    plain Ctrl+C in dev mode, AND an unhandled exception (sys.excepthook
    prints "HOWL CRASH" and then Python's normal shutdown sequence — which
    still runs atexit — continues). It does NOT fire on an external
    TerminateProcess (Stop-Process -Force, Task Manager, a hard crash) —
    that one is not interceptable, and a log that goes quiet after a
    heartbeat with no [process-exit] line at all is how that case is told
    apart from every case here.

    Because atexit can't tell those first three apart on its own — a crash
    is still an "orderly" shutdown as far as the interpreter is concerned —
    it never asserts "clean". The actual reason (or its absence) is decided
    by whoever called core.lifecycle.mark_shutdown_reason() on the way out:
    TrayIcon._quit() ("tray") or _global_exception_handler() above
    ("unhandled-exception"). No call means no one confirmed a clean
    stop — reported as "unknown", not "clean". See howl-watchdog.ps1
    for how this gets used.

    Defensive because atexit runs during interpreter teardown, when
    sys.stdout (our redirected log file, see _setup_logging) may already be
    in a half-torn-down state.
    """
    try:
        from core.lifecycle import get_shutdown_reason
        reason = get_shutdown_reason() or "unknown"
        print(f"[process-exit] {_ts()} reason={reason}")
        sys.stdout.flush()
    except Exception:
        pass


def main():
    if not _acquire_single_instance_lock():
        print("Howl ya está corriendo — saliendo. Usa Howl.bat para reiniciar (mata la instancia previa).")
        return

    # Fresh start (manual, Howl.bat, or Windows autostart) means the user
    # wants Howl running again — clear any "stopped on purpose" marker so
    # howl-watchdog.ps1 doesn't mistake this session for one it should leave
    # alone. Howl.bat also deletes this file directly; doing it here too is
    # what makes the Windows autostart path (which launches main.py
    # directly, bypassing Howl.bat) clean up after itself as well.
    from core.lifecycle import clear_stopped_marker
    clear_stopped_marker()

    atexit.register(_on_shutdown)

    # Hotkey must open instantly even on a loaded machine: see core/priority.py.
    try:
        from core.priority import boost_process, boost_thread, THREAD_PRIORITY_ABOVE_NORMAL
        boost_process()
        boost_thread(threading.get_native_id(), THREAD_PRIORITY_ABOVE_NORMAL)  # Qt/UI thread
    except Exception as e:
        print(f"[priority] boost skipped: {e}")

    # PyQt6 has HiDPI scaling always-on by default; AA_EnableHighDpiScaling
    # was removed from the API. Nothing to set here.
    app = QApplication(sys.argv)
    app.setApplicationName("Howl")
    app.setQuitOnLastWindowClosed(False)

    # Allow Ctrl+C to kill the app
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    howl = HowlApp()
    howl.start()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

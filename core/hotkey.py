import sys
import threading
from contextlib import contextmanager

from pynput import keyboard
from PyQt6.QtCore import QObject, pyqtSignal

import config
from core.win_topmost import fullscreen_app_active

# On Windows the auto-paste injects a synthetic Ctrl+V. Race conditions around
# suspend/resume can leave the tracked _ctrl_held flag stale (True), which would
# let a bare Space start recording. Query the OS for the *real* physical Ctrl
# state to gate the Ctrl+Space toggle, so it can never fire without Ctrl held.
if sys.platform == "win32":
    import ctypes
    _user32 = ctypes.windll.user32
else:
    _user32 = None


class HotkeyListener(QObject):
    """Global hotkey listener with two modes:

    1. Hold Ctrl+Shift: press-and-hold recording
    2. Ctrl+Space: hands-free toggle (press to start, press again to stop)
    """

    pressed = pyqtSignal()
    released = pyqtSignal()
    repaste = pyqtSignal()      # Alt+Shift+Z: paste last cleaned transcription
    raw_repaste = pyqtSignal()  # Ctrl+Shift+R: paste last RAW (pre-cleaner) transcription

    def __init__(self):
        super().__init__()
        self._ctrl_held = False
        self._shift_held = False
        self._alt_held = False
        self._recording = False
        self._hands_free = False
        self._suspended = False
        self._space_held = False  # debounce: Windows key auto-repeat re-fires Space
        # Latch for the game-mute transition log. Lives HERE and not in
        # win_topmost because that module is called from two threads and its
        # latch would double-fire; this object is only touched by pynput's.
        self._muted = False
        self._mute_failed = False  # one-shot latch for the warning below
        self._listener: keyboard.Listener | None = None
        # Ctrl+Space can be served by RegisterHotKey (core/winhotkey.py) instead
        # of the pynput hook; when it is, the pynput Space branch stands down.
        self._winhk = None
        self._rh_active = False
        # English practice chord (config.ENGLISH_MODE_HOTKEY): its own
        # RegisterHotKey instance so a clash never touches the normal chords.
        # english_mode is read by main when recording starts.
        self._winhk_en = None
        self.english_mode = False
        # Recording flags are now written from two threads (pynput hook and the
        # RegisterHotKey thread); one re-entrant lock keeps toggles atomic.
        self._lock = threading.RLock()

    def _start_registerhotkey(self):
        if sys.platform != "win32" or not config.HOTKEY_REGISTERHOTKEY:
            print("[hotkey] Ctrl+Space via pynput hook (RegisterHotKey disabled)")
            return
        try:
            from core.winhotkey import WinHotkeys, MOD_CONTROL, MOD_SHIFT, VK_SPACE
            # Exact-modifier match: Ctrl+Shift+Space is the hold->hands-free upgrade.
            hk = WinHotkeys([(MOD_CONTROL, VK_SPACE), (MOD_CONTROL | MOD_SHIFT, VK_SPACE)],
                            self._on_win_hotkey)
            if hk.start():
                self._winhk = hk
                self._rh_active = True
                print("[hotkey] Ctrl+Space registered via RegisterHotKey (high-priority thread)")
            else:
                print(f"[hotkey] WARNING: RegisterHotKey failed ({hk.error}); "
                      "falling back to pynput hook for Ctrl+Space")
        except Exception as e:
            print(f"[hotkey] WARNING: RegisterHotKey setup raised ({e!r}); "
                  "falling back to pynput hook for Ctrl+Space")

    def _start_english_hotkey(self):
        spec = getattr(config, "ENGLISH_MODE_HOTKEY", "")
        if not spec or sys.platform != "win32":
            return
        try:
            from core.winhotkey import WinHotkeys, parse_chord
            chord = parse_chord(spec)
            if chord is None:
                print(f"[hotkey] WARNING: ENGLISH_MODE_HOTKEY={spec!r} not understood; english mode disabled")
                return
            hk = WinHotkeys([chord], self._on_english_hotkey)
            if hk.start():
                self._winhk_en = hk
                print(f"[hotkey] english mode registered: {spec}")
            else:
                print(f"[hotkey] WARNING: english mode chord {spec} unavailable ({hk.error}); english mode disabled")
        except Exception as e:
            print(f"[hotkey] WARNING: english mode setup raised ({e!r}); english mode disabled")

    def _on_english_hotkey(self, _modifiers):
        """WM_HOTKEY for the English chord: same toggle as Ctrl+Space, English flag on."""
        if not self._suspended:
            self._ctrl_space(english=True)

    def _on_win_hotkey(self, _modifiers):
        """WM_HOTKEY for Ctrl(+Shift)+Space, on the RegisterHotKey thread."""
        handled = False
        if not self._suspended:
            handled = self._ctrl_space()
        if not handled:
            # RegisterHotKey swallowed the chord; nothing for us to do (muted by
            # a fullscreen app, or paste in flight) so give Space back to the
            # focused app, exactly as the non-swallowing pynput hook used to.
            self._replay_space()

    @staticmethod
    def _replay_space():
        try:
            import ctypes
            from ctypes import wintypes

            class KI(ctypes.Structure):
                _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                            ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                            ("dwExtraInfo", ctypes.c_size_t)]

            class INPUT(ctypes.Structure):
                class _U(ctypes.Union):
                    _fields_ = [("ki", KI), ("pad", ctypes.c_byte * 32)]
                _anonymous_ = ("u",)
                _fields_ = [("type", wintypes.DWORD), ("u", _U)]

            arr = (INPUT * 2)()
            for n, flags in enumerate((0, 2)):  # KEYEVENTF_KEYUP = 2
                arr[n].type = 1
                arr[n].ki.wVk = 0x20
                arr[n].ki.dwFlags = flags
            _user32.SendInput(2, arr, ctypes.sizeof(INPUT))
        except Exception as e:
            print(f"[hotkey] could not hand Space back: {e!r}")

    def start(self):
        self._start_registerhotkey()
        self._start_english_hotkey()
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.daemon = True
        self._listener.start()
        # The low-level hook callback runs on this thread; if it is slow Windows
        # drops the key event (LowLevelHooksTimeout). Give it the top slot.
        try:
            from core.priority import boost_thread
            print(f"[priority] hook thread boosted={boost_thread(self._listener.native_id)}")
        except Exception as e:
            print(f"[priority] hook thread boost skipped: {e}")

    def stop(self):
        if self._winhk_en:
            self._winhk_en.stop()
            self._winhk_en = None
        if self._winhk:
            self._winhk.stop()
            self._winhk = None
            self._rh_active = False
        if self._listener:
            self._listener.stop()
            self._listener = None
            
    def suspend(self):
        self._suspended = True

    def resume(self):
        # Reset modifier flags so a missed release-event during suspension
        # (common when the paste steal-focus briefly blocks pynput) cannot
        # leave _ctrl_held / _shift_held / _alt_held permanently True.
        self._ctrl_held = False
        self._shift_held = False
        self._alt_held = False
        self._space_held = False
        self._suspended = False

    @contextmanager
    def suspended(self):
        """Context manager that guarantees resume() even if the body raises."""
        self.suspend()
        try:
            yield
        finally:
            self.resume()

    def force_stop_recording(self) -> bool:
        """Stop an active recording from app code (session cap, tray, rescue).

        Clears the recording flags so hold/hands-free state can't go stale and
        emits `released` — exactly what a physical stop does. Returns True if
        the signal was emitted (False = this listener wasn't tracking a
        recording, e.g. it was started via the tray icon).
        """
        if not self._recording:
            return False
        self._recording = False
        self._hands_free = False
        self.released.emit()
        return True

    def abort_recording(self) -> None:
        """Clear recording flags WITHOUT emitting `released`.

        For when the recorder failed to start: there is nothing to stop, but
        leaving _recording=True would eat the next Ctrl+Space toggle.
        """
        self._recording = False
        self._hands_free = False

    def sync_external_start(self) -> None:
        """Record that a recording was started OUTSIDE this listener.

        The tray click starts recording through main.py without the listener
        ever seeing a key event, so _recording stayed False and the next
        Ctrl+Space read as "start another" instead of "stop this one" — and
        under the game mute that start is blocked, leaving the recording
        unstoppable from the keyboard. Marked hands-free because there is no
        physical chord held that a release could stop.
        """
        self._recording = True
        self._hands_free = True
        self.english_mode = False

    def sync_external_stop(self) -> None:
        """Record that a recording was stopped OUTSIDE this listener."""
        self._recording = False
        self._hands_free = False

    def _ctrl_active(self) -> bool:
        """Real Ctrl state used to gate the Ctrl+Space toggle.

        On Windows, ask the OS (GetAsyncKeyState) so a stale _ctrl_held left by
        the synthetic auto-paste Ctrl+V can't let a bare Space start recording.
        High bit (0x8000) set => key currently down. VK_CONTROL = 0x11.
        On other platforms, trust the tracked flag.
        """
        if _user32 is not None:
            return bool(_user32.GetAsyncKeyState(0x11) & 0x8000)
        return self._ctrl_held

    def _key_down(self, vk: int) -> bool:
        """OS says this key is physically down. Off Windows, trust the flags."""
        if _user32 is not None:
            return bool(_user32.GetAsyncKeyState(vk) & 0x8000)
        return True

    def _muted_by_game(self) -> bool:
        """True when a fullscreen app is focused and shortcuts must not fire.

        Logs ONE line per state transition, never per keypress: this runs on
        pynput's low-level hook callback, which Windows silently unhooks if it
        takes too long — no per-keypress I/O belongs in this path. The verdict
        itself is cached ~150ms inside fullscreen_app_active(), so mashing
        keys costs a couple of WinAPI reads per second at most.
        """
        try:
            if not config.GAME_MUTE_ENABLED:
                # Clear the latch too, or flipping the flag back on later prints
                # a transition that already happened while it was off.
                self._muted = False
                return False
            muted = fullscreen_app_active()
            if muted != self._muted:
                self._muted = muted
                print(
                    "[hotkey] muted: fullscreen app in foreground"
                    if muted
                    else "[hotkey] unmuted"
                )
            return muted
        except Exception:
            # NOTHING may escape this method. It runs on pynput's low-level
            # hook callback, and an exception there makes pynput call
            # self.stop() — every hotkey dead until fluentpill restarts. False is
            # the safe direction: shortcuts keep working. Deliberately bare:
            # even the logging inside fullscreen_app_active()'s own except
            # path can throw (full disk, dead handle).
            # The config read is inside the try on purpose: a renamed flag
            # would otherwise escape here.
            if not self._mute_failed:
                self._mute_failed = True
                try:
                    print("[hotkey] game mute disabled: detector raised")
                except Exception:
                    pass
            return False

    def _ctrl_space(self, english: bool = False) -> bool:
        """Ctrl(+Shift)+Space action. Returns False when it did nothing (muted).

        Shared by the pynput path and the RegisterHotKey path.
        """
        with self._lock:
            muted = self._muted_by_game()
            # Ctrl+Space: toggle hands-free recording — EXCEPT when a hold
            # session (Ctrl+Shift both physically down) is in progress: then
            # Space upgrades that session to hands-free in place (recorder
            # keeps running, no audio gap) instead of stopping it.
            #
            # MUTED while a game is focused (trigger #5): the upgrade turns a
            # recording that releasing Ctrl+Shift stops into one it does not,
            # and Space is a routine key in an RTS. Muting it means an in-game
            # Ctrl+Space falls through to the stop branch below and STOPS the
            # dictation — stopping is always safe.
            #
            # The _shift_held guard keeps a STUCK _recording (release dropped
            # while suspended, Shift long gone) on the stop branch below —
            # self-recovery in one press. Do not remove it.
            if (self._recording and not self._hands_free and self._shift_held
                    and not muted):
                self._hands_free = True
                print("[hotkey] hold upgraded to hands-free")
                return True
            # Always branches on _recording so a stuck _recording=True is
            # self-recovering: the next Ctrl+Space press will stop it even if
            # the previous release event was dropped while suspended.
            if self._recording:
                # STOP half: NEVER muted. Golden rule.
                self._recording = False
                self._hands_free = False
                self.released.emit()
                return True
            if muted:
                return False
            self._hands_free = True
            self._recording = True
            self.english_mode = english
            self.pressed.emit()
            return True

    def _on_press(self, key):
        if getattr(self, '_suspended', False):
            return

        is_ctrl = key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)
        is_shift = key in (keyboard.Key.shift, keyboard.Key.shift_r)
        is_alt = key in (keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr)
        is_space = key == keyboard.Key.space

        if is_ctrl:
            self._ctrl_held = True
        elif is_shift:
            self._shift_held = True
        elif is_alt:
            self._alt_held = True
        elif is_space and self._ctrl_active():
            # RegisterHotKey owns Ctrl(+Shift)+Space when active; still return so
            # the chord never falls through to the hold-mode block below.
            if self._rh_active:
                return
            # Ctrl+Alt+Space belongs to the English chord, never the normal toggle.
            if self._winhk_en is not None and self._alt_held:
                return
            # Debounce: Windows auto-repeat re-fires press events while Space
            # is held — without this, one slightly-long Ctrl+Space would
            # toggle (or upgrade-then-stop) multiple times.
            if self._space_held:
                return
            # Set BEFORE any muted return in _ctrl_space, or the debounce stops
            # working while a game is focused.
            self._space_held = True
            self._ctrl_space()
            return

        # Alt+Shift+Z: re-paste last cleaned transcription
        # Use VK code 90 (Z) because with Alt held, key.char may be None on Windows
        is_z = (hasattr(key, 'vk') and key.vk == 90) or (hasattr(key, 'char') and key.char in ('z', 'Z'))
        if is_z and self._alt_held and self._shift_held:
            if self._muted_by_game():
                return
            self.repaste.emit()
            return

        # Ctrl+Shift+R: re-paste last RAW (pre-cleaner) transcription
        # Must check BEFORE hold-mode block so R with Ctrl+Shift doesn't start recording.
        # Use VK 82 because with modifiers held, key.char may be None on Windows.
        is_r = (hasattr(key, 'vk') and key.vk == 82) or (hasattr(key, 'char') and key.char in ('r', 'R'))
        if is_r and self._ctrl_held and self._shift_held:
            if self._muted_by_game():
                return
            self.raw_repaste.emit()
            return

        # Hold mode: Ctrl+Shift together. Gate on the REAL key state too: a
        # dropped Shift release left _shift_held stale and bare Ctrl started
        # recording (2026-10-15). Inside the LL hook the key being pressed is
        # not yet down for GetAsyncKeyState, so trust the event for that one.
        if (self._ctrl_held and self._shift_held and not self._recording
                and (is_ctrl or self._key_down(0x11))
                and (is_shift or self._key_down(0x10))):
            if self._muted_by_game():
                return
            with self._lock:
                if self._recording:
                    return
                self._recording = True
                self._hands_free = False
                self.english_mode = False
                self.pressed.emit()

    def _on_release(self, key):
        if self._suspended:
            return

        is_ctrl = key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)
        is_shift = key in (keyboard.Key.shift, keyboard.Key.shift_r)
        is_alt = key in (keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr)

        if is_ctrl:
            self._ctrl_held = False
        elif is_shift:
            self._shift_held = False
        elif is_alt:
            self._alt_held = False
        elif key == keyboard.Key.space:
            self._space_held = False

        # Hold mode: stop when any key released (but not in hands-free mode)
        with self._lock:
            if self._recording and not self._hands_free:
                if not (self._ctrl_held and self._shift_held):
                    self._recording = False
                    self.released.emit()

"""System hotkeys via Win32 RegisterHotKey, on a dedicated high-priority thread.

Why: pynput's low-level keyboard hook sits in the input path of EVERY key and
Windows delivers its callback only when that hook thread gets CPU. RegisterHotKey
posts a single WM_HOTKEY to our own thread's queue, so a busy machine only has
to schedule one thread (raised to THREAD_PRIORITY_HIGHEST) to open the pill.

Contract: start() returns True only if EVERY requested chord registered. On any
failure (chord owned by another app) nothing stays registered and the caller
falls back to the pynput hook. Windows only; never raises.
"""

import sys
import threading

_WIN = sys.platform == "win32"

MOD_ALT, MOD_CONTROL, MOD_SHIFT = 0x1, 0x2, 0x4
MOD_NOREPEAT = 0x4000  # one WM_HOTKEY per physical press, no auto-repeat
VK_SPACE = 0x20
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
THREAD_PRIORITY_HIGHEST = 2


_MOD_NAMES = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT,
              "shift": MOD_SHIFT, "win": 0x8}


def parse_chord(spec: str):
    """"ctrl+alt+space" -> (modifiers, vk), or None if empty/unsupported.

    Only chords ending in Space are supported (all Howl chords are Space-based).
    """
    parts = [p.strip() for p in (spec or "").lower().split("+") if p.strip()]
    if len(parts) < 2 or parts[-1] != "space":
        return None
    mods = 0
    for name in parts[:-1]:
        if name not in _MOD_NAMES:
            return None
        mods |= _MOD_NAMES[name]
    return (mods, VK_SPACE) if mods else None


class WinHotkeys:
    """Registers (modifiers, vk) chords; calls on_hotkey(modifiers) on the hook thread."""

    def __init__(self, chords, on_hotkey):
        self._chords = list(chords)  # [(modifiers, vk), ...]
        self._on_hotkey = on_hotkey
        self._thread = None
        self._tid = 0
        self._ready = threading.Event()
        self._ok = False
        self.error = ""

    def start(self, timeout: float = 3.0) -> bool:
        if not _WIN:
            self.error = "not windows"
            return False
        self._thread = threading.Thread(target=self._run, name="howl-hotkey", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.error = "register timed out"
            return False
        return self._ok

    def stop(self):
        if self._tid and _WIN:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._tid = 0

    def _run(self):
        import ctypes
        from ctypes import wintypes
        user32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        k32.GetCurrentThread.restype = wintypes.HANDLE
        k32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        registered = []
        try:
            self._tid = k32.GetCurrentThreadId()
            k32.SetThreadPriority(k32.GetCurrentThread(), THREAD_PRIORITY_HIGHEST)
            for i, (mods, vk) in enumerate(self._chords, start=1):
                if not user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
                    self.error = f"RegisterHotKey(mods={mods:#x}, vk={vk:#x}) failed, winerror={ctypes.get_last_error() or k32.GetLastError()}"
                    return
                registered.append(i)
            self._ok = True
        except Exception as e:
            self.error = f"register raised: {e}"
            return
        finally:
            if not self._ok:
                for i in registered:
                    user32.UnregisterHotKey(None, i)
                self._tid = 0
            self._ready.set()
        try:
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self._on_hotkey(self._chords[msg.wParam - 1][0])
                    except Exception as e:  # a handler bug must not kill the loop
                        print(f"[hotkey] handler raised: {e!r}")
        finally:
            for i in registered:
                user32.UnregisterHotKey(None, i)

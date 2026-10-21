"""Windows-only WinAPI helpers to keep a window topmost without stealing
focus, and to detect a fullscreen/game app in the foreground so the pill
can back off instead of stealing the screen from it.

All public functions are fully defensive — they never raise. On any WinAPI
failure they return a neutral value (False / None) and log via print(),
matching the style of core/app_context.py. On macOS/Linux every function is
a no-op that returns the same neutral value without touching ctypes.windll
(which does not exist there) — safe to import and call from any platform.
"""

import sys
import time

_IS_WINDOWS = sys.platform == "win32"

# ---------------------------------------------------------------------------
# WinAPI constants
# ---------------------------------------------------------------------------

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080

GWL_STYLE = -16
WS_CAPTION = 0x00C00000
SW_SHOWMAXIMIZED = 3

# QUERY_USER_NOTIFICATION_STATE values (SHQueryUserNotificationState) that
# are STRONG, self-sufficient evidence of a fullscreen-exclusive app: 3
# QUNS_RUNNING_D3D_FULL_SCREEN, 4 QUNS_PRESENTATION_MODE. Deliberately does
# NOT include 2 QUNS_BUSY ("full-screen app running" per docs, but measured
# on this machine firing with GetForegroundWindow()==NULL — not tied to any
# window we can verify, so it only counts alongside a positive geometric
# check, never alone) nor 7 QUNS_APP (only means *some* Store app is
# running somewhere, not that it is foreground or fullscreen — treating it
# as a signal was a silent false positive that could disable this whole
# feature just by having any Store app open in the background). 1
# NOT_PRESENT / 5 ACCEPTS_NOTIFICATIONS / 6 QUIET_TIME are normal desktop
# use. Every state outside this strong set — including 2 and 7 — falls
# through to the geometric check in fullscreen_app_active().
_QUNS_STRONG_STATES = frozenset({3, 4})

# Window classes that can legitimately cover an entire monitor without being
# a "fullscreen app" we should avoid — desktop, taskbar, other shell chrome.
_NON_FULLSCREEN_CLASSES = frozenset({
    "progman",
    "workerw",
    "shell_traywnd",
    "windows.ui.core.corewindow",
})

# Lowered from 0.5 on 2026-09-05: core/hotkey.py consults this whenever a
# shortcut chord is actually in play (Ctrl+Shift held, as in an RTS), so the
# TTL is the width of both wrong-verdict windows at a focus change —
# entering the game (a chord can still slip through) and leaving it
# (the first chord gets eaten). The pill is unaffected: its timer runs at
# 1000ms, so it already recomputed on every tick. Do not go below ~50ms:
# that is pointless recompute churn while a game key is mashed.
_FULLSCREEN_CACHE_SECONDS = 0.15
_fullscreen_cache_time = 0.0
_fullscreen_cache_value = False


# ---------------------------------------------------------------------------
# ensure_topmost — force HWND_TOPMOST without activating the window
# ---------------------------------------------------------------------------

def _set_z_order(hwnd: int, insert_after: int) -> bool:
    """Shared SetWindowPos call for both the TOPMOST and NOTOPMOST cases.

    Never raises. Returns True on apparent success, False on any failure or
    on a non-Windows platform.
    """
    if not _IS_WINDOWS or not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        set_window_pos = user32.SetWindowPos
        # Declaring argtypes/restype matters here: without them a 64-bit
        # HWND can get silently truncated to 32 bits by ctypes' default
        # int-guessing marshaling.
        set_window_pos.argtypes = [
            wintypes.HWND, wintypes.HWND,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.UINT,
        ]
        set_window_pos.restype = wintypes.BOOL

        ok = set_window_pos(
            wintypes.HWND(hwnd), wintypes.HWND(insert_after),
            0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
        return bool(ok)
    except Exception as e:
        print(f"[win-topmost] _set_z_order({insert_after}) failed: {e}")
        return False


def ensure_topmost(hwnd: int) -> bool:
    """Force HWND_TOPMOST on ``hwnd`` via SetWindowPos. Never raises.

    Returns True on apparent success, False on any failure or on a
    non-Windows platform (no-op there).
    """
    return _set_z_order(hwnd, HWND_TOPMOST)


def demote_topmost(hwnd: int) -> bool:
    """Move ``hwnd`` to HWND_NOTOPMOST via SetWindowPos. Never raises.

    Needed for the fullscreen-game exception: once a window has ever been
    made TOPMOST, its WS_EX_TOPMOST flag stays set and Windows keeps
    painting it above a plain (non-topmost) fullscreen game/app — just not
    reasserting topmost is NOT enough to stop that. This explicitly drops
    the flag so the pill stops covering the game; ensure_topmost() puts it
    back once the fullscreen app is gone.

    Returns True on apparent success, False on any failure or on a
    non-Windows platform (no-op there).
    """
    return _set_z_order(hwnd, HWND_NOTOPMOST)


# ---------------------------------------------------------------------------
# apply_noactivate_style — WS_EX_NOACTIVATE + WS_EX_TOOLWINDOW
# ---------------------------------------------------------------------------

def apply_noactivate_style(hwnd: int) -> bool:
    """Add WS_EX_NOACTIVATE + WS_EX_TOOLWINDOW to ``hwnd``'s ex-style.

    Never raises. Effect: the window never steals focus and is hidden from
    Alt+Tab. Returns True on apparent success, False otherwise (no-op on
    non-Windows).

    GetWindowLongPtrW/SetWindowLongPtrW return 0 on BOTH legitimate success
    (previous value/style genuinely 0) and failure — the only way to tell
    them apart is GetLastError() right after the call. Skipping that check
    would risk reading a failed GetWindowLongPtrW as "current ex-style is
    0" and silently wiping out ex-styles the window already had, and would
    report success even if the write never happened. use_last_error=True on
    the DLL handle plus set_last_error(0) before each call makes
    ctypes.get_last_error() reflect that specific call, not some unrelated
    Win32 call made in between by the runtime.
    """
    if not _IS_WINDOWS or not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        LONG_PTR = ctypes.c_ssize_t  # matches pointer width on this build

        # 64-bit Python: GetWindowLongPtrW/SetWindowLongPtrW. Fall back to
        # the 32-bit-only GetWindowLongW/SetWindowLongW if the Ptr variants
        # are missing (older/32-bit builds).
        has_ptr_variant = hasattr(user32, "GetWindowLongPtrW") and hasattr(
            user32, "SetWindowLongPtrW"
        )
        get_fn = user32.GetWindowLongPtrW if has_ptr_variant else user32.GetWindowLongW
        set_fn = user32.SetWindowLongPtrW if has_ptr_variant else user32.SetWindowLongW

        get_fn.argtypes = [wintypes.HWND, ctypes.c_int]
        get_fn.restype = LONG_PTR
        set_fn.argtypes = [wintypes.HWND, ctypes.c_int, LONG_PTR]
        set_fn.restype = LONG_PTR

        hwnd_t = wintypes.HWND(hwnd)

        ctypes.set_last_error(0)
        current_style = get_fn(hwnd_t, GWL_EXSTYLE)
        if current_style == 0 and ctypes.get_last_error() != 0:
            print(
                f"[win-topmost] apply_noactivate_style: GetWindowLong*W "
                f"failed, GetLastError={ctypes.get_last_error()}"
            )
            return False

        new_style = current_style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        if new_style != current_style:
            ctypes.set_last_error(0)
            result = set_fn(hwnd_t, GWL_EXSTYLE, LONG_PTR(new_style))
            if result == 0 and ctypes.get_last_error() != 0:
                print(
                    f"[win-topmost] apply_noactivate_style: SetWindowLong*W "
                    f"failed, GetLastError={ctypes.get_last_error()}"
                )
                return False
        return True
    except Exception as e:
        print(f"[win-topmost] apply_noactivate_style failed: {e}")
        return False


# ---------------------------------------------------------------------------
# fullscreen_app_active — the "except games" detector
# ---------------------------------------------------------------------------

def _query_user_notification_state() -> "int | None":
    """Raw QUERY_USER_NOTIFICATION_STATE value, or None if the call failed.

    Returning the raw int (instead of a pre-judged bool) lets the caller
    treat states differently: some are strong evidence on their own, some
    need corroboration, some are meaningless. See _QUNS_STRONG_STATES.
    """
    import ctypes

    shell32 = ctypes.windll.shell32
    fn = shell32.SHQueryUserNotificationState
    fn.argtypes = [ctypes.POINTER(ctypes.c_int)]
    fn.restype = ctypes.c_long  # HRESULT — S_OK == 0

    state = ctypes.c_int(0)
    hr = fn(ctypes.byref(state))
    if hr != 0:
        return None
    return state.value


def _foreground_covers_its_monitor(exclude_hwnd: "int | None") -> bool:
    """True when the foreground window's rect fully covers its monitor.

    Catches borderless-fullscreen apps/games/video that don't set the D3D
    full-screen flag SHQueryUserNotificationState looks for.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    get_fg = user32.GetForegroundWindow
    get_fg.restype = wintypes.HWND
    hwnd = get_fg()  # restype HWND (c_void_p) auto-converts to a plain int (or None)
    if not hwnd:
        return False
    if exclude_hwnd and int(hwnd) == int(exclude_hwnd):
        return False

    get_class_name = user32.GetClassNameW
    get_class_name.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    get_class_name.restype = ctypes.c_int
    buf = ctypes.create_unicode_buffer(256)
    get_class_name(hwnd, buf, 256)
    class_name = (buf.value or "").strip().lower()
    if not class_name or class_name in _NON_FULLSCREEN_CLASSES:
        return False

    # Second decisive signal, checked first because it is the cheaper one: a
    # merely MAXIMIZED window reports SW_SHOWMAXIMIZED, a fullscreen game does
    # not. This covers apps that draw their own title bar (Electron shells,
    # Spotify, Discord, a frameless IDE) and therefore lack WS_CAPTION while
    # being nothing but maximized. Without it, any of those in the foreground
    # would silently switch the pill's always-on-top back off — the exact bug
    # this module exists to fix. Measured on this machine: orca.exe maximized
    # reports showCmd=SW_SHOWMAXIMIZED and keeps WS_CAPTION; textinputhost.exe
    # covers its monitor with no caption at all (excluded by class above).
    class _WINDOWPLACEMENT(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.UINT),
            ("flags", wintypes.UINT),
            ("showCmd", wintypes.UINT),
            ("ptMinPosition", wintypes.POINT),
            ("ptMaxPosition", wintypes.POINT),
            ("rcNormalPosition", wintypes.RECT),
            # Present in the current Windows SDK's WINDOWPLACEMENT (60 bytes
            # total). Windows 11 accepted the 44-byte version without this
            # field too (verified), but there's no reason to rely on that
            # leniency when the correct size is free to declare.
            ("rcDevice", wintypes.RECT),
        ]

    get_placement = user32.GetWindowPlacement
    get_placement.argtypes = [wintypes.HWND, ctypes.POINTER(_WINDOWPLACEMENT)]
    get_placement.restype = wintypes.BOOL
    placement = _WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(_WINDOWPLACEMENT)
    if not get_placement(hwnd, ctypes.byref(placement)):
        # Can't verify placement — the safe direction on any doubt here is
        # "not fullscreen" (worst case the pill stays visible over a window
        # it didn't need to; the dangerous direction would be disappearing
        # behind a window that isn't actually fullscreen).
        return False
    if placement.showCmd == SW_SHOWMAXIMIZED:
        return False

    # Decisive signal against false positives: a genuinely fullscreen game or
    # a browser's real F11 video fullscreen strips the title bar (WS_CAPTION)
    # entirely. A normal window that merely happens to be MAXIMIZED (e.g. a
    # maximized Chrome/IDE window) keeps WS_CAPTION even though its rect can
    # overshoot the monitor bounds by DWM's invisible ~7px resize border —
    # without this check that overshoot alone was enough to misfire on any
    # maximized app.
    #
    # GetWindowLongW can legitimately return 0, but that's indistinguishable
    # from a failure without GetLastError() — and unlike apply_noactivate_style
    # (where a false "0" just means "add the bits" and is harmless), a false
    # "0 style" reading HERE misreads as "no WS_CAPTION", pushing straight
    # towards the dangerous false-positive fullscreen verdict. Any doubt
    # returns False.
    user32_le = ctypes.WinDLL("user32", use_last_error=True)
    get_style = user32_le.GetWindowLongW
    get_style.argtypes = [wintypes.HWND, ctypes.c_int]
    get_style.restype = ctypes.c_long
    ctypes.set_last_error(0)
    style = get_style(hwnd, GWL_STYLE)
    if style == 0 and ctypes.get_last_error() != 0:
        return False
    if style & WS_CAPTION:
        return False

    get_window_rect = user32.GetWindowRect
    get_window_rect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    get_window_rect.restype = wintypes.BOOL
    win_rect = wintypes.RECT()
    if not get_window_rect(hwnd, ctypes.byref(win_rect)):
        return False

    MONITOR_DEFAULTTONEAREST = 2
    monitor_from_window = user32.MonitorFromWindow
    monitor_from_window.argtypes = [wintypes.HWND, wintypes.DWORD]
    monitor_from_window.restype = wintypes.HMONITOR
    hmonitor = monitor_from_window(hwnd, MONITOR_DEFAULTTONEAREST)
    if not hmonitor:
        return False

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    get_monitor_info = user32.GetMonitorInfoW
    get_monitor_info.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
    get_monitor_info.restype = wintypes.BOOL
    if not get_monitor_info(hmonitor, ctypes.byref(mi)):
        return False

    mon = mi.rcMonitor
    tol = 2  # px tolerance
    return (
        win_rect.left <= mon.left + tol
        and win_rect.top <= mon.top + tol
        and win_rect.right >= mon.right - tol
        and win_rect.bottom >= mon.bottom - tol
    )


def fullscreen_app_active(exclude_hwnd: "int | None" = None) -> bool:
    """True when a game/fullscreen-exclusive app is in the foreground.

    QUERY_USER_NOTIFICATION_STATE values in _QUNS_STRONG_STATES (D3D
    exclusive full-screen, presentation mode) are trusted on their own.
    Every other state — including the ambiguous QUNS_BUSY and the
    meaningless QUNS_APP, and a failed query — falls through to the
    monitor-covering foreground-window check (catches borderless-fullscreen
    games/video that don't set the D3D flag, and independently confirms the
    ambiguous states instead of trusting them blindly). Desktop/taskbar/shell
    surfaces are excluded by window class, and ``exclude_hwnd`` (e.g. the
    pill's own HWND) is never counted as fullscreen.

    Cached ~150ms so a key-by-key caller costs a couple of WinAPI reads per
    second at most. The pill's timer runs at 1000ms and therefore recomputes
    on every tick regardless.
    Never raises; returns False on any failure, doubt, or on non-Windows.

    The cache is shared across exclusion domains: the pill passes its own
    HWND as exclude_hwnd and core/hotkey.py passes none, but a cached value
    is valid for both. Even if the pill somehow became the foreground window,
    it is 52px tall and can never satisfy the "covers its monitor within 2px"
    test, so the verdict is identical with or without the exclusion. Revisit
    this if fluentpill ever gives a LARGE window's HWND to this function.
    """
    global _fullscreen_cache_time, _fullscreen_cache_value

    if not _IS_WINDOWS:
        return False

    now = time.monotonic()
    if now - _fullscreen_cache_time < _FULLSCREEN_CACHE_SECONDS:
        return _fullscreen_cache_value

    result = False
    try:
        state = _query_user_notification_state()
        if state in _QUNS_STRONG_STATES:
            result = True
        else:
            result = _foreground_covers_its_monitor(exclude_hwnd)
    except Exception as e:
        print(f"[win-topmost] fullscreen_app_active failed: {e}")
        result = False

    _fullscreen_cache_time = now
    _fullscreen_cache_value = result
    return result

"""Keep the hotkey path responsive under CPU load (Windows only, never raises).

Why: fluentpill is launched by a scheduled task whose default priority (7) starts it as
BelowNormal. With the machine busy, the keyboard-hook thread and the Qt thread
wait behind everything else, so Ctrl+Space opens late or not at all. Also, an idle
process gets its working set trimmed, and the first press pays the page-in.

What: AboveNormal (never Realtime), no EcoQoS / timer throttling, a hard minimum
working set, and a helper to raise a single thread (the pynput hook thread).
"""

import sys

_WIN = sys.platform == "win32"

ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
THREAD_PRIORITY_ABOVE_NORMAL = 1
THREAD_PRIORITY_HIGHEST = 2
THREAD_SET_INFORMATION = 0x0020
PROCESS_POWER_THROTTLING = 4          # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
THROTTLE_EXECUTION_SPEED = 0x1        # EcoQoS
THROTTLE_IGNORE_TIMER_RESOLUTION = 0x4
QUOTA_LIMITS_HARDWS_MIN_ENABLE = 0x1
WORKING_SET_MIN_BYTES = 96 * 1024 * 1024
WORKING_SET_MAX_BYTES = 2048 * 1024 * 1024


def boost_process() -> None:
    """AboveNormal + opt out of EcoQoS + hard-minimum working set."""
    if not _WIN:
        return
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    h = k32.GetCurrentProcess()

    k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    ok_prio = bool(k32.SetPriorityClass(h, ABOVE_NORMAL_PRIORITY_CLASS))

    class POWER_THROTTLING(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                    ("StateMask", wintypes.ULONG)]
    # StateMask 0 with the bit in ControlMask = "throttling explicitly OFF".
    st = POWER_THROTTLING(1, THROTTLE_EXECUTION_SPEED | THROTTLE_IGNORE_TIMER_RESOLUTION, 0)
    k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    ok_eco = bool(k32.SetProcessInformation(h, PROCESS_POWER_THROTTLING, ctypes.byref(st), ctypes.sizeof(st)))

    k32.SetProcessWorkingSetSizeEx.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
    ok_ws = bool(k32.SetProcessWorkingSetSizeEx(
        h, WORKING_SET_MIN_BYTES, WORKING_SET_MAX_BYTES, QUOTA_LIMITS_HARDWS_MIN_ENABLE))

    print(f"[priority] AboveNormal={ok_prio} ecoqos_off={ok_eco} working_set_min_96MB={ok_ws}")


def boost_thread(native_id: int, level: int = THREAD_PRIORITY_HIGHEST) -> bool:
    """Raise one thread (by native id) relative to its process class. Never raises."""
    if not _WIN or not native_id:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.OpenThread.restype = wintypes.HANDLE
        k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        th = k32.OpenThread(THREAD_SET_INFORMATION, False, native_id)
        if not th:
            return False
        try:
            return bool(k32.SetThreadPriority(th, level))
        finally:
            k32.CloseHandle(th)
    except Exception as e:
        print(f"[priority] boost_thread failed: {e}")
        return False

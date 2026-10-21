"""Foreground-app context for tone/format-aware cleaning (Typeless-style).

The app that will receive the paste is captured when recording starts/stops
and injected as an ``<APP_CONTEXT>`` metadata block into the cleaner's *user*
message — never the system prompt, whose cached prefix must stay
byte-identical. The LLM only adjusts register/format per category (email vs
chat vs code...), never content.

Privacy: by default only the app name and derived category are sent to the
cleaner LLM. The window title is used LOCALLY to categorize browser tabs
(Gmail vs WhatsApp Web...) and only travels to the LLM when
APP_CONTEXT_SEND_TITLE=true. This deliberately avoids the Typeless failure
mode of shipping URLs/window titles off-device by default.

All functions are fully defensive — they never raise. On any failure the
capture is simply None and the cleaner behaves exactly as before.
"""

import sys
from dataclasses import dataclass

from core.branding import is_own_app_name


@dataclass(frozen=True)
class AppContext:
    app_name: str    # Friendly name shown to the LLM ("WhatsApp", "Cursor")
    category: str    # email | chat | code | ai | notes | docs | general
    title: str = ""  # Window title — local categorization; sent only if opted in
    is_terminal: bool = False  # Local flag — never sent to the LLM (see _TERMINAL_PROCESSES)


# exe name (lowercase, no path) → (friendly name, category)
_PROCESS_CATEGORIES: dict[str, tuple[str, str]] = {
    # Email
    "outlook.exe": ("Outlook", "email"),
    "olk.exe": ("Outlook", "email"),
    "hxmail.exe": ("Correo de Windows", "email"),
    "thunderbird.exe": ("Thunderbird", "email"),
    "mailbird.exe": ("Mailbird", "email"),
    # Chat
    "slack.exe": ("Slack", "chat"),
    "discord.exe": ("Discord", "chat"),
    "teams.exe": ("Microsoft Teams", "chat"),
    "ms-teams.exe": ("Microsoft Teams", "chat"),
    "whatsapp.exe": ("WhatsApp", "chat"),
    "telegram.exe": ("Telegram", "chat"),
    "signal.exe": ("Signal", "chat"),
    # Code editors / IDEs / terminals
    "code.exe": ("Visual Studio Code", "code"),
    "cursor.exe": ("Cursor", "code"),
    "windsurf.exe": ("Windsurf", "code"),
    "antigravity.exe": ("Antigravity", "code"),
    "devenv.exe": ("Visual Studio", "code"),
    "idea64.exe": ("IntelliJ IDEA", "code"),
    "pycharm64.exe": ("PyCharm", "code"),
    "webstorm64.exe": ("WebStorm", "code"),
    "sublime_text.exe": ("Sublime Text", "code"),
    "notepad++.exe": ("Notepad++", "code"),
    "zed.exe": ("Zed", "code"),
    "windowsterminal.exe": ("Windows Terminal", "code"),
    "wt.exe": ("Windows Terminal", "code"),
    "powershell.exe": ("PowerShell", "code"),
    "pwsh.exe": ("PowerShell", "code"),
    "cmd.exe": ("Consola", "code"),
    "conhost.exe": ("Consola", "code"),
    "openconsole.exe": ("Consola", "code"),
    "alacritty.exe": ("Alacritty", "code"),
    "wezterm-gui.exe": ("WezTerm", "code"),
    "mintty.exe": ("Git Bash", "code"),
    "orca.exe": ("Orca", "code"),
    # AI assistants (desktop apps)
    "claude.exe": ("Claude", "ai"),
    "chatgpt.exe": ("ChatGPT", "ai"),
    # Notes
    "obsidian.exe": ("Obsidian", "notes"),
    "notion.exe": ("Notion", "notes"),
    "onenote.exe": ("OneNote", "notes"),
    "logseq.exe": ("Logseq", "notes"),
    # Docs
    "winword.exe": ("Word", "docs"),
    "excel.exe": ("Excel", "docs"),
    "powerpnt.exe": ("PowerPoint", "docs"),
}

_BROWSER_PROCESSES: frozenset = frozenset({
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
    "opera.exe", "opera_gx.exe", "vivaldi.exe", "arc.exe", "zen.exe",
})

# Browser tab detection: substring (lowercase) in window title → (friendly, category).
# First match wins — keep the more specific needles on top.
_BROWSER_TITLE_HINTS: list[tuple[str, str, str]] = [
    ("gmail", "Gmail", "email"),
    ("outlook", "Outlook Web", "email"),
    ("whatsapp", "WhatsApp Web", "chat"),
    ("telegram", "Telegram Web", "chat"),
    ("slack", "Slack", "chat"),
    ("discord", "Discord", "chat"),
    ("messenger", "Messenger", "chat"),
    ("chatgpt", "ChatGPT", "ai"),
    ("claude", "Claude", "ai"),
    ("gemini", "Gemini", "ai"),
    ("copilot", "Copilot", "ai"),
    ("perplexity", "Perplexity", "ai"),
    ("ai studio", "AI Studio", "ai"),
    ("google docs", "Google Docs", "docs"),
    ("documentos de google", "Google Docs", "docs"),
    ("google sheets", "Google Sheets", "docs"),
    ("notion", "Notion", "notes"),
    ("github", "GitHub", "code"),
    ("jira", "Jira", "code"),
]

# Terminals and consoles. Ctrl+Z there is SIGTSTP — it suspends whatever runs in
# the foreground (Claude Code, vim, a REPL) instead of undoing the paste — so the
# correction/refine flows must never try to replace text with Ctrl+Z + paste.
# Deliberately kept OUT of the LLM-visible `category`: these stay in "code" so the
# cleaner's <APP_CONTEXT> block keeps using the categories the cached system
# prompt already documents (a new category value would be unknown to it).
# Known gap: an integrated terminal inside an editor (VS Code, Cursor) reports the
# editor's exe, so it can't be told apart from the editor pane and won't match here.
_TERMINAL_PROCESSES: frozenset = frozenset({
    "windowsterminal.exe", "wt.exe", "openconsole.exe", "conhost.exe",
    "powershell.exe", "pwsh.exe", "cmd.exe",
    "alacritty.exe", "wezterm-gui.exe", "kitty.exe", "mintty.exe",
    "hyper.exe", "tabby.exe", "warp.exe", "putty.exe",
    "wsl.exe", "wslhost.exe", "ubuntu.exe", "bash.exe",
    # Orca is an Electron IDE whose main surface is agent terminals. Treating a
    # mixed-surface app as a terminal is the safe error: the worst case is the
    # user pasting the correction by hand, vs. a suspended agent session.
    "orca.exe",
    # macOS — _mac_foreground() appends ".exe" to the app name to reuse this map.
    "terminal.exe", "iterm2.exe", "ghostty.exe",
})

# Shell/system surfaces that don't tell us where the paste will land (e.g. the
# user stopped recording by clicking the tray icon → foreground is the shell).
# A capture that hits one of these keeps the previous snapshot instead.
_IGNORE_PROCESSES: frozenset = frozenset({
    "explorer.exe",
    "shellexperiencehost.exe",
    "searchhost.exe",
    "startmenuexperiencehost.exe",
    "",
})


def _categorize(process: str, title: str) -> AppContext:
    proc = process.lower()
    terminal = proc in _TERMINAL_PROCESSES
    known = _PROCESS_CATEGORIES.get(proc)
    if known:
        return AppContext(known[0], known[1], title, is_terminal=terminal)
    if proc in _BROWSER_PROCESSES:
        low = title.lower()
        for needle, friendly, category in _BROWSER_TITLE_HINTS:
            if needle in low:
                return AppContext(friendly, category, title)
    stem = proc[:-4] if proc.endswith(".exe") else proc
    return AppContext(
        stem.capitalize() if stem else "desconocida", "general", title,
        is_terminal=terminal,
    )


def _win_foreground() -> "tuple[str, str] | None":
    """Return (exe_name, window_title) of the foreground window on Windows."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None

    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value or ""

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        path_buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, path_buf, ctypes.byref(size)):
            return None
        exe = path_buf.value.replace("/", "\\").rsplit("\\", 1)[-1]
        return exe, title
    finally:
        kernel32.CloseHandle(handle)


def _mac_foreground() -> "tuple[str, str] | None":
    """Best-effort frontmost app name on macOS (no window title)."""
    import subprocess
    result = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to get name of first process whose frontmost is true'],
        capture_output=True, text=True, timeout=2,
    )
    name = result.stdout.strip()
    if not name or is_own_app_name(name):
        return None
    # Reuse the Windows map by trying "<name>.exe" — covers Slack, Discord, etc.
    return f"{name}.exe", ""


# Snapshot written on the main thread (hotkey press/release), read later by
# the transcription worker thread. One dictation at a time → no locking needed.
_captured: "AppContext | None" = None


def capture_app_context(reset: bool = False) -> None:
    """Snapshot the foreground app. NEVER raises.

    Called with reset=True at record start (fresh dictation) and reset=False
    at record stop (refresh: closest to paste time, but a failed or shell-y
    capture keeps the start-time snapshot).
    """
    global _captured
    try:
        if reset:
            _captured = None
        from config import APP_CONTEXT_ENABLED
        if not APP_CONTEXT_ENABLED:
            _captured = None
            return
        if sys.platform == "win32":
            info = _win_foreground()
        elif sys.platform == "darwin":
            info = _mac_foreground()
        else:
            info = None
        if not info:
            return
        exe, title = info
        if exe.lower() in _IGNORE_PROCESSES:
            return
        _captured = _categorize(exe, title)
        print(f"[app-context] app={_captured.app_name} category={_captured.category}")
    except Exception as e:
        print(f"[app-context] capture failed: {e}")


def get_captured_context() -> "AppContext | None":
    """Return the last captured foreground-app context (or None)."""
    return _captured


def target_is_terminal() -> bool:
    """True when this dictation was pasted into a terminal/console. NEVER raises.

    Prefers the snapshot taken at record stop (that IS the app that received the
    paste; the foreground may since have moved to Howl's own toast). Falls back
    to a live foreground probe so this stays correct with APP_CONTEXT_ENABLED
    off — that flag governs what is sent to the cleaner LLM, and must not
    silently re-enable a Ctrl+Z that suspends the user's shell session. The
    probe reads only the process name and never leaves the machine.
    """
    try:
        if _captured is not None:
            return _captured.is_terminal
        if sys.platform == "win32":
            info = _win_foreground()
        elif sys.platform == "darwin":
            info = _mac_foreground()
        else:
            info = None
        return bool(info) and info[0].lower() in _TERMINAL_PROCESSES
    except Exception as e:
        print(f"[app-context] terminal probe failed: {e}")
        return False

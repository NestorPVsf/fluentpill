"""Clipboard operations: copy text, paste via keyboard simulation."""

import subprocess
import time
import sys

from core.branding import is_own_app_name

_saved_app: str | None = None
is_mac = sys.platform == "darwin"


def save_frontmost_app():
    """Save the currently focused application before recording starts."""
    global _saved_app
    if is_mac:
        try:
            result = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of first process whose frontmost is true'],
                capture_output=True, text=True, timeout=2,
            )
            name = result.stdout.strip()
            if name and not is_own_app_name(name):
                _saved_app = name
        except Exception:
            pass


def _win_release_modifiers():
    """Release all modifier keys before simulating paste, to avoid ghost modifiers."""
    from pynput.keyboard import Controller, Key
    kb = Controller()
    for mod in (Key.alt_l, Key.alt_r, Key.alt_gr, Key.shift, Key.shift_r, Key.ctrl_l, Key.ctrl_r):
        try:
            kb.release(mod)
        except Exception:
            pass
    time.sleep(0.05)


def paste_text(text: str):
    """Copy text to clipboard and paste it via keyboard simulation."""
    global _saved_app
    if is_mac:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        if _saved_app:
            try:
                subprocess.run(
                    ["osascript", "-e", f'tell application "{_saved_app}" to activate'],
                    check=True, timeout=2,
                )
                time.sleep(0.12)
            except Exception:
                pass
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down'],
            check=True,
        )
        _saved_app = None
    else:
        import pyperclip
        from pynput.keyboard import Controller, Key

        _win_release_modifiers()

        pyperclip.copy(text)
        time.sleep(0.05)
        keyboard = Controller()
        with keyboard.pressed(Key.ctrl):
            keyboard.press('v')
            keyboard.release('v')


def _copy(text: str):
    """Put text on the system clipboard."""
    if is_mac:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
    else:
        import pyperclip
        pyperclip.copy(text)


def _target_is_terminal() -> bool:
    """Whether this dictation was pasted into a terminal. Never raises."""
    try:
        from core.app_context import target_is_terminal
        return target_is_terminal()
    except Exception:
        return False


def undo_and_paste_text(new_text: str) -> bool:
    """Undo the previous paste (Ctrl+Z/Cmd+Z) and paste the new text.

    Returns True when the target app was actually edited, False when the text
    was only left on the clipboard for the user to paste by hand.

    Terminals take the clipboard-only path: there Ctrl+Z is SIGTSTP, so instead
    of undoing the paste it suspends whatever runs in the foreground — a Claude
    Code / vim / REPL session vanishing mid-work (recoverable with `fg`, but it
    looks like a crash). Terminals have no undo to fall back on either, and
    blind backspaces or Ctrl+U could eat text typed after the paste, so the safe
    move is to send no keystroke at all.
    """
    global _saved_app
    if _target_is_terminal():
        _copy(new_text)
        print("[clipboard] terminal target — correction copied to clipboard, no Ctrl+Z sent")
        return False
    if is_mac:
        if _saved_app:
            try:
                subprocess.run(
                    ["osascript", "-e", f'tell application "{_saved_app}" to activate'],
                    check=True, timeout=2,
                )
                time.sleep(0.12)
            except Exception:
                pass
        subprocess.run(["pbcopy"], input=new_text.encode("utf-8"), check=True)
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to keystroke "z" using command down'],
            check=True,
        )
        time.sleep(0.1)
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down'],
            check=True,
        )
        _saved_app = None
    else:
        import pyperclip
        from pynput.keyboard import Controller, Key

        _win_release_modifiers()

        pyperclip.copy(new_text)
        keyboard = Controller()
        with keyboard.pressed(Key.ctrl):
            keyboard.press('z')
            keyboard.release('z')
        time.sleep(0.1)
        with keyboard.pressed(Key.ctrl):
            keyboard.press('v')
            keyboard.release('v')
    return True

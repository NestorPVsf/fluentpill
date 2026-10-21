"""Check: bare Ctrl must not start hold recording when _shift_held is stale.

Run: venv/Scripts/python.exe tools/verify_hold_gate.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pynput import keyboard
from core.hotkey import HotkeyListener

h = HotkeyListener()
h._muted_by_game = lambda: False
fired = []
h.pressed.connect(lambda: fired.append(1))
down = set()
h._key_down = lambda vk: vk in down

# Stale Shift flag, Shift really up: bare Ctrl must NOT record.
h._shift_held = True
down = {0x11}
h._key_down = lambda vk: vk in down
h._on_press(keyboard.Key.ctrl_l)
assert not fired and not h._recording, "bare Ctrl started recording"

# Real Ctrl+Shift (Ctrl already down, Shift being pressed): must record.
h._ctrl_held = True; h._shift_held = False
h._on_press(keyboard.Key.shift)
assert fired and h._recording, "real Ctrl+Shift did not start"
print("OK")

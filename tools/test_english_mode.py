"""Asserts for the English practice mode (no network, no audio).

Run: venv/Scripts/python.exe tools/test_english_mode.py
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from core import english
from core.winhotkey import parse_chord, MOD_ALT, MOD_CONTROL, MOD_SHIFT, VK_SPACE
from db.database import TranscriptionDB

ORIG = "Yesterday I have been in the meeting and we discussed about the budget"
GOOD = ('{"corrected": "Yesterday I was in the meeting and we discussed the budget",'
        ' "corrections": [{"original": "I have been", "better": "I was",'
        ' "why": "Pasado simple con \\"yesterday\\"", "type": "grammar"},'
        ' {"original": "discussed about", "better": "discussed",'
        ' "why": "discuss no lleva about", "type": "bogus"}], "upgrade": "I sat in on the budget meeting."}')

# --- 1. parsing and fallbacks -------------------------------------------------
r = english.parse_response(GOOD, ORIG)
assert r["ok"] and r["corrected"].startswith("Yesterday I was"), r
assert len(r["corrections"]) == 2 and r["corrections"][1]["type"] == "grammar", r  # unknown type -> grammar
assert r["upgrade"].startswith("I sat in"), r

fenced = english.parse_response("```json\n" + GOOD + "\n```", ORIG)
assert fenced["ok"] and len(fenced["corrections"]) == 2, fenced

broken = '{"corrected": "Yesterday I was in the meeting", "corrections": [{"original": "I hav'
r = english.parse_response(broken, ORIG)
assert r["ok"] and r["corrected"] == "Yesterday I was in the meeting" and r["corrections"] == [], r

for garbage in ("", "   ", "sorry I cannot", "{not json", '{"corrected": ""}', "[1, 2]"):
    r = english.parse_response(garbage, ORIG)
    assert not r["ok"] and r["corrected"] == ORIG, (garbage, r)

too_long = '{"corrected": "' + "blah " * 60 + '", "corrections": []}'
r = english.parse_response(too_long, ORIG)
assert not r["ok"] and r["corrected"] == ORIG, r  # > 2x original -> original

# no-op corrections (original == better) are dropped; cap at 6
many = '{"corrected": "ok", "corrections": [' + ",".join(
    '{"original": "a%d", "better": "b%d", "why": "x", "type": "grammar"}' % (i, i) for i in range(9)
) + ', {"original": "same", "better": "same"}]}'
assert len(english.parse_response(many, "ok ok")["corrections"]) == 6

# correct_english: provider chain with fakes, never raises
config.OPENAI_API_KEY, config.GROQ_API_KEY = "", ""
r = english.correct_english(ORIG)
assert not r["ok"] and r["corrected"] == ORIG, r  # no keys -> original
config.OPENAI_API_KEY = "k"
english._call_openai = lambda *a, **k: "total nonsense"
english._call_groq = lambda *a, **k: None
r = english.correct_english(ORIG)
assert not r["ok"] and r["corrected"] == ORIG, r  # unusable reply -> original
english._call_openai = lambda *a, **k: GOOD
r = english.correct_english(ORIG)
assert r["ok"] and r["corrected"].startswith("Yesterday I was"), r
def boom(*a, **k):
    raise RuntimeError("network down")
english._call_openai = boom
r = english.correct_english(ORIG)
assert r["corrected"] == ORIG, r  # exception swallowed

# --- 2/3. DB: migration on an old database, save and read ---------------------
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
    path = os.path.join(d, "old.db")
    with sqlite3.connect(path) as c:  # pre-feature schema: only transcriptions, no extra columns
        c.execute("CREATE TABLE transcriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,"
                  " language TEXT, duration_seconds REAL, model TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        c.execute("INSERT INTO transcriptions (text) VALUES ('hola')")
    db = TranscriptionDB(path)
    TranscriptionDB(path)  # second init must be idempotent
    with sqlite3.connect(path) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "english_corrections" in tables, tables
    assert db.get_by_id(1)["text"] == "hola"  # old data survives

    tid = db.insert("Yesterday I was in the meeting", language="en", raw_text=ORIG, was_cleaned=True)
    assert db.get_by_id(tid)["language"] == "en"
    assert db.insert_english_corrections(tid, [], "") == 0
    n = db.insert_english_corrections(tid, english.parse_response(GOOD, ORIG)["corrections"], "I sat in.")
    assert n == 2, n
    rows = db.get_english_corrections()
    assert len(rows) == 2 and rows[0]["transcription_id"] == tid and rows[0]["upgrade"] == "I sat in.", rows
    assert {r["better"] for r in rows} == {"I was", "discussed"}
    assert len(db.get_english_corrections(limit=1)) == 1
    assert len(db.get_english_corrections(since="2999-01-01")) == 0
    assert len(db.get_english_corrections(since="2000-01-01")) == 2

# --- 4. chord parser ----------------------------------------------------------
assert parse_chord("ctrl+alt+space") == (MOD_CONTROL | MOD_ALT, VK_SPACE)
assert parse_chord(" Ctrl + Shift + Space ") == (MOD_CONTROL | MOD_SHIFT, VK_SPACE)
for bad in ("", "space", "ctrl+x", "ctrl+foo+space", "alt+"):
    assert parse_chord(bad) is None, bad
assert config.ENGLISH_MODE_HOTKEY == "ctrl+alt+space" or os.getenv("ENGLISH_MODE_HOTKEY") is not None

# --- 5. language passed to Whisper -------------------------------------------
from core.transcriber import Transcriber

seen = []
class FakeTranscriptions:
    def create(self, **kw):
        seen.append(kw)
        return "hello"
class FakeClient:
    class audio:  # noqa: N801
        transcriptions = FakeTranscriptions()

t = Transcriber.__new__(Transcriber)
t.client = FakeClient()
t._whisper_prompt = "vocabulario: Supabase, Vercel"
t._transcribe_groq(b"x" * 200, english=True)
assert seen[-1]["language"] == "en" and "prompt" not in seen[-1], seen[-1]
t._transcribe_groq(b"x" * 200)
assert seen[-1]["language"] == config.WHISPER_LANGUAGE == "es" and seen[-1]["prompt"], seen[-1]
assert config.WHISPER_LANGUAGE == "es"  # the Spanish default stays on purpose

# --- 6. hotkey: English chord leaves the normal one untouched -----------------
from core.hotkey import HotkeyListener

h = HotkeyListener()
h._muted_by_game = lambda: False
fired, stopped = [], []
h.pressed.connect(lambda: fired.append(h.english_mode))
h.released.connect(lambda: stopped.append(1))

h._on_english_hotkey(MOD_CONTROL | MOD_ALT)
assert fired == [True] and h._recording and h.english_mode, fired
h._on_english_hotkey(MOD_CONTROL | MOD_ALT)  # same chord stops
assert stopped == [1] and not h._recording

h._ctrl_space()  # normal Ctrl+Space: starts WITHOUT english
assert fired == [True, False] and not h.english_mode, fired
h._ctrl_space()
assert stopped == [1, 1]

h.english_mode = True  # stale flag must not leak into a hold session
h._ctrl_held = True
h._key_down = lambda vk: True
from pynput import keyboard
h._on_press(keyboard.Key.shift)
assert fired[-1] is False and h._recording, fired
h._recording = False

h.english_mode = True
h.sync_external_start()  # tray click
assert h.english_mode is False

# disabled by config: nothing registers, nothing raises
config.ENGLISH_MODE_HOTKEY = ""
h._start_english_hotkey()
assert h._winhk_en is None

print("OK")

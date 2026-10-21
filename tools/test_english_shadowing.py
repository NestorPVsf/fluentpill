"""Check: shadowing (score, DB table, mic buttons, flow with a fake recorder). No network.

Run: venv/Scripts/python.exe tools/test_english_shadowing.py
"""
import io
import os
import sqlite3
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from core import shadowing  # noqa: E402
from db.database import TranscriptionDB  # noqa: E402
from ui.english_popup import EnglishPopup, PlayButton  # noqa: E402
from ui.english_shadowing import MicButton  # noqa: E402

app = QApplication.instance() or QApplication([])


def pump(cond, timeout=3.0):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    return cond()


# ---- scoring ---------------------------------------------------------------
S = shadowing.score
assert S("I want to go home", "i want to go home")["score"] == 100
assert S("I want to go home.", "I WANT, to go home!")["score"] == 100, "case and punctuation ignored"
r = S("I would like a coffee", "I want like a coffee")
assert 60 <= r["score"] < 100 and r["changed"] == [("want", "would")] and not r["missing"], r
r = S("I would really like a coffee", "I like a coffee")
assert r["missing"] == ["would", "really"] and r["score"] < 85, r
assert S("one two three", "")["score"] == 0
assert S("one two three", "four five six")["score"] == 0
assert S("", "anything")["score"] == 0
assert S("don't go", "dont go")["score"] < 100, "apostrophe words are single words"
assert shadowing.band(85) == "green" and shadowing.band(84) == "amber"
assert shadowing.band(60) == "amber" and shadowing.band(59) == "red"
fb = shadowing.feedback(S("I would like a coffee", "I want like a coffee"), "I want like a coffee")
assert "dijiste want por would" in fb and fb.startswith(str(S("I would like a coffee", "I want like a coffee")["score"]))
assert shadowing.feedback(S("a b", "a b"), "a b") == "100 · perfecto"
assert shadowing.feedback(S("a b", ""), "") == "0 · no se oyó nada"

# ---- DB table: idempotent migration, insert ---------------------------------
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    path = os.path.join(tmp, "t.db")
    db = TranscriptionDB(path)
    TranscriptionDB(path)                               # second init must not fail
    db.insert_english_shadowing("I would like a coffee", "I want like a coffee", 77)
    with sqlite3.connect(path) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(english_shadowing)")]
        rows = c.execute("SELECT target, heard, score FROM english_shadowing").fetchall()
    assert cols == ["id", "created_at", "target", "heard", "score"], cols
    assert rows == [("I would like a coffee", "I want like a coffee", 77)], rows

# ---- popup: one mic per play button ------------------------------------------
def corr(n):
    return [{"original": f"wrong {i}", "better": f"right {i}", "why": f"Porque {i}.", "type": "grammar"}
            for i in range(n)]


p = EnglishPopup()
assert p.show_result(corr(3), "a b c", "a better b c")
assert len(p.play_buttons) == 4 and len(p.mic_buttons) == 4, "3 rows + the upgrade line"
assert all(isinstance(b, MicButton) for b in p.mic_buttons)
assert [m.target for m in p.mic_buttons] == [b.text_to_say for b in p.play_buttons]
assert all(m.result_label is not None and not m.result_label.isVisible() for m in p.mic_buttons)

config.ENGLISH_SHADOWING_ENABLED = False
p.show_result(corr(2), "x", "x better")
assert len(p.play_buttons) == 3 and p.mic_buttons == [], "flag off: no mic buttons"
config.ENGLISH_SHADOWING_ENABLED = True
p.show_result(corr(2), "x", "x better")
assert len(p.mic_buttons) == 3


# ---- full flow with a fake recorder and a fake transcriber -------------------
class FakeRec:
    def __init__(self):
        self.is_recording = False
        self.secs, self.peak = 2.0, 1500.0

    def start(self):
        self.is_recording = True

    def stop(self):
        self.is_recording = False
        return self.secs

    def peak_rms(self, skip_initial_seconds=0.0):
        return self.peak

    def get_wav_buffer(self):
        return io.BytesIO(b"RIFF" + b"\0" * 200)


rec = FakeRec()
said = {"text": "I want like a coffee"}
saved, busy = [], {"v": False}
p.shadower.recorder_factory = lambda: rec
p.configure_shadowing(transcribe=lambda wav: said["text"],
                      mic_busy=lambda: busy["v"],
                      on_attempt=lambda t, h, s: saved.append((t, h, s)))
p.show_result([{"original": "a", "better": "I would like a coffee", "why": "x"}], "z", "")
h0 = p.height()
mic = p.mic_buttons[0]
mic.click()
assert mic.state == "recording" and rec.is_recording
mic.click()                                             # second press stops and scores
assert mic.state == "working" and not rec.is_recording
assert pump(lambda: mic.state == "idle" and saved)
assert saved[0][0] == "I would like a coffee" and saved[0][1] == said["text"] and 60 <= saved[0][2] < 100, saved
assert mic.result_label.isVisible() and "dijiste want por would" in mic.result_label.text()
assert "#FBBF24" in mic.result_label.text(), "amber for 60-84"
assert p.height() > h0, "panel grows to fit the score line"
screen_h = (QApplication.primaryScreen()).availableGeometry().height()
assert p.height() <= screen_h * 0.70 + 1

# green and red colours
said["text"] = "i would like a coffee"
mic.click(); mic.click(); assert pump(lambda: len(saved) == 2)
assert saved[1][2] == 100 and "#4ADE80" in mic.result_label.text()
said["text"] = "banana"
mic.click(); mic.click(); assert pump(lambda: len(saved) == 3)
assert saved[2][2] < 60 and "#F87171" in mic.result_label.text()

# silence: no transcription, nothing saved
rec.peak = 5.0
mic.click(); mic.click()
assert mic.state == "idle" and "no se oyó nada" in mic.result_label.text() and len(saved) == 3
rec.peak = 1500.0

# a dictation owns the mic: the button refuses to record
busy["v"] = True
mic.click()
assert mic.state == "idle" and not rec.is_recording and "en uso" in mic.result_label.text()
busy["v"] = False

# auto-stop after the cap
import ui.english_shadowing as es  # noqa: E402
es.MAX_SECONDS = 0.05
said["text"] = "i would like a coffee"
mic.click()
assert pump(lambda: len(saved) == 4), "12 s cap must stop and score on its own"
es.MAX_SECONDS = 12.0

# cancel (dictation starts / panel closes): late result is dropped
slow = {"go": False}
p.configure_shadowing(transcribe=lambda wav: (time.sleep(0.2), "late")[1], mic_busy=None,
                      on_attempt=lambda t, h, s: saved.append((t, h, s)))
mic.click(); mic.click()
p.cancel_shadowing()
time.sleep(0.4); app.processEvents()
assert mic.state == "idle" and len(saved) == 4, "result after cancel must be ignored"
mic.click()
assert rec.is_recording
p.cancel_shadowing()
assert not rec.is_recording and mic.state == "idle"

# a transcription error shows a hint and saves nothing
def boom(wav):
    raise RuntimeError("down")
p.configure_shadowing(transcribe=boom, mic_busy=None, on_attempt=lambda *a: saved.append(a))
mic.click(); mic.click()
assert pump(lambda: "No se pudo transcribir" in mic.result_label.text()) and len(saved) == 4

# a new dictation result replaces the rows, recording is dropped
mic.click()
p.show_result(corr(1), "", "")
assert not rec.is_recording

print("test_english_shadowing OK")

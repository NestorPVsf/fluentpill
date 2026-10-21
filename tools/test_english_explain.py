"""Check: "Explícame" mini-lesson (core/english_explain.py) + the popup's button and 0.75x switch.

No network: the LLM clients are replaced. Offscreen Qt.
Run: venv/Scripts/python.exe tools/test_english_explain.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from core import english_explain  # noqa: E402

CORR = [
    {"original": "I want that we finish", "better": "I want us to finish", "why": "want + objeto + to", "type": "grammar"},
    {"original": "meeting start", "better": "meeting starts", "why": "sujeto singular", "type": "grammar"},
]
ORIG, FIXED, UP = "I want that we finish before the meeting start", "I want us to finish before the meeting starts", "Let's wrap this up"

# ---- the module -----------------------------------------------------------
config.OPENAI_API_KEY, config.GROQ_API_KEY = "k-openai", ""
calls = []
reply = {"value": "Querías decir otra cosa."}


def fake_openai(system, user, api_key, model, timeout, json_mode=True, max_tokens=1500):
    calls.append({"system": system, "user": user, "json_mode": json_mode, "model": model})
    return reply["value"]


english_explain._call_openai = fake_openai
english_explain._cache.clear()

out = english_explain.explain(ORIG, FIXED, CORR, UP)
assert out == "Querías decir otra cosa." and len(calls) == 1
u = calls[0]["user"]
for needle in (ORIG, FIXED, "I want us to finish", "meeting starts", "sujeto singular", UP):
    assert needle in u, f"prompt misses {needle!r}"
assert calls[0]["json_mode"] is False, "the lesson is free text, not JSON"
assert "Eres el profe de inglés" in calls[0]["system"], "prompt file not loaded"

english_explain.explain(ORIG, FIXED, CORR, UP)
assert len(calls) == 1, "second press must come from the cache"
english_explain.explain("another dictation", FIXED, CORR, UP)
assert len(calls) == 2, "a different dictation is a different cache entry"

# failure -> ExplainError with a Spanish hint, not cached, retry works
english_explain._cache.clear()
reply["value"] = None
try:
    english_explain.explain(ORIG, FIXED, CORR, UP)
    raise AssertionError("must raise")
except english_explain.ExplainError as e:
    assert "Reintenta" in e.hint
n = len(calls)
reply["value"] = "Ahora sí."
assert english_explain.explain(ORIG, FIXED, CORR, UP) == "Ahora sí." and len(calls) == n + 1

# no keys at all -> clear hint
english_explain._cache.clear()
config.OPENAI_API_KEY = ""
try:
    english_explain.explain(ORIG, FIXED, CORR, UP)
    raise AssertionError("must raise")
except english_explain.ExplainError as e:
    assert "clave" in e.hint
config.OPENAI_API_KEY = "k-openai"

# fallback to Groq when OpenAI returns nothing
english_explain._cache.clear()
reply["value"] = None
config.GROQ_API_KEY = "k-groq"
english_explain._call_groq = lambda *a, **k: "Desde Groq."
assert english_explain.explain(ORIG, FIXED, CORR, UP) == "Desde Groq."

# ---- the popup ------------------------------------------------------------
from PyQt6.QtWidgets import QApplication, QLabel  # noqa: E402

from ui.english_popup import EnglishPopup, MAX_HEIGHT_PX, SLOW_RATE, Speaker  # noqa: E402

app = QApplication.instance() or QApplication([])


def wait(cond, secs=3.0):
    end = time.monotonic() + secs
    while time.monotonic() < end and not cond():
        app.processEvents()
        time.sleep(0.01)
    return cond()


p = EnglishPopup()
seen = []
state = {"fail": True}


def fake_explain(original, corrected, corrections, upgrade):
    seen.append((original, corrected, corrections, upgrade))
    if state["fail"]:
        raise english_explain.ExplainError("boom", "No se pudo generar la explicación. Reintenta.")
    return "Primera parte de la clase.\n\n" + "Más texto de la clase. " * 60


p.explainer.explain_fn = fake_explain
assert p.show_result(CORR, FIXED, UP, original=ORIG) is True
assert p.btn_explain.text() == "Explícame" and p.btn_explain.isEnabled()
assert p.btn_slow.isCheckable() and p.btn_slow.text() == "0,75×"
assert not p.btn_explain.isHidden() and not p.btn_slow.isHidden()

# failure: short message, button turns into "Reintentar"
p.btn_explain.click()
assert p.btn_explain.text() == "Explicando…" and not p.btn_explain.isEnabled()
assert wait(lambda: p.btn_explain.text() == "Reintentar"), "error state never reached"
assert "Reintenta" in p._explain_label.text() and p._explain_box.isVisible()
assert len(seen) == 1 and seen[0][0] == ORIG and seen[0][1] == FIXED and seen[0][3] == UP
assert [c["better"] for c in seen[0][2]] == ["I want us to finish", "meeting starts"]

# retry works and the lesson is shown; the long text scrolls inside the panel
state["fail"] = False
p.btn_explain.click()
assert wait(lambda: p.btn_explain.text() == "Ocultar"), "lesson never arrived"
assert len(seen) == 2 and "Primera parte" in p._explain_label.text()
geo = (QApplication.screenAt(p.pos()) or QApplication.primaryScreen()).availableGeometry()
assert p.height() <= MAX_HEIGHT_PX and p.height() <= geo.height() * 0.7 + 1
assert p.scroll.verticalScrollBar().maximum() > 0, "a long lesson must scroll inside the panel"

# folding does not call the LLM again
p.btn_explain.click()
assert not p._explain_box.isVisible() and p.btn_explain.text() == "Explícame" and len(seen) == 2
p.btn_explain.click()
assert p._explain_box.isVisible() and p.btn_explain.text() == "Ocultar" and len(seen) == 2

# a new dictation resets the section and drops a late lesson
state["fail"] = False
p.show_result(CORR[:1], "x", "", original="o")
assert p.btn_explain.text() == "Explícame" and not p._explain_box.isVisible()
p.btn_explain.click()
p.show_result(CORR[:1], "y", "", original="o2")      # replaced while the call is in flight
time.sleep(0.2)
for _ in range(20):
    app.processEvents()
    time.sleep(0.01)
assert not p._explain_box.isVisible() and p.btn_explain.text() == "Explícame", "late lesson leaked"

# the flag hides the button
config.ENGLISH_EXPLAIN_ENABLED = False
p.show_result(CORR, FIXED, UP, original=ORIG)
assert p.btn_explain.isHidden()
config.ENGLISH_EXPLAIN_ENABLED = True
p.show_result(CORR, FIXED, UP, original=ORIG)
assert not p.btn_explain.isHidden()

# ---- slow voice -----------------------------------------------------------
assert p.speaker.rate == 1.0
p.btn_slow.click()
assert p.btn_slow.isChecked() and p.speaker.rate == SLOW_RATE == 0.75
p.show_result(CORR, FIXED, UP, original=ORIG)          # remembered across dictations
assert p.btn_slow.isChecked() and p.speaker.rate == 0.75


class FakePlayer:
    def __init__(self):
        self.log = []

    def setSource(self, _):
        self.log.append("source")

    def setPlaybackRate(self, r):
        self.log.append(("rate", r))

    def play(self):
        self.log.append("play")

    def stop(self):
        pass


sp = Speaker()
sp._player = FakePlayer()
btn = p.play_buttons[0]
sp._current, sp._token = btn, 5
sp.set_rate(0.75)
sp._on_finished(5, "C:/tmp/x.wav", "")
log = sp._player.log
assert ("rate", 0.75) in log and log.index(("rate", 0.75)) < log.index("play"), log
sp.set_rate(1.0)
sp._current, sp._token = btn, 6
sp._on_finished(6, "C:/tmp/x.wav", "")
assert sp._player.log[-2:] == [("rate", 1.0), "play"], sp._player.log
p.btn_slow.click()
assert p.speaker.rate == 1.0

print("test_english_explain OK")

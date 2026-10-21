"""Check: "listen" buttons of the English panel + the TTS cache (network mocked).

Run: venv/Scripts/python.exe tools/test_english_tts.py
"""
import base64
import io
import os
import sys
import tempfile
import time
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Cache goes to a throwaway folder, never to the real %LOCALAPPDATA%\fluentpill\tts.
_TMP = tempfile.mkdtemp(prefix="fluentpill-tts-test-")
os.environ["LOCALAPPDATA"] = _TMP

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from core import english_tts  # noqa: E402
from ui.english_popup import EnglishPopup, PlayButton  # noqa: E402

app = QApplication.instance() or QApplication([])
config.GEMINI_API_KEY = "test-key"


def pump(cond, timeout=3.0):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    return cond()


def wav_bytes(frames=2400):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x01\x00" * frames)
    return buf.getvalue()


class FakeResp:
    def __init__(self, status=200, audio=None, mime="audio/wav"):
        self.status_code = status
        self.text = "boom" if status != 200 else ""
        self._audio = audio

    def json(self):
        return {"steps": [{"type": "model_output", "content": [
            {"type": "audio", "mime_type": "audio/wav",
             "data": base64.b64encode(self._audio).decode()}]}]}


calls = []
behaviour = {"resp": lambda: FakeResp(200, wav_bytes())}


def fake_post(url, headers, payload, timeout):
    calls.append(payload)
    r = behaviour["resp"]()
    if isinstance(r, Exception):
        raise r
    return r


english_tts._post = fake_post

# ---- cache: the second request for the same text does not hit the API
p1 = english_tts.synthesize("keep doing that job")
assert p1.is_file() and p1.suffix == ".wav" and p1.parent.name == "tts"
assert p1.parent.parent.name == "fluentpill", p1
assert len(calls) == 1
p2 = english_tts.synthesize("keep doing that job")
assert p2 == p1 and len(calls) == 1, "second request must come from the cache"
with wave.open(str(p1)) as w:
    assert w.getframerate() == 24000 and w.getnchannels() == 1 and w.getsampwidth() == 2
# the request carries the voice, the model and the style
sent = calls[0]
assert sent["generation_config"]["speech_config"][0]["voice"] == config.ENGLISH_TTS_VOICE
assert sent["model"] == config.ENGLISH_TTS_MODEL
assert "native American English" in sent["input"][0]["content"][0]["annotations"][0]["style"]
assert sent["input"][0]["content"][0]["text"] == "keep doing that job", "style must not be spoken"

# ---- cache key changes with voice, model and text
k = english_tts.cache_key("hello", "m", "Iapetus")
assert k != english_tts.cache_key("hello", "m", "Kore")
assert k != english_tts.cache_key("hello", "m2", "Iapetus")
assert k != english_tts.cache_key("hello!", "m", "Iapetus")
assert k == english_tts.cache_key("hello", "m", "Iapetus")
config.ENGLISH_TTS_VOICE, saved = "Kore", config.ENGLISH_TTS_VOICE
p3 = english_tts.synthesize("keep doing that job")
config.ENGLISH_TTS_VOICE = saved
assert p3 != p1 and len(calls) == 2, "another voice must not reuse the cache"

# ---- raw PCM from the API gets wrapped as WAV
behaviour["resp"] = lambda: FakeResp(200, b"\x02\x00" * 4800)
p4 = english_tts.synthesize("raw pcm please")
with wave.open(str(p4)) as w:
    assert w.getnframes() == 4800 and w.getframerate() == 24000

# ---- errors: TtsError with a hint, nothing left in the cache
behaviour["resp"] = lambda: FakeResp(500)
try:
    english_tts.synthesize("will fail")
    raise AssertionError("expected TtsError")
except english_tts.TtsError as e:
    assert e.hint
assert not english_tts.cache_path("will fail", config.ENGLISH_TTS_MODEL, config.ENGLISH_TTS_VOICE,
                                  config.ENGLISH_TTS_STYLE).exists()
config.GEMINI_API_KEY = ""
try:
    english_tts.synthesize("no key")
    raise AssertionError("expected TtsError")
except english_tts.TtsError as e:
    assert "clave" in e.hint
config.GEMINI_API_KEY = "test-key"

# ---- panel: one button per row, plus one for the upgrade (none without it)
def corr(n):
    return [{"original": f"wrong {i}", "better": f"right {i}", "why": f"Porque {i}."} for i in range(n)]


panel = EnglishPopup()
assert panel.show_result(corr(3), "a b c", "a better b c")
assert len(panel.play_buttons) == 4
assert [b.text_to_say for b in panel.play_buttons] == ["right 0", "right 1", "right 2", "a better b c"]
panel.show_result(corr(2), "same", "same")
assert len(panel.play_buttons) == 2, "upgrade equal to corrected -> no button"
panel.show_result(corr(2), "x", "")
assert len(panel.play_buttons) == 2, "no upgrade -> no button"
panel.show_result(corr(1), "x", "x better")
assert len(panel.play_buttons) == 2
b0 = panel.play_buttons[0]
assert (b0.width(), b0.height()) == (24, 24)
assert b0.focusPolicy() == Qt.FocusPolicy.NoFocus and b0.toolTip() == "Escuchar"
assert panel.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

# ---- an API failure leaves the button on play, with a hint, no exception
behaviour["resp"] = lambda: FakeResp(500)
b0 = panel.play_buttons[0]
b0.click()
assert b0.state == "loading"
assert pump(lambda: b0.state == "idle" and b0.toolTip() != "Escuchar")
assert panel.speaker._current is None
behaviour["resp"] = lambda: ConnectionError("offline")
b0.click()
assert pump(lambda: b0.state == "idle")

# ---- playback: stop on second press, other button cuts the first
class FakePlayer:
    def __init__(self):
        self.sources, self.stopped, self.playing = [], 0, False
        self.rate = None

    def setPlaybackRate(self, rate):
        self.rate = rate

    def setSource(self, url):
        self.sources.append(url.toLocalFile())

    def play(self):
        self.playing = True

    def stop(self):
        self.stopped += 1
        self.playing = False


fake = FakePlayer()
panel.speaker._make_player = lambda: fake
behaviour["resp"] = lambda: FakeResp(200, wav_bytes())
panel.show_result(corr(2), "x", "x better")
a, b, c = panel.play_buttons
a.click()
assert pump(lambda: a.state == "playing") and fake.playing and fake.sources[-1].endswith(".wav")
assert a.toolTip() == "Parar"
b.click()
assert pump(lambda: b.state == "playing")
assert a.state == "idle", "pressing another play cuts the previous one"
b.click()
assert b.state == "idle" and not fake.playing, "second press stops"
# a result that arrives after the user pressed stop is dropped
calls_before = len(fake.sources)
c.click()
assert c.state == "loading"
c.click()
assert c.state == "idle"
time.sleep(0.2)
app.processEvents()
assert c.state == "idle" and len(fake.sources) == calls_before and not fake.playing
# replacing the panel content silences it and leaves no stale button
a.click()
assert pump(lambda: a.state == "playing")
panel.show_result(corr(1), "x", "")
assert not fake.playing and len(panel.play_buttons) == 1
panel.play_buttons[0].click()
assert pump(lambda: fake.playing)
panel.hide()
assert not fake.playing, "closing the panel stops the audio"

print("test_english_tts OK")

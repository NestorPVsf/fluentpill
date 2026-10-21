"""Check: the "is this my own app?" tests recognise both names, and the
panel serves the brand files.

Run: venv/Scripts/python.exe tools/test_own_app_name.py
"""
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.branding import APP_NAME, is_own_app_name  # noqa: E402
import core.app_context as app_context  # noqa: E402
import core.clipboard as clipboard  # noqa: E402

# --- the helper itself -------------------------------------------------------
assert APP_NAME == "fluentpill"
for own in ("Howl", "fluentpill", "FluentPill", "howl", " fluentpill "):
    assert is_own_app_name(own), own
for other in (None, "", "Slack", "Howl2", "fluentpill-helper", "Notepad"):
    assert not is_own_app_name(other), other


def fake_osascript(name):
    return mock.patch.object(
        subprocess, "run", return_value=SimpleNamespace(stdout=name + "\n")
    )


# --- core/app_context.py: macOS foreground probe -----------------------------
for own in ("Howl", "fluentpill"):
    with fake_osascript(own):
        assert app_context._mac_foreground() is None, f"app_context ignores {own}"
with fake_osascript("Slack"):
    assert app_context._mac_foreground() == ("Slack.exe", ""), "other apps still pass"

# --- core/clipboard.py: remembers the app to paste back into ------------------
with mock.patch.object(clipboard, "is_mac", True):
    for own in ("Howl", "fluentpill"):
        clipboard._saved_app = None
        with fake_osascript(own):
            clipboard.save_frontmost_app()
        assert clipboard._saved_app is None, f"clipboard must not save {own}"
    with fake_osascript("Slack"):
        clipboard.save_frontmost_app()
    assert clipboard._saved_app == "Slack"

# --- no stray literal comparison against the old name -------------------------
bad = []
for folder in ("core", "ui", "web"):
    for fn in os.listdir(os.path.join(ROOT, folder)):
        if fn.endswith(".py"):
            text = open(os.path.join(ROOT, folder, fn), encoding="utf-8").read()
            for i, line in enumerate(text.splitlines(), 1):
                if ('== "Howl"' in line or '!= "Howl"' in line) and "branding" not in fn:
                    bad.append(f"{folder}/{fn}:{i}")
assert not bad, f"use is_own_app_name() instead of comparing to 'Howl': {bad}"

# --- panel: visible name, favicon and lockup are served -----------------------
from web.server import app  # noqa: E402

app.config["PANEL_PORT"] = 5000
c = app.test_client()
H = {"base_url": "http://127.0.0.1:5000"}
html = c.get("/", **H).get_data(as_text=True)
assert "<title>fluentpill" in html, "panel title"
assert 'alt="fluentpill"' in html and "Howl" not in html, "no visible Howl in panel"
for path, mime in (
    ("/brand/favicon.svg", "image/svg+xml"),
    ("/brand/favicon-32.png", "image/png"),
    ("/brand/fluentpill-lockup-dark.svg", "image/svg+xml"),
):
    r = c.get(path, **H)
    assert r.status_code == 200 and r.mimetype == mime, (path, r.status_code, r.mimetype)
    assert len(r.get_data()) > 0
assert c.get("/brand/..%2Fconfig.py", **H).status_code == 404
assert c.get("/brand/fluentpill.ico", **H).status_code == 404, "whitelist only"

print("test_own_app_name OK")

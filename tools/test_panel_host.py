"""Check: panel rejects foreign Host (DNS rebinding) and cross-site writes.

Run: venv/Scripts/python.exe tools/test_panel_host.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from web.server import app  # noqa: E402

app.config["PANEL_PORT"] = 5000
c = app.test_client()
OWN = "http://127.0.0.1:5000"
# retranscribe on a missing id: any non-403 answer proves the guard let it through
POST = "/api/transcriptions/999999/retranscribe"


def status(method, path, host=None, **headers):
    kw = {"headers": headers}
    if host:
        kw["base_url"] = f"http://{host}"
    return getattr(c, method)(path, **kw).status_code


assert status("get", "/") == 403, "bare Host without port"
assert status("get", "/", host="127.0.0.1:5000") == 200
assert status("get", "/", host="localhost:5000") == 200
assert status("get", "/api/transcriptions", host="evil.com:5000") == 403
assert status("get", "/api/transcriptions", host="evil.com") == 403
assert status("get", "/api/transcriptions", host="127.0.0.1:6000") == 403, "wrong port"
assert status("get", "/api/transcriptions", host="127.0.0.1.evil.com:5000") == 403
assert status("post", POST, host="evil.com:5000", Origin="http://evil.com:5000") == 403
assert status("post", POST, host="127.0.0.1:5000", Origin="http://evil.com") == 403
assert status("post", POST, host="127.0.0.1:5000", Origin="null") == 403
assert status("post", POST, host="127.0.0.1:5000", Origin=OWN) != 403, "own origin"
assert status("post", POST, host="localhost:5000", Origin="http://localhost:5000") != 403
assert status("post", POST, host="127.0.0.1:5000") != 403, "no Origin (curl)"
assert status("post", POST, host="127.0.0.1:5000", **{"Sec-Fetch-Site": "cross-site"}) == 403
assert status("post", POST, host="127.0.0.1:5000", **{"Sec-Fetch-Site": "same-origin"}) != 403
assert status("delete", "/api/transcriptions/older-than/99999", host="127.0.0.1:5000",
              Origin="http://evil.com") == 403
# Port comes from config, not hard-coded
app.config["PANEL_PORT"] = 5001
assert status("get", "/", host="127.0.0.1:5000") == 403
assert status("get", "/", host="127.0.0.1:5001") == 200
print("test_panel_host OK")

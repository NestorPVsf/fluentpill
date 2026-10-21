"""Checks for GET /api/english/stats (temporary DB, never the real one).

Run: venv/Scripts/python.exe tools/test_english_stats.py
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import TranscriptionDB  # noqa: E402
from web.server import app  # noqa: E402

HOST = "http://127.0.0.1:5000"


def seed(db: TranscriptionDB, rows):
    """rows: (original, better, why, type, days_ago)"""
    tid = db.insert("hello", language="en")
    db.insert_english_corrections(tid, [{"original": o, "better": b, "why": w, "type": t}
                                        for o, b, w, t, _ in rows])
    with sqlite3.connect(db.db_path) as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM english_corrections ORDER BY id").fetchall()]
        for rid, row in zip(ids[-len(rows):], rows):
            conn.execute("UPDATE english_corrections SET created_at = datetime('now', ?) WHERE id = ?",
                         (f"-{row[4]} days", rid))
    return tid


def client_for(path):
    app.config["PANEL_PORT"] = 5000
    app.config["DB_PATH"] = path
    return app.test_client()


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    # --- 1. empty DB: empty lists, no error -----------------------------------
    empty = os.path.join(tmp, "empty.db")
    TranscriptionDB(empty)
    c = client_for(empty)
    for days in ("7", "30", "all"):
        r = c.get(f"/api/english/stats?days={days}", base_url=HOST)
        assert r.status_code == 200, (days, r.status_code)
        j = r.get_json()
        assert j["dictations"] == 0 and j["corrections"] == 0, j
        assert j["by_type"] == {} and j["repeated"] == [] and j["recent"] == [], j

    # --- 2. grouping ----------------------------------------------------------
    path = os.path.join(tmp, "seeded.db")
    db = TranscriptionDB(path)
    seed(db, [
        ("principal reason", "main reason", "main es mas natural", "vocabulary", 1),
        ("the principal thing", "Main reason.", "main es mas natural", "vocabulary", 2),
        ("principal point", "  MAIN   reason ", "otra explicacion", "vocabulary", 3),
        ("I have been", "I was", "pasado simple", "grammar", 1),
        ("I have been", "I was", "pasado simple", "grammar", 2),   # same original twice
        ("discussed about", "discussed", "sin about", "grammar", 0),  # appears once
        ("old slip", "old fix", "viejo", "grammar", 40),
        ("old slip two", "old fix", "viejo", "grammar", 41),
    ])
    c = client_for(path)
    j = c.get("/api/english/stats?days=7", base_url=HOST).get_json()
    top = j["repeated"][0]
    assert top["count"] == 3 and top["better"].lower().startswith("main"), top
    assert len(top["examples"]) == 2 and len(set(top["examples"])) == 2, top
    assert top["why"], top
    assert j["repeated"][1]["count"] == 2, j["repeated"]
    assert all(r["count"] >= 2 for r in j["repeated"]), "single occurrences must not be 'repeated'"
    assert "discussed" not in [r["better"] for r in j["repeated"]]
    assert len(j["repeated"][1]["examples"]) == 1, "identical originals are listed once"

    # --- 3. period filter -----------------------------------------------------
    assert j["corrections"] == 6, j["corrections"]  # the two 40-day-old rows are out
    assert all("old" not in r["better"] for r in j["repeated"]), "days=7 must exclude old rows"
    assert all("old" not in r["original"] for r in j["recent"])
    j30 = c.get("/api/english/stats?days=30", base_url=HOST).get_json()
    assert j30["corrections"] == 6
    jall = c.get("/api/english/stats?days=all", base_url=HOST).get_json()
    assert jall["corrections"] == 8, jall["corrections"]
    assert any(r["better"] == "old fix" and r["count"] == 2 for r in jall["repeated"]), jall["repeated"]
    assert jall["by_type"] == {"vocabulary": 3, "grammar": 5}, jall["by_type"]
    assert jall["dictations"] == 1

    # --- 4. recent is capped at 20, newest first ------------------------------
    many = os.path.join(tmp, "many.db")
    db2 = TranscriptionDB(many)
    seed(db2, [(f"o{i}", f"b{i}", "w", "grammar", 0) for i in range(30)])
    jm = client_for(many).get("/api/english/stats?days=all", base_url=HOST).get_json()
    assert len(jm["recent"]) == 20 and jm["corrections"] == 30, (len(jm["recent"]), jm["corrections"])

    # --- 5. bad input and the Host guard -------------------------------------
    c = client_for(path)
    assert c.get("/api/english/stats?days=abc", base_url=HOST).status_code == 400
    assert c.get("/api/english/stats?days=-1", base_url=HOST).status_code == 400
    assert c.get("/api/english/stats?days=7", base_url="http://evil.com:5000").status_code == 403
    assert c.get("/api/english/stats?days=7", base_url="http://127.0.0.1.evil.com:5000").status_code == 403
    assert c.get("/api/english/stats?days=7", base_url="http://127.0.0.1:6000").status_code == 403
    assert c.get("/api/english/stats", base_url=HOST).status_code == 200  # default is 7 days

app.config.pop("DB_PATH", None)
print("test_english_stats OK")

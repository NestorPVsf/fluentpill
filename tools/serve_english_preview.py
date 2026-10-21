"""Serve the panel on a temporary port against a COPY of a database (for screenshots).

Run: venv/Scripts/python.exe tools/serve_english_preview.py <db> <port> [--seed]
  --seed  ignore <db> and build a temp DB full of repeated mistakes.
Never touches the real panel on :5000 and never writes to <db>.
"""
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import TranscriptionDB  # noqa: E402
from web.server import app  # noqa: E402

SEED = [
    # (original, better, why, type, days_ago)
    ("principal reason", "main reason", "«Principal» existe, pero «main» es lo natural aquí.", "vocabulary", 0),
    ("the principal problem", "main reason", "«Principal» existe, pero «main» es lo natural aquí.", "vocabulary", 1),
    ("principal idea", "main reason", "«Principal» existe, pero «main» es lo natural aquí.", "vocabulary", 3),
    ("I have been yesterday", "I was", "Con «yesterday» va pasado simple, no presente perfecto.", "grammar", 0),
    ("I have been there last week", "I was", "Con «yesterday» va pasado simple, no presente perfecto.", "grammar", 2),
    ("discussed about the budget", "discussed the budget", "«Discuss» no lleva «about».", "grammar", 1),
    ("we discussed about it", "discussed the budget", "«Discuss» no lleva «about».", "grammar", 4),
    ("more people for doing that", "anyone else to do that", "En negativas, «anyone else» es más natural.", "naturalness", 5),
    ("more people for doing that", "anyone else to do that", "En negativas, «anyone else» es más natural.", "naturalness", 6),
    ("stay doing that job", "keep doing that job", "«Keep doing» expresa continuar una actividad.", "naturalness", 2),
    ("all of my account for Gmail", "access to my Gmail account", "Se dice «access to» una cuenta.", "grammar", 0),
]


def main():
    db_arg, port = sys.argv[1], int(sys.argv[2])
    tmp = tempfile.mkdtemp(prefix="fp-preview-")
    path = os.path.join(tmp, "preview.db")
    if "--seed" in sys.argv:
        db = TranscriptionDB(path)
        for _ in range(9):
            db.insert("seeded english dictation", language="en")
        tid = db.insert("seeded english dictation", language="en")
        db.insert_english_corrections(tid, [{"original": o, "better": b, "why": w, "type": t}
                                            for o, b, w, t, _ in SEED])
        with sqlite3.connect(path) as conn:
            for i, row in enumerate(SEED, start=1):
                conn.execute("UPDATE english_corrections SET created_at = datetime('now', ?, ?) WHERE id = ?",
                             (f"-{row[4]} days", f"-{i * 7} minutes", i))
    else:
        shutil.copyfile(db_arg, path)
    app.config["DB_PATH"] = path
    app.config["PANEL_PORT"] = port
    print(f"serving {path} on 127.0.0.1:{port}", flush=True)
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


main()

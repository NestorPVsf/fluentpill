"""Check: one-time Howl -> fluentpill data-folder migration (core/lifecycle.py).

Run: venv/Scripts/python.exe tools/test_rename_migration.py
Everything happens under a temp LOCALAPPDATA; the real folders are never touched.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import lifecycle  # noqa: E402

root = Path(tempfile.mkdtemp(prefix="fluentpill-migr-test-"))


def fresh(name: str) -> Path:
    base = root / name
    base.mkdir()
    return base


try:
    # 1. old exists, new doesn't -> migrated, content kept
    b = fresh("a")
    (b / "Howl" / "recordings").mkdir(parents=True)
    (b / "Howl" / "stopped-by-user").write_text("x", encoding="utf-8")
    (b / "Howl" / "recordings" / "r.wav").write_bytes(b"RIFF")
    d, note = lifecycle.migrate_legacy_appdata(b)
    assert d == b / "fluentpill" and not (b / "Howl").exists(), (d, list(b.iterdir()))
    assert (d / "stopped-by-user").read_text(encoding="utf-8") == "x"
    assert (d / "recordings" / "r.wav").read_bytes() == b"RIFF"
    assert note and "migrated" in note, note
    # second run is a no-op
    d2, note2 = lifecycle.migrate_legacy_appdata(b)
    assert d2 == d and note2 is None

    # 2. both exist -> nothing touched, the new one wins
    b = fresh("b")
    (b / "Howl").mkdir()
    (b / "Howl" / "old.txt").write_text("old", encoding="utf-8")
    (b / "fluentpill").mkdir()
    (b / "fluentpill" / "new.txt").write_text("new", encoding="utf-8")
    d, note = lifecycle.migrate_legacy_appdata(b)
    assert d == b / "fluentpill" and note is None
    assert (b / "Howl" / "old.txt").exists() and not (b / "fluentpill" / "old.txt").exists()
    assert (b / "fluentpill" / "new.txt").exists()

    # 3. neither exists -> new one is created
    b = fresh("c")
    d, note = lifecycle.migrate_legacy_appdata(b)
    assert d == b / "fluentpill" and d.is_dir() and note is None
    assert not (b / "Howl").exists()

    # 4. rename fails (locked file) -> old folder used, note explains, nothing lost
    b = fresh("d")
    (b / "Howl").mkdir()
    (b / "Howl" / "fluentpill.log").write_text("log", encoding="utf-8")
    real_replace = os.replace

    def locked(src, dst):
        raise PermissionError(32, "The process cannot access the file")

    os.replace = locked
    try:
        d, note = lifecycle.migrate_legacy_appdata(b)
    finally:
        os.replace = real_replace
    assert d == b / "Howl" and not (b / "fluentpill").exists(), d
    assert note and "could not rename" in note, note
    assert (b / "Howl" / "fluentpill.log").exists()

    # 5. appdata_dir() end to end through LOCALAPPDATA (Windows) / HOME (elsewhere)
    b = fresh("e")
    if sys.platform == "win32":
        os.environ["LOCALAPPDATA"] = str(b)
        legacy = b / "Howl"
    else:
        os.environ["HOME"] = str(b)
        legacy = b / ".config" / "Howl"
        legacy.parent.mkdir()
    legacy.mkdir()
    (legacy / "k.txt").write_text("k", encoding="utf-8")
    lifecycle._resolved_dir = None
    got = lifecycle.appdata_dir()
    assert got.name == "fluentpill" and (got / "k.txt").exists() and not legacy.exists(), got
    assert lifecycle.migration_note() and "migrated" in lifecycle.migration_note()
    assert lifecycle.stopped_marker_path() == got / "stopped-by-user"
    print("OK: rename migration (5 cases)")
finally:
    shutil.rmtree(root, ignore_errors=True)

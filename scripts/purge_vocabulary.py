"""One-off cleanup: purge learned vocabulary words that don't look like
proper nouns / technical terms (prompt-poisoning noise from the old, looser
learn() filter). Uses the same criterion as core.dictionary.Dictionary
._looks_like_proper_or_technical: keep only words containing an uppercase
letter, a digit, or an internal '.', '-', '/' separator.

Usage:
    venv\\Scripts\\python.exe scripts/purge_vocabulary.py          # dry-run
    venv\\Scripts\\python.exe scripts/purge_vocabulary.py --apply  # delete for real
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DB_PATH  # noqa: E402
from core.dictionary import Dictionary  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually delete (default: dry-run)")
    args = parser.parse_args()

    # Short-lived connection — the app may have the DB open concurrently;
    # sqlite handles this fine as long as we don't hold the connection open.
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("SELECT word FROM vocabulary ORDER BY word").fetchall()
    finally:
        conn.close()

    def passes(word: str) -> bool:
        # Same normalization learn() applies before judging a token: strip
        # edge punctuation, then require uppercase/digit/internal separator.
        normalized = Dictionary._normalize_token(word)
        if not normalized:
            return False
        return Dictionary._looks_like_proper_or_technical(normalized)

    to_delete = [w for (w,) in rows if not passes(w)]
    to_keep = [w for (w,) in rows if passes(w)]

    print(f"Total vocabulary words: {len(rows)}")
    print(f"Would keep ({len(to_keep)}): {to_keep}")
    print(f"Would delete ({len(to_delete)}): {to_delete}")

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to delete.")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        for word in to_delete:
            conn.execute("DELETE FROM vocabulary WHERE word = ?", (word,))
        conn.commit()
    finally:
        conn.close()
    print(f"\nDeleted {len(to_delete)} words.")


if __name__ == "__main__":
    main()

import sqlite3
from config import DB_PATH


class TranscriptionDB:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transcriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    language TEXT,
                    duration_seconds REAL,
                    model TEXT DEFAULT 'whisper-large-v3-turbo',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_transcriptions_created_at
                ON transcriptions(created_at)
            """)
            # Idempotent migration — adds columns if they don't exist yet
            for ddl in (
                "ALTER TABLE transcriptions ADD COLUMN raw_text TEXT",
                "ALTER TABLE transcriptions ADD COLUMN was_cleaned INTEGER DEFAULT 0",
                "ALTER TABLE transcriptions ADD COLUMN app_name TEXT",
                "ALTER TABLE transcriptions ADD COLUMN app_category TEXT",
                "ALTER TABLE transcriptions ADD COLUMN audio_path TEXT",
            ):
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass  # column already exists
            # Legacy table kept only so existing JSON backups remain importable.
            # New code never reads or writes from it.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS corrections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wrong TEXT NOT NULL,
                    correct TEXT NOT NULL,
                    times_seen INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(wrong, correct)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vocabulary (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    word TEXT NOT NULL UNIQUE,
                    times_seen INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Passive vocabulary learning (2026-08-24): candidates extracted
            # from the raw-vs-cleaned diff of each dictation wait here until
            # they're seen VOCAB_PROMOTE_AFTER times, then get promoted into
            # `vocabulary`. A 'rejected' row is never bumped or re-proposed.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vocab_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    word TEXT NOT NULL UNIQUE,
                    times_seen INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'pending',
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    sample_raw TEXT,
                    sample_clean TEXT
                )
            """)

            # English practice mode: one row per correction, linked to the
            # transcription it came from (nullable if the save failed).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS english_corrections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transcription_id INTEGER,
                    original TEXT NOT NULL,
                    better TEXT NOT NULL,
                    why TEXT,
                    type TEXT,
                    upgrade TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Shadowing attempts: the phrase to repeat, what was heard, 0-100.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS english_shadowing (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    target TEXT NOT NULL,
                    heard TEXT NOT NULL,
                    score INTEGER NOT NULL
                )
            """)

    def insert_english_shadowing(self, target: str, heard: str, score: int) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO english_shadowing (target, heard, score) VALUES (?, ?, ?)",
                         (target, heard, int(score)))

    def insert_english_corrections(self, transcription_id, corrections: list, upgrade: str = "") -> int:
        """Store the corrections of one English dictation. Returns rows written."""
        if not corrections:
            return 0
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO english_corrections (transcription_id, original, better, why, type, upgrade) VALUES (?, ?, ?, ?, ?, ?)",
                [(transcription_id, c["original"], c["better"], c.get("why", ""), c.get("type", ""), upgrade or None)
                 for c in corrections],
            )
        return len(corrections)

    def get_english_corrections(self, limit: int = 100, since: str = None) -> list:
        """Newest first. `since` is an ISO date/time ('2026-10-20' or '2026-10-20 08:00:00')."""
        sql = "SELECT * FROM english_corrections"
        args: list = []
        if since:
            sql += " WHERE created_at >= ?"
            args.append(since)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(limit)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    @staticmethod
    def _norm_phrase(text: str) -> str:
        """Lowercase, drop punctuation, collapse spaces: 'Main reason.' == 'main reason'."""
        import re
        return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())

    def english_stats(self, days: int = None, top: int = 5, recent: int = 20) -> dict:
        """Aggregates for the 'Your repeated mistakes' panel.

        `days=None` means all time. Repeated mistakes group corrections by the
        normalised `better` phrase; 2+ occurrences count as repeated.
        """
        from datetime import datetime, timedelta, timezone
        since = None
        if days:
            since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        rows = self.get_english_corrections(limit=100000, since=since)  # newest first
        with sqlite3.connect(self.db_path) as conn:
            sql = "SELECT COUNT(*) FROM transcriptions WHERE language = 'en'"
            args: list = []
            if since:
                sql += " AND created_at >= ?"
                args.append(since)
            dictations = conn.execute(sql, args).fetchone()[0]

        by_type: dict = {}
        groups: dict = {}
        for r in rows:
            t = r.get("type") or "grammar"
            by_type[t] = by_type.get(t, 0) + 1
            key = self._norm_phrase(r["better"])
            if not key:
                continue
            g = groups.setdefault(key, {"better": r["better"], "count": 0, "examples": [], "why": "", "type": t})
            g["count"] += 1
            if not g["why"] and r.get("why"):
                g["why"] = r["why"]  # newest first, so this is the most recent explanation
            o = r["original"]
            if len(g["examples"]) < 2 and self._norm_phrase(o) not in [self._norm_phrase(e) for e in g["examples"]]:
                g["examples"].append(o)
        repeated = sorted((g for g in groups.values() if g["count"] >= 2), key=lambda g: -g["count"])[:top]
        return {
            "days": days,
            "dictations": dictations,
            "corrections": len(rows),
            "by_type": by_type,
            "repeated": repeated,
            "recent": rows[:recent],
        }

    def insert(self, text: str, language: str = None, duration_seconds: float = None,
               model: str = "whisper-large-v3-turbo",
               raw_text: str = None, was_cleaned: bool = False,
               app_name: str = None, app_category: str = None,
               audio_path: str = None) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO transcriptions (text, language, duration_seconds, model, raw_text, was_cleaned, app_name, app_category, audio_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (text, language, duration_seconds, model, raw_text, 1 if was_cleaned else 0, app_name, app_category, audio_path),
            )
            return cursor.lastrowid

    def get_by_id(self, transcription_id: int) -> dict | None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM transcriptions WHERE id = ?", (transcription_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_model_and_texts(self, transcription_id: int, text: str, raw_text: str,
                               model: str, was_cleaned: bool = False) -> bool:
        """Used by the dashboard's re-transcribe action."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE transcriptions SET text = ?, raw_text = ?, model = ?, was_cleaned = ? WHERE id = ?",
                (text, raw_text, model, 1 if was_cleaned else 0, transcription_id),
            )
            return cur.rowcount > 0

    def update_texts(self, transcription_id: int, text: str = None, raw_text: str = None) -> bool:
        """Update text and/or raw_text for an existing row. Returns True if a row was changed."""
        fields = {}
        if text is not None:
            fields["text"] = text
        if raw_text is not None:
            fields["raw_text"] = raw_text
        if not fields:
            return False
        set_clause = ", ".join(f"{col} = ?" for col in fields)
        values = list(fields.values()) + [transcription_id]
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                f"UPDATE transcriptions SET {set_clause} WHERE id = ?",
                values,
            )
            return cur.rowcount > 0

    def get_recent(self, limit: int = 20) -> list:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM transcriptions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def search(self, query: str, limit: int = 20) -> list:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM transcriptions WHERE text LIKE ? ORDER BY created_at DESC LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def count(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM transcriptions").fetchone()[0]

    def stats(self) -> dict:
        """Aggregate productivity stats for the dashboard.

        Word counts are computed in Python (SQLite can't split words); with a
        few thousand rows this stays comfortably under a few ms. "Time saved"
        compares dictation audio time against typing the same words at 40 WPM
        (conservative average typing speed).
        """
        from datetime import datetime, timezone

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT text, duration_seconds, created_at FROM transcriptions"
            ).fetchall()

        total_words = 0
        total_seconds = 0.0
        today_words = 0
        today_count = 0
        # created_at is CURRENT_TIMESTAMP → UTC in SQLite; compare in UTC.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for text, dur, created in rows:
            words = len((text or "").split())
            total_words += words
            total_seconds += dur or 0.0
            if created and str(created).startswith(today):
                today_words += words
                today_count += 1

        audio_minutes = total_seconds / 60.0
        avg_wpm = (total_words / audio_minutes) if audio_minutes > 0 else 0.0
        typing_minutes = total_words / 40.0
        saved_minutes = max(0.0, typing_minutes - audio_minutes)

        return {
            "total_dictations": len(rows),
            "total_words": total_words,
            "total_audio_minutes": round(audio_minutes, 1),
            "avg_wpm": round(avg_wpm, 1),
            "saved_minutes": round(saved_minutes, 1),
            "today_dictations": today_count,
            "today_words": today_words,
        }

    def delete_older_than_days(self, days: int) -> int:
        """Delete transcriptions older than N days. Returns count deleted."""
        if days < 0:
            return 0
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "DELETE FROM transcriptions WHERE created_at < datetime('now', ?)",
                (f"-{days} days",),
            )
            conn.commit()
            return cur.rowcount

    # --- Vocabulary (fed into Whisper prompt, never used for substitutions) ---

    def add_vocabulary_word(self, word: str):
        word = word.strip()
        if not word:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO vocabulary (word) VALUES (?)
                ON CONFLICT(word) DO UPDATE SET times_seen = times_seen + 1
            """, (word,))

    def get_vocabulary_words(self, limit: int = 200) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT word FROM vocabulary ORDER BY times_seen DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [row[0] for row in rows]

    def delete_vocabulary_word(self, word: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("DELETE FROM vocabulary WHERE word = ?", (word,))
            return cur.rowcount

    def clear_vocabulary(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("DELETE FROM vocabulary")
            return cur.rowcount

    # --- Vocabulary candidates (passive learning, raw-vs-cleaned diff) ---

    def bump_vocab_candidate(self, word: str, sample_raw: str = None, sample_clean: str = None) -> dict:
        """Upsert a vocabulary candidate.

        Increments `times_seen` and refreshes `last_seen` on every sighting,
        EXCEPT once the row is 'rejected' — that word must never come back,
        so a rejected row is neither incremented nor re-proposed; its status
        is simply returned as-is. Samples are stored only on first insert
        (kept as the earliest, most legible context) and never overwritten.
        """
        word = word.strip()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT word, times_seen, status FROM vocab_candidates WHERE word = ?",
                (word,),
            ).fetchone()
            if row and row["status"] == "rejected":
                return dict(row)
            if row:
                conn.execute(
                    "UPDATE vocab_candidates SET times_seen = times_seen + 1, "
                    "last_seen = CURRENT_TIMESTAMP WHERE word = ?",
                    (word,),
                )
                return {"word": word, "times_seen": row["times_seen"] + 1, "status": row["status"]}
            conn.execute(
                "INSERT INTO vocab_candidates (word, sample_raw, sample_clean) VALUES (?, ?, ?)",
                (word, sample_raw, sample_clean),
            )
            return {"word": word, "times_seen": 1, "status": "pending"}

    def get_vocab_candidates(self, status: str = "pending", limit: int = 100) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM vocab_candidates WHERE status = ? "
                "ORDER BY times_seen DESC, last_seen DESC LIMIT ?",
                (status, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def set_vocab_candidate_status(self, word: str, status: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE vocab_candidates SET status = ? WHERE word = ?",
                (status, word.strip()),
            )
            return cur.rowcount > 0

    def count_vocab_candidates(self, status: str = "pending") -> int:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM vocab_candidates WHERE status = ?", (status,)
            ).fetchone()
            return row[0] if row else 0

    # --- Legacy corrections table (read-only, for inspection only) ---

    def clear_corrections(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("DELETE FROM corrections")
            return cur.rowcount

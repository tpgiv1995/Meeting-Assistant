"""SQLite persistence for meeting sessions, transcripts, summaries, and chat."""
import base64
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from core import paths as paths


def __getattr__(name):
    """Expose ``DB_PATH`` as a live property so callers always see the
    current data folder, even after a runtime migration."""
    if name == "DB_PATH":
        return paths.db_path()
    raise AttributeError(name)


@contextmanager
def _conn():
    db = paths.db_path()
    db.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 3000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT
            );
            CREATE TABLE IF NOT EXISTS transcript_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                text TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'loopback',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS speaker_labels (
                session_id TEXT NOT NULL,
                speaker_key TEXT NOT NULL,
                name TEXT NOT NULL,
                color TEXT,
                PRIMARY KEY (session_id, speaker_key)
            );
        """)
        # Live migrations: add columns / tables to databases created before these versions
        for migration in [
            "ALTER TABLE transcript_segments ADD COLUMN source TEXT NOT NULL DEFAULT 'loopback'",
            "ALTER TABLE transcript_segments ADD COLUMN start_time REAL NOT NULL DEFAULT 0",
            "ALTER TABLE transcript_segments ADD COLUMN end_time REAL NOT NULL DEFAULT 0",
            "ALTER TABLE transcript_segments ADD COLUMN label_override TEXT DEFAULT NULL",
            "ALTER TABLE transcript_segments ADD COLUMN source_override TEXT DEFAULT NULL",
            "ALTER TABLE speaker_labels ADD COLUMN color TEXT",
            "ALTER TABLE speaker_labels ADD COLUMN global_id TEXT DEFAULT NULL",
            # Global cross-session speaker identity tables
            """CREATE TABLE IF NOT EXISTS global_speakers (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                color       TEXT,
                centroid    BLOB,
                emb_count   INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS speaker_embeddings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                global_id    TEXT NOT NULL REFERENCES global_speakers(id) ON DELETE CASCADE,
                session_id   TEXT NOT NULL,
                speaker_key  TEXT NOT NULL,
                embedding    BLOB NOT NULL,
                duration_sec REAL NOT NULL,
                created_at   TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_emb_global  ON speaker_embeddings(global_id)",
            "CREATE INDEX IF NOT EXISTS idx_emb_session ON speaker_embeddings(session_id, speaker_key)",
            # Unlabeled-speaker embeddings: feed the post-meeting cleanup UI's
            # clustering pass. Populated live by _on_fingerprint_audio whenever
            # an embedding is extracted for a speaker_key with no global_id,
            # and backfilled from the session WAV when the cleanup view opens
            # on an older session that pre-dates this table.
            """CREATE TABLE IF NOT EXISTS unlabeled_embeddings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT NOT NULL,
                speaker_key  TEXT NOT NULL,
                embedding    BLOB NOT NULL,
                duration_sec REAL NOT NULL,
                created_at   TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_unlbl_session ON unlabeled_embeddings(session_id, speaker_key)",
            # Per-session noise flag: speakers marked noise are hidden from the
            # cleanup view's main clusters and collapsed into a "Noise" group.
            "ALTER TABLE speaker_labels ADD COLUMN is_noise INTEGER NOT NULL DEFAULT 0",
            # Session folders
            """CREATE TABLE IF NOT EXISTS folders (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            "ALTER TABLE sessions ADD COLUMN folder_id TEXT DEFAULT NULL",
            "ALTER TABLE sessions ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE folders ADD COLUMN parent_id TEXT DEFAULT NULL",
            "ALTER TABLE chat_messages ADD COLUMN attachments TEXT DEFAULT NULL",
            "ALTER TABLE chat_messages ADD COLUMN tool_calls TEXT DEFAULT NULL",
            # Per-session chat system prompt override (NULL = use global/default)
            "ALTER TABLE sessions ADD COLUMN chat_system_prompt TEXT DEFAULT NULL",
            # Per-session summary system prompt override (NULL = use global/default)
            "ALTER TABLE sessions ADD COLUMN summary_system_prompt TEXT DEFAULT NULL",
            # Rich-text notes (Quill Delta JSON). NULL = empty.
            "ALTER TABLE sessions ADD COLUMN notes TEXT DEFAULT NULL",
            "ALTER TABLE sessions ADD COLUMN notes_updated_at TEXT DEFAULT NULL",
            # Title lock: 1 = user manually set the title, auto-gen must skip this row
            "ALTER TABLE sessions ADD COLUMN title_user_set INTEGER NOT NULL DEFAULT 0",
            # Split rollback: sessions created from the same split share a group id
            "ALTER TABLE sessions ADD COLUMN split_group_id TEXT DEFAULT NULL",
            "CREATE INDEX IF NOT EXISTS idx_sessions_split_group ON sessions(split_group_id)",
            # Full-text search on session titles and transcript segments
            """CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
                session_id UNINDEXED,
                kind UNINDEXED,
                text,
                tokenize='porter unicode61'
            )""",
            # v2: recreate FTS with source_id column for segment-level linking
            "DROP TABLE IF EXISTS search_fts",
            """CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
                session_id UNINDEXED,
                source_id UNINDEXED,
                kind UNINDEXED,
                text,
                tokenize='porter unicode61'
            )""",
            # Semantic search embeddings per session
            """CREATE TABLE IF NOT EXISTS session_embeddings (
                session_id TEXT PRIMARY KEY,
                embedding  BLOB NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            # Global chat (cross-session AI conversations)
            """CREATE TABLE IF NOT EXISTS global_chat_conversations (
                id         TEXT PRIMARY KEY,
                title      TEXT NOT NULL DEFAULT 'New Chat',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS global_chat_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL REFERENCES global_chat_conversations(id) ON DELETE CASCADE,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                attachments     TEXT DEFAULT NULL,
                tool_calls      TEXT DEFAULT NULL
            )""",
        ]:
            try:
                conn.execute(migration)
            except Exception:
                pass  # already exists

        # Populate FTS index if it's empty (first migration or rebuild)
        fts_count = conn.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0]
        if fts_count == 0:
            _rebuild_fts(conn)


def _rebuild_fts(conn) -> None:
    """Populate the FTS index from existing sessions and transcript segments."""
    conn.execute("DELETE FROM search_fts")
    # Index session titles
    conn.execute(
        "INSERT INTO search_fts (session_id, source_id, kind, text) "
        "SELECT id, NULL, 'title', title FROM sessions WHERE title IS NOT NULL AND title != ''"
    )
    # Index transcript segments (source_id = segment rowid)
    conn.execute(
        "INSERT INTO search_fts (session_id, source_id, kind, text) "
        "SELECT session_id, id, 'segment', text FROM transcript_segments WHERE text IS NOT NULL AND text != ''"
    )


def fts_index_session_title(session_id: str, title: str) -> None:
    """Add or update a session title in the FTS index."""
    with _conn() as conn:
        conn.execute("DELETE FROM search_fts WHERE session_id = ? AND kind = 'title'",
                     (session_id,))
        if title and title.strip():
            conn.execute("INSERT INTO search_fts (session_id, source_id, kind, text) VALUES (?, NULL, 'title', ?)",
                         (session_id, title))


def fts_index_segment(session_id: str, text: str, segment_id: int | None = None) -> None:
    """Add a transcript segment to the FTS index."""
    if not text or not text.strip():
        return
    with _conn() as conn:
        conn.execute("INSERT INTO search_fts (session_id, source_id, kind, text) VALUES (?, ?, 'segment', ?)",
                     (session_id, segment_id, text))


def fts_remove_session(session_id: str) -> None:
    """Remove all FTS entries for a session."""
    with _conn() as conn:
        conn.execute("DELETE FROM search_fts WHERE session_id = ?", (session_id,))


def search_sessions(query: str, limit: int = 50) -> list[dict]:
    """Search sessions by title and transcript content using FTS5.

    Returns a list of {session_id, title, matches: [{kind, snippet}]} dicts,
    ordered by relevance.
    """
    if not query or not query.strip():
        return []
    # Escape FTS5 special characters and build a prefix query
    terms = query.strip().split()
    fts_query = " ".join(f'"{t}"*' for t in terms if t)
    if not fts_query:
        return []

    with _conn() as conn:
        rows = conn.execute(
            "SELECT f.session_id, f.source_id, f.kind,"
            "       snippet(search_fts, 3, '<mark>', '</mark>', '…', 40) AS snippet,"
            "       rank"
            " FROM search_fts f"
            " WHERE search_fts MATCH ?"
            " ORDER BY rank"
            " LIMIT ?",
            (fts_query, limit * 3),  # over-fetch so we can group
        ).fetchall()

        # Group by session, collect match snippets
        from collections import OrderedDict
        sessions: OrderedDict[str, dict] = OrderedDict()
        for r in rows:
            sid = r["session_id"]
            if sid not in sessions:
                # Look up session title
                title_row = conn.execute("SELECT title FROM sessions WHERE id = ?", (sid,)).fetchone()
                sessions[sid] = {
                    "session_id": sid,
                    "title": title_row["title"] if title_row else "",
                    "matches": [],
                    "best_rank": r["rank"],
                }
            if len(sessions[sid]["matches"]) < 3:  # max 3 snippets per session
                match = {
                    "kind": r["kind"],
                    "snippet": r["snippet"],
                }
                if r["source_id"] is not None:
                    match["segment_id"] = r["source_id"]
                sessions[sid]["matches"].append(match)

    results = list(sessions.values())[:limit]
    return results


def search_speakers(query: str, limit: int = 20) -> list[dict]:
    """Search speaker_labels by name. Returns sessions grouped by speaker match,
    each with a 'participant' kind match entry."""
    if not query or not query.strip():
        return []
    pattern = f"%{query.strip()}%"
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT sl.session_id, sl.name AS speaker_name,
                   s.title, s.started_at
            FROM speaker_labels sl
            JOIN sessions s ON s.id = sl.session_id
            WHERE sl.name LIKE ? COLLATE NOCASE
              AND sl.name != ''
            GROUP BY sl.session_id, lower(sl.name)
            ORDER BY s.started_at DESC
            LIMIT ?
            """,
            (pattern, limit * 3),
        ).fetchall()

    from collections import OrderedDict
    sessions: OrderedDict[str, dict] = OrderedDict()
    for r in rows:
        sid = r["session_id"]
        name = r["speaker_name"]
        # Highlight the matching portion in the speaker name
        idx = name.lower().find(query.strip().lower())
        if idx >= 0:
            snippet = (name[:idx] + "<mark>" + name[idx:idx+len(query.strip())]
                       + "</mark>" + name[idx+len(query.strip()):])
        else:
            snippet = name
        if sid not in sessions:
            sessions[sid] = {
                "session_id": sid,
                "title": r["title"] or "",
                "matches": [],
            }
        if len(sessions[sid]["matches"]) < 3:
            sessions[sid]["matches"].append({
                "kind": "participant",
                "snippet": snippet,
            })

    return list(sessions.values())[:limit]


# ── Semantic embeddings ──────────────────────────────────────────────────────

def save_session_embedding(session_id: str, embedding_bytes: bytes) -> None:
    """Store (or update) the semantic embedding for a session."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO session_embeddings (session_id, embedding, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET embedding=excluded.embedding, updated_at=excluded.updated_at",
            (session_id, embedding_bytes, datetime.utcnow().isoformat()),
        )


def get_all_session_embeddings() -> list[dict]:
    """Return all session embeddings: [{session_id, title, embedding_bytes}]."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT e.session_id, s.title, e.embedding "
            "FROM session_embeddings e "
            "JOIN sessions s ON s.id = e.session_id"
        ).fetchall()
    return [{"session_id": r["session_id"], "title": r["title"],
             "embedding_bytes": bytes(r["embedding"])} for r in rows]


def get_session_text_for_embedding(session_id: str) -> str | None:
    """Get concatenated title + transcript text for computing an embedding."""
    with _conn() as conn:
        session = conn.execute("SELECT title FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not session:
            return None
        segments = conn.execute(
            "SELECT text FROM transcript_segments WHERE session_id = ? ORDER BY start_time",
            (session_id,),
        ).fetchall()
    title = session["title"] or ""
    transcript = " ".join(r["text"] for r in segments if r["text"])
    if not transcript and not title:
        return None
    return f"{title}. {transcript}" if transcript else title


def delete_session_embedding(session_id: str) -> None:
    """Remove the embedding for a session."""
    with _conn() as conn:
        conn.execute("DELETE FROM session_embeddings WHERE session_id = ?", (session_id,))


def get_unembedded_session_ids() -> list[str]:
    """Return session IDs that have transcript segments but no embedding yet."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT s.id FROM sessions s "
            "JOIN transcript_segments ts ON ts.session_id = s.id "
            "WHERE s.id NOT IN (SELECT session_id FROM session_embeddings)"
        ).fetchall()
    return [r["id"] for r in rows]


def _now() -> str:
    return datetime.utcnow().isoformat()


# ── Sessions ──────────────────────────────────────────────────────────────────

def create_session(
    title: str | None = None,
    *,
    started_at: str | None = None,
    ended_at: str | None = None,
    split_group_id: str | None = None,
    session_id: str | None = None,
) -> str:
    """Create a new session row.

    ``started_at`` / ``ended_at`` are ISO timestamps. Both default to ``None``,
    in which case ``started_at`` becomes "now" and the session is treated as
    in-progress until ``end_session`` is called. Pass both when creating a
    derived session (e.g. a split) whose timeline lives inside another
    session's recording window. ``split_group_id`` tags this row as a member
    of a split-rollback group. ``session_id`` pins a specific UUID (used by
    split-restore to reuse a deterministic id when appropriate).
    """
    sid = session_id or str(uuid.uuid4())
    now = _now()
    actual_started = started_at or now
    with _conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, started_at, ended_at, split_group_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, title or f"Meeting {actual_started[:16].replace('T', ' ')}",
             actual_started, ended_at, split_group_id),
        )
    return sid


def get_session_split_group_id(session_id: str) -> str | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT split_group_id FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return (row and row["split_group_id"]) or None


def list_split_group_members(group_id: str) -> list[dict]:
    """Return `{id, title, started_at, ended_at}` for every session in a split
    group, ordered by their original position on the source timeline."""
    if not group_id:
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, title, started_at, ended_at, title_user_set "
            "FROM sessions WHERE split_group_id = ? ORDER BY started_at ASC",
            (group_id,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "title_user_set": bool(r["title_user_set"]),
        }
        for r in rows
    ]


def clear_split_group_for_sessions(session_ids: list[str]) -> None:
    """Detach the given sessions from their split group (used after restore
    when the user chose to keep some parts — they become standalone)."""
    if not session_ids:
        return
    with _conn() as conn:
        placeholders = ",".join("?" * len(session_ids))
        conn.execute(
            f"UPDATE sessions SET split_group_id = NULL WHERE id IN ({placeholders})",
            session_ids,
        )


def end_session(session_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?",
            (_now(), session_id),
        )


def resume_session(session_id: str) -> None:
    """Clear ended_at so a session can be appended to."""
    with _conn() as conn:
        conn.execute(
            "UPDATE sessions SET ended_at = NULL WHERE id = ?",
            (session_id,),
        )


def heal_stale_in_progress(active_session_id: str | None = None) -> int:
    """Set ended_at on any session that's marked in-progress but isn't the
    currently-recording one.

    Stale ``ended_at IS NULL`` rows happen when the app is killed mid-record,
    when a crashed split leaves a partial source row behind, or when other
    edge cases skip the normal stop-recording path. We compute a sensible
    ended_at from the last transcript segment's end_time (preferred) or fall
    back to ``started_at`` (zero-duration). Returns the number of rows fixed.
    """
    with _conn() as conn:
        rows = conn.execute(
            "SELECT s.id, s.started_at,"
            "       (SELECT MAX(end_time) FROM transcript_segments"
            "        WHERE session_id = s.id) AS last_seg"
            " FROM sessions s"
            " WHERE s.ended_at IS NULL"
        ).fetchall()
        fixed = 0
        for r in rows:
            sid = r["id"]
            if active_session_id and sid == active_session_id:
                continue  # actually recording — leave it alone
            started = r["started_at"]
            last_seg = r["last_seg"]
            new_end = None
            if started:
                try:
                    base = datetime.fromisoformat(started)
                    if last_seg and last_seg > 0:
                        new_end = (base + timedelta(seconds=float(last_seg))).isoformat()
                    else:
                        # No segments — call it a zero-duration record so it
                        # at least stops showing "In progress".
                        new_end = base.isoformat()
                except Exception:
                    new_end = _now()
            else:
                new_end = _now()
            conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (new_end, sid),
            )
            fixed += 1
    return fixed


def update_session_title(session_id: str, title: str, *, user_set: bool = False) -> None:
    """Update a session's title.

    Pass ``user_set=True`` when the change originates from a user edit; that
    sets the title-lock flag so future auto-title generation skips this
    session. Pass ``user_set=False`` (default) for AI-generated titles; the
    lock flag is cleared so subsequent auto-gens can run freely.
    """
    with _conn() as conn:
        conn.execute(
            "UPDATE sessions SET title = ?, title_user_set = ? WHERE id = ?",
            (title, 1 if user_set else 0, session_id),
        )
    fts_index_session_title(session_id, title)


def is_title_user_set(session_id: str) -> bool:
    """Return True if the session's title was manually set by the user."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT title_user_set FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return bool(row["title_user_set"]) if row else False


def get_title_generation_context(session_id: str, limit: int = 8) -> dict:
    """Gather context for auto-title generation.

    Returns a dict with:
      - started_at:   ISO timestamp of the current session (for day/time hints)
      - participants: list of {name, global_id} for labelled speakers
      - similar_past_meetings: past titles scored by speaker-overlap,
        day-of-week match, and time-of-day proximity. Highest-scoring first.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT started_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            return {"started_at": None, "participants": [], "similar_past_meetings": []}
        started_at = row["started_at"]

        # Current-session participants (only labelled ones matter for naming)
        part_rows = conn.execute(
            "SELECT name, global_id FROM speaker_labels "
            "WHERE session_id = ? AND name IS NOT NULL AND name != ''",
            (session_id,),
        ).fetchall()
        participants = [
            {"name": r["name"], "global_id": r["global_id"]}
            for r in part_rows
        ]
        participant_global_ids = [p["global_id"] for p in participants if p["global_id"]]

        # Parse current start for day-of-week / time comparisons
        try:
            cur_dt = datetime.fromisoformat(started_at)
            cur_dow = cur_dt.weekday()
            cur_hour = cur_dt.hour + cur_dt.minute / 60.0
        except Exception:
            cur_dt, cur_dow, cur_hour = None, None, None

        # Candidate past sessions: the most recent completed ones with a title
        past = conn.execute(
            "SELECT id, title, started_at FROM sessions "
            "WHERE id != ? AND title IS NOT NULL AND title != '' "
            "ORDER BY started_at DESC LIMIT 50",
            (session_id,),
        ).fetchall()

        scored = []
        for p in past:
            pid = p["id"]
            shared_speakers = 0
            same_dow = False
            hour_delta = None
            score = 0.0

            # Speaker overlap weighted heavily (people → recurring meeting pattern)
            if participant_global_ids:
                placeholders = ",".join("?" * len(participant_global_ids))
                r = conn.execute(
                    f"SELECT COUNT(DISTINCT global_id) AS c FROM speaker_labels "
                    f"WHERE session_id = ? AND global_id IN ({placeholders})",
                    [pid, *participant_global_ids],
                ).fetchone()
                shared_speakers = int(r["c"] or 0)
                score += shared_speakers * 3.0

            # Day-of-week and time-of-day match → weekly cadence signal
            if cur_dt is not None:
                try:
                    p_dt = datetime.fromisoformat(p["started_at"])
                    if p_dt.weekday() == cur_dow:
                        score += 1.0
                        same_dow = True
                    p_hour = p_dt.hour + p_dt.minute / 60.0
                    delta = abs(cur_hour - p_hour)
                    delta = min(delta, 24 - delta)  # wrap across midnight
                    if delta < 2.0:
                        score += (2.0 - delta) * 0.5
                        hour_delta = delta
                except Exception:
                    pass

            if score > 0:
                scored.append({
                    "id": pid,
                    "title": p["title"],
                    "started_at": p["started_at"],
                    "shared_speakers": shared_speakers,
                    "same_dow": same_dow,
                    "hour_delta": hour_delta,
                    "score": round(score, 2),
                })

        scored.sort(key=lambda x: -x["score"])

    return {
        "started_at": started_at,
        "participants": participants,
        "similar_past_meetings": scored[:limit],
    }


def get_session_chat_prompt(session_id: str) -> str | None:
    """Return the per-session chat system prompt override, or None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT chat_system_prompt FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return row["chat_system_prompt"] if row else None


def set_session_chat_prompt(session_id: str, prompt: str | None) -> None:
    """Store a per-session chat system prompt override. Pass None to clear."""
    with _conn() as conn:
        conn.execute(
            "UPDATE sessions SET chat_system_prompt = ? WHERE id = ?",
            (prompt, session_id),
        )


def get_session_summary_prompt(session_id: str) -> str | None:
    """Return the per-session summary system prompt override, or None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT summary_system_prompt FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return row["summary_system_prompt"] if row else None


def set_session_summary_prompt(session_id: str, prompt: str | None) -> None:
    """Store a per-session summary system prompt override. Pass None to clear."""
    with _conn() as conn:
        conn.execute(
            "UPDATE sessions SET summary_system_prompt = ? WHERE id = ?",
            (prompt, session_id),
        )


def get_session_notes(session_id: str) -> dict | None:
    """Return the rich-text notes (Quill Delta JSON) for a session, or None.

    The stored value is a JSON-encoded Quill Delta. Returns the parsed dict,
    or None if no notes exist.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT notes, notes_updated_at FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    if not row or not row["notes"]:
        return None
    try:
        return {
            "delta": json.loads(row["notes"]),
            "updated_at": row["notes_updated_at"],
        }
    except (TypeError, ValueError):
        return None


def set_session_notes(session_id: str, delta: dict | list | None) -> None:
    """Store the rich-text notes (Quill Delta) for a session. Pass None to clear.

    The Delta may be a dict ({"ops": [...]}) or a list of ops. Stored as JSON.
    """
    if delta is None:
        encoded = None
    else:
        encoded = json.dumps(delta, ensure_ascii=False)
    with _conn() as conn:
        conn.execute(
            "UPDATE sessions SET notes = ?, notes_updated_at = ? WHERE id = ?",
            (encoded, _now() if encoded else None, session_id),
        )


def update_session_times(session_id: str, started_at: str | None = None, ended_at: str | None = None) -> None:
    """Update session timestamps.  Pass None to leave a value unchanged."""
    sets = []
    vals = []
    if started_at is not None:
        sets.append("started_at = ?")
        vals.append(started_at)
    if ended_at is not None:
        sets.append("ended_at = ?")
        vals.append(ended_at)
    if not sets:
        return
    vals.append(session_id)
    with _conn() as conn:
        conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", vals)


def rebuild_session_fts(session_id: str) -> None:
    """Rebuild FTS rows for one session from current title and transcript."""
    with _conn() as conn:
        conn.execute("DELETE FROM search_fts WHERE session_id = ?", (session_id,))
        row = conn.execute("SELECT title FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row and row["title"]:
            conn.execute(
                "INSERT INTO search_fts (session_id, source_id, kind, text) VALUES (?, NULL, 'title', ?)",
                (session_id, row["title"]),
            )
        rows = conn.execute(
            "SELECT id, text FROM transcript_segments WHERE session_id = ? AND text != ''",
            (session_id,),
        ).fetchall()
        for seg in rows:
            conn.execute(
                "INSERT INTO search_fts (session_id, source_id, kind, text) VALUES (?, ?, 'segment', ?)",
                (session_id, seg["id"], seg["text"]),
            )


def trim_session_segments(session_id: str, start_sec: float, end_sec: float) -> int:
    """Keep transcript segments overlapping [start_sec, end_sec], shifting to zero."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, start_time, end_time FROM transcript_segments WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        kept = 0
        for r in rows:
            s = float(r["start_time"] or 0.0)
            e = float(r["end_time"] or 0.0)
            if e <= start_sec or s >= end_sec:
                conn.execute("DELETE FROM transcript_segments WHERE id = ?", (r["id"],))
                continue
            ns = max(0.0, s - start_sec)
            ne = max(ns, min(e, end_sec) - start_sec)
            conn.execute(
                "UPDATE transcript_segments SET start_time = ?, end_time = ? WHERE id = ?",
                (ns, ne, r["id"]),
            )
            kept += 1
        conn.execute("DELETE FROM summaries WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM session_embeddings WHERE session_id = ?", (session_id,))
    rebuild_session_fts(session_id)
    return kept


def create_split_session(
    source_session_id: str,
    start_sec: float,
    end_sec: float,
    title: str | None = None,
    *,
    split_group_id: str | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> str:
    """Create a new session containing clamped transcript/speaker data from a range.

    Caller may pass explicit ``started_at`` / ``ended_at`` (preferred — lets
    ``split_session`` compute a single base time and stamp every part
    consistently). If omitted, derive them from the source's ``started_at``
    plus ``start_sec`` / ``end_sec``. Falls back to ``_now()`` only as a
    last resort, with a warning.
    """
    source = get_session(source_session_id)
    if not source:
        raise ValueError("Source session not found")

    new_started_at = started_at
    new_ended_at = ended_at
    if new_started_at is None or new_ended_at is None:
        src_started = source.get("started_at")
        if src_started:
            try:
                base = datetime.fromisoformat(src_started)
                if new_started_at is None:
                    new_started_at = (base + timedelta(seconds=start_sec)).isoformat()
                if new_ended_at is None:
                    new_ended_at = (base + timedelta(seconds=end_sec)).isoformat()
            except Exception as e:
                from core import log as _log
                _log.warn(
                    "storage",
                    f"create_split_session: could not parse source started_at "
                    f"{src_started!r} for {source_session_id[:8]}: {e}; "
                    f"part will use _now() and end up at the wrong time on "
                    f"the timeline.",
                )

    sid = create_session(
        title or f"{source['title']} (split)",
        started_at=new_started_at,
        ended_at=new_ended_at,
        split_group_id=split_group_id,
    )
    used_speakers: set[str] = set()
    with _conn() as conn:
        for seg in source.get("segments", []):
            s = float(seg.get("start_time") or 0.0)
            e = float(seg.get("end_time") or 0.0)
            if e <= start_sec or s >= end_sec:
                continue
            ns = max(0.0, s - start_sec)
            ne = max(ns, min(e, end_sec) - start_sec)
            cur = conn.execute(
                "INSERT INTO transcript_segments "
                "(session_id, text, source, start_time, end_time, label_override, source_override, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    seg.get("text", ""),
                    seg.get("source", "loopback"),
                    ns,
                    ne,
                    seg.get("label_override"),
                    seg.get("source_override"),
                    _now(),
                ),
            )
            seg_id = cur.lastrowid
            if seg.get("text"):
                conn.execute(
                    "INSERT INTO search_fts (session_id, source_id, kind, text) VALUES (?, ?, 'segment', ?)",
                    (sid, seg_id, seg.get("text", "")),
                )
            used_speakers.add(seg.get("source", "loopback"))
            if seg.get("source_override"):
                used_speakers.add(seg["source_override"])

        for profile in source.get("speaker_profiles", []):
            if profile["speaker_key"] in used_speakers:
                conn.execute(
                    "INSERT INTO speaker_labels (session_id, speaker_key, name, color) VALUES (?, ?, ?, ?)",
                    (sid, profile["speaker_key"], profile["name"], profile.get("color")),
                )
    rebuild_session_fts(sid)
    delete_session_embedding(sid)
    return sid


def list_session_ids_in_folder(folder_id: str, *, recursive: bool = True) -> list[str]:
    """Return all session IDs under a folder. Walks subfolders when ``recursive``.

    Used by bulk operations (e.g. "Update titles in folder") so the server is
    the single source of truth about folder membership instead of the client
    snapshot, which can drift.
    """
    with _conn() as conn:
        # Collect all relevant folder IDs (BFS through parent_id graph)
        folder_ids = [folder_id]
        if recursive:
            seen = {folder_id}
            queue = [folder_id]
            while queue:
                next_round = []
                placeholders = ",".join("?" * len(queue))
                rows = conn.execute(
                    f"SELECT id FROM folders WHERE parent_id IN ({placeholders})",
                    queue,
                ).fetchall()
                for r in rows:
                    fid = r["id"]
                    if fid not in seen:
                        seen.add(fid)
                        next_round.append(fid)
                folder_ids.extend(next_round)
                queue = next_round

        placeholders = ",".join("?" * len(folder_ids))
        rows = conn.execute(
            f"SELECT id FROM sessions WHERE folder_id IN ({placeholders}) "
            f"ORDER BY started_at DESC",
            folder_ids,
        ).fetchall()
    return [r["id"] for r in rows]


def delete_session(session_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM search_fts WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM session_embeddings WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM transcript_segments WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM summaries WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM speaker_labels WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    # Clean up WAV file if it exists
    wav_path = paths.audio_dir() / f"{session_id}.wav"
    if wav_path.exists():
        try:
            wav_path.unlink()
        except OSError:
            pass
    video_path = paths.video_dir() / f"{session_id}.mp4"
    if video_path.exists():
        try:
            video_path.unlink()
        except OSError:
            pass
    # Notes attachments live in data_dir()/notes/<session_id>/
    notes_dir = paths.data_dir() / "notes" / session_id
    if notes_dir.exists():
        try:
            import shutil as _shutil
            _shutil.rmtree(notes_dir, ignore_errors=True)
        except OSError:
            pass


def list_sessions() -> list[dict]:
    audio_dir = paths.audio_dir()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT s.id, s.title, s.started_at, s.ended_at,"
            "       s.folder_id, s.sort_order, s.split_group_id,"
            "       (SELECT MAX(ts.end_time) FROM transcript_segments ts"
            "        WHERE ts.session_id = s.id) AS last_segment_time"
            " FROM sessions s ORDER BY s.started_at DESC"
        ).fetchall()
        # Batch-fetch speaker labels with voice-library color fallback (deduplicated by name)
        speaker_rows = conn.execute(
            "SELECT sl.session_id, sl.name, "
            "       COALESCE(gs.color, sl.color) AS color"
            " FROM speaker_labels sl"
            " LEFT JOIN global_speakers gs ON gs.id = sl.global_id"
            " GROUP BY sl.session_id, lower(sl.name)"
            " ORDER BY sl.session_id, lower(sl.name)"
        ).fetchall()
    # Group speakers by session_id
    speakers_by_session: dict[str, list[dict]] = {}
    for sr in speaker_rows:
        sid = sr["session_id"]
        speakers_by_session.setdefault(sid, []).append(
            {"name": sr["name"], "color": sr["color"]}
        )
    return [
        {**dict(r),
         "has_audio": (audio_dir / f"{r['id']}.wav").exists(),
         "speakers": speakers_by_session.get(r["id"], [])}
        for r in rows
    ]


# ── Folders ───────────────────────────────────────────────────────────────────

def list_folders() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, name, sort_order, parent_id, created_at"
            " FROM folders ORDER BY sort_order ASC, created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def create_folder(name: str, parent_id: str | None = None) -> str:
    fid = str(uuid.uuid4())
    now = _now()
    with _conn() as conn:
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) FROM folders WHERE parent_id IS ?",
            (parent_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO folders (id, name, sort_order, parent_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (fid, name.strip(), max_order + 1, parent_id, now, now),
        )
    return fid


def rename_folder(folder_id: str, name: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE folders SET name=?, updated_at=? WHERE id=?",
            (name.strip(), _now(), folder_id),
        )


def delete_folder(folder_id: str, delete_contents: bool = False) -> list[str]:
    """Delete a folder.

    If *delete_contents* is True, recursively delete all child folders and
    their sessions (including WAV files).  Returns the list of deleted
    session IDs so the caller can clear active-session state if needed.

    If False, sessions are uncategorized and child folders are reparented.
    """
    deleted_session_ids: list[str] = []
    with _conn() as conn:
        if delete_contents:
            # Collect all folder IDs to delete (recursive)
            all_folder_ids = []
            stack = [folder_id]
            while stack:
                fid = stack.pop()
                all_folder_ids.append(fid)
                children = conn.execute(
                    "SELECT id FROM folders WHERE parent_id=?", (fid,)
                ).fetchall()
                stack.extend(r["id"] for r in children)

            # Collect and delete sessions in all those folders
            placeholders = ",".join("?" * len(all_folder_ids))
            rows = conn.execute(
                f"SELECT id FROM sessions WHERE folder_id IN ({placeholders})",
                all_folder_ids,
            ).fetchall()
            deleted_session_ids = [r["id"] for r in rows]

            for sid in deleted_session_ids:
                conn.execute("DELETE FROM transcript_segments WHERE session_id=?", (sid,))
                conn.execute("DELETE FROM summaries WHERE session_id=?", (sid,))
                conn.execute("DELETE FROM chat_messages WHERE session_id=?", (sid,))
                conn.execute("DELETE FROM speaker_labels WHERE session_id=?", (sid,))
                conn.execute("DELETE FROM sessions WHERE id=?", (sid,))

            # Delete all collected folders
            conn.execute(
                f"DELETE FROM folders WHERE id IN ({placeholders})",
                all_folder_ids,
            )
        else:
            parent = conn.execute(
                "SELECT parent_id FROM folders WHERE id=?", (folder_id,)
            ).fetchone()
            parent_id = parent["parent_id"] if parent else None
            conn.execute(
                "UPDATE folders SET parent_id=? WHERE parent_id=?",
                (parent_id, folder_id),
            )
            conn.execute("UPDATE sessions SET folder_id=NULL WHERE folder_id=?", (folder_id,))
            conn.execute("DELETE FROM folders WHERE id=?", (folder_id,))

    # Clean up WAV files outside the transaction
    audio_dir = paths.audio_dir()
    for sid in deleted_session_ids:
        wav_path = audio_dir / f"{sid}.wav"
        if wav_path.exists():
            try:
                wav_path.unlink()
            except OSError:
                pass

    return deleted_session_ids


def set_session_folder(session_id: str, folder_id: str | None) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE sessions SET folder_id=? WHERE id=?",
            (folder_id, session_id),
        )


def bulk_set_folder(session_ids: list[str], folder_id: str | None) -> None:
    if not session_ids:
        return
    with _conn() as conn:
        # Assign to folder with sort_order at the end
        max_order = 0
        if folder_id:
            max_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) FROM sessions WHERE folder_id=?",
                (folder_id,),
            ).fetchone()[0]
        conn.executemany(
            "UPDATE sessions SET folder_id=?, sort_order=? WHERE id=?",
            [(folder_id, max_order + 1 + i, sid) for i, sid in enumerate(session_ids)],
        )


def bulk_reorder(folders: list[dict] | None = None,
                 sessions: list[dict] | None = None) -> None:
    """Batch-update sort_order (and parent_id/folder_id) for folders and sessions.

    folders:  list of {id, sort_order, parent_id}
    sessions: list of {id, sort_order, folder_id}
    """
    with _conn() as conn:
        if folders:
            conn.executemany(
                "UPDATE folders SET sort_order=?, parent_id=?, updated_at=? WHERE id=?",
                [(f["sort_order"], f.get("parent_id"), _now(), f["id"]) for f in folders],
            )
        if sessions:
            conn.executemany(
                "UPDATE sessions SET sort_order=?, folder_id=? WHERE id=?",
                [(s["sort_order"], s.get("folder_id"), s["id"]) for s in sessions],
            )


def reset_session_transcript(session_id: str) -> None:
    """Delete transcript and speaker data for a session.

    The sessions row (title, timestamps), chat history, and last-known
    summary are preserved so reanalysis doesn't wipe user-visible data
    that the new transcript will replace anyway.
    """
    with _conn() as conn:
        conn.execute("DELETE FROM transcript_segments WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM speaker_labels WHERE session_id = ?", (session_id,))


def restore_session_snapshot(session_id: str, snapshot: dict) -> None:
    """Replace a session's transcript/speaker/chat/summary state from a snapshot."""
    session = snapshot or {}
    segments = session.get("segments", [])
    speaker_profiles = session.get("speaker_profiles", [])
    chat_messages = session.get("chat_messages", [])
    summary = session.get("summary") or ""

    with _conn() as conn:
        conn.execute(
            "UPDATE sessions SET title = ?, started_at = ?, ended_at = ? WHERE id = ?",
            (
                session.get("title") or f"Meeting {_now()[:16].replace('T', ' ')}",
                session.get("started_at"),
                session.get("ended_at"),
                session_id,
            ),
        )
        conn.execute("DELETE FROM search_fts WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM session_embeddings WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM transcript_segments WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM summaries WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM speaker_labels WHERE session_id = ?", (session_id,))

        for seg in segments:
            conn.execute(
                "INSERT INTO transcript_segments "
                "(session_id, text, source, start_time, end_time, label_override, source_override, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    seg.get("text", ""),
                    seg.get("source", "loopback"),
                    float(seg.get("start_time") or 0.0),
                    float(seg.get("end_time") or 0.0),
                    seg.get("label_override"),
                    seg.get("source_override"),
                    _now(),
                ),
            )

        if summary:
            conn.execute(
                "INSERT INTO summaries (session_id, content, created_at) VALUES (?, ?, ?)",
                (session_id, summary, _now()),
            )

        for msg in chat_messages:
            conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, created_at, attachments, tool_calls) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    msg.get("role", "assistant"),
                    msg.get("content", ""),
                    msg.get("created_at") or _now(),
                    msg.get("attachments"),
                    msg.get("tool_calls"),
                ),
            )

        for profile in speaker_profiles:
            conn.execute(
                "INSERT INTO speaker_labels (session_id, speaker_key, name, color) VALUES (?, ?, ?, ?)",
                (
                    session_id,
                    profile.get("speaker_key"),
                    profile.get("name") or profile.get("speaker_key"),
                    profile.get("color"),
                ),
            )
    rebuild_session_fts(session_id)


def get_session(session_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, title, started_at, ended_at, notes, notes_updated_at "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None

        segments = conn.execute(
            "SELECT id, text, source, start_time, end_time, label_override, source_override "
            "FROM transcript_segments WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()

        summary_row = conn.execute(
            "SELECT content FROM summaries WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()

        messages = conn.execute(
            "SELECT role, content, created_at, attachments, tool_calls FROM chat_messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()

        speaker_labels = conn.execute(
            "SELECT speaker_key, name, color FROM speaker_labels WHERE session_id = ?",
            (session_id,),
        ).fetchall()

    notes_payload = None
    if row["notes"]:
        try:
            notes_payload = {
                "delta": json.loads(row["notes"]),
                "updated_at": row["notes_updated_at"],
            }
        except (TypeError, ValueError):
            notes_payload = None

    return {
        **{k: row[k] for k in ("id", "title", "started_at", "ended_at")},
        "segments": [
            {"id": r["id"], "text": r["text"], "source": r["source"],
             "start_time": r["start_time"], "end_time": r["end_time"],
             "label_override": r["label_override"],
             "source_override": r["source_override"]}
            for r in segments
        ],
        "summary": summary_row["content"] if summary_row else "",
        "chat_messages": [dict(m) for m in messages],
        "speaker_labels": {r["speaker_key"]: r["name"] for r in speaker_labels},
        "speaker_profiles": [
            {"speaker_key": r["speaker_key"], "name": r["name"], "color": r["color"]}
            for r in speaker_labels
        ],
        "notes": notes_payload,
    }


# ── Transcript ────────────────────────────────────────────────────────────────

def get_segment(segment_id: int) -> dict | None:
    """Retrieve a single transcript segment by ID."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, session_id, text, source, start_time, end_time, label_override, source_override "
            "FROM transcript_segments WHERE id = ?",
            (segment_id,),
        ).fetchone()
    return dict(row) if row else None


def get_segments_by_speaker(session_id: str, speaker_key: str) -> list[dict]:
    """Return all segments for a given speaker_key in a session, with timing info."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, start_time, end_time FROM transcript_segments "
            "WHERE session_id = ? AND source = ? ORDER BY id",
            (session_id, speaker_key),
        ).fetchall()
    return [dict(r) for r in rows]


def save_segment(
    session_id: str,
    text: str,
    source: str = "loopback",
    start_time: float = 0.0,
    end_time: float = 0.0,
) -> int:
    """Save a transcript segment. Returns the DB row id."""
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO transcript_segments "
            "(session_id, text, source, start_time, end_time, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, text, source, start_time, end_time, _now()),
        )
        seg_id = cur.lastrowid
        # Index in FTS for cross-session search
        if text and text.strip():
            try:
                conn.execute(
                    "INSERT INTO search_fts (session_id, source_id, kind, text) VALUES (?, ?, 'segment', ?)",
                    (session_id, seg_id, text),
                )
            except Exception:
                pass
        return seg_id


# ── Summary ───────────────────────────────────────────────────────────────────

def save_summary(session_id: str, content: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO summaries (session_id, content, created_at) VALUES (?, ?, ?)",
            (session_id, content, _now()),
        )


# ── Chat ──────────────────────────────────────────────────────────────────────

def update_segment(segment_id: int, text: str, end_time: float) -> None:
    """Update an existing segment's text and end_time (used for merging)."""
    with _conn() as conn:
        conn.execute(
            "UPDATE transcript_segments SET text = ?, end_time = ? WHERE id = ?",
            (text, end_time, segment_id),
        )


def update_segment_source(segment_id: int, source: str) -> None:
    """Update a segment's source/speaker label."""
    with _conn() as conn:
        conn.execute(
            "UPDATE transcript_segments SET source = ? WHERE id = ?",
            (source, segment_id),
        )


def save_segment_label_override(segment_id: int, label: str | None) -> None:
    """Set or clear a per-segment label override."""
    with _conn() as conn:
        conn.execute(
            "UPDATE transcript_segments SET label_override = ? WHERE id = ?",
            (label, segment_id),
        )


def save_segment_source_override(segment_id: int, source_override: str | None) -> None:
    """Set or clear a per-segment speaker-key reassignment."""
    with _conn() as conn:
        conn.execute(
            "UPDATE transcript_segments SET source_override = ? WHERE id = ?",
            (source_override, segment_id),
        )


def get_speaker_profile(session_id: str, speaker_key: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT speaker_key, name, color FROM speaker_labels WHERE session_id = ? AND speaker_key = ?",
            (session_id, speaker_key),
        ).fetchone()
    return dict(row) if row else None


def list_speaker_profiles(session_id: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT speaker_key, name, color FROM speaker_labels "
            "WHERE session_id = ? ORDER BY lower(name), speaker_key",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def save_speaker_label(
    session_id: str,
    speaker_key: str,
    name: str | None = None,
    color: str | None = None,
) -> dict:
    existing = get_speaker_profile(session_id, speaker_key) or {}
    final_name = (name or existing.get("name") or speaker_key).strip()
    final_color = (color.strip() if isinstance(color, str) else existing.get("color"))
    with _conn() as conn:
        conn.execute(
            "INSERT INTO speaker_labels (session_id, speaker_key, name, color) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(session_id, speaker_key) DO UPDATE SET name=excluded.name, color=excluded.color",
            (session_id, speaker_key, final_name, final_color),
        )
    return {
        "speaker_key": speaker_key,
        "name": final_name,
        "color": final_color,
    }


def clear_chat_messages(session_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))


def save_chat_message(session_id: str, role: str, content: str,
                      attachments: str | None = None,
                      tool_calls: str | None = None) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, created_at, attachments, tool_calls)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, role, content, _now(), attachments, tool_calls),
        )


# ── Global Chat Conversations ───────────────────────────────────────────────

def create_global_conversation(title: str = "New Chat") -> str:
    cid = str(uuid.uuid4())
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO global_chat_conversations (id, title, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (cid, title, now, now),
        )
    return cid


def list_global_conversations() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT c.id, c.title, c.created_at, c.updated_at,"
            "       (SELECT COUNT(*) FROM global_chat_messages m"
            "        WHERE m.conversation_id = c.id) AS message_count"
            " FROM global_chat_conversations c"
            " ORDER BY c.updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_global_conversation(conversation_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, title, created_at, updated_at"
            " FROM global_chat_conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if not row:
            return None
        msgs = conn.execute(
            "SELECT id, role, content, created_at, attachments, tool_calls"
            " FROM global_chat_messages"
            " WHERE conversation_id = ?"
            " ORDER BY id ASC",
            (conversation_id,),
        ).fetchall()
    result = dict(row)
    result["messages"] = [dict(m) for m in msgs]
    return result


def save_global_chat_message(conversation_id: str, role: str, content: str,
                             attachments: str | None = None,
                             tool_calls: str | None = None) -> None:
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO global_chat_messages"
            " (conversation_id, role, content, created_at, attachments, tool_calls)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, role, content, now, attachments, tool_calls),
        )
        conn.execute(
            "UPDATE global_chat_conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )


def delete_global_conversation(conversation_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM global_chat_messages WHERE conversation_id = ?",
                     (conversation_id,))
        conn.execute("DELETE FROM global_chat_conversations WHERE id = ?",
                     (conversation_id,))


def rename_global_conversation(conversation_id: str, title: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE global_chat_conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title.strip(), _now(), conversation_id),
        )


def clear_global_chat_messages(conversation_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM global_chat_messages WHERE conversation_id = ?",
                     (conversation_id,))
        conn.execute(
            "UPDATE global_chat_conversations SET updated_at = ? WHERE id = ?",
            (_now(), conversation_id),
        )


def get_dashboard_analytics() -> dict:
    from datetime import timedelta
    with _conn() as conn:
        total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

        # Total recording time (sum of max end_time per session, in seconds)
        total_time_row = conn.execute(
            "SELECT COALESCE(SUM(max_time), 0) FROM"
            " (SELECT MAX(end_time) AS max_time FROM transcript_segments"
            "  GROUP BY session_id)"
        ).fetchone()
        total_seconds = total_time_row[0] or 0

        total_segments = conn.execute(
            "SELECT COUNT(*) FROM transcript_segments"
        ).fetchone()[0]

        # Total word count across all transcripts
        total_words_row = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(text) - LENGTH(REPLACE(text, ' ', '')) + 1), 0)"
            " FROM transcript_segments WHERE text != ''"
        ).fetchone()
        total_words = total_words_row[0] or 0

        speaker_count = conn.execute(
            "SELECT COUNT(*) FROM global_speakers"
        ).fetchone()[0]

        # Sessions this week (last 7 days)
        week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
        sessions_this_week = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE started_at >= ?",
            (week_ago,),
        ).fetchone()[0]

        # Average session duration
        avg_duration_row = conn.execute(
            "SELECT AVG(max_time) FROM"
            " (SELECT MAX(end_time) AS max_time FROM transcript_segments"
            "  GROUP BY session_id)"
        ).fetchone()
        avg_duration_seconds = avg_duration_row[0] or 0

        # Weekly activity (sessions per day for last 14 days)
        two_weeks_ago = (datetime.utcnow() - timedelta(days=13)).strftime("%Y-%m-%d")
        weekly_activity = conn.execute(
            "SELECT DATE(started_at) AS day, COUNT(*) AS count"
            " FROM sessions WHERE DATE(started_at) >= ?"
            " GROUP BY DATE(started_at)"
            " ORDER BY day ASC",
            (two_weeks_ago,),
        ).fetchall()

        # Most active speakers (by number of sessions they appear in)
        # Include total talk-time per speaker
        top_speakers = conn.execute(
            "SELECT gs.name, gs.color,"
            "  COUNT(DISTINCT sl.session_id) AS session_count,"
            "  COALESCE(SUM(ts.end_time - ts.start_time), 0) AS talk_seconds"
            " FROM global_speakers gs"
            " JOIN speaker_labels sl ON sl.global_id = gs.id"
            " LEFT JOIN transcript_segments ts"
            "   ON ts.session_id = sl.session_id AND ts.source = sl.speaker_key"
            " GROUP BY gs.id"
            " ORDER BY session_count DESC"
            " LIMIT 8"
        ).fetchall()

        # Recent sessions with more detail (for the widget)
        recent_sessions = conn.execute(
            "SELECT s.id, s.title, s.started_at,"
            "  (SELECT MAX(ts.end_time) FROM transcript_segments ts"
            "   WHERE ts.session_id = s.id) AS duration_seconds,"
            "  (SELECT COUNT(DISTINCT ts.source) FROM transcript_segments ts"
            "   WHERE ts.session_id = s.id) AS speaker_count"
            " FROM sessions s ORDER BY s.started_at DESC LIMIT 10"
        ).fetchall()

    # Build activity heatmap (fill in missing days)
    activity_map = {r["day"]: r["count"] for r in weekly_activity}
    activity_data = []
    for i in range(14):
        day = (datetime.utcnow() - timedelta(days=13 - i)).strftime("%Y-%m-%d")
        activity_data.append({"day": day, "count": activity_map.get(day, 0)})

    return {
        "total_sessions": total_sessions,
        "total_seconds": total_seconds,
        "total_segments": total_segments,
        "total_words": total_words,
        "speaker_count": speaker_count,
        "sessions_this_week": sessions_this_week,
        "avg_duration_seconds": avg_duration_seconds,
        "activity": activity_data,
        "top_speakers": [dict(r) for r in top_speakers],
        "recent_sessions": [dict(r) for r in recent_sessions],
    }


# ── Import / Export ─────────────────────────────────────────────────────────

EXPORT_FORMAT_VERSION = 1


def export_session_data(session_id: str, include: set[str] | None = None) -> dict | None:
    """Export a session's data as a JSON-serializable dict.

    *include* is a set of data categories to export.  If None, all categories
    are included.  Recognised keys:

        metadata, transcription, summary, chat, speakers, speaker_embeddings

    Media files (audio/video) are handled by the caller since they are large
    binary files that go straight into the zip.
    """
    all_cats = {"metadata", "transcription", "summary", "chat", "speakers", "speaker_embeddings", "notes"}
    cats = all_cats if include is None else (include & all_cats)

    with _conn() as conn:
        row = conn.execute(
            "SELECT id, title, started_at, ended_at, folder_id, notes, notes_updated_at "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None

        pkg: dict = {
            "format_version": EXPORT_FORMAT_VERSION,
            "exported_at": _now(),
            "session_id": session_id,
        }

        if "metadata" in cats:
            pkg["metadata"] = {
                "title": row["title"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
            }

        if "notes" in cats and row["notes"]:
            try:
                pkg["notes"] = {
                    "delta": json.loads(row["notes"]),
                    "updated_at": row["notes_updated_at"],
                }
            except (TypeError, ValueError):
                pass

        if "transcription" in cats:
            segments = conn.execute(
                "SELECT text, source, start_time, end_time, label_override, source_override "
                "FROM transcript_segments WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
            pkg["segments"] = [dict(s) for s in segments]

        if "summary" in cats:
            srow = conn.execute(
                "SELECT content FROM summaries WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            pkg["summary"] = srow["content"] if srow else ""

        if "chat" in cats:
            msgs = conn.execute(
                "SELECT role, content, created_at, attachments, tool_calls "
                "FROM chat_messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
            pkg["chat_messages"] = [dict(m) for m in msgs]

        if "speakers" in cats:
            labels = conn.execute(
                "SELECT speaker_key, name, color, global_id "
                "FROM speaker_labels WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            pkg["speaker_labels"] = [dict(l) for l in labels]

        if "speaker_embeddings" in cats:
            embs = conn.execute(
                "SELECT se.speaker_key, se.embedding, se.duration_sec, "
                "       gs.name AS global_name, gs.color AS global_color "
                "FROM speaker_embeddings se "
                "LEFT JOIN global_speakers gs ON gs.id = se.global_id "
                "WHERE se.session_id = ?",
                (session_id,),
            ).fetchall()
            pkg["speaker_embeddings"] = [
                {
                    "speaker_key": e["speaker_key"],
                    "embedding_b64": base64.b64encode(bytes(e["embedding"])).decode(),
                    "duration_sec": e["duration_sec"],
                    "global_name": e["global_name"],
                    "global_color": e["global_color"],
                }
                for e in embs
            ]

    return pkg


def import_session_data(pkg: dict) -> str:
    """Import a session from an exported package dict.  Returns the new session ID.

    Speaker embeddings are handled separately by the caller (needs the
    fingerprint DB which lives outside storage).
    """
    version = pkg.get("format_version", 0)
    if version < 1:
        raise ValueError("Unsupported or missing format_version")

    sid = str(uuid.uuid4())
    now = _now()

    meta = pkg.get("metadata", {})
    title = meta.get("title") or f"Imported Meeting {now[:16].replace('T', ' ')}"
    started_at = meta.get("started_at") or now
    ended_at = meta.get("ended_at")

    with _conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, started_at, ended_at) VALUES (?, ?, ?, ?)",
            (sid, title, started_at, ended_at),
        )

        # Transcript segments
        for seg in pkg.get("segments", []):
            cur = conn.execute(
                "INSERT INTO transcript_segments "
                "(session_id, text, source, start_time, end_time, label_override, source_override, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    seg.get("text", ""),
                    seg.get("source", "loopback"),
                    float(seg.get("start_time") or 0.0),
                    float(seg.get("end_time") or 0.0),
                    seg.get("label_override"),
                    seg.get("source_override"),
                    now,
                ),
            )
            seg_id = cur.lastrowid
            if seg.get("text", "").strip():
                conn.execute(
                    "INSERT INTO search_fts (session_id, source_id, kind, text) VALUES (?, ?, 'segment', ?)",
                    (sid, seg_id, seg["text"]),
                )

        # Summary
        summary = pkg.get("summary", "")
        if summary:
            conn.execute(
                "INSERT INTO summaries (session_id, content, created_at) VALUES (?, ?, ?)",
                (sid, summary, now),
            )

        # Chat messages
        for msg in pkg.get("chat_messages", []):
            conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, created_at, attachments, tool_calls) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    msg.get("role", "assistant"),
                    msg.get("content", ""),
                    msg.get("created_at") or now,
                    msg.get("attachments"),
                    msg.get("tool_calls"),
                ),
            )

        # Speaker labels (import without global_id links - they'll be re-linked
        # by the fingerprint system on the importing instance)
        for label in pkg.get("speaker_labels", []):
            conn.execute(
                "INSERT INTO speaker_labels (session_id, speaker_key, name, color) VALUES (?, ?, ?, ?)",
                (sid, label["speaker_key"], label.get("name", label["speaker_key"]), label.get("color")),
            )

        # Rich-text notes (Quill Delta)
        notes_payload = pkg.get("notes")
        if isinstance(notes_payload, dict) and notes_payload.get("delta") is not None:
            try:
                conn.execute(
                    "UPDATE sessions SET notes = ?, notes_updated_at = ? WHERE id = ?",
                    (json.dumps(notes_payload["delta"], ensure_ascii=False),
                     notes_payload.get("updated_at") or now,
                     sid),
                )
            except (TypeError, ValueError):
                pass

        # FTS: index title
        if title and title.strip():
            conn.execute(
                "INSERT INTO search_fts (session_id, source_id, kind, text) VALUES (?, NULL, 'title', ?)",
                (sid, title),
            )

    return sid

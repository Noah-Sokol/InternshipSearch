"""
desc: Raw learning-signal log. One row per Noah action; nothing aggregated
    is stored (see model.py for the tally). Owns its own table, created lazily,
    so database/schema.sql is untouched.
args:
    (see functions)
output:
    (functions below)
"""

from datetime import datetime, timezone

from database.db import get_conn

# Points each kind of action is worth when tallied. Edit freely: totals are
# recomputed from raw events, so a change re-counts the whole history.
SIGNAL_WEIGHTS = {"applied": 3, "queued": 2, "up": 1, "down": -1}


def _ensure_table(conn):
    """
    desc: Create the signals table if absent.
    args:
        conn (sqlite3.Connection): open connection from db.get_conn().
    output:
        None
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signals (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL,
            kind           TEXT NOT NULL,
            created_at     TEXT NOT NULL
        )
        """
    )


def record_signal(application_id: int, kind: str, created_at: datetime = None) -> bool:
    """
    desc: Append one signal event. Unknown kinds are ignored (returns False)
        so a typo can't pollute the log.
    args:
        application_id (int): applications.id the action was taken on.
        kind (str): a key of SIGNAL_WEIGHTS.
        created_at (datetime | None): event time (default now, UTC).
    output:
        bool: True if a row was written.
    """
    if kind not in SIGNAL_WEIGHTS:
        return False
    when = (created_at or datetime.now(timezone.utc).replace(tzinfo=None)).isoformat(timespec="seconds")
    with get_conn() as conn:
        _ensure_table(conn)
        conn.execute(
            "INSERT INTO signals (application_id, kind, created_at) VALUES (?,?,?)",
            (application_id, kind, when),
        )
    return True


def load_signals() -> list:
    """
    desc: Read every raw signal event, oldest first.
    args:
        (none)
    output:
        list[dict]: {"id","application_id","kind","created_at" (datetime)}.
    """
    with get_conn() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT id, application_id, kind, created_at FROM signals ORDER BY id"
        ).fetchall()
    out = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["created_at"])
        except (TypeError, ValueError):
            continue
        out.append({"id": r["id"], "application_id": r["application_id"],
                    "kind": r["kind"], "created_at": ts})
    return out


def signals_fingerprint() -> tuple:
    """
    desc: Cheap fingerprint of the log (row count, max id) for score caching.
    args:
        (none)
    output:
        tuple: (count, max_id).
    """
    with get_conn() as conn:
        _ensure_table(conn)
        row = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM signals").fetchone()
    return (row[0], row[1])


def reset_signals() -> int:
    """
    desc: Delete every signal (the Preferences "Reset learning" button).
    args:
        (none)
    output:
        int: rows deleted.
    """
    with get_conn() as conn:
        _ensure_table(conn)
        return conn.execute("DELETE FROM signals").rowcount

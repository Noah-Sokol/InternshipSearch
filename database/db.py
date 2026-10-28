import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone
from config import DB_PATH
from taxonomy import clean_text

_SCHEMA = Path(__file__).resolve().parent / "schema.sql"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the Streamlit review UI's frequent reads proceed without
    # blocking on (or being blocked by) writes from a concurrently-running
    # scraper/main.py process; busy_timeout avoids an immediate "database is
    # locked" error if two connections do collide on a write. Both are cheap,
    # safe to set on every connection, and don't change query semantics.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _add_column(conn, table: str, column_def: str) -> bool:
    """
    desc: Add a column if it's missing. Tolerates another session adding the
        same column at the same moment (two app tabs opening at once).
    args:
        conn (sqlite3.Connection): open connection.
        table (str): table name.
        column_def (str): column name plus type, e.g. "last_seen_at TIMESTAMP".
    output:
        bool: True if this call added the column.
    """
    name = column_def.split()[0]
    if name in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
        return False
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            return False
        raise
    return True


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(_SCHEMA.read_text())
        # migration: add date_posted to jobs if this db predates the column
        _add_column(conn, "jobs", "date_posted TEXT")
        # migration: add last_seen_at to jobs (stale-posting detection)
        if _add_column(conn, "jobs", "last_seen_at TIMESTAMP"):
            # backfill to migration time (not discovered_at): the old backfill flagged
            # every existing posting "possibly closed" until the first scrape.
            conn.execute("UPDATE jobs SET last_seen_at = CURRENT_TIMESTAMP WHERE last_seen_at IS NULL")
        # migration: add score_adjustment to applications
        _add_column(conn, "applications", "score_adjustment INTEGER DEFAULT 0")
        # migration: per-application resume override (a config.RESUME_CHOICES label; NULL = automatic)
        _add_column(conn, "applications", "resume_override TEXT")
        # migration: indexes for the query patterns review/app.py hits on
        # every Streamlit rerun (status filters, the applications->jobs join,
        # and the created_at/updated_at sorts). Also present in schema.sql for
        # brand-new databases; repeated here explicitly (CREATE INDEX IF NOT
        # EXISTS is idempotent) so a pre-existing applications.db gets them
        # the next time init_db() runs, with no need to recreate the file.
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_applications_status_created
                ON applications(status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_applications_status_updated
                ON applications(status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_applications_job_id
                ON applications(job_id);
            CREATE INDEX IF NOT EXISTS idx_applications_updated_at
                ON applications(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_cover_letter_drafts_application_id
                ON cover_letter_drafts(application_id);
            """
        )
    # status rename runs on its own connection/transaction (see its docstring)
    migrate_statuses()
    migrate_graduate_programs()


# Status model (stored value == UI label):
#   discovered -> Discovered tab   queued -> Queue tab (only fillable status)
#   applied / interviewing / offer / rejected -> Applied tab
#   dismissed -> "Not interested"
STATUS_MIGRATION_VERSION = 1   # PRAGMA user_version once the rename has run


GRADUATE_MIGRATION_VERSION = 3   # PRAGMA user_version once graduate postings were dismissed


def _backup_db_file(conn, label: str = "pre_status_rename") -> Path:
    """
    desc: Copy the live DB into a timestamped file under <db dir>/backups/ with
        sqlite's online backup API (WAL-safe). Used before destructive migrations.
    args:
        conn (sqlite3.Connection): open connection to the DB being backed up.
    output:
        Path: the backup file written.
    """
    backup_dir = Path(DB_PATH).parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"applications_{datetime.now():%Y%m%d_%H%M%S}_{label}.db"
    n = 1
    while dest.exists():  # two runs in the same second
        dest = dest.with_name(f"{dest.stem}_{n}{dest.suffix}")
        n += 1
    out = sqlite3.connect(dest)
    try:
        conn.backup(out)
    finally:
        out.close()
    return dest


def migrate_statuses() -> bool:
    """
    desc: One-time, idempotent rename of stored statuses to match UI labels:
        queued->discovered, staged->queued, approved/submitted->applied,
        rejected-with-no-submitted_at (old "Skip")->dismissed. Guarded by
        PRAGMA user_version so the ambiguous 'queued' is never renamed twice.
        Takes a timestamped backup in database/backups/ BEFORE changing
        anything, and only when there are application rows to migrate.
    args:
        none
    output:
        bool: True if a migration ran, False if already up to date.
    """
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        if conn.execute("PRAGMA user_version").fetchone()[0] >= STATUS_MIGRATION_VERSION:
            return False
        has_rows = conn.execute("SELECT 1 FROM applications LIMIT 1").fetchone()
        if has_rows:
            _backup_db_file(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-check under the write lock: a second session that started at
            # the same time must not rename 'queued' a second time.
            if conn.execute("PRAGMA user_version").fetchone()[0] >= STATUS_MIGRATION_VERSION:
                conn.execute("ROLLBACK")
                return False
            # order matters: free the word 'queued' before reusing it
            conn.execute("UPDATE applications SET status='discovered' WHERE status='queued'")
            conn.execute("UPDATE applications SET status='queued' WHERE status='staged'")
            # approved rows never had a submit time; use last update so the
            # Applied tab's Submitted column isn't blank
            conn.execute(
                "UPDATE applications SET status='applied', "
                "submitted_at=COALESCE(submitted_at, updated_at) "
                "WHERE status IN ('approved','submitted')"
            )
            conn.execute(
                "UPDATE applications SET status='dismissed' "
                "WHERE status='rejected' AND submitted_at IS NULL"
            )
            conn.execute(f"PRAGMA user_version = {STATUS_MIGRATION_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return True
    finally:
        conn.close()


def migrate_graduate_programs() -> int | None:
    """
    desc: One-time, idempotent dismissal of existing graduate-only postings
        (classify.is_graduate_only) sitting in 'discovered' or 'queued':
        status -> 'dismissed', notes -> "graduate program". Never touches
        applied/interviewing/offer/rejected/dismissed. Guarded by PRAGMA
        user_version (>= 2 means done); runs only after the status rename
        (user_version >= 1). The guard and the candidate scan are repeated
        inside BEGIN IMMEDIATE so concurrent sessions cannot double-run it.
        Backs up the DB first when there are rows to scan.
    args:
        none
    output:
        int | None: rows dismissed, or None if the migration was not needed
        (already done, or statuses not migrated yet).
    """
    from classify import is_graduate_only
    from taxonomy import GRADUATE_PROGRAM_NOTE

    def _needed(c) -> bool:
        v = c.execute("PRAGMA user_version").fetchone()[0]
        return STATUS_MIGRATION_VERSION <= v < GRADUATE_MIGRATION_VERSION

    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        if not _needed(conn):
            return None
        if conn.execute("SELECT 1 FROM applications LIMIT 1").fetchone():
            _backup_db_file(conn, "pre_graduate_dismiss")
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not _needed(conn):
                conn.execute("ROLLBACK")
                return None
            rows = conn.execute(
                "SELECT a.id, j.title, j.description FROM applications a "
                "JOIN jobs j ON a.job_id = j.id WHERE a.status IN ('discovered','queued')"
            ).fetchall()
            ids = [r["id"] for r in rows if is_graduate_only(r["title"], r["description"])]
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            for app_id in ids:
                conn.execute(
                    "UPDATE applications SET status='dismissed', notes=?, updated_at=? "
                    "WHERE id=? AND status IN ('discovered','queued')",
                    (GRADUATE_PROGRAM_NOTE, now, app_id),
                )
            conn.execute(f"PRAGMA user_version = {GRADUATE_MIGRATION_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return len(ids)
    finally:
        conn.close()


def upsert_job(platform: str, external_id: str, company: str, title: str,
               location: str, url: str, description: str, date_posted: str = "") -> int:
    # Cleanse once at ingestion (smart quotes/dashes -> ASCII, whitespace runs
    # collapsed) so every downstream consumer -- classification, search,
    # dedup, the review UI, CSV export -- works from the same tidy text
    # instead of each scraper's own inconsistent raw output. url/date_posted
    # are structured values, left untouched.
    company = clean_text(company)
    title = clean_text(title)
    location = clean_text(location)
    description = clean_text(description)

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO jobs (platform, external_id, company, title, location, url, description, date_posted,
                              last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(platform, external_id) DO UPDATE SET
                title = excluded.title,
                company = excluded.company,
                description = excluded.description,
                location = excluded.location,
                date_posted = excluded.date_posted,
                last_seen_at = CURRENT_TIMESTAMP
            """,
            (platform, external_id, company, title, location, url, description, date_posted),
        )
        row = conn.execute(
            "SELECT id FROM jobs WHERE platform=? AND external_id=?",
            (platform, external_id),
        ).fetchone()
        job_id = row["id"]

    # Classify once per job here, rather than every downstream consumer
    # (scraper filters, evaluator ranking, review-queue search) re-deriving
    # categories from raw text on every call. A re-scrape that changes a
    # posting's text (the ON CONFLICT DO UPDATE above) recomputes facts too.
    from database.job_facts import compute_and_save
    compute_and_save(job_id, title, company, location, description)
    return job_id


def create_application(job_id: int, cover_letter: str, form_data: dict) -> int:
    from classify import is_graduate_only
    from taxonomy import GRADUATE_PROGRAM_NOTE
    with get_conn() as conn:
        job = conn.execute(
            "SELECT title, description FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        # Graduate-only postings are born dismissed (never discovered/queued)
        grad = bool(job) and is_graduate_only(job["title"], job["description"])
        cur = conn.execute(
            """
            INSERT INTO applications (job_id, cover_letter, form_data, status, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, cover_letter, json.dumps(form_data),
             "dismissed" if grad else "discovered",
             GRADUATE_PROGRAM_NOTE if grad else None),
        )
        app_id = cur.lastrowid
        conn.execute(
            "INSERT INTO cover_letter_drafts (application_id, content, selected) VALUES (?, ?, 1)",
            (app_id, cover_letter),
        )
        return app_id


def _get_by_status(status: str):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT a.*, j.company, j.title, j.location, j.url, j.platform, j.description,
                   j.last_seen_at
            FROM applications a
            JOIN jobs j ON a.job_id = j.id
            WHERE a.status = ?
            ORDER BY a.created_at DESC
            """,
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_discovered_applications():
    """desc: Applications in the Discovered tab (status 'discovered'). output: list[dict]."""
    return _get_by_status("discovered")


def get_queued_applications():
    """desc: Applications in the Queue tab (status 'queued'; the only fillable status). output: list[dict]."""
    return _get_by_status("queued")


_KEEP_CLOSED_NOTE = object()   # update_status default: clear notes, except a closed-posting note


def _utcnow() -> datetime:
    """desc: Naive UTC now, same text format as SQLite CURRENT_TIMESTAMP. output: datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def update_status(application_id: int, status: str, notes=_KEEP_CLOSED_NOTE):
    """
    desc: Set an application's status. 'applied' stamps submitted_at=now;
        moving back to 'queued'/'discovered' (undo) clears submitted_at.
    args:
        application_id (int): applications.id.
        status (str): new status (discovered/queued/applied/interviewing/offer/rejected/dismissed).
        notes (str | None): replaces notes (None clears them). When omitted,
            notes are cleared except a "may be closed" note from the
            description fetcher, which stays with the row.
    output:
        None
    """
    now = _utcnow()
    keep = 1 if notes is _KEEP_CLOSED_NOTE else 0
    if keep:
        notes = None
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE applications
            SET status=?,
                notes=CASE WHEN ? = 1 AND notes LIKE '%may be closed%' THEN notes ELSE ? END,
                updated_at=?,
                submitted_at=CASE WHEN ? = 'applied' THEN ?
                                  WHEN ? IN ('queued', 'discovered') THEN NULL
                                  ELSE submitted_at END
            WHERE id=?
            """,
            (status, keep, notes, now, status, now, status, application_id),
        )


def update_cover_letter(application_id: int, content: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE applications SET cover_letter=?, updated_at=? WHERE id=?",
            (content, _utcnow(), application_id),
        )
        conn.execute(
            "UPDATE cover_letter_drafts SET selected=0 WHERE application_id=?",
            (application_id,),
        )
        version = conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM cover_letter_drafts WHERE application_id=?",
            (application_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO cover_letter_drafts (application_id, version, content, selected) VALUES (?,?,?,1)",
            (application_id, version, content),
        )


def get_saved_cover_letter(application_id: int) -> str:
    """
    desc: Read the cover letter currently saved for an application.
    args:
        application_id (int): applications.id.
    output (str):
        the saved letter, or '' if none / the application doesn't exist.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT cover_letter FROM applications WHERE id=?", (application_id,)
        ).fetchone()
    return ((row["cover_letter"] if row else "") or "").strip()


def save_cover_letter_if_empty(application_id: int, content: str) -> str:
    """
    desc: Save a freshly generated letter ONLY if the application has no saved
        letter yet -- a saved letter is never overwritten by fill-time
        generation. Returns whatever letter is stored afterwards, so a caller
        that lost a race gets the winner's text.
    args:
        application_id (int): applications.id.
        content (str): the generated letter.
    output (str):
        the letter now saved for the application.
    """
    existing = get_saved_cover_letter(application_id)
    if existing:
        return existing
    update_cover_letter(application_id, content)
    return content


def remove_from_queue(application_id: int):
    """Return a queued app back to Discovered so it can be re-selected later (no learning signal)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE applications SET status='discovered', updated_at=? WHERE id=? AND status='queued'",
            (_utcnow(), application_id),
        )


def bump_score(application_id: int, delta: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE applications SET score_adjustment = COALESCE(score_adjustment, 0) + ? WHERE id=?",
            (delta, application_id),
        )


def set_resume_override(application_id: int, label: str | None):
    """
    desc: Set (or clear) the per-application resume override. Only labels in
        config.RESUME_CHOICES are stored; anything else clears it back to
        automatic selection by interest area.
    args:
        application_id (int): applications.id.
        label (str | None): a RESUME_CHOICES label, or None for automatic.
    output:
        None.
    """
    from config import RESUME_CHOICES
    value = label if label in RESUME_CHOICES else None
    now = datetime.now(timezone.utc).replace(tzinfo=None)   # naive UTC, same format as the other writes
    with get_conn() as conn:
        conn.execute(
            "UPDATE applications SET resume_override=?, updated_at=? WHERE id=?",
            (value, now, application_id),
        )


def update_form_data(application_id: int, form_data: dict):
    with get_conn() as conn:
        conn.execute(
            "UPDATE applications SET form_data=?, updated_at=? WHERE id=?",
            (json.dumps(form_data), _utcnow(), application_id),
        )


def get_application(application_id: int):
    """
    desc: One application joined with its job, including the description
        (get_all_applications omits it because it runs on every rerun).
    args:
        application_id (int): applications.id.
    output:
        dict | None: the row, or None if it doesn't exist.
    """
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT a.*, j.company, j.title, j.location, j.url, j.platform, j.description,
                   j.last_seen_at
            FROM applications a JOIN jobs j ON a.job_id = j.id
            WHERE a.id = ?
            """,
            (application_id,),
        ).fetchone()
    return dict(row) if row else None


def get_all_applications(status_filter: str = None):
    with get_conn() as conn:
        if status_filter:
            rows = conn.execute(
                """
                SELECT a.*, j.company, j.title, j.location, j.url, j.platform, j.last_seen_at
                FROM applications a JOIN jobs j ON a.job_id = j.id
                WHERE a.status = ?
                ORDER BY a.updated_at DESC
                """,
                (status_filter,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT a.*, j.company, j.title, j.location, j.url, j.platform, j.last_seen_at
                FROM applications a JOIN jobs j ON a.job_id = j.id
                ORDER BY a.updated_at DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]


def touch_job_seen(platform: str, external_id: str) -> bool:
    """
    desc: Mark a posting as seen right now (jobs.last_seen_at). Called when a
        scrape re-finds a posting that is already stored, so postings that
        vanish from every source age into "possibly closed".
    args:
        platform (str): source platform of the posting.
        external_id (str): the posting's stable ID on that platform.
    output:
        bool: True if a stored job matched and was updated.
    """
    conn = get_conn()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE jobs SET last_seen_at = CURRENT_TIMESTAMP WHERE platform=? AND external_id=?",
                (platform, external_id),
            )
        return cur.rowcount > 0
    finally:
        conn.close()

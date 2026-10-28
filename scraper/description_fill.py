"""
Fill missing job descriptions in the database from the posting pages.

Only rows whose description is empty/short are touched, and a real
description is never overwritten. After a fill the graduate-program filter
and the closed-posting text check are re-run on the updated row. Read-only
on the network (see scraper/description_fetch.py); never applies anywhere.
"""

from datetime import datetime, timezone
from urllib.parse import urlparse

from database.db import get_conn
from scraper.description_fetch import MIN_DESCRIPTION_CHARS, fetch_description
from taxonomy import GRADUATE_PROGRAM_NOTE, clean_text

ACTIVE_STATUSES = ("queued", "discovered")
_BLOCKED_STATUS_ERRORS = ("HTTP 429", "HTTP 999", "HTTP 403", "HTTP 503")
_BLOCK_AFTER = 3          # consecutive 429/999/403/503 from one domain -> stop asking it this run


def _now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")   # aware UTC, stored in the DB's existing text format


def select_candidates(statuses=ACTIVE_STATUSES, limit: int = None, app_ids=None,
                      min_app_id: int = None) -> list:
    """
    desc: Applications whose job has an empty/short description and a URL.
    args:
        statuses (tuple[str]): application statuses to include.
        limit (int | None): max rows (queued first, then oldest id).
        app_ids (list[int] | None): restrict to these application ids.
        min_app_id (int | None): only applications with id greater than this.
    output:
        list[dict]: rows with app_id, job_id, status, notes, title, company,
        location, url, description (one per job).
    """
    sql = (
        "SELECT a.id AS app_id, a.status, a.notes, j.id AS job_id, j.title, j.company, "
        "j.location, j.url, j.description FROM applications a JOIN jobs j ON a.job_id = j.id "
        f"WHERE a.status IN ({','.join('?' for _ in statuses)}) "
        "AND (j.description IS NULL OR length(trim(j.description)) < ?) "
        "AND j.url IS NOT NULL AND trim(j.url) != ''"
    )
    params = [*statuses, MIN_DESCRIPTION_CHARS]
    if app_ids:
        sql += f" AND a.id IN ({','.join('?' for _ in app_ids)})"
        params += list(app_ids)
    if min_app_id is not None:
        sql += " AND a.id > ?"
        params.append(min_app_id)
    sql += " ORDER BY (a.status = 'queued') DESC, a.id"
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    seen, out = set(), []
    for r in rows:
        if r["job_id"] in seen:
            continue
        seen.add(r["job_id"])
        out.append(r)
    return out[:limit] if limit else out


def apply_result(row: dict, result) -> dict:
    """
    desc: Store a fetch result for one row: description (only if the job still
        has none/short), date_posted (only if empty), refreshed job facts, then
        the graduate filter and the closed-posting note.
    args:
        row (dict): a select_candidates row.
        result (DescriptionResult): fetch outcome.
    output:
        dict: {"filled": bool, "dismissed": bool, "closed": str}.
    """
    from classify import is_graduate_only
    from database.job_facts import compute_and_save

    out = {"filled": False, "dismissed": False, "closed": result.closed or ""}
    text = clean_text(result.description) if result.description else ""
    if text:
        with get_conn() as conn:
            cur = conn.execute(
                "UPDATE jobs SET description = ?, "
                "date_posted = CASE WHEN date_posted IS NULL OR date_posted = '' THEN ? ELSE date_posted END "
                "WHERE id = ? AND (description IS NULL OR length(trim(description)) < ?)",
                (text, result.date_posted or "", row["job_id"], MIN_DESCRIPTION_CHARS),
            )
            out["filled"] = cur.rowcount > 0
    if out["filled"]:
        # separate connection/transaction (compute_and_save opens its own)
        compute_and_save(row["job_id"], row["title"], row["company"] or "",
                         row["location"] or "", text)
    with get_conn() as conn:
        if out["filled"]:
            if row["status"] in ACTIVE_STATUSES and is_graduate_only(row["title"], text):
                conn.execute(
                    "UPDATE applications SET status='dismissed', notes=?, updated_at=? "
                    "WHERE id=? AND status IN ('discovered','queued')",
                    (GRADUATE_PROGRAM_NOTE, _now_utc(), row["app_id"]),
                )
                out["dismissed"] = True
        if out["closed"] and not out["dismissed"]:
            note = f"Posting may be closed ({out['closed']})"
            current = conn.execute("SELECT notes FROM applications WHERE id=?",
                                   (row["app_id"],)).fetchone()
            existing = (current["notes"] or "") if current else ""
            if "may be closed" not in existing:
                conn.execute("UPDATE applications SET notes=?, updated_at=? WHERE id=?",
                             (f"{existing} {note}".strip(), _now_utc(), row["app_id"]))
    return out


def fill_rows(rows: list, fetch=None, on_failure=None) -> dict:
    """
    desc: Fetch and store descriptions for candidate rows, one at a time.
    args:
        rows (list[dict]): select_candidates rows.
        fetch (Callable[[str], DescriptionResult] | None): injectable for tests; default fetch_description.
        on_failure (Callable[[dict, str], None] | None): called per failed row.
    output:
        dict: {"tried", "filled", "failed", "dismissed", "closed"} counts.
    """
    fetch = fetch or fetch_description
    stats = {"tried": 0, "filled": 0, "failed": 0, "dismissed": 0, "closed": 0}
    strikes: dict = {}
    for row in rows:
        domain = (urlparse(row["url"]).hostname or "").lower()
        if strikes.get(domain, 0) >= _BLOCK_AFTER:
            stats["failed"] += 1
            if on_failure:
                on_failure(row, "skipped: domain is rate-limiting this run")
            continue
        stats["tried"] += 1
        try:
            result = fetch(row["url"])
        except Exception as e:   # never let one page stop the batch
            result = None
            err = f"{type(e).__name__}"
        if result is not None:
            err = result.error
            strikes[domain] = strikes.get(domain, 0) + 1 if err in _BLOCKED_STATUS_ERRORS else 0
            applied = apply_result(row, result)
            stats["filled"] += applied["filled"]
            stats["dismissed"] += applied["dismissed"]
            stats["closed"] += bool(applied["closed"])
            if applied["filled"]:
                continue
            err = err or "no description found"
        stats["failed"] += 1
        if on_failure:
            on_failure(row, err)
    return stats


def fill_one(app_id: int, fetch=None) -> tuple:
    """
    desc: Fetch the description for a single application (Detail page button).
        Works for any status, but never overwrites a real description.
    args:
        app_id (int): applications.id.
        fetch (Callable): injectable fetcher.
    output:
        tuple[bool, str]: (filled, short message).
    """
    rows = select_candidates(statuses=("queued", "discovered", "applied", "interviewing",
                                       "offer", "rejected", "dismissed"), app_ids=[app_id])
    if not rows:
        return False, "Nothing to fetch (already has a description, or no URL)."
    msgs = []
    stats = fill_rows(rows, fetch=fetch, on_failure=lambda r, e: msgs.append(e))
    if stats["filled"]:
        return True, "Description saved." + (" Graduate program: moved to Not interested."
                                             if stats["dismissed"] else "")
    return False, f"Could not fetch a description ({msgs[0] if msgs else 'unknown'})."

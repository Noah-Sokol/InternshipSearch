"""
desc: Owns a small side-table, job_facts, that persists the ONE canonical
    classify.JobFacts computed for each job row -- so every consumer (scraper
    hard filters, evaluator ranking, the review queue's field= search) reads
    the same, already-computed classification instead of re-deriving it from
    raw text on every call. Mirrors the convention evaluator/score.py already
    established for its own evaluator_scores table: uses database.db.get_conn()
    only for the connection, and never touches database/schema.sql or db.py's
    own schema-init path.

    Unlike evaluator_scores (which depends on tunable weights/today's-date and
    must be invalidated on every Preferences change), JobFacts depends only on
    a job's raw text plus classify.py's own logic -- it never goes stale from
    a Preferences tune, so it's safe to persist rather than recompute per call.
    The one real invalidation trigger (a future classify.py logic change) is
    handled by facts_version: get_facts() recomputes automatically when a
    stored row's facts_version is behind classify.FACTS_VERSION.
args:
    (see individual functions)
output:
    (module of persistence functions over the job_facts table)
"""
import json

from classify import FACTS_VERSION, JobFacts, classify_job
from database.db import _add_column, get_conn


def _ensure_table(conn) -> None:
    """desc: Create job_facts if absent, and migrate in any columns a schema
        change since added (mirrors database/db.py::init_db()'s own
        ALTER-TABLE-if-missing convention for the jobs table).
    args: conn (sqlite3.Connection).
    output: None."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_facts (
            job_id              INTEGER PRIMARY KEY REFERENCES jobs(id),
            facts_version       INTEGER NOT NULL,
            is_internship_title INTEGER NOT NULL,
            canonical_location  TEXT NOT NULL,
            location_segments_json TEXT NOT NULL,
            seniority_reward_json  TEXT NOT NULL,
            seniority_penalty_json TEXT NOT NULL,
            interest_hits_json  TEXT NOT NULL,
            best_interest_area  TEXT,
            best_interest_strength REAL NOT NULL DEFAULT 0,
            eligibility_bucket  TEXT NOT NULL,
            eligibility_rationale TEXT NOT NULL DEFAULT '',
            mentions_target_cycle INTEGER NOT NULL DEFAULT 0,
            ib_excluded         INTEGER NOT NULL DEFAULT 0,
            computed_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(job_facts)")}
    if "all_locations_json" not in existing_cols:
        _add_column(conn, "job_facts", "all_locations_json TEXT NOT NULL DEFAULT '[]'")


def _facts_to_row(job_id: int, facts: JobFacts) -> tuple:
    return (
        job_id, facts.facts_version, int(facts.is_internship_title),
        facts.canonical_location, json.dumps(sorted(facts.all_locations)),
        json.dumps(facts.location_segments),
        json.dumps(facts.seniority_reward_hits), json.dumps(facts.seniority_penalty_hits),
        json.dumps(facts.interest_hits), facts.best_interest_area,
        facts.best_interest_strength,
        facts.eligibility_bucket, facts.eligibility_rationale,
        int(facts.mentions_target_cycle), int(facts.ib_excluded),
    )


def _row_to_facts(row) -> JobFacts:
    return JobFacts(
        facts_version=row["facts_version"],
        is_internship_title=bool(row["is_internship_title"]),
        canonical_location=row["canonical_location"],
        all_locations=frozenset(json.loads(row["all_locations_json"])),
        location_segments=json.loads(row["location_segments_json"]),
        seniority_reward_hits=json.loads(row["seniority_reward_json"]),
        seniority_penalty_hits=json.loads(row["seniority_penalty_json"]),
        interest_hits=json.loads(row["interest_hits_json"]),
        best_interest_area=row["best_interest_area"],
        best_interest_strength=row["best_interest_strength"],
        eligibility_bucket=row["eligibility_bucket"],
        eligibility_rationale=row["eligibility_rationale"],
        mentions_target_cycle=bool(row["mentions_target_cycle"]),
        ib_excluded=bool(row["ib_excluded"]),
    )


def save_facts(job_id: int, facts: JobFacts) -> None:
    """desc: Upsert one job's facts. args: job_id (int), facts (JobFacts).
    output: None."""
    with get_conn() as conn:
        _ensure_table(conn)
        conn.execute(
            """
            INSERT INTO job_facts (
                job_id, facts_version, is_internship_title, canonical_location,
                all_locations_json, location_segments_json,
                seniority_reward_json, seniority_penalty_json,
                interest_hits_json, best_interest_area, best_interest_strength,
                eligibility_bucket, eligibility_rationale,
                mentions_target_cycle, ib_excluded, computed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(job_id) DO UPDATE SET
                facts_version=excluded.facts_version,
                is_internship_title=excluded.is_internship_title,
                canonical_location=excluded.canonical_location,
                all_locations_json=excluded.all_locations_json,
                location_segments_json=excluded.location_segments_json,
                seniority_reward_json=excluded.seniority_reward_json,
                seniority_penalty_json=excluded.seniority_penalty_json,
                interest_hits_json=excluded.interest_hits_json,
                best_interest_area=excluded.best_interest_area,
                best_interest_strength=excluded.best_interest_strength,
                eligibility_bucket=excluded.eligibility_bucket,
                eligibility_rationale=excluded.eligibility_rationale,
                mentions_target_cycle=excluded.mentions_target_cycle,
                ib_excluded=excluded.ib_excluded,
                computed_at=CURRENT_TIMESTAMP
            """,
            _facts_to_row(job_id, facts),
        )


def compute_and_save(job_id: int, title: str, company: str = "", location: str = "",
                      description: str = "", notes: str = "",
                      named_tag: str | None = None) -> JobFacts:
    """desc: Classify a job's raw text and persist the result in one call --
        the integration point database/db.py::upsert_job() calls after every
        upsert. args: job_id (int) plus classify_job()'s raw-text args.
    output: (JobFacts) the computed-and-saved facts."""
    facts = classify_job(title, company, location, description, notes, named_tag)
    save_facts(job_id, facts)
    return facts


def get_facts(job_id: int, title: str = None, company: str = "", location: str = "",
              description: str = "", notes: str = "",
              named_tag: str | None = None) -> JobFacts | None:
    """
    desc: Fetch a job's persisted facts, recomputing (and re-persisting)
        automatically if the stored row is missing or behind the current
        classify.FACTS_VERSION. Recompute requires the raw text args (title
        at minimum) -- if they're not supplied and a recompute is needed,
        returns the stale row rather than raising, so a read-only caller
        without raw text on hand never crashes.
    args:
        job_id (int): the job's row id.
        title, company, location, description, notes, named_tag: raw text for
            a recompute, if one turns out to be needed (all optional).
    output:
        (JobFacts | None): None only if no row exists AND no title was given
            to compute one fresh.
    """
    with get_conn() as conn:
        _ensure_table(conn)
        row = conn.execute("SELECT * FROM job_facts WHERE job_id=?", (job_id,)).fetchone()

    if row is not None and row["facts_version"] >= FACTS_VERSION:
        return _row_to_facts(row)

    if title is not None:
        return compute_and_save(job_id, title, company, location, description, notes, named_tag)

    return _row_to_facts(row) if row is not None else None


def get_facts_bulk(job_ids: list) -> dict:
    """desc: Fetch persisted facts for many jobs in one query (no recompute --
        for bulk read paths like the review queue that already have the raw
        row and can call compute_and_save themselves if they want a refresh).
        Rows behind the current classify.FACTS_VERSION are excluded rather
        than returned half-stale -- callers (review/query_filter.py) already
        treat a missing facts lookup as "fall back to substring" gracefully.
    args: job_ids (list[int]). output: (dict[int, JobFacts])."""
    if not job_ids:
        return {}
    with get_conn() as conn:
        _ensure_table(conn)
        placeholders = ",".join("?" for _ in job_ids)
        rows = conn.execute(
            f"SELECT * FROM job_facts WHERE job_id IN ({placeholders}) AND facts_version >= ?",
            [*job_ids, FACTS_VERSION],
        ).fetchall()
    return {row["job_id"]: _row_to_facts(row) for row in rows}

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,           -- 'linkedin' | 'handshake'
    external_id TEXT,                 -- platform's job id
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    date_posted TEXT,                 -- from schema.org/JobPosting datePosted, when the page provides it
    url TEXT,
    description TEXT,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- bumped whenever a scrape re-finds the posting
    UNIQUE(platform, external_id)
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES jobs(id),
    status TEXT DEFAULT 'discovered', -- discovered | queued | applied | interviewing | offer | rejected | dismissed
    cover_letter TEXT,
    form_data TEXT,                   -- JSON blob of pre-filled fields
    submitted_at TIMESTAMP,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cover_letter_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER REFERENCES applications(id),
    version INTEGER DEFAULT 1,
    content TEXT NOT NULL,
    selected INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes -- review/app.py (Streamlit) re-runs get_discovered_applications(),
-- get_queued_applications(), get_all_applications(), etc. on almost every
-- user interaction, each of which filters applications.status and/or joins
-- on jobs.id and orders by created_at/updated_at. Without these, every one
-- of those calls is a full table scan. CREATE INDEX IF NOT EXISTS makes this
-- safe to (re)run against an existing applications.db -- see db.py's
-- init_db(), which also applies these explicitly as a migration step so a
-- pre-existing DB picks them up without being recreated.
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

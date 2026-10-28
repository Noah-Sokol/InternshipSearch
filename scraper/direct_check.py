"""
Direct-URL checker for named pipeline programs (config.NAMED_TARGETS).

Keyword search on LinkedIn/Handshake often fails to surface named programs
like "Citadel Launch" or "Optiver Quantitative Intern" -- these live on the
company's own careers site. This module visits each target's direct_url
and determines whether a live, applyable posting exists there right now,
without needing any login (these are public pages).

Classifying an arbitrary company career page is inherently fuzzy, so the
"open" bar here is deliberately conservative: a page only counts as open
when it has a real interactive apply control AND the specific program's
distinctive words actually appear on it. Anything short of that falls back
to "watch" (a page that loaded fine but shows no clearly live posting),
which main.py surfaces but does NOT stage as an application. Better to make
Noah look manually than to stage a landing page as if it were a live role.
"""

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from playwright.sync_api import Page

CLOSED_SIGNALS = [
    "no longer accepting applications",
    "position has been filled",
    "this job is no longer available",
    "job not found",
    "page not found",
    "error 404",
    "404 error",
    "404 - page not found",
    "http 404",
]

# Interactive apply controls. We check for real elements, not just the word
# "apply" in body text -- footers, nav, and cookie banners routinely contain
# "apply" on pages that have no live posting at all.
APPLY_SELECTORS = [
    'button:has-text("apply")',
    'a:has-text("apply")',
    '[role="button"]:has-text("apply")',
    'input[type="submit"][value*="apply" i]',
]

# Generic words stripped out before measuring how "specific" a page is to a
# given program -- these appear on nearly every student careers page, so they
# carry no signal that THIS particular role is present.
GENERIC_PROGRAM_WORDS = {
    "intern", "interns", "internship", "internships", "summer", "program",
    "programs", "student", "students", "undergraduate", "campus", "career",
    "careers", "exploration", "opportunity", "opportunities", "the", "and",
    "for", "2024", "2025", "2026", "2027", "2028", "2029",
}

# Preferred containers for the real job description, in priority order.
# Falls back to full body text if none yield substantial content.
DESC_SELECTORS = [
    '[class*="job-description"]',
    '[class*="jobDescription"]',
    '[data-hook="job-description"]',
    'article',
    'main',
    '[role="main"]',
]

DESC_MAX_CHARS = 6000


@dataclass
class DirectCheckResult:
    company: str
    program: str
    url: str
    status: str  # "open" | "watch" | "closed" | "error"
    description: str
    title: str = ""         # from schema.org/JobPosting, when the page provides one
    location: str = ""      # from schema.org/JobPosting jobLocation, when present
    date_posted: str = ""   # from schema.org/JobPosting datePosted, when present


def _has_apply_control(page: Page) -> bool:
    """True if the page exposes a visible, interactive apply button/link."""
    for sel in APPLY_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return True
        except Exception:
            continue
    return False


def _program_tokens(program: str) -> list[str]:
    """Distinctive lowercase words from a program name (drops generic terms)."""
    words = re.findall(r"[a-z0-9]+", program.lower())
    return [w for w in words if len(w) >= 4 and w not in GENERIC_PROGRAM_WORDS]


def _looks_specific(page_text_lower: str, program: str) -> bool:
    """
    True if enough of the program's distinctive words appear on the page.

    Requires ~60% of the distinctive tokens (rounded up, min 1). A specific
    posting page ("Quantitative Researcher - Intern") contains its own role
    words; a generic hub page ("university recruiting") usually does not.
    """
    tokens = _program_tokens(program)
    if not tokens:
        return False
    needed = max(1, math.ceil(0.6 * len(tokens)))
    hits = sum(1 for t in tokens if t in page_text_lower)
    return hits >= needed


def _extract_jobposting_schema(page: Page) -> dict:
    """
    Look for a schema.org/JobPosting block in the page's JSON-LD
    (<script type="application/ld+json">) -- this is the structured data
    site owners embed for Google for Jobs, so when it's present it's a far
    more reliable source for title/location/datePosted than scraping visible
    text. Most career sites don't include it (confirmed only Optiver does
    among current NAMED_TARGETS), so this always degrades gracefully to {}.
    """
    for script in page.query_selector_all('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.inner_text())
        except (json.JSONDecodeError, TypeError):
            continue

        candidates = data if isinstance(data, list) else [data]
        for entry in candidates:
            if isinstance(entry, dict) and entry.get("@graph"):
                candidates.extend(entry["@graph"])

        for entry in candidates:
            if isinstance(entry, dict) and entry.get("@type") == "JobPosting":
                return entry

    return {}


def _job_location_str(job_location) -> str:
    """jobLocation can be a Place/PostalAddress object or a list of them."""
    if isinstance(job_location, list):
        job_location = job_location[0] if job_location else None
    if not isinstance(job_location, dict):
        return ""

    address = job_location.get("address")
    if isinstance(address, dict):
        parts = [address.get("addressLocality"), address.get("addressRegion")]
        joined = ", ".join(p for p in parts if p)
        if joined:
            return joined

    return job_location.get("name", "")


def _extract_description(page: Page) -> str:
    """Pull the real job description, preferring content containers over body."""
    for sel in DESC_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                txt = el.inner_text().strip()
                if len(txt) >= 200:
                    return txt[:DESC_MAX_CHARS]
        except Exception:
            continue
    return page.inner_text("body").strip()[:DESC_MAX_CHARS]


def check_target(page: Page, target: dict) -> DirectCheckResult:
    """
    Visit a single named target's direct_url and classify it.

    'open'   -- a live, specific posting: has an interactive apply control
                AND the program's distinctive words appear on the page
    'watch'  -- page loaded fine but no clearly live posting (a landing/program
                page, or a specific role that hasn't appeared yet)
    'closed' -- page explicitly signals the posting is closed/filled
    'error'  -- couldn't load the page at all
    """
    url = target.get("direct_url")
    if not url:
        return DirectCheckResult(target["company"], target["program"], "", "error", "no direct_url configured")

    # These named targets are JS-heavy career SPAs whose DOMContentLoaded
    # frequently fires late (Optiver reliably ~22s) or flakes under load, so a
    # strict goto-with-timeout would mislabel a perfectly live posting as
    # "error". Instead: navigate leniently (wait only for "commit" -- the
    # response headers), then give the SPA a bounded window to render and read
    # whatever actually loaded. Only a genuinely unreachable page (no usable
    # body text after two attempts) is reported as "error".
    text = ""
    last_err = None
    for attempt in range(2):
        try:
            page.goto(url, wait_until="commit", timeout=45000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=20000)
            except Exception:
                pass  # partial render is still worth reading
            time.sleep(1.5)
            text = page.inner_text("body").strip()
            if text:
                break
        except Exception as e:
            last_err = e
            time.sleep(2)  # brief backoff before the single retry

    if not text:
        return DirectCheckResult(
            target["company"], target["program"], url, "error",
            str(last_err) if last_err else "page returned no readable content",
        )

    lowered = text.lower()
    schema = _extract_jobposting_schema(page)
    schema_title = schema.get("title") or schema.get("name") or ""
    schema_location = _job_location_str(schema.get("jobLocation"))
    schema_date_posted = schema.get("datePosted", "")

    if any(signal in lowered for signal in CLOSED_SIGNALS):
        return DirectCheckResult(
            target["company"], target["program"], url, "closed", text[:1000],
            title=schema_title, location=schema_location, date_posted=schema_date_posted,
        )

    if _has_apply_control(page) and _looks_specific(lowered, target["program"]):
        return DirectCheckResult(
            target["company"], target["program"], url, "open", _extract_description(page),
            title=schema_title, location=schema_location, date_posted=schema_date_posted,
        )

    return DirectCheckResult(
        target["company"], target["program"], url, "watch", text[:1000],
        title=schema_title, location=schema_location, date_posted=schema_date_posted,
    )


def external_id_for(target: dict) -> str:
    """Stable id for a named target so upsert_job dedupes correctly."""
    key = f"{target['company']}::{target['program']}::{target.get('direct_url','')}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def check_all_targets(page: Page, targets: list[dict]) -> list[DirectCheckResult]:
    """
    Check every named target that isn't permanently disqualified
    (eligibility == 'not_eligible' is skipped -- no point re-checking a
    program whose grad-year requirement structurally excludes Noah).
    """
    results = []
    for target in targets:
        if target.get("eligibility") == "not_eligible":
            continue
        print(f"  Checking: {target['company']} - {target['program']}")
        result = check_target(page, target)
        print(f"    -> {result.status}")
        results.append(result)
    return results

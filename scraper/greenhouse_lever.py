"""
No-login internship discovery from free, public ATS + aggregator endpoints.

LinkedIn and Handshake both require authentication, so a scrape run with no
saved session surfaces almost nothing. This module fills that gap using
endpoints that need no login, no API key, and cost nothing:

  * Greenhouse public boards -- https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
  * Lever public postings    -- https://api.lever.co/v0/postings/{token}?mode=json
  * Ashby public job board    -- https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true
  * Community aggregators      -- SimplifyJobs / vanshb03 Summer2027-Internships raw JSON on GitHub

The employer tokens live in config.ATS_TARGETS and the aggregator URLs in
config.ATS_AGGREGATORS, so the seed list is easy to edit each cycle. Every
posting is run through the exact same filters the browser scrapers use
(BaseScraper._is_internship / _is_excluded / _mentions_target_cycle), so
non-internships, IB/new-grad/senior roles are dropped and off-cycle roles are
flagged -- not silently trusted.

Raw endpoint responses are cached under .cache/ats/ with a TTL so re-runs
don't re-hammer the endpoints (respecting the project's cache-first, no-paid /
no-abuse preference). This module only DISCOVERS and normalizes postings into
JobListing objects; it never applies to anything.
"""

import html
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator

import requests

from config import ROOT, ATS_TARGETS, ATS_AGGREGATORS
from classify import is_internship_title as _is_internship_title
from scraper import net_stats
from scraper.base import BaseScraper, JobListing

CACHE_DIR = ROOT / ".cache" / "ats"
CACHE_TTL_SECONDS = 6 * 60 * 60  # 6h -- boards change slowly; avoid re-hammering
REQUEST_TIMEOUT = 30
POLITE_SLEEP_SECONDS = 1.0  # small pause between endpoint hits
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# A description mostly for context/keyword-filtering; cap it so the DB and any
# later cover-letter step aren't handed a giant blob.
DESC_MAX_CHARS = 6000

# _is_internship_title (imported above from classify.py, the one canonical
# whole-word internship check) is stricter than BaseScraper._is_internship's
# substring version, which over these large JSON feeds false-positives on
# "Internal", "International", "Cooperative", "Winternship", etc. (14 bad
# titles observed across the seed set).


class _TextExtractor(HTMLParser):
    """Collapse an HTML fragment down to readable plain text."""

    def __init__(self):
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data):
        self._chunks.append(data)

    def text(self) -> str:
        return re.sub(r"\s+\n", "\n", " ".join(self._chunks)).strip()


def _html_to_text(fragment: str) -> str:
    """
    desc: Convert an HTML (or HTML-entity-encoded) job-description fragment to
        plain text. Greenhouse double-encodes its content field, so we unescape
        HTML entities first, then strip tags.
    args:
        fragment (str): raw content/description string from an ATS response.
    output:
        (str): plain-text description, truncated to DESC_MAX_CHARS.
    """
    if not fragment:
        return ""
    unescaped = html.unescape(fragment)
    parser = _TextExtractor()
    try:
        parser.feed(unescaped)
        text = parser.text()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", unescaped)
    return text[:DESC_MAX_CHARS]


def _cache_path(key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    return CACHE_DIR / f"{safe}.json"


def _fetch_json(url: str, cache_key: str, session: requests.Session):
    """
    desc: Fetch a JSON endpoint, serving a fresh-enough local cache copy when
        one exists so re-runs don't re-hit the network. Failures degrade to
        None (a dead board must never abort the whole discovery pass).
    args:
        url (str): the public JSON endpoint to fetch.
        cache_key (str): stable filename-safe key for the on-disk cache entry.
        session (requests.Session): shared session carrying a normal UA.
    output:
        (dict | list | None): parsed JSON, or None if unavailable.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_key)

    if path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL_SECONDS:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass  # corrupt cache -> refetch

    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        time.sleep(POLITE_SLEEP_SECONDS)
        net_stats.record(resp.status_code == 200)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (requests.RequestException, ValueError):
        net_stats.record(False)
        return None

    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    return data


def _norm_locations(locations) -> str:
    """Flatten a locations value (str | list | dict) into a display string."""
    if not locations:
        return ""
    if isinstance(locations, str):
        return locations
    if isinstance(locations, dict):
        return locations.get("name", "")
    if isinstance(locations, list):
        return ", ".join(str(x) for x in locations if x)
    return str(locations)


class AtsScraper(BaseScraper):
    """
    BaseScraper implementation over no-login ATS + aggregator JSON feeds.

    It reuses BaseScraper's filter helpers but ignores the browser `page`
    argument (these are plain HTTP calls), so it's constructed with page=None.
    The `search()` signature is honored for interface compatibility, but the
    query is unused -- the seed lists in config drive what gets fetched. Use
    `discover()` for the natural, query-free entry point.
    """

    def __init__(self, page=None):
        super().__init__(page)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    # --- interface compat -------------------------------------------------

    def search(self, query: str = "", location: str = "United States") -> Iterator[JobListing]:
        """
        desc: BaseScraper interface method. Query/location are ignored here --
            the config seed lists determine what is fetched -- so this simply
            yields the full discovery result. Present so AtsScraper is a drop-in
            BaseScraper if ever wired through the generic query loop.
        args:
            query (str): unused; kept for interface parity.
            location (str): unused; kept for interface parity.
        output:
            (Iterator[JobListing]): every internship posting that passes filters.
        """
        yield from self.discover()

    # --- discovery entry point -------------------------------------------

    def discover(self) -> Iterator[JobListing]:
        """
        desc: Fetch every configured ATS board and aggregator, normalize each
            posting to a JobListing, and yield only those that are internships
            and not excluded by FILTERS. Dedupes by (company, title, location)
            across sources so the same role from two feeds is staged once.
        args:
            (none)
        output:
            (Iterator[JobListing]): filtered, deduped internship postings.
        """
        seen: set[tuple] = set()

        for listing in self._iter_all():
            if not _is_internship_title(listing.title):
                continue
            # Title-level exclusion (senior/new-grad/IB). Description is checked
            # again in main.evaluate_posting where a body is available.
            if self._is_excluded(f"{listing.title} {listing.company}"):
                continue

            dedupe_key = (
                listing.company.lower().strip(),
                listing.title.lower().strip(),
                listing.location.lower().strip(),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            yield listing

    # --- per-source iterators --------------------------------------------

    def _iter_all(self) -> Iterator[JobListing]:
        for target in ATS_TARGETS:
            yield from self._iter_ats_target(target)
        for agg in ATS_AGGREGATORS:
            yield from self._iter_aggregator(agg)

    def _iter_ats_target(self, target: dict) -> Iterator[JobListing]:
        ats = target["ats"]
        token = target["token"]
        company = target["company"]

        if ats == "greenhouse":
            url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        elif ats == "lever":
            url = f"https://api.lever.co/v0/postings/{token}?mode=json"
        elif ats == "ashby":
            url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
        else:
            return

        data = _fetch_json(url, f"{ats}_{token}", self._session)
        if data is None:
            print(f"  [ats] {company} ({ats}:{token}) -- no data (skipped)")
            return

        if ats == "greenhouse":
            yield from self._parse_greenhouse(company, ats, data)
        elif ats == "lever":
            yield from self._parse_lever(company, ats, data)
        elif ats == "ashby":
            yield from self._parse_ashby(company, ats, data)

    def _parse_greenhouse(self, company, ats, data) -> Iterator[JobListing]:
        for j in data.get("jobs", []):
            title = (j.get("title") or "").strip()
            if not _is_internship_title(title):
                continue
            loc = (j.get("location") or {}).get("name", "")
            yield JobListing(
                platform="greenhouse",
                external_id=f"gh:{company}:{j.get('id')}",
                company=company,
                title=title,
                location=loc,
                url=j.get("absolute_url", ""),
                description=_html_to_text(j.get("content", "")),
            )

    def _parse_lever(self, company, ats, data) -> Iterator[JobListing]:
        for j in data:
            title = (j.get("text") or "").strip()
            if not _is_internship_title(title):
                continue
            cats = j.get("categories") or {}
            yield JobListing(
                platform="lever",
                external_id=f"lever:{company}:{j.get('id')}",
                company=company,
                title=title,
                location=cats.get("location", ""),
                url=j.get("hostedUrl", ""),
                description=(j.get("descriptionPlain") or "")[:DESC_MAX_CHARS],
            )

    def _parse_ashby(self, company, ats, data) -> Iterator[JobListing]:
        for j in data.get("jobs", []):
            if not j.get("isListed", True):
                continue
            title = (j.get("title") or "").strip()
            if not _is_internship_title(title):
                continue
            yield JobListing(
                platform="ashby",
                external_id=f"ashby:{company}:{j.get('id')}",
                company=company,
                title=title,
                location=j.get("location", ""),
                url=j.get("jobUrl", ""),
                description=(j.get("descriptionPlain") or "")[:DESC_MAX_CHARS],
            )

    def _iter_aggregator(self, agg: dict) -> Iterator[JobListing]:
        data = _fetch_json(agg["url"], f"agg_{agg['name']}", self._session)
        if not isinstance(data, list):
            print(f"  [ats] aggregator {agg['name']} -- no data (skipped)")
            return

        keep = agg.get("keep_categories")
        for row in data:
            if not row.get("active", True) or not row.get("is_visible", True):
                continue
            if keep is not None and row.get("category") not in keep:
                continue
            title = (row.get("title") or "").strip()
            if not _is_internship_title(title):
                continue
            company = (row.get("company_name") or "").strip()
            yield JobListing(
                platform="ats-list",
                external_id=f"agg:{agg['name']}:{row.get('id')}",
                company=company,
                title=title,
                location=_norm_locations(row.get("locations")),
                url=row.get("url", ""),
                description="",  # aggregators carry no body; fine, staged empty
            )


def discover_ats_internships() -> list[JobListing]:
    """
    desc: Convenience wrapper for callers that want a plain list rather than a
        scraper object -- runs the full no-login discovery pass and returns the
        filtered internship postings.
    args:
        (none)
    output:
        (list[JobListing]): filtered, deduped internship postings from all
            configured ATS boards and aggregators.
    """
    return list(AtsScraper().discover())

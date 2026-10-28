"""
No-login LinkedIn job discovery via the public guest endpoint.

LinkedIn serves its logged-out ("guest") job search through an undocumented but
public endpoint that needs no account, no cookie, and no browser:

  * Search/list:  https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=...&start=0
  * Per-job body: https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jobId}

Both return HTML fragments. The search fragment yields one card per posting with
the exact title, company, location, and a /jobs/view/{id} hyperlink; the per-job
fragment carries the full description inside .show-more-less-html__markup. This
was confirmed live before the module was written, so discovery + cover-letter
generation both work without ever logging in.

This replaces the authenticated Playwright LinkedIn scraper for DISCOVERY: it is
a drop-in BaseScraper (same search()/fetch_description() surface), so main's
_scrape_platform_query stages guest hits through the identical filter + review
path. Nothing here applies to anything.

Practical constraints honored (cache-first, no paid/no abuse):
  * Every endpoint response is cached under .cache/linkedin_guest/ with a TTL so
    re-runs don't re-hit LinkedIn.
  * A polite pause spaces requests; the per-job body endpoint is the one LinkedIn
    rate-limits first, so descriptions are fetched lazily (on demand) and each
    job id is fetched at most once per run.
"""

import html as html_lib
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator
from urllib.parse import quote_plus, urlparse

import requests

from config import ROOT
from scraper import net_stats
from scraper.base import BaseScraper, JobListing

BASE = "https://www.linkedin.com"
SEARCH_ENDPOINT = BASE + "/jobs-guest/jobs/api/seeMoreJobPostings/search"
JOB_ENDPOINT = BASE + "/jobs-guest/jobs/api/jobPosting"

CACHE_DIR = ROOT / ".cache" / "linkedin_guest"
CACHE_TTL_SECONDS = 6 * 60 * 60  # 6h -- postings change slowly; avoid re-hammering
REQUEST_TIMEOUT = 30
POLITE_SLEEP_SECONDS = 2.0  # the guest body endpoint rate-limits fast; stay slow
_BLOCK_AFTER = 3             # consecutive 403/429/999 -> stop requesting this run
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# Guest search returns ~10 cards per `start` offset. Page through a bounded few
# so the long tail surfaces without turning one query into dozens of requests.
RESULTS_PER_PAGE = 10
MAX_PAGES_PER_QUERY = 4  # start = 0, 10, 20, 30

# Experience-level facet: 1=Internship, 2=Entry level. Mirrors the browser
# scraper's default (many real internships are mistagged "Entry level"); title/
# body checks downstream keep genuine full-time roles out, so this only widens.
EXPERIENCE_LEVELS = "1,2"

DESC_MAX_CHARS = 6000


class _DescriptionParser(HTMLParser):
    """
    Extract readable text from the .show-more-less-html__markup container of a
    guest job-body fragment, tracking div depth so nested markup doesn't cut the
    capture short.
    """

    def __init__(self):
        super().__init__()
        self._capturing = False
        self._depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        if self._capturing:
            self._depth += 1
            return
        cls = dict(attrs).get("class", "") or ""
        if "show-more-less-html__markup" in cls:
            self._capturing = True
            self._depth = 1

    def handle_endtag(self, tag):
        if tag != "div" or not self._capturing:
            return
        self._depth -= 1
        if self._depth <= 0:
            self._capturing = False

    def handle_data(self, data):
        if self._capturing:
            self._chunks.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._chunks)).strip()


def _clean(fragment: str) -> str:
    """Unescape HTML entities and collapse whitespace in a short text field."""
    return re.sub(r"\s+", " ", html_lib.unescape(fragment or "")).strip()


def _cache_path(key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    return CACHE_DIR / f"{safe}.html"


class LinkedInGuestScraper(BaseScraper):
    """
    BaseScraper over LinkedIn's no-login guest endpoints. Constructed with no
    browser page (plain HTTP), so page=None. search() yields internship-titled
    JobListings with a resolvable /jobs/view link; fetch_description() lazily
    pulls each job's body. Job ids are de-duplicated across queries within a run
    so the same posting is never re-yielded or re-fetched.
    """

    def __init__(self, page=None):
        super().__init__(page)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._seen_ids: set[str] = set()
        self._blocked_strikes = 0   # consecutive 403/429/999
        self._halted = False        # set after _BLOCK_AFTER strikes: no more requests this run

    # ------------------------------------------------------------------
    # HTTP + cache
    # ------------------------------------------------------------------

    def _fetch_text(self, url: str, cache_key: str) -> str | None:
        """
        desc: Fetch an HTML fragment, serving a fresh-enough cached copy when one
            exists so re-runs don't re-hit LinkedIn. Failures (including the 429
            rate-limit) degrade to None so a single miss never aborts the run.
        args:
            url (str): the guest endpoint URL.
            cache_key (str): filename-safe key for the on-disk cache entry.
        output (str | None): the fragment text, or None if unavailable.
        """
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cache_path(cache_key)

        if path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL_SECONDS:
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                pass  # unreadable cache -> refetch

        if self._halted:
            return None

        try:
            resp = self._session.get(url, timeout=REQUEST_TIMEOUT)
            time.sleep(POLITE_SLEEP_SECONDS)
            net_stats.record(resp.status_code == 200)
            if resp.status_code != 200:
                if resp.status_code in (403, 429, 999):
                    self._blocked_strikes += 1
                    if self._blocked_strikes >= _BLOCK_AFTER:
                        self._halted = True
                    print(f"  [linkedin-guest] blocked ({resp.status_code}) -- "
                          + ("stopping for this run" if self._halted else "backing off"))
                return None
            self._blocked_strikes = 0
            text = resp.text
        except requests.RequestException:
            net_stats.record(False)
            return None

        try:
            path.write_text(text, encoding="utf-8")
        except OSError:
            pass
        return text

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _build_search_url(self, query: str, location: str, start: int) -> str:
        params = [
            f"keywords={quote_plus(query)}",
            f"location={quote_plus(location)}",
            f"start={start}",
            "sortBy=DD",  # most recent first
        ]
        if EXPERIENCE_LEVELS:
            params.append(f"f_E={quote_plus(EXPERIENCE_LEVELS)}")
        return SEARCH_ENDPOINT + "?" + "&".join(params)

    @staticmethod
    def _extract_job_id(href: str) -> str:
        """
        desc: Pull the numeric job id off a guest /jobs/view href. The guest link
            is a slug ending in the id (".../...-at-company-4443088018?..."); the
            trailing digits of the path are the id.
        args:
            href (str): the card's anchor href.
        output (str): the numeric job id, or "" if none could be read.
        """
        if not href:
            return ""
        path = urlparse(href).path
        m = re.search(r"(\d+)$", path)
        return m.group(1) if m else ""

    def _parse_cards(self, fragment: str) -> list[JobListing]:
        """
        desc: Parse a guest search fragment into JobListings, one per <li> card.
            Permissive: a card is kept only if it has a title and a resolvable
            job id; missing company/location degrade to "" rather than dropping
            the card. easy_apply is left False (not readable here, not relied on
            for any submit path).
        args:
            fragment (str): the raw HTML returned by the search endpoint.
        output (list[JobListing]): listings found on this page (may be empty).
        """
        results: list[JobListing] = []
        # The endpoint returns a flat series of <li> cards; split on the tag so
        # each field regex is scoped to a single card (no cross-card mismatch).
        for block in re.split(r"<li[ >]", fragment)[1:]:
            href_m = re.search(r'href="([^"]*jobs/view[^"]*)"', block)
            if not href_m:
                continue
            href = html_lib.unescape(href_m.group(1)).split("?")[0]
            job_id = self._extract_job_id(href)
            if not job_id:
                continue

            title_m = re.search(
                r'base-search-card__title"[^>]*>(.*?)</', block, re.S)
            company_m = re.search(
                r'base-search-card__subtitle"[^>]*>(?:\s*<a[^>]*>)?(.*?)</', block, re.S)
            loc_m = re.search(
                r'job-search-card__location"[^>]*>(.*?)</', block, re.S)

            title = _clean(title_m.group(1)) if title_m else ""
            if not title:
                continue

            results.append(JobListing(
                platform="linkedin",
                external_id=job_id,
                company=_clean(company_m.group(1)) if company_m else "",
                title=title,
                location=_clean(loc_m.group(1)) if loc_m else "",
                url=f"{BASE}/jobs/view/{job_id}/",
                description="",  # fetched on demand
                easy_apply=False,
            ))
        return results

    def search(self, query: str, location: str = "United States") -> Iterator[JobListing]:
        """
        desc: Search the guest endpoint for a keyword query and yield internship
            JobListings, paging up to MAX_PAGES_PER_QUERY. Title-level internship
            and exclusion filtering happen here; body-level exclusion and human
            review happen downstream. Job ids already yielded earlier in this
            run (from any query) are skipped so nothing is staged or fetched
            twice.
        args:
            query (str): free-text search keywords.
            location (str): location filter (default "United States").
        output (Iterator[JobListing]): de-duplicated internship listings whose
            titles pass _is_internship and are not caught by _is_excluded.
        """
        for page_num in range(MAX_PAGES_PER_QUERY):
            start = page_num * RESULTS_PER_PAGE
            url = self._build_search_url(query, location, start)
            fragment = self._fetch_text(url, f"search_{quote_plus(query)}_{start}")
            if not fragment:
                break

            cards = self._parse_cards(fragment)
            if not cards:
                break

            for job in cards:
                if job.external_id in self._seen_ids:
                    continue
                if not self._is_internship(job.title):
                    continue
                if self._is_excluded(job.title):
                    continue
                self._seen_ids.add(job.external_id)
                yield job

    # ------------------------------------------------------------------
    # Description
    # ------------------------------------------------------------------

    def fetch_description(self, job: JobListing) -> str:
        """
        desc: Fetch a single job's body from the guest jobPosting endpoint and
            return its description text (from .show-more-less-html__markup).
            Best-effort and cache-first: returns "" if the body is unavailable
            (e.g. rate-limited) rather than raising, so a miss never aborts the
            surrounding stage loop.
        args:
            job (JobListing): the listing whose description should be fetched
                (job.external_id is the guest job id).
        output (str): the stripped description text (capped at DESC_MAX_CHARS),
            or "" if unavailable.
        """
        job_id = job.external_id
        if not job_id:
            return ""
        fragment = self._fetch_text(f"{JOB_ENDPOINT}/{job_id}", f"job_{job_id}")
        if not fragment:
            return ""
        parser = _DescriptionParser()
        try:
            parser.feed(fragment)
            text = parser.text()
        except Exception:
            text = ""
        if not text:
            # Fallback: strip all tags if the markup class wasn't found.
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_lib.unescape(fragment))).strip()
        return text[:DESC_MAX_CHARS]

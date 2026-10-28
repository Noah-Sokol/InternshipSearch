import time
import json
from typing import Iterator
from urllib.parse import urlparse, quote_plus
from playwright.sync_api import Page
from scraper.base import BaseScraper, JobListing
from config import HANDSHAKE_SESSION, SESSION_DIR


# ----------------------------------------------------------------------
# Module-level search tunables (see scraper/linkedin.py for the rationale on
# keeping these here rather than in config.py; they could be promoted later).
#
# Handshake is a React single-page app: the job list is rendered client-side
# after the initial navigation and virtualizes/lazy-loads as you scroll, so
# the two things that most limited breadth previously were (a) not waiting long
# enough / not scrolling for cards to mount, and (b) a single brittle
# data-hook selector set that drops every card if one attribute name changes.
# ----------------------------------------------------------------------

# Restrict to Handshake's internship job-type facet. Kept ON (Noah only wants
# internships) but made explicit/toggleable. Downstream title/body checks are
# the real guarantee, so if the facet code ever changes this can be turned off
# to fall back to keyword-only search without losing exclusion safety.
RESTRICT_TO_INTERNSHIPS = True
INTERNSHIP_JOB_TYPE = "1"  # Handshake job_type facet code for "Internship"

# Results requested per page and max pages paged through per query.
PER_PAGE = 25
MAX_PAGES_PER_QUERY = 8

# How long (seconds) to wait/poll for SPA job cards to render before giving up
# on a page. Handshake can take several seconds to hydrate the list.
CARD_RENDER_TIMEOUT = 12


class HandshakeScraper(BaseScraper):
    BASE = "https://app.joinhandshake.com"

    def __init__(self, page: Page):
        super().__init__(page)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def save_session(self):
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        storage = self.page.context.storage_state()
        HANDSHAKE_SESSION.write_text(json.dumps(storage))
        print("[handshake] session saved")

    @staticmethod
    def load_session_state() -> dict | None:
        if HANDSHAKE_SESSION.exists():
            return json.loads(HANDSHAKE_SESSION.read_text())
        return None

    def is_logged_in(self) -> bool:
        """
        desc: Confirm the Handshake session is authenticated by loading the
            student jobs page and checking the resulting URL *path* only. When
            logged out, Handshake redirects to "/login?redirect=.../stu/jobs",
            which still contains the substring "stu/jobs"; matching on the path
            prevents that false positive (which previously caused silent
            logged-out guest scraping).
        args: (none)
        output (bool): True if the resolved path is under /stu/jobs or
            /stu/dashboard (logged in), False otherwise.
        """
        self.page.goto(f"{self.BASE}/stu/jobs", wait_until="domcontentloaded")
        time.sleep(2)
        path = urlparse(self.page.url).path
        return path.startswith("/stu/jobs") or path.startswith("/stu/dashboard")

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _build_search_url(self, query: str, page: int) -> str:
        """
        desc: Build a Handshake student job-search URL for a keyword query and
            page number, applying the internship job-type facet only when
            RESTRICT_TO_INTERNSHIPS is set.
        args:
            query (str): free-text search keywords.
            page (int): 1-based result page number.
        output (str): a fully-formed Handshake /stu/jobs search URL.
        """
        params = [
            f"query={quote_plus(query)}",
            f"page={page}",
            f"per_page={PER_PAGE}",
            "sort_direction=desc",
            "sort_column=relevant",
        ]
        if RESTRICT_TO_INTERNSHIPS:
            params.append(f"job_type={INTERNSHIP_JOB_TYPE}")
        return f"{self.BASE}/stu/jobs?" + "&".join(params)

    def search(self, query: str, location: str = "") -> Iterator[JobListing]:
        """
        desc: Search Handshake for a keyword query and yield internship
            JobListings, paging through results up to MAX_PAGES_PER_QUERY. Waits
            for the SPA to render cards on each page. Title-level internship and
            exclusion filtering happens here; body-level exclusion and human
            review happen downstream.
        args:
            query (str): free-text search keywords.
            location (str): unused (Handshake location filtering is account /
                facet driven); kept for BaseScraper signature compatibility.
        output (Iterator[JobListing]): de-duplicated internship listings whose
            titles pass _is_internship and are not caught by _is_excluded.
        """
        seen = set()

        for page_num in range(1, MAX_PAGES_PER_QUERY + 1):
            # Navigate by URL each page rather than clicking a "next" control:
            # URL-driven paging is far more robust than depending on a single
            # data-hook button that the SPA may rename or disable.
            self.page.goto(self._build_search_url(query, page_num),
                           wait_until="domcontentloaded")
            self._wait_for_cards()

            listings = self._parse_current_page()
            if not listings:
                break

            new_on_page = 0
            for job in listings:
                if job.external_id in seen:
                    continue
                if not self._is_internship(job.title):
                    continue
                if self._is_excluded(job.title):
                    continue
                seen.add(job.external_id)
                new_on_page += 1
                yield job

            # If a whole page produced no *new* ids, we've likely run past the
            # real results (Handshake sometimes repeats the last page), so stop.
            if new_on_page == 0:
                break

    def _wait_for_cards(self):
        """
        desc: Wait for Handshake's SPA to render job cards after navigation,
            scrolling to trigger lazy loading. Polls up to CARD_RENDER_TIMEOUT
            seconds for any known card selector to appear. Best-effort: returns
            regardless of outcome so parsing still runs.
        args: (none)
        output (None): waits/scrolls as a side effect.
        """
        deadline = time.time() + CARD_RENDER_TIMEOUT
        while time.time() < deadline:
            if self._first_card_selector_that_matches():
                break
            time.sleep(0.5)
        # Scroll to force any virtualized/lazy cards into the DOM.
        try:
            for _ in range(5):
                self.page.mouse.wheel(0, 2500)
                time.sleep(0.4)
            self.page.mouse.wheel(0, -15000)
            time.sleep(0.3)
        except Exception:
            pass

    # Candidate card container selectors, tried in order. Only the
    # data-hook="jobs-card" set was ever confirmed against live markup; the
    # others are defensive fallbacks in case Handshake renames its test hooks
    # (data-hook attributes are test-only and change without notice). This set
    # NEEDS A LOGGED-IN LIVE CHECK to confirm which selector is current.
    _CARD_SELECTORS = [
        '[data-hook="jobs-card"]',
        '[data-hook="job-result-card"]',
        'div[data-hook*="job"][data-hook*="card"]',
        'a[href*="/jobs/"][data-hook]',
        'a[href*="/stu/jobs/"]',
    ]

    # Per-field candidate selectors within a card, tried in order. Where a
    # data-hook may have drifted we fall back to structural/anchor selectors.
    _TITLE_SELECTORS = [
        '[data-hook="jobs-card-title"]',
        '[data-hook="job-title"]',
        'h3',
        'a[href*="/jobs/"]',
    ]
    _COMPANY_SELECTORS = [
        '[data-hook="jobs-card-employer-name"]',
        '[data-hook="employer-name"]',
        '[data-hook*="employer"]',
    ]
    _LOCATION_SELECTORS = [
        '[data-hook="jobs-card-location"]',
        '[data-hook*="location"]',
    ]

    def _first_card_selector_that_matches(self) -> str | None:
        """
        desc: Return the first card container selector from _CARD_SELECTORS that
            matches at least one element on the current page.
        args: (none)
        output (str | None): the matching selector string, or None if no card
            selector matched.
        """
        for sel in self._CARD_SELECTORS:
            if self.page.query_selector(sel):
                return sel
        return None

    @staticmethod
    def _text_of(el) -> str:
        """
        desc: Safely extract stripped inner text from a Playwright element
            handle that may be None.
        args:
            el: a Playwright ElementHandle or None.
        output (str): the element's stripped inner text, or "" if el is None or
            extraction fails.
        """
        if not el:
            return ""
        try:
            return el.inner_text().strip()
        except Exception:
            return ""

    def _first_text(self, card, selectors: list[str]) -> str:
        """
        desc: Try each selector in order against a card element and return the
            first non-empty stripped text found.
        args:
            card: a Playwright ElementHandle for a job card.
            selectors (list[str]): candidate CSS selectors, most-specific first.
        output (str): first non-empty text, or "" if none matched.
        """
        for sel in selectors:
            text = self._text_of(card.query_selector(sel))
            if text:
                return text
        return ""

    @staticmethod
    def _extract_job_id(href: str) -> str:
        """
        desc: Derive a stable Handshake job id from a card link href, handling
            both "/jobs/<id>" and "/stu/jobs/<id>" forms and stripping any
            query string.
        args:
            href (str): the anchor href pulled off a job card.
        output (str): the extracted job id, or "" if no id could be parsed.
        """
        if not href:
            return ""
        if "/jobs/" in href:
            return href.split("/jobs/")[-1].split("?")[0].split("/")[0]
        return ""

    def _parse_current_page(self) -> list[JobListing]:
        """
        desc: Parse all job cards on the currently loaded Handshake results page
            into JobListings, using the first matching card selector and trying
            multiple per-field selectors. Parsing is permissive: a card is kept
            as long as a title and a resolvable job id are found; missing
            company/location degrade to empty strings rather than dropping the
            card, so real results aren't silently discarded when data-hook
            attributes drift.
        args: (none)
        output (list[JobListing]): listings found on the page (may be empty).
        """
        results = []
        card_sel = self._first_card_selector_that_matches()
        if not card_sel:
            return results

        cards = self.page.query_selector_all(card_sel)

        for card in cards:
            try:
                title = self._first_text(card, self._TITLE_SELECTORS)
                company = self._first_text(card, self._COMPANY_SELECTORS)
                location = self._first_text(card, self._LOCATION_SELECTORS)

                # Link: the card may itself be an anchor, or contain one.
                link_el = card.query_selector('a[href*="/jobs/"]') or \
                    card.query_selector('a')
                href = ""
                if link_el:
                    href = link_el.get_attribute("href") or ""
                if not href:
                    # Card element might itself carry the href.
                    href = card.get_attribute("href") or ""

                job_id = self._extract_job_id(href)
                if not title or not job_id:
                    continue

                full_url = (
                    href if href.startswith("http")
                    else f"{self.BASE}/stu/jobs/{job_id}"
                )

                results.append(JobListing(
                    platform="handshake",
                    external_id=job_id,
                    company=company,
                    title=title,
                    location=location,
                    url=full_url,
                    description="",
                    easy_apply=False,
                ))
            except Exception:
                continue

        return results

    # Description containers, tried in order. Only data-hook="job-description"
    # was confirmed; the rest are defensive fallbacks -- NEEDS A LOGGED-IN LIVE
    # CHECK to confirm the current container.
    _DESCRIPTION_SELECTORS = [
        '[data-hook="job-description"]',
        '[data-hook="details-body"]',
        '[data-hook*="description"]',
        'section[aria-label*="escription"]',
        'article',
    ]

    def fetch_description(self, job: JobListing) -> str:
        """
        desc: Load a single Handshake job's detail page and return its
            description text, trying each candidate container in order and
            waiting briefly for the SPA to render. Best-effort: returns "" if
            none match rather than raising.
        args:
            job (JobListing): the listing whose description should be fetched
                (job.url is navigated to).
        output (str): the stripped description text, or "" if no container
            matched.
        """
        self.page.goto(job.url, wait_until="domcontentloaded")
        time.sleep(2)  # allow SPA detail view to hydrate
        try:
            self.page.mouse.wheel(0, 1200)
            time.sleep(0.4)
        except Exception:
            pass
        for sel in self._DESCRIPTION_SELECTORS:
            text = self._text_of(self.page.query_selector(sel))
            if text:
                return text
        return ""

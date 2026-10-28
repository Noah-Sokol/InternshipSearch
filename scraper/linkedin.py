import time
import json
from typing import Iterator
from urllib.parse import urlparse, quote_plus
from playwright.sync_api import Page
from scraper.base import BaseScraper, JobListing
from config import LINKEDIN_SESSION, SESSION_DIR


# ----------------------------------------------------------------------
# Module-level search tunables.
#
# These live here rather than in config.py because this task is scoped to
# scraper/*.py only; each could be promoted to config.FILTERS / a dedicated
# search-config block later without changing call sites.
#
# The single biggest breadth killer in the previous version was forcing
# `f_AL=true` (LinkedIn "Easy Apply" only) on every query. The large majority
# of internships -- especially the named quant/finance/data-science pipeline
# programs Noah targets -- are NOT Easy Apply; they route out to an employer
# ATS. Restricting to Easy Apply therefore discarded most real postings before
# they were ever seen. We now default to NOT restricting so far more companies
# surface. Every hit still goes through fetch_description -> evaluate_posting
# and the human review queue before anything is ever applied to, so widening
# discovery here does not weaken any safety/exclusion guarantee.
# ----------------------------------------------------------------------

# If True, append LinkedIn's f_AL=true ("Easy Apply only") filter. Default
# False so non-Easy-Apply internships (the majority) are also discovered.
RESTRICT_TO_EASY_APPLY = False

# If True, append LinkedIn's f_E experience-level filter. LinkedIn's codes:
# 1=Internship, 2=Entry level, 3=Associate, 4=Mid-Senior, 5=Director,
# 6=Executive. We request Internship + Entry level, because a lot of genuine
# internships are mistagged by employers as "Entry level" and were being
# dropped by an internship-only (f_E=1) filter. Title/body internship checks
# (_is_internship / _is_excluded) still keep non-internship entry-level roles
# out, so this only widens, it does not let full-time roles through review.
RESTRICT_EXPERIENCE_LEVEL = True
EXPERIENCE_LEVELS = ["1", "2"]  # Internship, Entry level

# Max result pages fetched per query. Bumped from 5 -> 8 so more of the long
# tail surfaces per keyword. Kept bounded to stay polite and avoid long runs.
MAX_PAGES_PER_QUERY = 8


class LinkedInScraper(BaseScraper):
    BASE = "https://www.linkedin.com"

    def __init__(self, page: Page):
        super().__init__(page)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def save_session(self):
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        storage = self.page.context.storage_state()
        LINKEDIN_SESSION.write_text(json.dumps(storage))
        print("[linkedin] session saved")

    @staticmethod
    def load_session_state() -> dict | None:
        if LINKEDIN_SESSION.exists():
            return json.loads(LINKEDIN_SESSION.read_text())
        return None

    def is_logged_in(self) -> bool:
        """
        desc: Confirm the current LinkedIn session is authenticated by loading
            the feed and checking the resulting URL *path* only. When logged
            out, LinkedIn redirects to "/login/?session_redirect=.../feed",
            which still contains the substring "feed"; matching on the path
            prevents that false positive (which previously caused silent
            logged-out guest scraping).
        args: (none)
        output (bool): True if the feed page resolved to a /feed path
            (logged in), False otherwise.
        """
        self.page.goto(f"{self.BASE}/feed", wait_until="domcontentloaded")
        return urlparse(self.page.url).path.startswith("/feed")

    # ------------------------------------------------------------------
    # Terminal (headless) credential login
    #
    # These let --login authenticate from the terminal: the caller prompts for
    # the email/password (never stored), submit_credentials fills the form, and
    # resolve_login polls the tab -- prompting for a one-time 2FA code in the
    # terminal via the get_otp callback. Anything LinkedIn throws that a
    # terminal can't handle (a CAPTCHA or a non-OTP "confirm it's you"
    # checkpoint) is reported back as "captcha" so the caller can reopen a real
    # window. None of these methods navigate away from an in-progress challenge:
    # login state is detected by inspecting the current URL/DOM only.
    # ------------------------------------------------------------------

    LOGIN_URL = BASE + "/login"

    # OTP / 2FA code entry field on the /checkpoint/ page, tried in order.
    _OTP_INPUT_SELECTORS = [
        "input[name=pin]",
        "#input__phone_verification_pin",
        "#input__email_verification_pin",
        "input[autocomplete='one-time-code']",
    ]
    _OTP_SUBMIT_SELECTORS = [
        "#two-step-submit-button",
        "button[type=submit]",
    ]

    def _passive_logged_in(self) -> bool:
        """
        desc: Non-navigating login check -- True once the tab has landed on the
            authenticated /feed path. Used inside resolve_login's poll loop so a
            2FA/CAPTCHA flow in progress is never disrupted by a navigation.
        args: (none)
        output (bool): True if the current URL path is under /feed.
        """
        try:
            return urlparse(self.page.url).path.startswith("/feed")
        except Exception:
            return False

    def _otp_field(self):
        """
        desc: Return the first present OTP input element handle, or None. Its
            presence is how we know LinkedIn is asking for a one-time code
            rather than showing a CAPTCHA/other checkpoint.
        args: (none)
        output (ElementHandle | None): the OTP field if present, else None.
        """
        for sel in self._OTP_INPUT_SELECTORS:
            el = self.page.query_selector(sel)
            if el:
                return el
        return None

    def _on_checkpoint(self) -> bool:
        """
        desc: True if the current URL is a LinkedIn /checkpoint challenge (2FA,
            CAPTCHA, or "confirm it's you"). Combined with _otp_field() to tell
            a resolvable OTP prompt apart from one that needs a real window.
        args: (none)
        output (bool): True if the path contains /checkpoint.
        """
        try:
            return "/checkpoint" in urlparse(self.page.url).path
        except Exception:
            return False

    def submit_credentials(self, email: str, password: str) -> bool:
        """
        desc: Load the LinkedIn login page and submit the username/password
            form. Does not resolve any downstream 2FA/CAPTCHA challenge (see
            resolve_login). The credentials are used only for this form fill and
            are never persisted by this method.
        args:
            email (str): LinkedIn account email.
            password (str): LinkedIn account password.
        output (bool): True if the form was submitted (or the tab was already
            authenticated); False if the login form could not be filled.
        """
        self.page.goto(self.LOGIN_URL, wait_until="domcontentloaded")
        time.sleep(2)
        if self._passive_logged_in():
            return True
        try:
            self.page.fill("#username", email)
            self.page.fill("#password", password)
        except Exception:
            return False
        for sel in ("button[type=submit]", "button[aria-label='Sign in']"):
            btn = self.page.query_selector(sel)
            if btn:
                try:
                    btn.click()
                    break
                except Exception:
                    continue
        time.sleep(3)
        return True

    def submit_otp(self, code: str) -> bool:
        """
        desc: Enter a one-time 2FA code into the checkpoint PIN field and submit
            it.
        args:
            code (str): the one-time code from the user's authenticator/SMS/
                email.
        output (bool): True if a PIN field was found and the code submitted,
            False if no PIN field was present.
        """
        field = self._otp_field()
        if not field:
            return False
        try:
            field.fill(code.strip())
        except Exception:
            return False
        for sel in self._OTP_SUBMIT_SELECTORS:
            btn = self.page.query_selector(sel)
            if btn:
                try:
                    btn.click()
                    break
                except Exception:
                    continue
        time.sleep(3)
        return True

    def resolve_login(self, get_otp, timeout_s: int = 120) -> str:
        """
        desc: After submit_credentials, poll the tab until login resolves,
            prompting for a one-time code in the terminal (via get_otp) if a PIN
            prompt appears. Never navigates the tab, so an in-progress challenge
            isn't disrupted; state is read from the URL/DOM only.
        args:
            get_otp (Callable[[], str]): called at most once to obtain a
                one-time code when a PIN prompt appears; may return "" to
                decline (which routes to the window fallback).
            timeout_s (int): overall budget in seconds (default 120).
        output (str): "success" (authenticated), "captcha" (a non-OTP checkpoint
            needs a real window), or "timeout".
        """
        deadline = time.time() + timeout_s
        asked_otp = False
        while time.time() < deadline:
            if self._passive_logged_in():
                return "success"
            if self._otp_field() and not asked_otp:
                asked_otp = True
                code = get_otp()
                if code:
                    self.submit_otp(code)
                    continue
                return "captcha"  # user declined -- fall back to a window
            if self._on_checkpoint() and (asked_otp or not self._otp_field()):
                # A checkpoint that isn't (or is no longer) a plain OTP prompt:
                # CAPTCHA / "confirm it's you" / app approval. Not solvable from
                # a terminal -- let the caller reopen a real window.
                return "captcha"
            time.sleep(2)
        return "timeout"

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _build_search_url(self, query: str, location: str) -> str:
        """
        desc: Assemble a LinkedIn job-search URL from a keyword query and
            location, applying only the filters enabled by the module-level
            tunables. Easy-Apply restriction and experience-level restriction
            are both opt-in so the default search surfaces the widest set of
            internships; results are still filtered by title/body downstream
            and reviewed by a human before any application.
        args:
            query (str): free-text search keywords (e.g. "data science intern").
            location (str): location string (e.g. "United States").
        output (str): a fully-formed LinkedIn /jobs/search/ URL.
        """
        params = [
            f"keywords={quote_plus(query)}",
            f"location={quote_plus(location)}",
            "sortBy=DD",  # most recent first
        ]
        if RESTRICT_TO_EASY_APPLY:
            params.append("f_AL=true")
        if RESTRICT_EXPERIENCE_LEVEL and EXPERIENCE_LEVELS:
            # f_E accepts a comma-separated list of experience-level codes.
            params.append(f"f_E={quote_plus(','.join(EXPERIENCE_LEVELS))}")
        return f"{self.BASE}/jobs/search/?" + "&".join(params)

    def search(self, query: str, location: str = "United States") -> Iterator[JobListing]:
        """
        desc: Search LinkedIn jobs for a keyword query and yield internship
            JobListings, paging through results up to MAX_PAGES_PER_QUERY.
            Title-level internship and exclusion filtering is applied here;
            full-description exclusion and human review happen downstream.
        args:
            query (str): free-text search keywords.
            location (str): location filter (default "United States").
        output (Iterator[JobListing]): de-duplicated internship listings whose
            titles pass _is_internship and are not caught by _is_excluded.
        """
        url = self._build_search_url(query, location)
        self.page.goto(url, wait_until="domcontentloaded")
        time.sleep(3)

        seen = set()
        page_num = 0

        while page_num < MAX_PAGES_PER_QUERY:
            self._scroll_result_list()
            listings = self._parse_current_page()
            if not listings:
                break

            for job in listings:
                if job.external_id in seen:
                    continue
                # Keep only internship-titled roles, and drop anything the
                # exclusion filter catches at the title level (new-grad /
                # senior / IB). Body-level exclusion still runs downstream.
                if not self._is_internship(job.title):
                    continue
                if self._is_excluded(job.title):
                    continue
                seen.add(job.external_id)
                yield job

            if not self._go_to_next_page():
                break
            page_num += 1

    def _scroll_result_list(self):
        """
        desc: Scroll the results pane to force LinkedIn's lazy-loaded job cards
            to render. The authenticated ("member") jobs list virtualizes and
            only mounts cards as they scroll into view, so without this many
            cards on a page never appear in the DOM and were silently missed.
            Best-effort: any failure is swallowed so parsing still proceeds.
        args: (none)
        output (None): scrolls the page as a side effect.
        """
        try:
            for _ in range(6):
                self.page.mouse.wheel(0, 2500)
                time.sleep(0.4)
            # Scroll back to top so pagination controls are reachable.
            self.page.mouse.wheel(0, -15000)
            time.sleep(0.3)
        except Exception:
            pass

    def _go_to_next_page(self) -> bool:
        """
        desc: Advance to the next page of LinkedIn results. Tries several
            pagination affordances because member vs. guest markup differ:
            the aria-labelled "View next page" button (member), a generic
            next-page button, and finally numbered pagination buttons.
        args: (none)
        output (bool): True if a next-page control was found and clicked,
            False if none was available (end of results).
        """
        # Ordered list of candidate "next" controls, most specific first.
        for sel in (
            'button[aria-label="View next page"]',
            'button[aria-label="Next"]',
            'button.jobs-search-pagination__button--next',
            'a[aria-label="Next"]',
        ):
            btn = self.page.query_selector(sel)
            if btn:
                try:
                    btn.click()
                    time.sleep(2)
                    return True
                except Exception:
                    continue
        return False

    # (card, title, company, location, link) selector sets tried in order.
    # LinkedIn serves different markup for authenticated ("member") vs.
    # logged-out ("guest") search pages, and the member markup in particular
    # has churned across redesigns. Only the guest set (base-search-card) was
    # ever confirmed against live markup; the member sets are best-effort and
    # NEED A LOGGED-IN LIVE CHECK to confirm the class names still match.
    #
    # Each set now lists several *comma-separated* candidate selectors per
    # field so a single class-name change doesn't drop the whole card. Missing
    # optional fields (company/location) no longer discard the card -- only a
    # usable link is truly required, since external_id/url derive from it.
    _CARD_SELECTOR_SETS = [
        {
            # Member ("/jobs/search/") layout -- UNVERIFIED, needs login check.
            "card": "div.job-card-container, li.jobs-search-results__list-item, "
                    "div.jobs-search-results__list-item, li[data-occludable-job-id]",
            "title": ".job-card-list__title, .job-card-container__link, "
                     "a.job-card-list__title--link, .artdeco-entity-lockup__title",
            "company": ".job-card-container__company-name, "
                       ".job-card-container__primary-description, "
                       ".artdeco-entity-lockup__subtitle",
            "location": ".job-card-container__metadata-item, "
                        ".job-card-container__metadata-wrapper, "
                        ".artdeco-entity-lockup__caption",
            "link": "a.job-card-container__link, a.job-card-list__title--link, "
                    "a[href*='/jobs/view/']",
        },
        {
            # Guest ("/jobs/search") public layout -- CONFIRMED historically.
            "card": ".base-search-card, .job-search-card",
            "title": ".base-search-card__title, .job-search-card__title",
            "company": ".base-search-card__subtitle, "
                       ".base-search-card__subtitle-link",
            "location": ".job-search-card__location",
            "link": "a.base-card__full-link, a[href*='/jobs/view/']",
        },
    ]

    def _first_selector_that_matches(self):
        """
        desc: Pick the first _CARD_SELECTOR_SETS entry whose "card" selector
            matches at least one element on the current page.
        args: (none)
        output (dict | None): the matching selector set, or None if no set
            matched any card on the page.
        """
        for s in self._CARD_SELECTOR_SETS:
            if self.page.query_selector(s["card"]):
                return s
        return None

    @staticmethod
    def _text_of(el) -> str:
        """
        desc: Safely extract stripped inner text from a Playwright element
            handle that may be None.
        args:
            el: a Playwright ElementHandle or None.
        output (str): the element's stripped inner text, or "" if el is None
            or extraction fails.
        """
        if not el:
            return ""
        try:
            return el.inner_text().strip()
        except Exception:
            return ""

    @staticmethod
    def _extract_job_id(href: str) -> str:
        """
        desc: Derive a stable LinkedIn job id from a card/link href. Handles
            both "/jobs/view/<id>/" URLs and "?currentJobId=<id>" query
            variants used by the member layout; falls back to the raw href.
        args:
            href (str): the anchor href pulled off a job card.
        output (str): the extracted numeric job id, or the raw href if no
            recognizable id pattern was found.
        """
        if not href:
            return ""
        if "/jobs/view/" in href:
            return href.split("/jobs/view/")[-1].split("/")[0].split("?")[0]
        if "currentJobId=" in href:
            return href.split("currentJobId=")[-1].split("&")[0]
        return href

    def _parse_current_page(self) -> list[JobListing]:
        """
        desc: Parse all job cards on the currently loaded LinkedIn results
            page into JobListings using the first matching selector set. Parsing
            is intentionally permissive: a card is kept as long as a title and
            a resolvable job id are found; missing company/location degrade to
            empty strings rather than dropping the card, so real results aren't
            silently discarded when member selectors drift.
        args: (none)
        output (list[JobListing]): listings found on the page (may be empty).
            easy_apply is left False here since it can't be reliably read from
            the card without the Easy-Apply-only filter; it is not relied on
            for any submit path (discovery/staging only).
        """
        results = []
        selectors = self._first_selector_that_matches()
        if not selectors:
            return results

        cards = self.page.query_selector_all(selectors["card"])

        for card in cards:
            try:
                title = self._text_of(card.query_selector(selectors["title"]))
                company = self._text_of(card.query_selector(selectors["company"]))
                location = self._text_of(card.query_selector(selectors["location"]))

                link_el = card.query_selector(selectors["link"])
                href = (link_el.get_attribute("href") or "") if link_el else ""

                # As a last resort for id/url, try the card's own data attr
                # (member layout exposes data-occludable-job-id / data-job-id).
                job_id = self._extract_job_id(href)
                if not job_id:
                    job_id = (
                        card.get_attribute("data-occludable-job-id")
                        or card.get_attribute("data-job-id")
                        or ""
                    )

                # A card with neither a title nor an id is unusable.
                if not title or not job_id:
                    continue

                url = (
                    f"{self.BASE}/jobs/view/{job_id}/"
                    if job_id and "/" not in job_id and "http" not in job_id
                    else (href or f"{self.BASE}/jobs/view/{job_id}/")
                )

                results.append(JobListing(
                    platform="linkedin",
                    external_id=job_id,
                    company=company,
                    title=title,
                    location=location,
                    url=url,
                    description="",  # fetched on demand
                    easy_apply=False,
                ))
            except Exception:
                continue

        return results

    # Member and guest job-view pages use different description containers,
    # tried in order (same churn caveat as _CARD_SELECTOR_SETS). Extra
    # fallbacks added; the last entry is a broad main-content catch so a body
    # is still returned even when the exact class changed -- NEEDS A LOGGED-IN
    # LIVE CHECK to confirm the preferred member container.
    _DESCRIPTION_SELECTORS = [
        ".jobs-description__content",
        ".jobs-description-content__text",
        ".jobs-box__html-content",
        ".show-more-less-html__markup",
        ".description__text",
        "#job-details",
        "article",
    ]

    def fetch_description(self, job: JobListing) -> str:
        """
        desc: Load a single job's detail page and return its description text,
            trying each candidate container selector in order. Best-effort:
            returns "" if none match rather than raising, so a description
            miss never aborts the surrounding scrape/stage loop.
        args:
            job (JobListing): the listing whose description should be fetched
                (job.url is navigated to).
        output (str): the stripped description text, or "" if no container
            matched.
        """
        self.page.goto(job.url, wait_until="domcontentloaded")
        time.sleep(1)
        # Nudge lazy content / "see more" region into the DOM.
        try:
            self.page.mouse.wheel(0, 1500)
            time.sleep(0.4)
        except Exception:
            pass
        for sel in self._DESCRIPTION_SELECTORS:
            text = self._text_of(self.page.query_selector(sel))
            if text:
                return text
        return ""

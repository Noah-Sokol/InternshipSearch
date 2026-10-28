"""
Context-aware breadth engine for internship discovery (re-runnable, self-improving).

This module casts a WIDE net for internship postings that are actually relevant
to Noah, then filters that net down so breadth doesn't turn into noise. It is a
complement to -- deliberately NOT a duplicate of -- the curated ATS source in
scraper/greenhouse_lever.py (which polls a hand-picked list of Greenhouse/Lever/
Ashby boards plus the SimplifyJobs / vanshb03 community JSON feeds).

What makes it "context-aware": instead of relying only on config.SEARCH_QUERIES,
it reads Noah's actual profile (profile/noah.yaml) and config interests and
DERIVES an expanded set of search terms and a relevant employer/token universe
from his real skills, projects, and interests (XGBoost / ML / MLB-prediction /
sports-analytics / quant-adjacent -> sports-analytics + ML + quant-adjacent
queries and companies). Those derived terms drive a soft relevance score.

Sources (all FREE, no login, no API key, verified live before coding against
them -- and chosen to NOT overlap greenhouse_lever.py):

  * SpeedyApply community lists (a different repo family than SimplifyJobs):
      - https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/README.md
      - https://raw.githubusercontent.com/speedyapply/2027-AI-College-Jobs/main/README.md
    These are markdown tables (FAANG+/Quant/Other) carrying real per-posting
    apply URLs and salary, parsed with a strict row regex.
  * Lightweight company-career-page discovery: context-derived candidate
    Greenhouse/Lever/Ashby board slugs (scraper/coverage_sources.json) that are
    NOT already in config.ATS_TARGETS are probed live; any board that yields a
    relevance-passing internship is kept and PERSISTED back to the JSON so the
    next run starts broader (self-improving universe).

Every candidate posting goes through:
  1. HARD filters, reused verbatim from BaseScraper (_is_internship /
     _is_excluded / _mentions_target_cycle) + config.FILTERS -- IB, new-grad,
     senior stay excluded exactly as everywhere else in the project.
  2. A stricter whole-word internship gate (these bulk feeds false-positive on
     "Internal" / "International" -- same issue greenhouse_lever.py documented).
  3. A SOFT relevance score (interest match, sophomore/Summer-2027 fit,
     seniority, location) -- only postings clearing RELEVANCE_THRESHOLD are kept.

Politeness: normal User-Agent, request timeouts, a small sleep between network
hits, and a TTL cache under .cache/coverage/ so re-runs are cheap and don't
re-hammer any endpoint. Discovery + normalization only; this module never
applies to anything. It returns list[scraper.base.JobListing] with a stable
external_id per posting for downstream dedupe.
"""

import json
import re
import time
from datetime import date
from pathlib import Path
from typing import Iterator

import requests
import yaml

from config import ROOT, PROFILE_PATH, FILTERS, SEARCH_QUERIES, ATS_TARGETS
from classify import is_internship_title as _is_internship_title
from scraper import net_stats
from scraper.base import BaseScraper, JobListing

# --- module-level config (could be promoted to config.py later) -------------

COVERAGE_SOURCES_PATH = ROOT / "scraper" / "coverage_sources.json"
CACHE_DIR = ROOT / ".cache" / "coverage"
CACHE_TTL_SECONDS = 6 * 60 * 60  # 6h -- feeds change slowly; be polite on re-run
REQUEST_TIMEOUT = 30
POLITE_SLEEP_SECONDS = 1.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
DESC_MAX_CHARS = 6000

# Keep only postings scoring at or above this. Tuned so a clearly-relevant role
# (ML/DS/quant intern, sophomore/2027-friendly, target location) clears it while
# an off-interest generic intern (e.g. "Marketing Intern") does not.
RELEVANCE_THRESHOLD = 2

# _is_internship_title (imported above from classify.py) is the shared,
# stricter-than-BaseScraper._is_internship whole-word check -- drops
# "Internal" / "International" / "Winternship" false positives on bulk feeds.
# Previously this module carried its own byte-identical copy of that regex;
# now there's one definition, shared with greenhouse_lever.py and base.py.

# SpeedyApply markdown table row:
#   | <a href="companyURL"><strong>Company</strong></a> | Title | Location | Salary | <a href="applyURL"> ... |
_SPEEDYAPPLY_ROW_RE = re.compile(
    r'\|\s*<a href="[^"]*"><strong>(?P<company>[^<]+)</strong></a>\s*\|'
    r'\s*(?P<title>[^|]+?)\s*\|'
    r'\s*(?P<location>[^|]*?)\s*\|'
    r'\s*(?P<salary>[^|]*?)\s*\|'
    r'\s*<a href="(?P<url>[^"]+)"'
)

# Interest vocabulary Noah's roles should hit. Seeded with the obvious core and
# then EXPANDED from his actual profile at runtime (see _derive_context).
_BASE_INTEREST_TERMS = {
    "data science", "data scientist", "machine learning", "ml", "quant",
    "quantitative", "analytics", "analyst", "research", "statistics",
    "statistical", "modeling", "model", "python", "trading", "sports",
    "baseball", "sabermetrics", "prediction", "forecasting", "algorithm",
}

# Words in a title that signal a role OFF Noah's interests (soft negative -- not
# a hard exclude; hard excludes live in FILTERS). Kept small and unambiguous.
_OFF_INTEREST_TERMS = {
    "marketing", "sales", "recruiting", "recruiter", "hr ", "human resources",
    "design", "designer", "ux", "ui", "content", "social media", "brand",
    "legal", "accounting", "audit", "tax", "communications", "pr ",
    "customer success", "operations intern", "warehouse", "mechanical",
    "electrical", "civil", "chemical", "biology", "nursing", "teacher",
}


# ---------------------------------------------------------------------------
# context derivation
# ---------------------------------------------------------------------------

def _derive_context(profile: dict) -> dict:
    """
    desc: Read Noah's profile + config interests and derive an expanded,
        context-driven search vocabulary and query set -- so discovery/scoring
        reflect his real skills/projects/interests rather than only the static
        config.SEARCH_QUERIES list. Only profile *keywords* are used locally to
        rank/expand terms; nothing personal is sent off-machine.
    args:
        profile (dict): parsed profile/noah.yaml.
    output:
        (dict): {
            "interest_terms" (set[str]): expanded lowercase relevance vocabulary,
            "queries" (list[str]): expanded lowercase search-term strings,
        }
    """
    terms = set(_BASE_INTEREST_TERMS)
    queries: set[str] = set()

    # Start from the static queries so we never lose existing coverage.
    for group in SEARCH_QUERIES.values():
        for q in group:
            queries.add(q.lower())

    # Pull skills/libraries/interests straight out of the profile.
    skills = profile.get("skills", {}) or {}
    for bucket in ("languages", "libraries", "apis", "tools"):
        for item in skills.get(bucket, []) or []:
            terms.add(str(item).lower())
    for m in skills.get("modeling", []) or []:
        terms.add(str(m).lower())

    for interest in profile.get("interests", []) or []:
        text = str(interest).lower()
        terms.add(text)
        for word in re.findall(r"[a-z]+", text):
            if len(word) >= 4:
                terms.add(word)

    # Mine project/experience text for domain nouns (e.g. "MLB", "WAR",
    # "prediction market", "backtesting") to widen the sports/quant vocabulary.
    project_blob = json.dumps(profile.get("projects", []) or []).lower()
    for kw in ("mlb", "war", "backtest", "prediction market", "money line",
               "statcast", "fangraphs", "odds", "calibration", "gradient",
               "random forest", "roi"):
        if kw in project_blob:
            terms.add(kw)

    # Derived search queries: cross Noah's strongest domains with "intern" and
    # the target cycle so the term set actively broadens discovery, not just
    # scoring. These are the context-driven additions to config.SEARCH_QUERIES.
    cycle = FILTERS.get("prioritize_year", "2027")
    derived = [
        "machine learning intern", "ml engineer intern",
        "data scientist intern", "quantitative research intern",
        "quantitative developer intern", "sports analytics intern",
        "baseball analytics intern", "sabermetrics intern",
        "quantitative trading intern", "systematic trading intern",
        "applied scientist intern", "research intern machine learning",
        f"data science intern summer {cycle}",
        f"quantitative intern summer {cycle}",
        f"sports data scientist intern {cycle}",
    ]
    for q in derived:
        queries.add(q.lower())

    return {"interest_terms": terms, "queries": sorted(queries)}


# ---------------------------------------------------------------------------
# small utilities
# ---------------------------------------------------------------------------

def _cache_path(key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    return CACHE_DIR / f"{safe}.json"


def _fetch(url: str, cache_key: str, session: requests.Session, as_json: bool):
    """
    desc: Fetch a URL (JSON or text), serving a fresh-enough local cache copy
        when present so re-runs stay cheap and polite. Any failure degrades to
        None -- a single dead source must never abort the whole coverage pass.
    args:
        url (str): the public endpoint to fetch.
        cache_key (str): filename-safe key for the on-disk cache entry.
        session (requests.Session): shared session carrying a normal UA.
        as_json (bool): parse the response body as JSON when True, else text.
    output:
        (dict | list | str | None): parsed JSON, raw text, or None.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_key)

    if path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL_SECONDS:
        try:
            raw = path.read_text(encoding="utf-8")
            return json.loads(raw) if as_json else raw
        except (json.JSONDecodeError, OSError):
            pass  # corrupt/mismatched cache -> refetch

    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        time.sleep(POLITE_SLEEP_SECONDS)
        net_stats.record(resp.status_code == 200)
        if resp.status_code != 200:
            return None
        body = resp.text
        data = resp.json() if as_json else body
    except (requests.RequestException, ValueError):
        net_stats.record(False)
        return None

    try:
        path.write_text(body if not as_json else json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    return data


def _load_sources() -> dict:
    """
    desc: Load the module-owned self-improving source universe.
    args:
        (none)
    output:
        (dict): parsed coverage_sources.json (empty-ish dict on failure).
    """
    try:
        return json.loads(COVERAGE_SOURCES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_sources(data: dict) -> None:
    """
    desc: Persist the (possibly grown) source universe back to disk so the next
        run starts broader than this one. Best-effort; never raises.
    args:
        data (dict): the source universe to write.
    output:
        (None)
    """
    try:
        COVERAGE_SOURCES_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def _config_ats_tokens() -> set[tuple]:
    """(ats, token) pairs already covered by greenhouse_lever.py -- never re-probe."""
    return {(t["ats"], t["token"]) for t in ATS_TARGETS}


# ---------------------------------------------------------------------------
# relevance scoring (the soft gate on top of the hard FILTERS)
# ---------------------------------------------------------------------------

def score_relevance(listing: JobListing, interest_terms: set[str]) -> int:
    """
    desc: Soft relevance score for a posting given Noah's context. Layered ON
        TOP of the hard FILTERS (which have already dropped IB/new-grad/senior);
        this ranks how well a surviving posting matches his interests, cycle
        fit, and preferred locations, and lets breadth be trimmed to signal.
    args:
        listing (JobListing): a candidate posting (title/company/location/desc).
        interest_terms (set[str]): expanded relevance vocabulary from context.
    output:
        (int): relevance points; higher is a better fit. Compared against
            RELEVANCE_THRESHOLD by the caller.
    """
    title = listing.title.lower()
    blob = f"{listing.title} {listing.company} {listing.description}".lower()
    loc = listing.location.lower()
    score = 0

    # Interest match: title hits count double (title is the strongest signal).
    if any(t in title for t in interest_terms):
        score += 2
    elif any(t in blob for t in interest_terms):
        score += 1

    # Cycle fit: target year present anywhere is a strong positive.
    if FILTERS.get("prioritize_year", "2027") in blob:
        score += 1

    # Sophomore / underclass-friendly language.
    if any(w in blob for w in ("sophomore", "first-year", "first year",
                               "second-year", "second year", "underclass",
                               "freshman", "step ", "explore ")):
        score += 1

    # Location preference.
    if any(city in loc or city in blob for city in FILTERS.get("target_locations", [])):
        score += 1

    # Soft negative: clearly off-interest titles get pushed below threshold.
    if any(t in title for t in _OFF_INTEREST_TERMS):
        score -= 2

    return score


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------

class CoverageEngine(BaseScraper):
    """
    Context-aware breadth engine over free, no-login sources that complement the
    curated ATS scraper. Reuses BaseScraper's hard filters, ignores the browser
    `page` (plain HTTP), and is constructed with page=None. Use discover() as the
    query-free entry point; search() is present only for BaseScraper parity.
    """

    def __init__(self, page=None):
        super().__init__(page)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
        profile = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
        self._context = _derive_context(profile)
        self._sources = _load_sources()
        self.stats: dict[str, int] = {}  # per-source relevant-posting counts

    # --- interface compat -------------------------------------------------

    def search(self, query: str = "", location: str = "United States") -> Iterator[JobListing]:
        """
        desc: BaseScraper interface method. Query/location are ignored (the
            derived context + source universe drive discovery), so this simply
            yields the full relevance-gated result.
        args:
            query (str): unused; kept for interface parity.
            location (str): unused; kept for interface parity.
        output:
            (Iterator[JobListing]): relevant, deduped internship postings.
        """
        yield from self.discover()

    # --- main entry point -------------------------------------------------

    def discover(self, persist: bool = True) -> list[JobListing]:
        """
        desc: Run the full breadth pass: pull SpeedyApply lists and probe
            context-derived ATS boards, apply the hard FILTERS + whole-word
            gate, keep only postings clearing RELEVANCE_THRESHOLD, dedupe, and
            (optionally) persist any newly-verified ATS board back to
            coverage_sources.json so the next run starts broader.
        args:
            persist (bool): when True, write newly-verified boards to disk
                (self-improving behavior). Set False for a read-only dry run.
        output:
            (list[JobListing]): relevant, deduped internship postings with a
                stable external_id each.
        """
        interest_terms = self._context["interest_terms"]
        seen: set[tuple] = set()
        results: list[JobListing] = []

        def consider(listing: JobListing, source_label: str) -> bool:
            if not _is_internship_title(listing.title):
                return False
            if self._is_excluded(f"{listing.title} {listing.company} {listing.description}"):
                return False
            if score_relevance(listing, interest_terms) < RELEVANCE_THRESHOLD:
                return False
            key = (
                listing.company.lower().strip(),
                listing.title.lower().strip(),
                listing.location.lower().strip(),
            )
            if key in seen:
                return False
            seen.add(key)
            results.append(listing)
            self.stats[source_label] = self.stats.get(source_label, 0) + 1
            return True

        # 1) SpeedyApply community lists (new repo family).
        for repo in self._sources.get("aggregator_repos", []):
            label = f"repo:{repo['name']}"
            for listing in self._iter_speedyapply(repo):
                consider(listing, label)

        # 2) Context-derived ATS board discovery (self-improving).
        newly_verified = self._discover_ats_boards(consider)
        if persist and newly_verified:
            self._persist_verified(newly_verified)

        return results

    # --- source: SpeedyApply markdown lists -------------------------------

    def _iter_speedyapply(self, repo: dict) -> Iterator[JobListing]:
        """
        desc: Parse a SpeedyApply README markdown table into JobListings. Each
            table row carries a company, title, location, salary, and a real
            apply URL, which becomes the posting url and (hashed) external_id.
        args:
            repo (dict): {"name", "url", ...} from coverage_sources.json.
        output:
            (Iterator[JobListing]): one listing per parsed table row (unfiltered;
                the caller applies the hard + relevance gates).
        """
        text = self._fetch_text(repo["url"], f"repo_{repo['name']}")
        if not text:
            print(f"  [coverage] {repo['name']} -- no data (skipped)")
            return

        for m in _SPEEDYAPPLY_ROW_RE.finditer(text):
            company = m.group("company").strip()
            title = re.sub(r"\s+", " ", m.group("title")).strip()
            location = m.group("location").strip()
            url = m.group("url").strip()
            salary = m.group("salary").strip()
            desc = f"Salary: {salary}" if salary and salary != "N/A" else ""
            ext = f"speedyapply:{repo['name']}:{re.sub(r'[^a-z0-9]', '', (company + title).lower())[:40]}"
            yield JobListing(
                platform="coverage-list",
                external_id=ext,
                company=company,
                title=title,
                location=location,
                url=url,
                description=desc,
            )

    # --- source: context-derived ATS board discovery ----------------------

    def _discover_ats_boards(self, consider) -> list[dict]:
        """
        desc: Probe context-derived candidate Greenhouse/Lever/Ashby board slugs
            that are NOT already in config.ATS_TARGETS. Already-verified boards
            (from coverage_sources.json) are probed first; candidate guesses
            after. Any board that yields at least one relevance-passing intern is
            returned as "verified" so discover() can persist it.
        args:
            consider (callable): the caller's gate/collect closure; returns True
                when a listing was kept.
        output:
            (list[dict]): verified board descriptors ({"ats","token","company"})
                seen this run (dedup/merge handled by _persist_verified).
        """
        already = _config_ats_tokens()
        candidates: list[dict] = []

        # Previously-verified boards (persisted) -- re-poll these first.
        for b in self._sources.get("verified_ats_boards", []):
            pair = (b["ats"], b["token"])
            if pair not in already:
                candidates.append({"ats": b["ats"], "token": b["token"],
                                   "company": b.get("company", b["token"])})
                already.add(pair)

        # Context-derived guesses at new slugs.
        for ats, tokens in (self._sources.get("ats_token_candidates", {}) or {}).items():
            if ats.startswith("_"):
                continue
            for token in tokens:
                pair = (ats, token)
                if pair in already:
                    continue
                already.add(pair)
                candidates.append({"ats": ats, "token": token, "company": token})

        verified: list[dict] = []
        for cand in candidates:
            kept_any = False
            for listing in self._iter_ats_board(cand):
                if consider(listing, f"ats:{cand['ats']}:{cand['token']}"):
                    kept_any = True
                    # Prefer the real company name off the posting when we have it.
                    cand["company"] = listing.company or cand["company"]
            if kept_any:
                verified.append(cand)
        return verified

    def _iter_ats_board(self, cand: dict) -> Iterator[JobListing]:
        """
        desc: Fetch a single candidate ATS board and yield its intern postings.
            Greenhouse/Lever/Ashby each have a distinct JSON shape (confirmed
            live). Any non-200 / dead board yields nothing (silently skipped).
        args:
            cand (dict): {"ats","token","company"} board descriptor.
        output:
            (Iterator[JobListing]): intern postings from that board (unfiltered
                beyond the whole-word title check; caller applies full gates).
        """
        ats, token, company = cand["ats"], cand["token"], cand["company"]
        if ats == "greenhouse":
            url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        elif ats == "lever":
            url = f"https://api.lever.co/v0/postings/{token}?mode=json"
        elif ats == "ashby":
            url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
        else:
            return

        data = self._fetch_json(url, f"{ats}_{token}")
        if data is None:
            return

        if ats == "greenhouse":
            for j in data.get("jobs", []):
                title = (j.get("title") or "").strip()
                if not _is_internship_title(title):
                    continue
                yield JobListing(
                    platform="coverage-ats",
                    external_id=f"cov-gh:{token}:{j.get('id')}",
                    company=company,
                    title=title,
                    location=(j.get("location") or {}).get("name", ""),
                    url=j.get("absolute_url", ""),
                    description=self._strip_html(j.get("content", "")),
                )
        elif ats == "lever":
            for j in data:
                title = (j.get("text") or "").strip()
                if not _is_internship_title(title):
                    continue
                cats = j.get("categories") or {}
                yield JobListing(
                    platform="coverage-ats",
                    external_id=f"cov-lever:{token}:{j.get('id')}",
                    company=company,
                    title=title,
                    location=cats.get("location", ""),
                    url=j.get("hostedUrl", ""),
                    description=(j.get("descriptionPlain") or "")[:DESC_MAX_CHARS],
                )
        elif ats == "ashby":
            for j in data.get("jobs", []):
                if not j.get("isListed", True):
                    continue
                title = (j.get("title") or "").strip()
                if not _is_internship_title(title):
                    continue
                yield JobListing(
                    platform="coverage-ats",
                    external_id=f"cov-ashby:{token}:{j.get('id')}",
                    company=company,
                    title=title,
                    location=j.get("location", ""),
                    url=j.get("jobUrl", ""),
                    description=(j.get("descriptionPlain") or "")[:DESC_MAX_CHARS],
                )

    # --- persistence of the self-improving universe -----------------------

    def _persist_verified(self, verified: list[dict]) -> None:
        """
        desc: Merge newly-verified ATS boards into coverage_sources.json's
            verified_ats_boards (deduped by ats+token), stamped with today's
            date, so the next run re-polls them first and starts broader.
        args:
            verified (list[dict]): {"ats","token","company"} boards that yielded
                a relevance-passing internship this run.
        output:
            (None)
        """
        existing = {(b["ats"], b["token"]): b
                    for b in self._sources.get("verified_ats_boards", [])}
        today = date.today().isoformat()
        for v in verified:
            existing[(v["ats"], v["token"])] = {
                "ats": v["ats"], "token": v["token"], "company": v["company"],
                "verified_on": today, "source": "auto-discovered",
            }
        self._sources["verified_ats_boards"] = sorted(
            existing.values(), key=lambda b: (b["ats"], b["token"])
        )
        _save_sources(self._sources)

    # --- fetch helpers (bound to this engine's session) -------------------

    def _fetch_json(self, url: str, cache_key: str):
        return _fetch(url, cache_key, self._session, as_json=True)

    def _fetch_text(self, url: str, cache_key: str):
        return _fetch(url, cache_key, self._session, as_json=False)

    @staticmethod
    def _strip_html(fragment: str) -> str:
        """desc: Cheap HTML->text for Greenhouse content. args: fragment (str).
        output: (str) plain text truncated to DESC_MAX_CHARS."""
        if not fragment:
            return ""
        import html as _html
        text = re.sub(r"<[^>]+>", " ", _html.unescape(fragment))
        return re.sub(r"\s+", " ", text).strip()[:DESC_MAX_CHARS]


# ---------------------------------------------------------------------------
# convenience wrapper (mirrors greenhouse_lever.discover_ats_internships)
# ---------------------------------------------------------------------------

def discover_coverage_internships(persist: bool = True) -> list[JobListing]:
    """
    desc: Convenience wrapper -- run the full context-aware breadth pass and
        return the relevant, deduped internship postings as a plain list. Prints
        a per-source summary so a standalone run shows what it surfaced.
    args:
        persist (bool): when True, persist newly-verified ATS boards for the
            next run (self-improving). False for a read-only dry run.
    output:
        (list[JobListing]): relevant, deduped internship postings.
    """
    engine = CoverageEngine()
    results = engine.discover(persist=persist)
    print(f"\n[coverage] {len(results)} relevant postings from {len(engine.stats)} sources:")
    for src, n in sorted(engine.stats.items(), key=lambda kv: -kv[1]):
        print(f"    {n:3d}  {src}")
    return results


if __name__ == "__main__":
    jobs = discover_coverage_internships()
    companies = sorted({j.company for j in jobs})
    print(f"\n[coverage] {len(companies)} distinct companies surfaced.")
    for j in jobs[:15]:
        print(f"  - {j.company}: {j.title}  [{j.location}]")

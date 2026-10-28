"""
desc: Single source of truth for every categorical "set" the filtering/search
    system reasons about -- location, seniority, interest area, eligibility,
    exclusion reason, platform. Before this module, these sets were defined
    up to 4 times in different files (config.py, evaluator/preferences.py,
    review/dedup.py, scraper/coverage.py) with different alias tables, all
    checked via raw substring containment (e.g. "ny" in loc), which both
    false-positives (a short alias matching inside an unrelated word) and
    false-negatives (typing "NYC" never matches a job stored as "New York").
    This module gives each set ONE definition and a real word-boundary
    tokenizer, so every consumer (scraper/*, evaluator/*, review/*) checks
    exact set membership instead of re-deriving its own fuzzy substring logic.

    Tunable vocabularies (INTEREST_AREAS, TARGET_LOCATIONS, SENIORITY_*, etc.)
    are physically defined here but mutated in place by
    evaluator/preferences.py's existing override system (load_overrides /
    save_overrides / reset_overrides, backed by evaluator/user_overrides.json)
    so the Preferences UI in review/app.py keeps working unchanged -- it
    imports these same objects through preferences.py and mutates them in
    place, never reassigning the names.
args:
    (module-level constants/enums/functions only)
output:
    (module of enums, dicts, lists, and small pure helper functions)
"""
import re
from enum import Enum


# ---------------------------------------------------------------------------
# Closed enums -- values equal to today's already-persisted strings, so no
# data migration is needed anywhere these are compared against DB columns.
# ---------------------------------------------------------------------------
class EligibilityBucket(str, Enum):
    CONFIRMED_FIT = "confirmed_fit"
    EXPLICIT_FIT = "explicit_fit"
    CLASS_YEAR_PROGRAM = "class_year_program"
    NEEDS_VERIFICATION = "needs_verification"
    UNKNOWN = "unknown"
    WRONG_COHORT = "wrong_cohort"
    NOT_ELIGIBLE = "not_eligible"


class ExclusionReason(str, Enum):
    NOT_INTERNSHIP = "not_internship"
    INVESTMENT_BANKING = "investment_banking"
    SENIOR_LEVEL = "senior_level"
    NEW_GRAD = "new_grad"
    RESTRICTED_PROGRAM = "restricted_program"


class Platform(str, Enum):
    LINKEDIN = "linkedin"
    HANDSHAKE = "handshake"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    ATS_LIST = "ats-list"
    COVERAGE_ATS = "coverage-ats"
    COVERAGE_LIST = "coverage-list"
    DIRECT = "direct"

    @classmethod
    def resolve(cls, raw: str) -> "Platform | None":
        """desc: Case-insensitive lookup by value; unknown strings -> None.
        args: raw (str). output: (Platform | None)."""
        if not raw:
            return None
        raw_norm = raw.strip().lower()
        for member in cls:
            if member.value == raw_norm:
                return member
        return None


# ---------------------------------------------------------------------------
# Location -- an OPEN canonical-key registry, deliberately NOT a closed enum.
# Noah adds new target cities through the Preferences UI at runtime; a closed
# enum would force a code edit every time he wants to add a city. Instead,
# canonicalize_location() maps every KNOWN alias to one canonical key, and
# lets any unrecognized city become its own canonical bucket (rather than
# collapsing to a generic "other") -- so "the set of jobs in location X" is a
# real, extensible partition of all locations, not a hardcoded shortlist.
# ---------------------------------------------------------------------------
LOCATION_ALIASES: dict[str, str] = {
    "new york": "new_york",
    "new york city": "new_york",
    "nyc": "new_york",
    "ny": "new_york",
    "manhattan": "new_york",
    "san francisco": "san_francisco",
    "sf": "san_francisco",
    "st. louis": "saint_louis",
    "st louis": "saint_louis",
    "saint louis": "saint_louis",
    "washington": "washington_dc",
    "washington dc": "washington_dc",
    "washington, dc": "washington_dc",
    "d.c.": "washington_dc",
    "dc": "washington_dc",
    "boston": "boston",
    "chicago": "chicago",
    "connecticut": "connecticut",
    "ct": "connecticut",
    "remote": "remote",
}

# Blank/placeholder location strings -- these carry no location information at
# all (distinct from a real, just-unrecognized city, which still gets its own
# canonical bucket per the module docstring above).
LOCATION_BLANK_SENTINELS = {"", "—", "-", "n/a", "tbd", "various", "multiple locations"}
LOCATION_UNKNOWN = "unknown"

# Default preferred-location set, as canonical keys (was raw strings scattered
# across config.FILTERS/evaluator.preferences/scraper.coverage before this
# module). Tunable at runtime via evaluator.preferences.save_overrides().
TARGET_LOCATIONS: list[str] = [
    "remote", "new_york", "saint_louis", "boston", "chicago",
    "san_francisco", "connecticut",
]


def _normalize_segment(segment: str) -> str:
    """desc: Lowercase/strip a single location segment for alias lookup.
    args: segment (str). output: (str)."""
    return re.sub(r"\s+", " ", segment.strip().lower().rstrip("."))


def canonicalize_location(raw: str) -> tuple[str, list[str]]:
    """
    desc: Turn a raw, free-text location string into a canonical key by exact
        (not substring) alias lookup. Splits on commas since postings often
        list "City, ST" or "City, State, Country"; each segment is checked
        against LOCATION_ALIASES in turn (first match wins) so "New York, NY"
        and "NYC" both resolve to the same "new_york" bucket. A location with
        no recognized alias becomes its OWN canonical key (its first, normalized
        segment) rather than a generic "other" -- see module docstring.
    args:
        raw (str): free-text location, e.g. "New York, NY" or "NYC" or "".
    output:
        tuple[str, list[str]]: (canonical_key, normalized_segments). Blank/
            placeholder input returns (LOCATION_UNKNOWN, []).
    """
    text = (raw or "").strip().lower()
    if text in LOCATION_BLANK_SENTINELS:
        return LOCATION_UNKNOWN, []

    segments = [_normalize_segment(s) for s in raw.split(",") if s.strip()]
    if not segments:
        return LOCATION_UNKNOWN, []

    for seg in segments:
        if seg in LOCATION_ALIASES:
            return LOCATION_ALIASES[seg], segments

    # No known alias -- the normalized first segment becomes its own bucket.
    return segments[0].replace(" ", "_"), segments


def all_canonical_locations(raw: str) -> frozenset:
    """
    desc: Return the FULL set of canonical location keys a posting spans --
        one key per comma-separated segment (alias-resolved, or self-as-
        bucket, same rule canonicalize_location() uses per segment) -- not
        just the single "first match wins" primary key canonicalize_location()
        returns. Needed because a broad company-wide req listing 30 offices
        ("Palo Alto, CA, ..., Washington, DC, SF, ..., Chicago, IL, ..., NYC,
        ...") legitimately belongs to MULTIPLE location sets at once: "is
        this posting available somewhere Noah prefers" is a set-intersection
        question, and checking only canonicalize_location()'s single primary
        key would silently miss a preferred city that isn't the first alias
        encountered in the raw string's own (arbitrary) ordering -- exactly
        the bug that motivated this function (a Google posting whose first
        recognized segment was "Washington, DC" scored zero location credit
        even though "NYC" was also in its location list).
    args:
        raw (str): free-text location, possibly a long multi-city list.
    output:
        (frozenset[str]): canonical keys, one per segment (deduped); empty
            for blank/placeholder input.
    """
    text = (raw or "").strip().lower()
    if text in LOCATION_BLANK_SENTINELS:
        return frozenset()
    segments = [_normalize_segment(s) for s in raw.split(",") if s.strip()]
    return frozenset(LOCATION_ALIASES.get(seg, seg.replace(" ", "_")) for seg in segments)


# ---------------------------------------------------------------------------
# Data cleansing -- raw scraped text is often messy (inconsistent whitespace,
# HTML entities that slipped through a scraper's own stripping, smart quotes/
# dashes) in ways that don't break matching outright but do produce ugly
# display text and near-duplicate company groupings ("Google", "Google  Inc.",
# "Google, LLC" as three different-looking strings for the same company).
# clean_text() is applied once at ingestion (database/db.py::upsert_job) so
# every downstream consumer works from the same tidy text;
# normalize_company_name() gives company names the same "canonical key"
# treatment locations get, so dedup (review/dedup.py) and any future
# company= exact-match feature share one definition of "the same company."
# ---------------------------------------------------------------------------
_SMART_PUNCTUATION = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", " ": " ",
}
_COMPANY_SUFFIXES = [
    ", inc.", ", inc", ", llc", ", ltd.", ", ltd",
    ", corp.", ", corp", " inc.", " inc", " llc", " ltd", " corp",
    " & co.", " & co",
]


def clean_text(text: str) -> str:
    """
    desc: Normalize raw scraped text for consistent storage/matching: maps
        smart quotes/dashes/nbsp to their plain-ASCII equivalents, collapses
        all whitespace runs (including newlines/tabs) to single spaces, and
        strips leading/trailing whitespace. Safe on already-clean text (a
        no-op) and on empty/None input (returns "").
    args:
        text (str): raw text, e.g. a scraped title/company/location/description.
    output:
        (str): cleaned text.
    """
    if not text:
        return ""
    for bad, good in _SMART_PUNCTUATION.items():
        text = text.replace(bad, good)
    return re.sub(r"\s+", " ", text).strip()


def normalize_company_name(name: str) -> str:
    """
    desc: Canonical lookup key for a company name -- lowercased, with common
        legal suffixes (", Inc.", " LLC", " & Co", ...) stripped, so "Google",
        "Google Inc.", and "Google, LLC" all resolve to the same key. Mirrors
        canonicalize_location()'s role for locations; the ORIGINAL display
        name (from clean_text(), not this function) is what's stored/shown --
        this key is for equality comparisons only (dedup grouping today; any
        future company= exact-match filter could reuse it the same way
        location= already does for canonicalize_location()).
    args:
        name (str): a company name, any casing/suffix.
    output:
        (str): normalized comparison key.
    """
    n = clean_text(name).lower()
    for suffix in _COMPANY_SUFFIXES:
        if n.endswith(suffix):
            n = n[: -len(suffix)].strip()
    return n


# ---------------------------------------------------------------------------
# Tokenizer primitives -- the actual bug fix for "ml" matching inside "html"
# and " ml "-padding hacks standing in for real word boundaries. Every keyword
# list (single-word or multi-word phrases) is matched with a word boundary
# anchoring its START (so "ml" can never match inside "html", "ny" can never
# match inside an unrelated longer word) plus an optional common-suffix group
# anchoring its END (so "new graduate" still catches "new graduates" and
# "full-time analyst" still catches "full-time analysts" -- the plural/
# inflected forms the old bare-substring checks caught "for free" -- without
# reopening the false-positive door a bare `\w*` wildcard suffix would: only a
# small, explicit set of English inflections is allowed, not arbitrary
# trailing characters).
# ---------------------------------------------------------------------------
_SUFFIX_GROUP = r"(?:'s|es|ed|ing|s|d)?"


_keyword_pattern_cache: dict[str, re.Pattern] = {}


def _keyword_pattern(keyword: str) -> re.Pattern:
    """desc: Word-boundary-anchored, suffix-tolerant pattern for one keyword,
        cached by keyword text. args: keyword (str), already lowercased/
        stripped. output: (re.Pattern)."""
    cached = _keyword_pattern_cache.get(keyword)
    if cached is None:
        cached = re.compile(rf"\b{re.escape(keyword)}{_SUFFIX_GROUP}\b")
        _keyword_pattern_cache[keyword] = cached
    return cached


def match_keywords(text: str, keywords: list[str]) -> list[str]:
    """
    desc: Return the subset of `keywords` that occur as whole words/phrases in
        `text` (case-insensitive, word-boundary matched at the start, with
        tolerance for simple trailing inflections -- not a bare substring
        check, and not naive whole-word-only either; see module note above).
    args:
        text (str): text to scan (any case).
        keywords (list[str]): candidate phrases.
    output:
        (list[str]): the keywords that matched, in their original list order.
    """
    if not keywords:
        return []
    lowered = (text or "").lower()
    return [k for k in keywords if _keyword_pattern(k.lower().strip()).search(lowered)]


# ---------------------------------------------------------------------------
# Tunable vocabularies -- moved verbatim from evaluator/preferences.py so
# there is one physical definition. evaluator/preferences.py re-exports these
# same objects (import, not copy) so its existing override system
# (load_overrides/save_overrides/reset_overrides, review/app.py's Preferences
# page) keeps mutating the one true copy in place.
# ---------------------------------------------------------------------------
INTEREST_AREAS = [
    {
        "name": "data_science_ml",
        "label": "Data Science / Machine Learning",
        "priority": 1.00,
        "keywords": [
            "data science", "data scientist", "machine learning", "ml",
            "deep learning", "ai/ml", "artificial intelligence",
            "data analyst", "analytics engineer", "applied scientist",
            "applied data", "data engineer", "nlp", "computer vision",
            "predictive model", "statistical model",
        ],
    },
    {
        "name": "quant_research_trading",
        "label": "Quantitative Research / Trading",
        "priority": 0.92,
        "keywords": [
            "quantitative research", "quantitative researcher", "quant research",
            "quantitative trader", "quantitative trading", "quant trader",
            "quantitative developer", "quant developer", "systematic",
            "algorithmic trading", "market making", "trading intern",
            "quantitative intern", "quantitative analyst",
        ],
    },
    {
        "name": "sports_analytics",
        "label": "Sports Analytics",
        "priority": 0.85,
        "keywords": [
            "sports analytics", "sports data", "baseball analytics",
            "sabermetrics", "basketball analytics", "football analytics",
            "player performance", "sports science", "sportsbook",
        ],
    },
    {
        "name": "quant_corp_finance",
        "label": "Quant / Corporate Finance",
        "priority": 0.72,
        "keywords": [
            "finance intern", "corporate finance", "capital markets",
            "asset management", "investment research", "equity research",
            "financial analyst", "risk", "treasury", "fp&a",
        ],
    },
]

IB_HARD_EXCLUDE = [
    "investment banking", "investment bank", "m&a", "mergers and acquisitions",
    "mergers & acquisitions", "leveraged finance", "restructuring",
    "ib analyst", "ib summer analyst", "coverage group", "sell-side coverage",
]

SENIORITY_REWARD = [
    "intern", "internship", "co-op", "sophomore", "underclass",
    "first-year", "second-year", "1st year", "2nd year", "freshman",
    "early insight", "discovery", "explore", "step", "insight program",
]
SENIORITY_PENALTY = [
    "senior", "staff", "principal", "director", "manager", "lead", "sr.",
    "new grad", "new graduate", "recent graduate", "graduate program",
    "full-time analyst", "full time analyst", "post-graduate",
    "experienced hire", "phd required", "master's required",
    # "class of 2025/2026" (this cycle's graduating seniors, not Noah's 2029
    # cohort) was previously only caught by config.FILTERS["exclude_keywords"]
    # (a separate, now-retired list) -- kept here so the one canonical
    # exclusion list doesn't silently lose that signal.
    "class of 2025", "class of 2026",
]
# Subset of SENIORITY_PENALTY that specifically signals "new grad" (vs. a
# generic senior-title word) -- used by classify.hard_exclusion() to pick
# ExclusionReason.NEW_GRAD vs ExclusionReason.SENIOR_LEVEL.
NEW_GRAD_SIGNALS = [
    "new grad", "new graduate", "recent graduate", "graduate program",
    "full-time analyst", "full time analyst", "post-graduate",
    "class of 2025", "class of 2026",
]

NAMED_ELIGIBILITY_TAG_TO_BUCKET = {
    "confirmed": EligibilityBucket.CONFIRMED_FIT.value,
    "check_now": EligibilityBucket.CONFIRMED_FIT.value,
    "time_sensitive": EligibilityBucket.CONFIRMED_FIT.value,
    "needs_verification": EligibilityBucket.NEEDS_VERIFICATION.value,
    "not_eligible_yet": EligibilityBucket.WRONG_COHORT.value,
    "not_eligible": EligibilityBucket.NOT_ELIGIBLE.value,
}

WRONG_COHORT_GRAD_YEARS = ["2026", "2027", "2028"]
RIGHT_COHORT_SIGNALS = ["2029", "class of 2029", "summer 2027"]
SOPHOMORE_SIGNALS = [
    "sophomore", "second-year", "second year", "2nd year", "2nd-year",
    "first- and second-year", "first or second year", "underclass",
]
RESTRICTED_SIGNALS = ["women only", "women's", "womens", "for women"]

PRESTIGE_TIERS = {
    1.00: [
        "two sigma", "d.e. shaw", "de shaw", "jane street", "citadel",
        "hudson river trading", "hrt", "optiver", "renaissance",
        "jump trading", "susquehanna", "sig", "drw", "imc trading",
        "google", "deepmind", "openai", "anthropic", "meta", "apple",
        "microsoft", "amazon", "nvidia",
    ],
    0.65: [
        "bloomberg", "palantir", "stripe", "databricks", "snowflake",
        "spotify", "linkedin", "airbnb", "lyft", "robinhood", "coinbase",
        "blackrock", "vanguard", "fidelity", "aqr", "man group",
        "bridgewater", "point72", "millennium", "balyasny",
        "american express", "capital one", "jpmorgan", "goldman sachs",
        "morgan stanley", "bank of america", "wells fargo",
        "federal reserve", "fed reserve",
    ],
    0.30: [
        "ernst & young", "ey", "deloitte", "kpmg", "pwc", "mckinsey",
        "bain", "bcg", "accenture", "ibm", "oracle", "salesforce",
        "intuit", "adobe", "workday", "servicenow",
    ],
}


def prestige_tier(company: str) -> float:
    """desc: Prestige fraction (0.0-1.00) for a company name, exact-token
        matched against PRESTIGE_TIERS. args: company (str). output: (float)."""
    if not company:
        return 0.0
    for frac in sorted(PRESTIGE_TIERS, reverse=True):
        if match_keywords(company, PRESTIGE_TIERS[frac]):
            return frac
    return 0.0


# ---------------------------------------------------------------------------
# Graduate-only programs (Q25): postings Noah (an undergraduate) can't apply to.
# Regexes run on lowercased text after classify._normalize_grad_text() has
# turned "Ph.D." into "phd", "Master’s" into "master's", etc.
# ---------------------------------------------------------------------------

GRADUATE_PROGRAM_NOTE = "graduate program"   # dismissal reason shown in the UI

_GRAD_DEGREE = r"(?:master'?s|masters|mba|phd|doctoral|doctorate|graduate)"

# Title words naming a graduate audience as the target.
GRAD_TITLE_PATTERNS = [
    r"\bphd\b", r"\bdoctoral\b", r"\bdoctorate\b", r"\bmba\b",
    r"\bmaster's\b", r"(?<!scrum )(?<!dungeon )\bmasters\b", r"\bmasters? (?:degree|program)\b",
    r"\bgraduate intern(?:ship)?s?\b", r"\bgraduate program\b", r"\bgrad intern(?:ship)?s?\b",
    # degree tag at the end: "Intern, MS" / "Intern - MS" / "Intern (MS)" (not "MS Office")
    r"[,(–—-]\s*(?:ms|msc|meng)\s*(?:\)|$|[,/])",
]

# Any of these in the same title / sentence means undergrads are eligible too.
GRAD_UNDERGRAD_GUARD = [
    r"\bundergrad", r"\bbachelor", r"\bbs\b", r"\bba\b", r"\bbs/ms\b", r"\bba/bs\b",
    r"\ball degree levels?\b", r"\bany degree\b", r"\ball levels\b", r"\ball students\b",
    r"\balso (?:eligible|considered|welcome|encouraged)\b", r"\bbonus\b", r"\bpreferred\b",
    r"\ba plus\b", r"\bnice to have\b", r"\bdesired\b",
    r"\bsophomore", r"\bjunior", r"\bfreshm[ae]n\b", r"\bcollege students\b",
]

# Description sentences that REQUIRE current graduate enrollment.
GRAD_DESC_PATTERNS = [
    r"\b(?:pursuing|enrolled in|enrolled as|working toward|working towards|candidate for)\s+"
    r"(?:a|an|the)?\s*(?:full[- ]time\s+)?" + _GRAD_DEGREE + r"\b",
    r"\bcurrently in (?:a|an|the)\s+(?:full[- ]time\s+)?" + _GRAD_DEGREE + r"\b",
    r"\bgraduate students? only\b",
    r"\bonly (?:open to |available to |for )?(?:current )?graduate students?\b",
    r"\b(?:open|available) (?:only )?to (?:current )?graduate students?\b",
    # Requirement cues only; a bare "PhD students" is often just the team or mentors.
    r"\b(?:must|should|need to|required to|have to) (?:be|currently be) (?:a|an)?\s*(?:current\s+)?"
    r"(?:full[- ]time\s+)?" + _GRAD_DEGREE + r" (?:students?|candidates?)\b",
    r"\b(?:candidates?|applicants?|students?|interns?)\s+(?:must|should|need to)\s+(?:be\s+)?"
    r"(?:currently\s+)?(?:pursuing|enrolled in|working toward)\s+(?:a|an|the)?\s*"
    r"(?:ms|msc|ma|meng|phd)\b",
    r"\b(?:pursuing|enrolled in|working toward) (?:a|an|the)?\s*(?:ms|msc|meng|master of [a-z]+)\b",
    r"\bgraduate students? (?:currently )?(?:enrolled|pursuing)\b",
]

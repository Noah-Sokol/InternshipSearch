"""
desc: The one place raw job text (title/company/location/description/notes)
    gets turned into structured, canonical JobFacts. Before this module, "is
    this an internship title" was implemented 3 times (a buggy substring
    version in scraper/base.py plus two byte-identical stricter regexes
    copy-pasted into scraper/greenhouse_lever.py and scraper/coverage.py), and
    eligibility/seniority/interest-area classification lived only in
    evaluator/score.py, re-derived from a lowercased text blob on every call.
    classify_job() replaces all of that with one deterministic function whose
    output (JobFacts) is the shared vocabulary every filter/search/ranking
    consumer reads from.

    JobFacts is the FILTER/VOCABULARY layer only -- it never assigns point
    values. evaluator/score.py's weighted ranking and scraper/coverage.py's
    relevance gate remain separate ranking policies that consume JobFacts.

    No dependency on database/* -- scrapers need title-only gating
    (is_internship_title) before a job_id even exists.
args:
    (see classify_job() and hard_exclusion() below)
output:
    (JobFacts dataclass + classification functions)
"""
import re
from dataclasses import dataclass, field

from config import FILTERS
from taxonomy import (
    EligibilityBucket,
    GRAD_DESC_PATTERNS,
    GRAD_TITLE_PATTERNS,
    GRAD_UNDERGRAD_GUARD,
    ExclusionReason,
    IB_HARD_EXCLUDE,
    INTEREST_AREAS,
    NAMED_ELIGIBILITY_TAG_TO_BUCKET,
    NEW_GRAD_SIGNALS,
    RESTRICTED_SIGNALS,
    RIGHT_COHORT_SIGNALS,
    SENIORITY_PENALTY,
    SENIORITY_REWARD,
    SOPHOMORE_SIGNALS,
    WRONG_COHORT_GRAD_YEARS,
    all_canonical_locations,
    canonicalize_location,
    match_keywords,
)

# Bumped whenever this module's classification logic changes materially, so
# persisted JobFacts (database/job_facts.py) know to recompute rather than
# serve a stale row forever.
FACTS_VERSION = 2

# Whole-word internship gate. Stricter than a bare substring check (the bug
# in the old scraper/base.py::_is_internship): a plain "intern" substring
# false-positives on "Internal", "International", "Winternship".
_INTERNSHIP_WORD_RE = re.compile(r"\b(intern|interns|internship|internships|co-op|coop)\b")

# Context words required nearby a wrong-cohort grad year for it to count --
# a bare "2028" appearing anywhere (e.g. in an unrelated sentence) shouldn't
# be treated as a cohort signal.
_WRONG_COHORT_CONTEXT = ["graduat", "class of", "eligible", "expected graduation"]


def is_internship_title(title: str) -> bool:
    """
    desc: Whole-word check for internship language in a job title. The single
        canonical replacement for scraper/base.py's buggy substring version
        and the two identical stricter regexes previously duplicated in
        scraper/greenhouse_lever.py and scraper/coverage.py.
    args:
        title (str): the posting title.
    output:
        (bool): True only if an internship keyword appears as a whole word.
    """
    return bool(_INTERNSHIP_WORD_RE.search((title or "").lower()))


@dataclass(frozen=True)
class JobFacts:
    facts_version: int
    is_internship_title: bool
    canonical_location: str
    # The FULL set of canonical location keys this posting spans -- not just
    # canonical_location's single "first alias wins" primary key. A broad
    # multi-office req belongs to several location sets at once; membership
    # checks (score_location, review/query_filter.py's location= search)
    # must test against this set, not the scalar canonical_location, or a
    # preferred city can be silently missed if it isn't the first one listed.
    all_locations: frozenset = field(default_factory=frozenset)
    location_segments: list = field(default_factory=list)
    seniority_reward_hits: list = field(default_factory=list)
    seniority_penalty_hits: list = field(default_factory=list)
    interest_hits: dict = field(default_factory=dict)
    best_interest_area: str | None = None
    best_interest_strength: float = 0.0
    eligibility_bucket: str = EligibilityBucket.UNKNOWN.value
    eligibility_rationale: str = ""
    mentions_target_cycle: bool = False
    ib_excluded: bool = False


def _classify_eligibility(combined: str, named_tag: str | None) -> tuple[str, str]:
    """
    desc: Classify eligibility into a bucket + rationale. Ported from
        evaluator/score.py::classify_eligibility(), with every substring
        `in combined` check replaced by word/phrase-boundary matching via
        taxonomy.match_keywords(). Trust order: (1) curated named-target tag,
        (2) explicit right-cohort signals, (3) explicit wrong-cohort grad-year
        windows, (4) sophomore/class-year language, (5) restricted-program
        language, else unknown.
    args:
        combined (str): lowercased concatenation of title/company/description/notes.
        named_tag (str | None): eligibility tag from a matched named target.
    output:
        tuple[str, str]: (EligibilityBucket value, rationale string).
    """
    if named_tag:
        bucket = NAMED_ELIGIBILITY_TAG_TO_BUCKET.get(named_tag)
        if bucket:
            return bucket, f"named-target eligibility tag '{named_tag}'"

    right = match_keywords(combined, RIGHT_COHORT_SIGNALS)
    wrong_ctx = bool(match_keywords(combined, _WRONG_COHORT_CONTEXT))
    wrong = match_keywords(combined, WRONG_COHORT_GRAD_YEARS) if wrong_ctx else []
    if "summer 2027" in right and "2027" in wrong:
        wrong.remove("2027")

    restricted = match_keywords(combined, RESTRICTED_SIGNALS)
    soph = match_keywords(combined, SOPHOMORE_SIGNALS)

    if wrong and not right:
        return EligibilityBucket.WRONG_COHORT.value, (
            f"posting names a different grad cohort ({', '.join(sorted(set(wrong)))}) "
            "-- verify grad-year window"
        )
    if restricted:
        return EligibilityBucket.WRONG_COHORT.value, \
            "program appears identity/gender-restricted -- verify eligibility"
    if right:
        return EligibilityBucket.EXPLICIT_FIT.value, f"explicit right-cohort signal: {', '.join(right)}"
    if soph:
        return EligibilityBucket.CLASS_YEAR_PROGRAM.value, f"class-standing program language: {soph[0]}"

    return EligibilityBucket.UNKNOWN.value, "no explicit cohort signal -- verify recruiting year before applying"


def classify_job(title: str, company: str = "", location: str = "",
                  description: str = "", notes: str = "",
                  named_tag: str | None = None) -> JobFacts:
    """
    desc: Classify a single job posting's raw text into canonical JobFacts.
        The one function every consumer (scrapers' hard filters, evaluator's
        ranking, the review queue's field= search) should call instead of
        re-deriving categories from raw text themselves.
    args:
        title (str): posting title.
        company (str): posting company.
        location (str): raw, free-text location.
        description (str): posting body text.
        notes (str): any additional notes (e.g. a named-target eligibility note).
        named_tag (str | None): curated named-target eligibility tag, if any.
    output:
        (JobFacts): structured, canonical classification.
    """
    title = title or ""
    combined = " ".join(t for t in (title, company, description, notes) if t).lower()

    canonical_loc, segments = canonicalize_location(location)
    all_locs = all_canonical_locations(location)

    reward_hits = match_keywords(combined, SENIORITY_REWARD)
    penalty_hits = match_keywords(combined, SENIORITY_PENALTY)

    interest_hits: dict[str, list[str]] = {}
    best_area = None
    best_priority = -1.0
    best_strength = 0.0
    for area in INTEREST_AREAS:
        hits = match_keywords(combined, area["keywords"])
        if not hits:
            continue
        interest_hits[area["name"]] = hits
        strength = min(1.0, 0.6 + 0.15 * len(hits))
        if area["priority"] > best_priority:
            best_priority = area["priority"]
            best_area = area["name"]
            best_strength = strength

    eligibility_bucket, eligibility_rationale = _classify_eligibility(combined, named_tag)

    ib_excluded = bool(match_keywords(combined, IB_HARD_EXCLUDE))
    mentions_cycle = FILTERS.get("prioritize_year", "") in combined

    return JobFacts(
        facts_version=FACTS_VERSION,
        is_internship_title=is_internship_title(title),
        canonical_location=canonical_loc,
        all_locations=all_locs,
        location_segments=segments,
        seniority_reward_hits=reward_hits,
        seniority_penalty_hits=penalty_hits,
        interest_hits=interest_hits,
        best_interest_area=best_area,
        best_interest_strength=best_strength,
        eligibility_bucket=eligibility_bucket,
        eligibility_rationale=eligibility_rationale,
        mentions_target_cycle=mentions_cycle,
        ib_excluded=ib_excluded,
    )


def hard_exclusion(facts: JobFacts) -> str | None:
    """
    desc: The FILTER predicate -- exact, boolean. Returns why a posting should
        be hard-excluded from discovery entirely, or None if it survives.
        Replaces scraper/base.py's _is_excluded (which mashed IB/senior/
        new-grad into one undifferentiated substring check) with a single
        function that names the specific reason, reading the SAME tokenized
        hit lists evaluator/score.py's soft seniority penalty reads -- so the
        hard scraper-time gate and the soft post-staging penalty can never
        drift apart the way two independently-substring-matched lists could.
        Deliberately preserves the original hard-filter semantics: ANY
        penalty hit excludes, with no "reward words cancel it out" exception
        (that cancellation only exists in evaluator/score.py's soft, graded
        seniority scorer for postings that already survived this hard gate).
    args:
        facts (JobFacts): output of classify_job().
    output:
        (str | None): an ExclusionReason value, or None if not excluded.
    """
    if not facts.is_internship_title:
        return ExclusionReason.NOT_INTERNSHIP.value
    if facts.ib_excluded:
        return ExclusionReason.INVESTMENT_BANKING.value
    if facts.seniority_penalty_hits:
        if any(hit in NEW_GRAD_SIGNALS for hit in facts.seniority_penalty_hits):
            return ExclusionReason.NEW_GRAD.value
        return ExclusionReason.SENIOR_LEVEL.value
    # Restricted-program language (RESTRICTED_SIGNALS) deliberately does NOT
    # hard-exclude here -- per the original design it's a strong caution
    # (drops the eligibility bucket to wrong_cohort, handled in
    # _classify_eligibility above) rather than an automatic drop, since
    # context can vary. ExclusionReason.RESTRICTED_PROGRAM is reserved for a
    # future caller that wants to hard-gate on it explicitly.
    return None



_GRAD_TITLE_RES = [re.compile(p) for p in GRAD_TITLE_PATTERNS]
_GRAD_DESC_RES = [re.compile(p) for p in GRAD_DESC_PATTERNS]
_GRAD_GUARD_RES = [re.compile(p) for p in GRAD_UNDERGRAD_GUARD]
_SENTENCE_SPLIT_RE = re.compile(r"[.;!?\n\r•]+")


def _normalize_grad_text(text: str) -> str:
    """
    desc: Lowercase and collapse spelling variants (Ph.D., B.S., curly
        apostrophes) so the graduate regexes see one form.
    args:
        text (str): raw title or description.
    output:
        str: normalized text.
    """
    t = (text or "").lower().replace("’", "'").replace("‘", "'").replace("`", "'")
    t = re.sub(r"\bph\.\s?d\b\.?", "phd", t)
    t = re.sub(r"\bb\.\s?([sa])\b\.?", r"b\1", t)
    t = re.sub(r"\bm\.\s?s\b\.?", "ms", t)
    return t


def is_graduate_only(title: str, description: str = "") -> bool:
    """
    desc: True when a posting targets graduate students only: the title names a
        graduate audience (PhD / MBA / Master's / Graduate Intern ...), or a
        description sentence requires current graduate enrollment. Anything
        that also admits undergraduates (Bachelor's, BS/MS, "Undergraduate")
        in the same title/sentence is NOT flagged.
    args:
        title (str): posting title.
        description (str): posting body text.
    output:
        bool: True if graduate-only.
    """
    t = _normalize_grad_text(title)
    if t and any(r.search(t) for r in _GRAD_TITLE_RES) and not any(r.search(t) for r in _GRAD_GUARD_RES):
        return True
    if any(r.search(t) for r in _GRAD_GUARD_RES):   # e.g. a Sophomore program: grad mentions are incidental
        return False
    for sentence in _SENTENCE_SPLIT_RE.split(_normalize_grad_text(description)):
        if not any(r.search(sentence) for r in _GRAD_DESC_RES):
            continue
        if any(r.search(sentence) for r in _GRAD_GUARD_RES):
            continue
        return True
    return False

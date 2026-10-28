"""
desc: Preference model for the evaluator subsystem -- the weights and
    eligibility-fit fractions that define how much Noah should prefer a
    staged internship posting. This module owns every tunable SCORING-WEIGHT
    constant (WEIGHTS, PRESTIGE_TIERS, SKILL_KEYWORDS, URGENCY_*,
    ELIGIBILITY_FIT); the underlying CLASSIFICATION vocabularies (which
    keywords define an interest area, what counts as a preferred location,
    seniority/eligibility signal words) now live in taxonomy.py as the single
    canonical definition shared with scraper/* and classify.py, and are
    re-exported here UNCHANGED (same list/dict objects, not copies) so this
    module's existing runtime-override system (load_overrides/save_overrides/
    reset_overrides, backed by evaluator/user_overrides.json and wired into
    review/app.py's Preferences page) keeps mutating the one true copy in
    place with no change needed at any call site.
    Nothing here touches config.py or database/db.py. All scoring is
    deterministic and rule-based -- no network, no API calls.
args:
    (module-level constants only; see individual objects below)
output:
    (module of constants and small helper predicates: dict / list / set / func)
"""
from taxonomy import (  # noqa: F401 -- re-exported for evaluator/score.py + review/app.py
    INTEREST_AREAS,
    TARGET_LOCATIONS,
    canonicalize_location,
    prestige_tier,
)

# "Today" for urgency math. "" means the real clock (score._parse_today);
# tests pass a fixed date explicitly for reproducibility.
DEFAULT_TODAY = ""


# ---------------------------------------------------------------------------
# Top-level score component weights (points each component can contribute).
# The total possible from the positive components is ~100; penalties and the
# hard-zero rule are applied on top / can override. These are relative
# importances, chosen to reflect Noah's stated priorities:
#   eligibility fit and interest match dominate; a genuinely-eligible,
#   on-interest, closing-soon, named-confirmed role should float to the top.
# ---------------------------------------------------------------------------
WEIGHTS = {
    "interest": 30,       # how well the role matches Noah's interest areas
    "eligibility": 30,    # right cohort (sophomore / Summer 2027 / 2029 grad)
    "named_target": 12,   # a curated, confirmed named target beats a random hit
    "prestige": 10,       # company tier / selectivity bonus
    "seniority": 8,       # intern-level (reward) vs senior/new-grad language
    "location": 6,        # in a preferred location
    "urgency": 6,         # closing soon / time-sensitive
    "learned": 10,        # +/- cap on the signal learned from Noah's own actions (0 = off)
}


# INTEREST_AREAS is imported from taxonomy.py above (the interest-area
# keyword vocabulary is a classification concern shared with classify.py and
# scraper/*, not a scoring-only concern). This module still owns the WEIGHT
# applied to it (WEIGHTS["interest"]) and the skill-overlap bonus below.

# Skill/tooling keywords from Noah's real background. A posting that names the
# stack he actually knows is a stronger fit than a bare title match, so matches
# here add a small bonus inside the interest component (does not change which
# area wins, just rewards concrete overlap).
SKILL_KEYWORDS = [
    "python", "pandas", "numpy", "scikit-learn", "sklearn", "xgboost",
    "streamlit", "gradient boosting", "random forest", "sql", "statistics",
    "backtesting", "sports", "baseball", "mlb", "claude", "llm",
]
SKILL_BONUS_PER_HIT = 1.5
SKILL_BONUS_CAP = 6.0


# IB_HARD_EXCLUDE and SENIORITY_REWARD/SENIORITY_PENALTY are imported from
# taxonomy.py above -- they're the same word lists classify.py's hard-filter
# gate (classify.hard_exclusion) reads at scrape time, so the hard gate and
# this module's soft, graded seniority penalty (score_seniority(), below via
# evaluator/score.py) can never drift out of sync with each other.


# ---------------------------------------------------------------------------
# Eligibility fit. The scorer classifies each posting into one of these buckets
# from (in order of trust): a curated named-target eligibility tag, explicit
# grad-year windows in the text, or class-standing language. The value is the
# fraction of the eligibility weight awarded (1.0 = perfect fit, 0.0 = wrong
# cohort). "unknown" sits at a cautious middle -- rolling postings often omit a
# year, which is a verify-manually signal, not a disqualifier.
# ---------------------------------------------------------------------------
ELIGIBILITY_FIT = {
    "confirmed_fit": 1.00,        # curated named target tagged eligible for Noah
    "explicit_fit": 0.95,         # text names 2027 cycle / 2029 grad / sophomore
    "class_year_program": 0.90,   # class-standing program (fits by standing)
    "needs_verification": 0.62,   # plausible but unconfirmed
    "unknown": 0.50,              # no cohort signal -> verify before applying
    "wrong_cohort": 0.05,         # explicitly a different grad-year cohort
    "not_eligible": 0.00,         # curated as not eligible (incl. restricted)
}

# NAMED_ELIGIBILITY_TAG_TO_BUCKET, WRONG_COHORT_GRAD_YEARS,
# RIGHT_COHORT_SIGNALS, SOPHOMORE_SIGNALS, and RESTRICTED_SIGNALS are all
# imported from taxonomy.py above -- classify.py's eligibility classification
# reads the exact same lists this module's ELIGIBILITY_FIT converts to points.


# ---------------------------------------------------------------------------
# Location preference. Full award for an explicitly-preferred location, partial
# for "unknown/blank" (many named-target pages don't list one), zero (no bonus,
# not a penalty) for a clearly non-preferred location. TARGET_LOCATIONS itself
# (the preferred-location set, as canonical taxonomy.canonicalize_location()
# keys) is imported from taxonomy.py above.
# ---------------------------------------------------------------------------
LOCATION_FULL = 1.00
LOCATION_UNKNOWN = 0.45
LOCATION_OTHER = 0.0


# ---------------------------------------------------------------------------
# Urgency / time-sensitivity. Scored from explicit deadline dates in the text
# when parseable, otherwise from qualitative timing language in the notes.
# Closing sooner -> higher urgency (rank up). Fractions of the urgency weight.
# ---------------------------------------------------------------------------
URGENCY_LEVELS = {
    "overdue_or_now": 1.00,   # deadline passed/imminent, or "apply now/ASAP"
    "soon": 0.80,             # closing within ~30 days
    "this_cycle": 0.55,       # active this cycle, rolling, watch-now
    "later": 0.30,            # window opens later
    "unknown": 0.35,          # no timing info
}

# Qualitative timing phrases -> urgency level (checked in notes + description).
URGENCY_PHRASES = {
    "overdue_or_now": [
        "apply now", "apply asap", "apply immediately", "closing soon",
        "deadline approaching", "rolling basis", "rolling review",
        "fills fast", "past the", "golden window",
    ],
    "soon": [
        "check this url now", "check now", "expected this month",
        "info expected", "open now", "check the page", "check weekly",
        "starting now",
    ],
    "later": [
        "has not been posted yet", "not yet posted", "expect the next window",
        "opens around", "watch for", "when the", "historically opens",
        "check the careers page", "not been posted",
    ],
}


# ---------------------------------------------------------------------------
# OPTIONAL LLM-assisted relevance refinement hook. DISABLED BY DEFAULT.
# The deterministic scorer above is the whole default path -- it makes NO
# network or API calls, honoring the project's no-paid-APIs / don't-burn-quota
# rule. If ever enabled, an LLM could nudge the interest sub-score by at most
# +/- LLM_MAX_ADJUST points based on the full description; it must never change
# the IB hard-zero or the eligibility bucket. See evaluator/score.py's
# refine_with_llm() stub -- it raises unless explicitly turned on.
# ---------------------------------------------------------------------------
ENABLE_LLM_REFINEMENT = False
LLM_MAX_ADJUST = 5.0


# PRESTIGE_TIERS and prestige_tier() are imported from taxonomy.py above --
# "which tier is company X in" is a classification fact (a set-membership
# question), not a scoring-weight tunable, so it lives alongside
# LOCATION_ALIASES/INTEREST_AREAS rather than being redefined here.
#
# is_ib() previously lived here as a bare substring check; it's gone now --
# IB exclusion is computed once per job as JobFacts.ib_excluded (see
# classify.classify_job()) and evaluator/score.py reads that field directly,
# so there's no second, independently-substring-matched copy of the check.


# ---------------------------------------------------------------------------
# Runtime overrides -- lets the Preferences UI page (review/app.py) tune
# WEIGHTS, TARGET_LOCATIONS, and per-area interest priorities without editing
# this file. Persisted to a small JSON file next to this module; applied by
# mutating the module-level objects above IN PLACE (not reassigning the
# names), so evaluator/score.py's `P.WEIGHTS` etc. attribute lookups always
# see the current values with no code change needed there. Pure local file
# I/O -- no network, no API calls.
# ---------------------------------------------------------------------------
import json as _json
from pathlib import Path as _Path

_OVERRIDES_PATH = _Path(__file__).resolve().parent / "user_overrides.json"

# Pristine copies of the hardcoded defaults, captured now (before any
# override is applied) so "reset to defaults" has something to restore from.
_DEFAULT_WEIGHTS = dict(WEIGHTS)
_DEFAULT_TARGET_LOCATIONS = list(TARGET_LOCATIONS)
_DEFAULT_INTEREST_PRIORITIES = {a["name"]: a["priority"] for a in INTEREST_AREAS}


def _apply_overrides(data: dict) -> None:
    """
    desc: Apply an overrides dict to the live module-level objects, in place.
        Unknown/malformed keys are ignored rather than raising, so a partial
        or hand-edited override file can't crash the app.
    args:
        data (dict): {"weights": {...}, "target_locations": [...],
            "interest_priorities": {area_name: priority}}, any subset.
    output:
        None
    """
    weights = data.get("weights")
    if isinstance(weights, dict):
        for k, v in weights.items():
            if k in WEIGHTS:
                try:
                    WEIGHTS[k] = float(v)
                except (TypeError, ValueError):
                    continue

    locations = data.get("target_locations")
    if isinstance(locations, list):
        # note: `is instance list` (not truthiness) so an explicitly-saved
        # empty list clears TARGET_LOCATIONS instead of being ignored as
        # "no override" -- a bare `and locations` here previously made it
        # impossible to ever save "no preferred locations".
        #
        # Each entry is run through taxonomy.canonicalize_location() so a
        # user-typed "NYC" and "New York, NY" land on the SAME canonical key
        # ("new_york") that score_location()/classify.py compare against --
        # otherwise typing an alias here would silently create a preference
        # that never matches once location comparisons are exact rather than
        # substring. Order-preserving de-dup (a raw list `set()` would not
        # preserve the order the user typed them in, which review/app.py's
        # Preferences textarea round-trips).
        canonical = []
        for loc in locations:
            loc = str(loc).strip()
            if not loc:
                continue
            key, _ = canonicalize_location(loc)
            if key not in canonical:
                canonical.append(key)
        TARGET_LOCATIONS[:] = canonical

    priorities = data.get("interest_priorities")
    if isinstance(priorities, dict):
        for area in INTEREST_AREAS:
            if area["name"] in priorities:
                try:
                    area["priority"] = float(priorities[area["name"]])
                except (TypeError, ValueError):
                    continue


def load_overrides() -> None:
    """
    desc: Load evaluator/user_overrides.json (if present) and apply it over
        the hardcoded defaults. Safe to call multiple times. Any read/parse
        failure is swallowed and leaves current values untouched, so a
        corrupted override file degrades to "no override" rather than crashing.
    args:
        (none)
    output:
        None
    """
    if not _OVERRIDES_PATH.exists():
        return
    try:
        data = _json.loads(_OVERRIDES_PATH.read_text())
    except Exception:
        return
    _apply_overrides(data)


def save_overrides(weights: dict = None, target_locations: list = None,
                    interest_priorities: dict = None) -> None:
    """
    desc: Persist the given overrides to evaluator/user_overrides.json and
        apply them immediately (in-memory) so the change takes effect on the
        very next scoring pass, no restart needed. Any argument left as None
        keeps its previously-saved value (merges onto the existing file).
    args:
        weights (dict): {component_name: weight}, subset of WEIGHTS keys.
        target_locations (list): replacement list of preferred location strings.
        interest_priorities (dict): {area_name: priority float}.
    output:
        None
    """
    current = {}
    if _OVERRIDES_PATH.exists():
        try:
            current = _json.loads(_OVERRIDES_PATH.read_text())
        except Exception:
            current = {}

    if weights is not None:
        current["weights"] = weights
    if target_locations is not None:
        current["target_locations"] = target_locations
    if interest_priorities is not None:
        current["interest_priorities"] = interest_priorities

    _OVERRIDES_PATH.write_text(_json.dumps(current, indent=2))
    _apply_overrides(current)


def reset_overrides() -> None:
    """
    desc: Restore WEIGHTS, TARGET_LOCATIONS, and interest priorities to their
        hardcoded defaults and delete the override file.
    args:
        (none)
    output:
        None
    """
    WEIGHTS.clear()
    WEIGHTS.update(_DEFAULT_WEIGHTS)
    TARGET_LOCATIONS[:] = list(_DEFAULT_TARGET_LOCATIONS)
    for area in INTEREST_AREAS:
        if area["name"] in _DEFAULT_INTEREST_PRIORITIES:
            area["priority"] = _DEFAULT_INTEREST_PRIORITIES[area["name"]]
    if _OVERRIDES_PATH.exists():
        _OVERRIDES_PATH.unlink()


# Apply any saved overrides as soon as this module is imported, so every
# consumer (evaluator/score.py, review/app.py) sees tuned values by default.
load_overrides()

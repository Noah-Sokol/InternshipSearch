"""
desc: Deterministic, rule-based scorer that ranks the internship postings
    already staged in the review queue by how much Noah should prefer them.
    Reads staged applications READ-ONLY through database/db.py helpers, scores
    each on interest match, eligibility fit, seniority, location, named-target
    priority, and deadline urgency, hard-zeroes anything investment-banking,
    and prints a best-first ranking with a human-readable rationale. Also
    exposes score_staged() so other code (e.g. review/app.py) can consume the
    ranking. The default path makes NO network / API calls.

    Persistence: if --save is passed, scores are written to a SEPARATE table
    (evaluator_scores) that this module owns and creates itself -- it does NOT
    modify database/db.py's schema or init.

args:
    (see CLI below; primary entry points are score_staged() and main())
output:
    (functions returning score dicts; CLI prints a ranking / writes a table)

Usage:
    python -m evaluator                 # print ranked staged postings
    python -m evaluator.score           # same
    python -m evaluator --json          # emit ranking as JSON
    python -m evaluator --save          # also persist to evaluator_scores table
    python -m evaluator --today 2026-10-01   # override "today" for urgency
    python -m evaluator --top 5         # only the top N
"""

import re
import sys
import json
import argparse
from datetime import date, datetime

# READ-ONLY use of the existing DB helper. We never write through db.py.
from database.db import get_queued_applications, get_discovered_applications, get_conn
from database.job_facts import get_facts as _get_job_facts

from classify import FACTS_VERSION
from taxonomy import match_keywords
from evaluator import preferences as P
from evaluator.learning.model import learned_points, get_tally, learning_fingerprint


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def _app_text(app: dict) -> dict:
    """
    desc: Pull the relevant free-text fields out of a staged-application dict
        and package them for scoring. Also surfaces any eligibility_note stored
        inside the form_data JSON blob (main.py stashes named-target notes
        there). Never emits or logs the value anywhere off-machine.
    args:
        app (dict): one row from get_queued_applications() (application JOIN job).
    output:
        dict: {"title","company","location","description","notes","combined"}
            all strings; "combined" is the lowercased concatenation used for
            keyword matching.
    """
    title = (app.get("title") or "").strip()
    company = (app.get("company") or "").strip()
    location = (app.get("location") or "").strip()
    description = (app.get("description") or "").strip()
    notes = (app.get("notes") or "").strip()

    # eligibility_note tucked into form_data JSON, if present.
    form_note = ""
    try:
        fd = json.loads(app.get("form_data") or "{}")
        form_note = (fd.get("eligibility_note") or "").strip()
    except Exception:
        form_note = ""

    all_notes = " ".join(n for n in (notes, form_note) if n).strip()
    combined = " ".join([title, company, description, all_notes]).lower()
    return {
        "title": title,
        "company": company,
        "location": location,
        "description": description,
        "notes": all_notes,
        "combined": combined,
    }


def _parse_today(today_str: str) -> date:
    """
    desc: Parse a YYYY-MM-DD string into a date for urgency math.
    args:
        today_str (str): date in ISO format, or "" / None to use today's clock.
    output:
        date: the reference date.
    """
    if not today_str:
        return date.today()
    return datetime.strptime(today_str.strip(), "%Y-%m-%d").date()


# ---------------------------------------------------------------------------
# Component scorers -- each returns (points, rationale_str).
# ---------------------------------------------------------------------------
def score_interest(text: dict, facts):
    """
    desc: Score interest-area match, reading the classification already done
        once by classify.classify_job() (facts.best_interest_area/strength)
        instead of re-scanning text["combined"] against P.INTEREST_AREAS on
        every call. Adds a capped bonus for named skills/tools Noah actually
        uses (a pure scoring add-on, not a classification fact, so it's
        computed here rather than stored on JobFacts).
    args:
        text (dict): output of _app_text() (used only for the skill-bonus scan).
        facts (classify.JobFacts): this posting's canonical classification.
    output:
        tuple[float, str]: (points out of WEIGHTS['interest'] plus skill bonus,
            rationale).
    """
    if facts.best_interest_area is None:
        return 0.0, "no interest-area keywords matched"

    area = next(a for a in P.INTEREST_AREAS if a["name"] == facts.best_interest_area)
    base = P.WEIGHTS["interest"] * area["priority"] * facts.best_interest_strength

    # skill/tool overlap bonus -- word-boundary matched via taxonomy.match_keywords
    # rather than a bare substring scan, so e.g. "r" or "c" style short skill
    # tokens (if ever added) can't match inside an unrelated word.
    skill_hits = sorted(set(match_keywords(text["combined"], P.SKILL_KEYWORDS)))
    bonus = min(P.SKILL_BONUS_CAP, P.SKILL_BONUS_PER_HIT * len(skill_hits))

    # Cap interest at its weight so the overall total never exceeds max_score;
    # the skill bonus can lift a weak title match but not blow past the ceiling.
    pts = min(P.WEIGHTS["interest"], base + bonus)
    rat = f"interest: {area['label']} (priority {area['priority']:.2f})"
    if skill_hits:
        shown = ", ".join(skill_hits[:5])
        rat += f"; skills matched: {shown}"
    return pts, rat


def score_eligibility(facts):
    """
    desc: Convert the eligibility bucket classify.classify_job() already
        assigned into points. The bucket/rationale derivation itself
        (formerly this module's classify_eligibility()) now lives in
        classify.py, shared with the scraper-time hard filter -- this
        function is only the scoring-weight mapping (facts stay a pure
        classification result, uncontaminated by point values).
    args:
        facts (classify.JobFacts): this posting's canonical classification.
    output:
        tuple[float, str, str]: (points out of WEIGHTS['eligibility'],
            rationale, bucket key).
    """
    frac = P.ELIGIBILITY_FIT[facts.eligibility_bucket]
    pts = P.WEIGHTS["eligibility"] * frac
    rat = f"eligibility: {facts.eligibility_bucket} ({facts.eligibility_rationale})"
    return pts, rat, facts.eligibility_bucket


def score_seniority(facts):
    """
    desc: Reward intern/underclassman language, penalize senior/new-grad
        language. Net within +/- WEIGHTS['seniority']. Reads the same
        word-boundary-matched hit lists (facts.seniority_reward_hits/
        seniority_penalty_hits) classify.hard_exclusion() uses for the hard
        scraper-time gate, so this soft, graded penalty can never drift out
        of sync with what already got excluded before staging.
    args:
        facts (classify.JobFacts): this posting's canonical classification.
    output:
        tuple[float, str]: (points, rationale).
    """
    reward_hits = facts.seniority_reward_hits
    penalty_hits = facts.seniority_penalty_hits
    w = P.WEIGHTS["seniority"]

    pts = 0.0
    if reward_hits:
        pts += w * min(1.0, 0.7 + 0.15 * len(reward_hits))
    if penalty_hits:
        pts -= w * min(1.0, 0.5 * len(penalty_hits))
    pts = max(-w, min(w, pts))

    bits = []
    if reward_hits:
        bits.append(f"intern-level: {reward_hits[0]}")
    if penalty_hits:
        bits.append(f"senior/new-grad language: {penalty_hits[0]}")
    rat = "seniority: " + ("; ".join(bits) if bits else "neutral")
    return pts, rat


def score_location(facts, raw_location: str):
    """
    desc: Award points for a preferred location; partial for blank/unknown.
        Checks facts.all_locations -- the FULL set of canonical keys this
        posting spans -- for a non-empty intersection with P.TARGET_LOCATIONS,
        rather than comparing a single scalar "primary" location. A broad
        multi-office req ("Palo Alto, CA, ..., Washington, DC, ..., Chicago,
        IL, ..., NYC, ...") genuinely belongs to several location sets at
        once; checking only facts.canonical_location (whichever alias happens
        to appear FIRST in the posting's own city list) could silently miss a
        preferred city listed later -- exactly the false-negative this
        intersection check fixes. Also replaces the old `t in loc` substring
        scan, which could false-positive on a short alias inside an unrelated
        word.
    args:
        facts (classify.JobFacts): this posting's canonical classification.
        raw_location (str): the job's raw location text, for the rationale
            message only.
    output:
        tuple[float, str]: (points out of WEIGHTS['location'], rationale).
    """
    w = P.WEIGHTS["location"]
    if not facts.all_locations:
        return w * P.LOCATION_UNKNOWN, "location: unspecified (partial credit)"
    hits = sorted(facts.all_locations & set(P.TARGET_LOCATIONS))
    if hits:
        return w * P.LOCATION_FULL, f"location: preferred ({hits[0].replace('_', ' ')})"
    return w * P.LOCATION_OTHER, f"location: '{raw_location}' not in preferred set"


def score_prestige(company: str):
    """
    desc: Award points based on company prestige tier. Tier 1 = elite quant/tech,
        Tier 2 = strong brand, Tier 3 = known firm. No match = no bonus, no penalty.
    args:
        company (str): company name from the job posting.
    output:
        tuple[float, str]: (points out of WEIGHTS['prestige'], rationale).
    """
    frac = P.prestige_tier(company)
    pts = P.WEIGHTS["prestige"] * frac
    if frac == 0.0:
        rat = "prestige: unranked (no tier match)"
    elif frac >= 1.0:
        rat = f"prestige: tier 1 / elite ({company})"
    elif frac >= 0.6:
        rat = f"prestige: tier 2 / strong brand ({company})"
    else:
        rat = f"prestige: tier 3 / known firm ({company})"
    return pts, rat


def score_named_target(named_tag: str, company: str):
    """
    desc: Reward postings that correspond to a curated named target -- a
        confirmed named target should outrank a random keyword hit. Confirmed
        tags get full credit; verification-pending get partial; not-eligible /
        wrong-cohort tags get none (their eligibility bucket already penalizes).
    args:
        named_tag (str): the matched named-target eligibility tag, or None.
        company (str): posting company (for the rationale).
    output:
        tuple[float, str]: (points out of WEIGHTS['named_target'], rationale).
    """
    w = P.WEIGHTS["named_target"]
    if not named_tag:
        return 0.0, "not a curated named target"
    if named_tag in ("confirmed", "check_now", "time_sensitive"):
        return w, f"curated named target ({company}), eligibility confirmed"
    if named_tag == "needs_verification":
        return w * 0.6, f"curated named target ({company}), pending verification"
    # not_eligible / not_eligible_yet: no bonus.
    return 0.0, f"curated named target ({company}), but not currently eligible"


_DATE_RE = re.compile(
    r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b|"
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(20\d{2})\b",
    re.IGNORECASE,
)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
     "nov", "dec"], start=1)}


def _extract_dates(text_str: str):
    """
    desc: Pull candidate dates out of free text (ISO or "Mon D, YYYY").
    args:
        text_str (str): text to scan (notes + description).
    output:
        list[date]: parsed dates (may be empty).
    """
    out = []
    for m in _DATE_RE.finditer(text_str):
        try:
            if m.group(1):
                out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
            else:
                mon = _MONTHS[m.group(4).lower()[:3]]
                out.append(date(int(m.group(6)), mon, int(m.group(5))))
        except (ValueError, KeyError):
            continue
    return out


def score_urgency(text: dict, today: date):
    """
    desc: Score time-sensitivity. Prefers an explicit future deadline date
        (sooner -> higher). If no usable date, falls back to qualitative timing
        phrases in the notes/description. Closing-soon ranks up.
    args:
        text (dict): output of _app_text().
        today (date): reference date for deadline math.
    output:
        tuple[float, str]: (points out of WEIGHTS['urgency'], rationale).
    """
    w = P.WEIGHTS["urgency"]
    scan = f"{text['notes']} {text['description']}"
    combined = text["combined"]

    # (1) explicit dates -> nearest FUTURE date treated as a soft deadline.
    dates = _extract_dates(scan)
    future = sorted(d for d in dates if d >= today)
    if future:
        days = (future[0] - today).days
        if days <= 14:
            return w * P.URGENCY_LEVELS["overdue_or_now"], \
                f"urgency: deadline ~{future[0].isoformat()} (~{days}d)"
        if days <= 30:
            return w * P.URGENCY_LEVELS["soon"], \
                f"urgency: deadline ~{future[0].isoformat()} (~{days}d)"
        if days <= 120:
            return w * P.URGENCY_LEVELS["this_cycle"], \
                f"urgency: deadline ~{future[0].isoformat()} (~{days}d)"
        return w * P.URGENCY_LEVELS["later"], \
            f"urgency: deadline ~{future[0].isoformat()} (~{days}d out)"

    # (2) qualitative phrases (order = strongest first).
    for level in ("overdue_or_now", "soon", "later"):
        for phrase in P.URGENCY_PHRASES[level]:
            if phrase in combined:
                return w * P.URGENCY_LEVELS[level], f"urgency: '{phrase}'"

    return w * P.URGENCY_LEVELS["unknown"], "urgency: no timing signal"


# ---------------------------------------------------------------------------
# Named-target matching (evaluator-owned, so we don't have to import config).
# We import config.NAMED_TARGETS READ-ONLY only to recover a posting's curated
# eligibility tag; we never write config or depend on its mutation.
# ---------------------------------------------------------------------------
def _load_named_targets():
    """
    desc: Best-effort READ-ONLY load of config.NAMED_TARGETS for eligibility
        tags. If config can't be imported (e.g. run in isolation), returns [].
    args:
        (none)
    output:
        list[dict]: named-target dicts, or [] on failure.
    """
    try:
        from config import NAMED_TARGETS
        return list(NAMED_TARGETS)
    except Exception:
        return []


def _match_named_tag(app: dict, named_targets: list):
    """
    desc: Find the curated eligibility tag for a staged posting by matching its
        company (and loosely its title/program) against NAMED_TARGETS.
    args:
        app (dict): staged application row.
        named_targets (list): output of _load_named_targets().
    output:
        str | None: the eligibility tag ("confirmed", "not_eligible", ...) or
            None if the posting isn't a curated named target.
    """
    company = (app.get("company") or "").strip().lower()
    title = (app.get("title") or "").strip().lower()
    if not company:
        return None
    best = None
    for t in named_targets:
        tc = (t.get("company") or "").strip().lower()
        if not tc:
            continue
        if tc == company or tc in company or company in tc:
            # prefer a program-name overlap when a company has multiple entries
            prog = (t.get("program") or "").strip().lower()
            overlap = bool(prog and title and (prog[:20] in title or title[:20] in prog))
            score = 2 if overlap else 1
            if best is None or score > best[0]:
                best = (score, t.get("eligibility"))
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Optional LLM refinement hook -- DISABLED by default.
# ---------------------------------------------------------------------------
def refine_with_llm(app: dict, base_score: float):
    """
    desc: OPTIONAL, DISABLED-BY-DEFAULT hook to let an LLM nudge the interest
        sub-score based on the full description. Not called in the default
        scoring path. Honors the project's no-paid-APIs rule: it refuses to run
        unless preferences.ENABLE_LLM_REFINEMENT is explicitly set True, and
        even then it must never override the IB hard-zero or eligibility bucket.
        Left as a stub so no Anthropic API call can happen by accident.
    args:
        app (dict): staged application row.
        base_score (float): the deterministic score to (optionally) adjust.
    output:
        float: an adjustment in [-LLM_MAX_ADJUST, +LLM_MAX_ADJUST] (0.0 stub).
    raises:
        RuntimeError: if called while the feature flag is off.
    """
    if not P.ENABLE_LLM_REFINEMENT:
        raise RuntimeError(
            "LLM refinement is disabled by default (no external API calls in "
            "the default path). Enable evaluator.preferences.ENABLE_LLM_REFINEMENT "
            "and implement this hook to use it."
        )
    # Intentionally not implemented: no API call is made in this repo's default
    # path. An implementation would build a prompt from app['description'] and
    # clamp its output to +/- P.LLM_MAX_ADJUST. Never touches IB / eligibility.
    return 0.0


# ---------------------------------------------------------------------------
# Top-level scoring
# ---------------------------------------------------------------------------
def score_application(app: dict, today: date, named_targets: list, tally: dict = None):
    """
    desc: Score a single staged application. Applies the IB hard-zero first,
        then sums the weighted components. Produces a total and a rationale.
    args:
        app (dict): staged application row (application JOIN job).
        today (date): reference date for urgency.
        named_targets (list): curated named targets for eligibility tagging.
        tally (dict | None): precomputed learning tally (see evaluator.learning.model.get_tally).
    output:
        dict: {"application_id","company","title","location","url","score",
            "max_score","excluded","components","rationale"}.
    """
    text = _app_text(app)
    named_tag = _match_named_tag(app, named_targets)

    # Read the ONE canonical classification computed for this job (see
    # database/job_facts.py + classify.py) instead of re-deriving interest/
    # seniority/eligibility/IB from text["combined"] here. Passing the raw
    # text args lets get_facts() recompute transparently if the persisted
    # row predates this job (or classify.py's logic, via facts_version).
    facts = _get_job_facts(
        app.get("job_id"), title=text["title"], company=text["company"],
        location=text["location"], description=text["description"],
        notes=text["notes"], named_tag=named_tag,
    )

    # "learned" is a +/- nudge on top of the 100-point scale, not part of it
    max_score = sum(v for k, v in P.WEIGHTS.items() if k != "learned")

    # HARD EXCLUSION: investment banking -> score 0, no matter what.
    if facts.ib_excluded:
        return {
            "application_id": app.get("id"),
            "company": text["company"],
            "title": text["title"],
            "location": text["location"],
            "url": app.get("url", ""),
            "score": 0.0,
            "adjusted_score": 0.0,
            "max_score": max_score,
            "excluded": True,
            "eligibility_bucket": "ib_excluded",
            "components": {"ib_hard_exclude": 0.0},
            "rationale": "HARD-EXCLUDED: investment banking (Noah does not want IB).",
        }

    i_pts, i_rat = score_interest(text, facts)
    e_pts, e_rat, e_bucket = score_eligibility(facts)
    n_pts, n_rat = score_named_target(named_tag, text["company"])
    p_pts, p_rat = score_prestige(text["company"])
    s_pts, s_rat = score_seniority(facts)
    l_pts, l_rat = score_location(facts, text["location"])
    u_pts, u_rat = score_urgency(text, today)
    # learned component: capped +/- P.WEIGHTS["learned"] from Noah's past actions
    # (evaluator/learning); 0 disables it and leaves no trace in the rationale.
    learn_pts, learn_rat = learned_points(app, facts, tally)

    components = {
        "interest": round(i_pts, 2),
        "eligibility": round(e_pts, 2),
        "named_target": round(n_pts, 2),
        "prestige": round(p_pts, 2),
        "seniority": round(s_pts, 2),
        "location": round(l_pts, 2),
        "urgency": round(u_pts, 2),
        "learned": round(learn_pts, 2),
    }
    total = sum(components.values())
    # clamp to [0, max_score] (seniority penalty can't drive below 0)
    total = max(0.0, min(max_score, total))

    # score_adjustment (the Queue view's ▲/▼ manual votes) folded in here as
    # a SEPARATE "adjusted_score" field, capped at max_score -- "score" stays
    # the pure base value on purpose, since review/app.py's _capped_score()
    # already adds its own copy of the adjustment on top of "score"; adding
    # it here too would double-count it there.
    adj = app.get("score_adjustment") or 0
    adjusted_score = round(max(0.0, min(max_score, total + adj)), 2)
    parts = [i_rat, e_rat, n_rat, p_rat, s_rat, l_rat, u_rat]
    if learn_rat:
        parts.append(learn_rat)
    if adj:
        parts.insert(0, f"manual adjustment: {adj:+d}")
    rationale = " | ".join(parts)

    return {
        "application_id": app.get("id"),
        "company": text["company"],
        "title": text["title"],
        "location": text["location"],
        "url": app.get("url", ""),
        "score": round(total, 2),
        "adjusted_score": adjusted_score,
        "max_score": max_score,
        "excluded": False,
        "eligibility_bucket": e_bucket,
        "components": components,
        "rationale": rationale,
    }


# review/app.py calls score_staged() (via _preference_scores()) several times
# per Streamlit rerun -- once for the sidebar stats, and again from whichever
# view is active (Discovered, Queue, Detail, Applied) -- and Streamlit reruns on almost
# every widget interaction. Each call previously re-issued the DB query AND
# re-ran every text/regex-based component scorer over every staged row from
# scratch, even when nothing staged had changed since the last call in the
# same rerun (or since the last rerun, if the user just clicked something
# unrelated to scoring). Cache the ranked result and only recompute when
# something the scorer actually reads has changed: the staged rows' scoring
# inputs, the reference date, or the tunable weights/locations/priorities
# (which review/app.py's Preferences page can change at runtime via
# preferences.save_overrides()).
# Several score sets are live in one rerun (Queue and Discovered), so the cache
# is keyed by fingerprint and holds a few entries rather than a single slot.
_SCORE_CACHE = {}
_SCORE_CACHE_MAX = 6


def _score_cache_key(apps: list, today: date, named_targets: list):
    """
    desc: Build a cheap, hashable fingerprint of everything score_apps()'s
        output depends on, so repeated calls with unchanged inputs can reuse
        the previous ranking instead of rescoring every row.
    args:
        apps (list[dict]): the application rows being scored.
        today (date): resolved reference date for urgency math.
        named_targets (list[dict]): output of _load_named_targets().
    output:
        tuple: hashable key suitable for an equality-based cache lookup.
    """
    apps_fp = tuple(
        (a.get("id"), a.get("title"), a.get("company"), a.get("location"),
         a.get("description"), a.get("notes"), a.get("form_data"),
         a.get("score_adjustment"))
        for a in apps
    )
    named_fp = tuple(sorted(
        (t.get("company"), t.get("program"), t.get("eligibility"))
        for t in named_targets
    ))
    weights_fp = tuple(sorted(P.WEIGHTS.items()))
    locations_fp = tuple(P.TARGET_LOCATIONS)
    priorities_fp = tuple(sorted((a["name"], a["priority"]) for a in P.INTEREST_AREAS))
    # A classify.py logic change bumps FACTS_VERSION, which must invalidate
    # this cache too -- otherwise a code upgrade would keep serving scores
    # computed from a stale JobFacts classification until something else
    # (e.g. a row's own fields) happened to change first. The learning
    # fingerprint does the same for new signals / changed signal weights.
    return (today.isoformat(), apps_fp, named_fp, weights_fp, locations_fp,
            priorities_fp, FACTS_VERSION, learning_fingerprint())


def score_apps(apps: list, today_str: str = None):
    """
    desc: Score any list of application rows and return them ranked
        best-first. Makes no external calls and writes nothing. Memoized on a
        fingerprint of its inputs (see _score_cache_key); any change to a row,
        a preference, or the learning signals changes the key.
    args:
        apps (list[dict]): application JOIN job rows (e.g. from
            get_queued_applications() / get_discovered_applications()).
        today_str (str): optional "YYYY-MM-DD" override for urgency math; None
            uses preferences.DEFAULT_TODAY (reproducible), pass "" for real clock.
    output:
        list[dict]: score dicts (see score_application) sorted by adjusted
            score desc, IB-excluded last.
    """
    today = _parse_today(P.DEFAULT_TODAY if today_str is None else today_str)
    named_targets = _load_named_targets()

    cache_key = _score_cache_key(apps, today, named_targets)
    if cache_key not in _SCORE_CACHE:
        tally = get_tally()   # once per pass, not per row
        scored = [score_application(a, today, named_targets, tally) for a in apps]
        # sort by adjusted_score (base score + manual up/down votes, capped) so
        # this ranking agrees with what the views show for the same apps
        scored.sort(key=lambda s: (s["excluded"], -s["adjusted_score"], s["company"].lower()))
        if len(_SCORE_CACHE) >= _SCORE_CACHE_MAX:
            _SCORE_CACHE.clear()
        _SCORE_CACHE[cache_key] = scored

    # Always hand back copies (shallow, plus a copy of the nested
    # "components" dict) rather than the cached objects themselves, so a
    # caller that mutates its copy in place can never corrupt the cache.
    return [dict(s, components=dict(s["components"])) for s in _SCORE_CACHE[cache_key]]


def score_staged(today_str: str = None):
    """
    desc: Score every Queue application (status 'queued') best-first. Kept
        under its original name for review/app.py and the CLI.
    args:
        today_str (str): optional "YYYY-MM-DD" urgency override (see score_apps).
    output:
        list[dict]: see score_apps().
    """
    return score_apps(get_queued_applications(), today_str)


def score_discovered(today_str: str = None):
    """
    desc: Score every Discovered application (status 'discovered') best-first.
    args:
        today_str (str): optional "YYYY-MM-DD" urgency override (see score_apps).
    output:
        list[dict]: see score_apps().
    """
    return score_apps(get_discovered_applications(), today_str)


# ---------------------------------------------------------------------------
# Persistence -- SEPARATE table owned by the evaluator. Does NOT touch db.py.
# ---------------------------------------------------------------------------
def _ensure_scores_table(conn):
    """
    desc: Create the evaluator-owned scores table if absent. Kept entirely here
        so database/db.py's schema/init are never modified.
    args:
        conn (sqlite3.Connection): open connection (from db.get_conn()).
    output:
        None
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evaluator_scores (
            application_id   INTEGER PRIMARY KEY,
            score            REAL NOT NULL,
            max_score        REAL NOT NULL,
            excluded         INTEGER NOT NULL DEFAULT 0,
            eligibility_bucket TEXT,
            components_json  TEXT,
            rationale        TEXT,
            scored_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def save_scores(scored: list):
    """
    desc: Persist a ranking to the evaluator_scores table (upsert by
        application_id). Uses db.get_conn() only to reuse the same DB file/path;
        it creates and writes the evaluator's OWN table, never the app schema.
    args:
        scored (list): output of score_staged().
    output:
        int: number of rows written.
    """
    with get_conn() as conn:
        _ensure_scores_table(conn)
        for s in scored:
            conn.execute(
                """
                INSERT INTO evaluator_scores
                    (application_id, score, max_score, excluded,
                     eligibility_bucket, components_json, rationale, scored_at)
                VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(application_id) DO UPDATE SET
                    score=excluded.score,
                    max_score=excluded.max_score,
                    excluded=excluded.excluded,
                    eligibility_bucket=excluded.eligibility_bucket,
                    components_json=excluded.components_json,
                    rationale=excluded.rationale,
                    scored_at=CURRENT_TIMESTAMP
                """,
                (
                    s["application_id"], s["score"], s["max_score"],
                    1 if s["excluded"] else 0, s.get("eligibility_bucket"),
                    json.dumps(s["components"]), s["rationale"],
                ),
            )
        return len(scored)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_ranking(scored: list, top: int = None):
    """
    desc: Print a best-first ranking to stdout. Local output only -- no
        personal info (resume/GPA/contact) is included, per the ground rules.
    args:
        scored (list): output of score_staged().
        top (int): optionally limit to the top N rows.
    output:
        None
    """
    if not scored:
        print("No staged applications to rank. Run `python main.py` to stage some.")
        return

    rows = scored if not top else scored[:top]
    print(f"\nRanked staged postings (best first) -- {len(scored)} staged"
          + (f", showing top {len(rows)}" if top else "") + "\n")
    for i, s in enumerate(rows, 1):
        flag = "  [IB-EXCLUDED]" if s["excluded"] else ""
        loc = s["location"] or "location N/A"
        print(f"{i:>2}. {s['adjusted_score']:>6.1f}/{s['max_score']:.0f}{flag}  "
              f"{s['company']} - {s['title']}  ({loc})")
        comp = s["components"]
        comp_str = "  ".join(f"{k}={v}" for k, v in comp.items())
        print(f"      components: {comp_str}")
        print(f"      why: {s['rationale']}")
        if s.get("url"):
            print(f"      link: {s['url']}")
        print()


def main(argv=None):
    """
    desc: CLI entry point. Ranks staged postings and prints them best-first;
        optionally emits JSON and/or persists to the evaluator's own table.
    args:
        argv (list[str] | None): argument vector (defaults to sys.argv[1:]).
    output:
        int: process exit code (0 on success).
    """
    parser = argparse.ArgumentParser(
        prog="python -m evaluator",
        description="Rank staged internship postings by how much Noah should "
                    "prefer them (deterministic, no external API calls).",
    )
    parser.add_argument("--json", action="store_true",
                        help="Emit the ranking as JSON instead of text.")
    parser.add_argument("--save", action="store_true",
                        help="Persist scores to the evaluator_scores table "
                             "(separate from the app schema).")
    parser.add_argument("--today", default=None,
                        help="Reference date YYYY-MM-DD for urgency math "
                             "(default: project date). Pass 'now' for real clock.")
    parser.add_argument("--top", type=int, default=None,
                        help="Show only the top N postings.")
    args = parser.parse_args(argv)

    today_arg = "" if (args.today or "").lower() == "now" else args.today
    scored = score_staged(today_str=today_arg)

    if args.save:
        n = save_scores(scored)
        if not args.json:
            print(f"[evaluator] persisted {n} scores to evaluator_scores.")

    if args.json:
        print(json.dumps(scored, indent=2))
    else:
        _print_ranking(scored, top=args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())

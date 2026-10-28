"""
desc: The features a signal is attributed to. Each feature is fn(app, facts)
    -> key string or None (None = this posting has nothing for that feature).
    Signals on a posting credit every feature key it has; a candidate posting
    is then scored by the credit its own keys have earned. Add a feature by
    writing a function and registering it in FEATURES.
args:
    (see functions)
output:
    (FEATURES dict)
"""

from taxonomy import prestige_tier


def interest(app: dict, facts) -> str | None:
    """
    desc: Best-matching interest area name (e.g. "sports_analytics").
    args:
        app (dict): application JOIN job row.
        facts (classify.JobFacts | None): the posting's facts.
    output:
        str | None
    """
    return getattr(facts, "best_interest_area", None) or None


def company(app: dict, facts) -> str | None:
    """
    desc: Lowercased company name.
    args:
        app (dict): application JOIN job row.
        facts (classify.JobFacts | None): unused.
    output:
        str | None
    """
    name = (app.get("company") or "").strip().lower()
    return name or None


def prestige(app: dict, facts) -> str | None:
    """
    desc: Company prestige tier bucket ("tier_1".."tier_3"), None if unranked.
    args:
        app (dict): application JOIN job row.
        facts (classify.JobFacts | None): unused.
    output:
        str | None
    """
    frac = prestige_tier(app.get("company") or "")
    if frac >= 1.0:
        return "tier_1"
    if frac >= 0.6:
        return "tier_2"
    if frac > 0:
        return "tier_3"
    return None


def location(app: dict, facts) -> str | None:
    """
    desc: Canonical location key (e.g. "new_york").
    args:
        app (dict): application JOIN job row.
        facts (classify.JobFacts | None): the posting's facts.
    output:
        str | None
    """
    return getattr(facts, "canonical_location", None) or None


FEATURES = {
    "interest": interest,
    "company": company,
    "prestige": prestige,
    "location": location,
}


def feature_keys(app: dict, facts) -> dict:
    """
    desc: Evaluate every registered feature for one posting.
    args:
        app (dict): application JOIN job row.
        facts (classify.JobFacts | None): the posting's facts (may be None).
    output:
        dict: {feature_name: key} with None-valued features omitted.
    """
    out = {}
    for name, fn in FEATURES.items():
        try:
            key = fn(app, facts)
        except Exception:
            key = None
        if key:
            out[name] = key
    return out

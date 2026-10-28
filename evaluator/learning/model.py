"""
desc: Turns the raw signal log into score points. Every event is weighted
    (signals.SIGNAL_WEIGHTS), decayed by age, and credited to each feature key
    of the posting it was taken on (features.FEATURES). A candidate posting's
    learned points are the credit its own keys have earned, scaled and capped
    to +/- P.WEIGHTS["learned"]. All of this is recomputed from raw events, so
    changing a weight, the decay or the cap re-counts history; 0 disables.
args:
    (see functions)
output:
    (learned_points, get_tally, learning_fingerprint)
"""

from datetime import datetime, timezone

from database import db
from evaluator import preferences as P
from evaluator.learning import signals as S
from evaluator.learning.features import feature_keys

HALF_LIFE_DAYS = 60.0        # an event's weight halves every this many days
POINTS_PER_UNIT = 0.8        # score points per 1.0 of tallied credit (before the cap)
# Relative say of each feature in the total; a missing entry means 1.0.
FEATURE_WEIGHTS = {"interest": 1.0, "company": 1.0, "prestige": 0.5, "location": 0.5}

_VERBS = {"applied": "applied to", "queued": "queued", "up": "up-voted", "down": "down-voted"}

_TALLY_CACHE = {"key": None, "tally": None}


def learning_cap() -> float:
    """
    desc: The current cap (the Preferences "Learned" weight); 0 disables.
    args:
        (none)
    output:
        float
    """
    return float(P.WEIGHTS.get("learned", 0) or 0)


def learning_fingerprint() -> tuple:
    """
    desc: Everything the learned component depends on besides the posting,
        for score-cache keys.
    args:
        (none)
    output:
        tuple: hashable.
    """
    return (S.signals_fingerprint(), tuple(sorted(S.SIGNAL_WEIGHTS.items())),
            tuple(sorted(FEATURE_WEIGHTS.items())), HALF_LIFE_DAYS, POINTS_PER_UNIT,
            learning_cap(), str(db.DB_PATH))


def _load_signal_postings(app_ids: list) -> tuple:
    """
    desc: Fetch the application/job rows (and persisted facts) the logged
        signals point at.
    args:
        app_ids (list[int]): application ids.
    output:
        tuple: ({app_id: row dict}, {job_id: JobFacts}).
    """
    from database.job_facts import get_facts_bulk
    apps = {}
    with db.get_conn() as conn:
        for i in range(0, len(app_ids), 500):
            chunk = app_ids[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT a.id, a.job_id, j.company, j.title, j.location "
                f"FROM applications a JOIN jobs j ON a.job_id = j.id WHERE a.id IN ({marks})",
                chunk,
            ).fetchall()
            apps.update({r["id"]: dict(r) for r in rows})
    job_ids = sorted({a["job_id"] for a in apps.values()})
    facts = {}
    for i in range(0, len(job_ids), 500):
        facts.update(get_facts_bulk(job_ids[i:i + 500]))
    return apps, facts


def build_tally(now: datetime = None) -> dict:
    """
    desc: Tally every logged signal from scratch.
    args:
        now (datetime | None): reference time for decay (default utcnow).
    output:
        dict: {"total": {(feature, key): float},
               "counts": {(feature, key): {kind: n}},
               "by_app": {(app_id, feature, key): float}} -- by_app lets a
            posting's own actions be excluded from its own score.
    """
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    events = S.load_signals()
    total, counts, by_app = {}, {}, {}
    if not events:
        return {"total": total, "counts": counts, "by_app": by_app}
    apps, facts = _load_signal_postings(sorted({e["application_id"] for e in events}))
    for ev in events:
        weight = S.SIGNAL_WEIGHTS.get(ev["kind"])
        app = apps.get(ev["application_id"])
        if weight is None or app is None:
            continue
        age_days = max(0.0, (now - ev["created_at"]).total_seconds() / 86400.0)
        credit = weight * 0.5 ** (age_days / HALF_LIFE_DAYS)
        for feat, key in feature_keys(app, facts.get(app["job_id"])).items():
            fk = (feat, key)
            total[fk] = total.get(fk, 0.0) + credit
            kinds = counts.setdefault(fk, {})
            kinds[ev["kind"]] = kinds.get(ev["kind"], 0) + 1
            ak = (app["id"], feat, key)
            by_app[ak] = by_app.get(ak, 0.0) + credit
    return {"total": total, "counts": counts, "by_app": by_app}


def get_tally(now: datetime = None) -> dict:
    """
    desc: build_tally() memoized on learning_fingerprint() and the calendar day
        (decay moves slowly), so a scoring pass over thousands of postings
        tallies once.
    args:
        now (datetime | None): reference time (default utcnow).
    output:
        dict: see build_tally().
    """
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    key = (learning_fingerprint(), now.date().isoformat())
    if _TALLY_CACHE["key"] != key:
        _TALLY_CACHE["tally"] = build_tally(now)
        _TALLY_CACHE["key"] = key
    return _TALLY_CACHE["tally"]


def learned_points(app: dict, facts=None, tally: dict = None) -> tuple:
    """
    desc: Points the learned signal adds to one posting. The posting's own
        past actions are excluded (the up/down nudge already moves it).
    args:
        app (dict): application JOIN job row (needs "id", "company").
        facts (classify.JobFacts | None): the posting's facts.
        tally (dict | None): precomputed get_tally() (pass it when scoring many).
    output:
        tuple[float, str]: (points in [-cap, +cap], reason; "" when 0).
    """
    cap = learning_cap()
    if cap <= 0:
        return 0.0, ""
    tally = tally if tally is not None else get_tally()
    app_id = app.get("id")
    raw, best = 0.0, None   # best = (abs contribution, feature, key)
    for feat, key in feature_keys(app, facts).items():
        fk = (feat, key)
        credit = tally["total"].get(fk, 0.0) - tally["by_app"].get((app_id, feat, key), 0.0)
        contrib = credit * FEATURE_WEIGHTS.get(feat, 1.0)
        raw += contrib
        if best is None or abs(contrib) > best[0]:
            best = (abs(contrib), feat, key)
    pts = max(-cap, min(cap, raw * POINTS_PER_UNIT))
    if best is None or round(pts) == 0:
        return round(pts, 2), ""
    _, feat, key = best
    kinds = tally["counts"].get((feat, key), {})
    # describe the kind that moved this key the most, with its event count
    kind = max(kinds, key=lambda k: abs(S.SIGNAL_WEIGHTS.get(k, 0)) * kinds[k])
    label = key.replace("_", " ")
    if feat == "interest":
        label = label.title()
    return round(pts, 2), f"learned {pts:+.0f}: you {_VERBS.get(kind, kind)} {kinds[kind]} {label} postings"

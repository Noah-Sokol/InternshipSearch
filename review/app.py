import os
import re
import sys
import json
import time
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database.db import (
    get_discovered_applications, get_queued_applications, get_all_applications, get_application,
    update_status, update_cover_letter, update_form_data, bump_score,
    remove_from_queue, init_db, set_resume_override,
)
from database.job_facts import get_facts_bulk as _get_facts_bulk
from review.dedup import find_duplicate_groups
from generator.cover_letter import generate as generate_cover_letter, get_qualification_candidates
from review.query_filter import matches as _query_matches
from classify import is_graduate_only


def _filter_apps(apps: list, filter_query: str) -> list:
    """
    desc: Apply a review-queue filter_query to a list of app dicts, resolving
        location=/platform= against each app's canonical JobFacts (bulk-
        fetched once per call, not once per app) so those two fields match
        exactly rather than via substring. company=/title=/bare-term search
        stay fuzzy, unaffected by facts. A no-op (empty query) returns apps
        unchanged without touching the DB at all.
    args:
        apps (list[dict]): application rows (application JOIN job).
        filter_query (str): the query string from a filter box.
    output:
        (list[dict]): the subset of apps that satisfy the query.
    """
    if not filter_query.strip():
        return apps
    facts_by_job = _get_facts_bulk([a["job_id"] for a in apps if a.get("job_id")])
    return [
        a for a in apps
        if _query_matches(_haystack(a), filter_query, a, facts_by_job.get(a.get("job_id")))
    ]

try:
    from evaluator.score import score_staged as _score_staged
    from evaluator.score import score_discovered as _score_discovered
except Exception:
    _score_staged = None
    _score_discovered = None

try:
    from evaluator.learning import record_signal as _record_signal
    from evaluator.learning import reset_signals as _reset_signals
except Exception:
    _record_signal = None
    _reset_signals = None

try:
    from evaluator import preferences as _preferences
    from taxonomy import LOCATION_ALIASES as _LOCATION_ALIASES
    # The set of canonical location keys with at least one KNOWN alias (vs. a
    # city that became its own canonical bucket only because no alias
    # matched) -- used to flag possibly-mistyped locations on the
    # Preferences page without blocking the save (an unrecognized city is a
    # legitimate new bucket, not an error).
    _KNOWN_LOCATION_KEYS = set(_LOCATION_ALIASES.values())
except Exception:
    _preferences = None
    _KNOWN_LOCATION_KEYS = set()

try:
    from evaluator.ai_detection import score_text as _score_ai_text
except Exception:
    # Optional heavy deps (torch/transformers/binoculars) aren't installed --
    # the button degrades to an explanatory message instead of crashing the
    # whole review app. See requirements-ai-detection.txt for install instructions.
    _score_ai_text = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DB_INITED = False

def _init_db_once():
    global _DB_INITED
    if not _DB_INITED:
        init_db()
        _DB_INITED = True


def _signal(application_id: int, kind: str):
    """
    desc: Log a learning signal (evaluator/learning) for one of Noah's own
        actions. Never raises: learning is a bonus and must not break an action.
    args:
        application_id (int): applications.id acted on.
        kind (str): "queued" | "applied" | "up" | "down".
    output:
        None
    """
    if _record_signal is None:
        return
    try:
        _record_signal(application_id, kind)
    except Exception:
        pass


def _discovered_scores() -> dict:
    """
    desc: Preference scores for Discovered applications (cached in score.py).
    args:
        (none)
    output:
        dict: {application_id: score dict}; {} if the evaluator is unavailable.
    """
    if _score_discovered is None:
        return {}
    try:
        return {s["application_id"]: s for s in _score_discovered()}
    except Exception:
        return {}


def _preference_scores() -> dict:
    if _score_staged is None:
        return {}
    try:
        return {s["application_id"]: s for s in _score_staged()}
    except Exception:
        return {}


def _capped_score(app: dict, pref: dict):
    """
    desc: Compute an application's effective preference score -- base score
        (from the evaluator, 0 if unavailable) plus manual ▲/▼ adjustment --
        capped at the scorer's own max_score (100 by default) so repeated
        upvotes can't push a score past what the scale actually means. Also
        reports whether a real base score exists at all, so callers can still
        show "—" instead of a bare adjustment number when scoring is off.
    args:
        app (dict): an application row (has score_adjustment).
        pref (dict): {application_id: score dict} from _preference_scores();
            may be {} if scoring is unavailable or turned off.
    output:
        tuple[float, float, bool, bool]: (effective score capped at
            max_score, max_score, excluded, has_base_score).
    """
    s = pref.get(app["id"]) if pref else None
    excluded = bool(s and s.get("excluded"))
    has_base_score = s is not None and not excluded
    max_score = s.get("max_score", 100) if s else 100
    base = s.get("score", 0) if has_base_score else 0
    adj = app.get("score_adjustment") or 0
    return min(max_score, base + adj), max_score, excluded, has_base_score


def _sort_key(app: dict, pref: dict):
    """
    desc: Sort key for the Queue view's "Sort by preference score" option.
        IB-excluded applications always sort last, then possibly-closed
        (stale) ones sort below fresh ones; among the rest, a higher
        capped effective score (see _capped_score) sorts first -- matching
        the number actually shown in the Score column, including manual
        ▲/▼ adjustments.
    args:
        app (dict): an application row.
        pref (dict): {application_id: score dict} from _preference_scores().
    output:
        tuple: ascending sort key (use with sorted(), no reverse needed).
    """
    effective, _, excluded, _ = _capped_score(app, pref)
    if excluded:
        return (2, 0.0)
    if _is_stale(app):
        return (1, -effective)   # possibly-closed postings sort below fresh ones
    return (0, -effective)


def _haystack(app: dict) -> str:
    fields = [app.get("company"), app.get("title"), app.get("location"),
              app.get("platform"), app.get("description"), app.get("notes")]
    return " ".join(f for f in fields if f)


def _is_stale(app: dict) -> bool:
    """
    desc: Whether a posting looks possibly closed: its last_seen_at (set each
        time a scrape re-finds it) is older than config.STALE_DAYS. Postings
        with no last_seen_at are treated as fresh. Nothing is hidden or
        deleted because of this -- it only drives a marker and sort order.
    args:
        app (dict): an application row (joined with its job).
    output:
        bool: True if not seen for STALE_DAYS or more.
    """
    seen = app.get("last_seen_at")
    if not seen:
        return False
    try:
        import config
        days = int(getattr(config, "STALE_DAYS", 21))
        seen_dt = datetime.fromisoformat(str(seen).replace("Z", "")[:19])
    except Exception:
        return False
    return datetime.now(timezone.utc).replace(tzinfo=None) - seen_dt >= timedelta(days=days)


_STALE_MARK = "⏳ possibly closed"


def _platform_icon(platform: str) -> str:
    """
    desc: Just the emoji of _platform_label (small icon next to a company name).
    args:
        platform (str): jobs.platform value.
    output:
        str: the icon, e.g. "🌿".
    """
    return _platform_label(platform).split(" ", 1)[0]


def _platform_label(platform: str) -> str:
    icons = {
        "linkedin": "💼 LinkedIn",
        "handshake": "🤝 Handshake",
        "direct": "🎯 Direct",
        "greenhouse": "🌿 Greenhouse",
        "lever": "⚙️ Lever",
        "coverage": "📊 Coverage",
        "simplify": "📋 Simplify",
        "ashby": "🔷 Ashby",
    }
    return icons.get((platform or "").lower(), f"🔗 {platform or '—'}")


def _nav(view: str, app_id: int | None = None):
    """
    desc: Switch the main view. Opening the Detail page remembers which tab it
        was opened from so its back arrow returns there.
    args:
        view (str): "discovered" | "queue" | "applied" | "detail" | "preferences".
        app_id (int | None): application to show (detail view only).
    output:
        None
    """
    if view == "detail" and st.session_state.get("view") != "detail":
        st.session_state.detail_return = st.session_state.get("view", "queue")
    st.session_state.view = view
    st.session_state.selected_app_id = app_id


_LEVEL_TITLE_RES = {
    "PhD": r"ph\.?\s?d",
    "Master's": r"master'?s?",
    "MBA": r"mba",
    "Senior": r"senior",
    "Staff": r"staff",
    "Graduate": r"graduate",
}


def _level_excluded(app: dict, levels) -> bool:
    """
    desc: Discovered-tab "Exclude levels" test. Levels match the TITLE only, on
        word boundaries; PhD / Master's / MBA / Graduate also hide postings
        whose description is graduate-only (classify.is_graduate_only, which
        keeps its undergrad guard, so "Bachelor's, Master's or PhD" stays).
    args:
        app (dict): application row (title, description).
        levels (list[str]): selected level names.
    output:
        bool: True if the row should be hidden.
    """
    title = (app.get("title") or "").replace("’", "'").lower()
    for lv in levels:
        pat = _LEVEL_TITLE_RES.get(lv)
        if pat and re.search(pat, title):
            return True
    if any(lv in ("PhD", "Master's", "MBA", "Graduate") for lv in levels):
        return is_graduate_only(app.get("title") or "", app.get("description") or "")
    return False


def _cleanup(app_id: int):
    prefixes = (
        f"cl_editor_{app_id}", f"cl_pending_{app_id}", f"fd_{app_id}_",
        f"aiscore_{app_id}", f"regen_err_{app_id}", f"tried_quals_{app_id}",
        f"rv_ed_{app_id}", f"rv_pending_{app_id}", f"rv_err_{app_id}",
    )
    for k in list(st.session_state.keys()):
        if k.startswith(prefixes):
            del st.session_state[k]
    # the row has left the Queue: drop it from the Fill Out selection too
    st.session_state.pop(f"fchk_{app_id}", None)
    if isinstance(st.session_state.get("fill_checked"), set):
        st.session_state["fill_checked"].discard(app_id)


def _mark_applied(app: dict):
    """
    desc: Mark an application applied (Noah submitted it himself): sets status
        'applied' (stamps submitted_at), records the 'applied' learning signal,
        clears cached widget state. Never fills or submits anything.
    args:
        app (dict): application row (needs id, company).
    output:
        None
    """
    update_status(app["id"], "applied")
    _signal(app["id"], "applied")
    _cleanup(app["id"])
    st.toast(f"{app['company']} marked applied.")


def _dismiss(app: dict, note: str = "Not interested"):
    """
    desc: Mark an application Not interested ('dismissed'). Records no learning
        signal (by design).
    args:
        app (dict): application row.
        note (str): notes text stored with the status.
    output:
        None
    """
    update_status(app["id"], "dismissed", notes=note)
    _cleanup(app["id"])
    st.toast(f"Marked {app['company']} not interested.")


# Status -> (display label, st.badge color). Shared across every view so a
# given status always looks the same (Discovered, Queue, Detail, Applied).
_STATUS_BADGES = {
    "discovered": ("Discovered", "gray"),
    "queued": ("Queued", "blue"),
    "applied": ("Applied", "green"),
    "dismissed": ("Not interested", "gray"),
    "rejected": ("Rejected", "red"),
    "interviewing": ("Interviewing", "orange"),
    "offer": ("Offer 🎉", "green"),
}


def _status_badge(container, status: str):
    """Render a colored status badge into `container` (st, a column, ...).
    Falls back to plain text for any status not in _STATUS_BADGES.
    """
    label, color = _STATUS_BADGES.get(status, (status or "—", "gray"))
    container.badge(label, color=color)


def _chained_filters(count_key: str, key_prefix: str) -> str:
    """Render stacked filter inputs with + / Clear buttons.
    Returns a combined query string (non-empty parts joined with AND).
    """
    n = st.session_state[count_key]
    vals = []
    for i in range(n):
        if i > 0:
            st.caption("AND")
        v = st.text_input(
            "Filter" if i == 0 else f"Filter {i + 1}",
            placeholder=(
                'quant location=New York NOT banking'
                if i == 0
                else 'add another condition…'
            ),
            key=f"{key_prefix}_{i}",
            label_visibility="visible" if i == 0 else "collapsed",
        )
        vals.append(v.strip())

    btn_col, clear_col, _ = st.columns([1, 1, 8])
    with btn_col:
        if st.button("＋ Filter", key=f"{key_prefix}_add"):
            st.session_state[count_key] += 1
            st.rerun()
    with clear_col:
        if n > 1 and st.button("Clear", key=f"{key_prefix}_clear"):
            for i in range(n):
                st.session_state.pop(f"{key_prefix}_{i}", None)
            st.session_state[count_key] = 1
            st.rerun()

    return " AND ".join(f"({v})" for v in vals if v)


# ---------------------------------------------------------------------------
# Batch pre-fill (Fill Out buttons)
#
# Every Fill Out button in this app -- per-row, "Fill Out Selected", "Fill Out
# Top N" and the Detail view's "Fill Out in Chrome" -- goes through the same
# path: tools/batch_fill.py launched non-blocking, one Chrome tab per
# application. The prefill code only fills / selects / uploads; it NEVER
# clicks submit (CLAUDE.md #3) and NEVER changes an application's status.
# Noah reviews every tab and presses Submit himself, which is the human
# checkpoint CLAUDE.md #2 requires. Only apps whose status is in the
# runner's FILLABLE_STATUSES ('queued' = the Queue tab) can be filled;
# 'applied' means Noah already applied and is never filled.
# ---------------------------------------------------------------------------

try:
    from applicator.prefill.runner import (
        FILLABLE_STATUSES as _FILLABLE_STATUSES, REPORT_PATH as _FILL_REPORT_PATH,
    )
except Exception:
    from config import SESSION_DIR as _SESSION_DIR
    _FILLABLE_STATUSES = ("queued",)
    _FILL_REPORT_PATH = _SESSION_DIR / "prefill_report.json"

try:
    from applicator.prefill import answers as _answers
    from applicator.prefill.resumes import choose_resume as _choose_resume, interest_area_for as _interest_area_for
    from config import RESUME_CHOICES as _RESUME_CHOICES
except Exception:
    _answers = None

_FILL_LOG_PATH = _FILL_REPORT_PATH.parent / "prefill_run.log"   # gitignored (.sessions/)
_FILL_CAP = 10            # max tabs a single Fill Out batch may open (Q15)
_FILL_TOP_DEFAULT = 5     # default for "Fill Out Top N"
_FILL_POLL_SECONDS = 3    # report-panel refresh interval while a run is in progress
_FILL_CONFIRM_LINE = (
    "Opens one tab per application and fills them. Nothing is submitted -- "
    "you review and press Submit yourself."
)

# FillResult status (plus the runner's "skipped") -> (label, st.badge color)
_FILL_BADGES = {
    "filled": ("Filled", "green"),
    "partial": ("Partial", "orange"),
    "needs_login": ("Needs login", "blue"),
    "blocked": ("Blocked", "red"),
    "unsupported": ("Unsupported", "gray"),
    "error": ("Error", "red"),
    "skipped": ("Skipped", "gray"),
    "closed": ("Posting closed", "red"),
}


def _fillable(app: dict) -> bool:
    """
    desc: Whether a Fill Out button may act on this application -- its status
        is in the runner's FILLABLE_STATUSES (never 'applied') and it has a
        URL to open. batch_fill.py re-checks the status itself, so this is
        only for enabling/disabling buttons.
    args:
        app (dict): an application row.
    output:
        bool: True if the app can be pre-filled.
    """
    return app.get("status") in _FILLABLE_STATUSES and bool(app.get("url"))


def _fillable_label() -> str:
    """
    desc: Human label(s) for the fillable statuses, e.g. "Discovered".
    args:
        (none)
    output:
        str: display labels joined with " / ".
    """
    return " / ".join(_STATUS_BADGES.get(s, (s, ""))[0] for s in _FILLABLE_STATUSES)


def _fill_running() -> bool:
    """
    desc: Whether the batch-fill subprocess launched from this session is
        still running.
    args:
        (none)
    output:
        bool: True while the run is in progress.
    """
    run = st.session_state.get("fill_run")
    return bool(run) and run["proc"].poll() is None


def _launch_fill(app_ids: list) -> bool:
    """
    desc: Launch tools/batch_fill.py (non-blocking) to pre-fill the given
        applications, one Chrome tab each. Never submits and never changes
        a status; batch_fill.py only opens apps in FILLABLE_STATUSES. Output
        goes to a local, gitignored log the Fill report panel reads for
        progress. Refuses to start while another run is in progress (both
        would drive the same Chrome).
    args:
        app_ids (list[int]): application ids in priority order (capped at
            _FILL_CAP).
    output:
        bool: True if a run was started.
    """
    ids = [int(i) for i in app_ids][:_FILL_CAP]
    if not ids:
        return False
    if _fill_running():
        st.toast("A fill run is already in progress -- wait for it to finish.")
        return False
    _FILL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    with open(_FILL_LOG_PATH, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "tools" / "batch_fill.py"),
             "--app-ids", *[str(i) for i in ids], "--max-tabs", str(len(ids))],
            cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
        )
    st.session_state["fill_run"] = {"proc": proc, "ids": ids, "started": time.time()}
    # rows in this batch wear the "Filled · review in Chrome" badge until Noah
    # answers Applied ✓ / Not yet in the fill report; checkboxes reset.
    st.session_state.setdefault("fill_pending_ids", set()).update(ids)
    st.session_state.setdefault("fill_not_yet", set()).difference_update(ids)
    _clear_fill_checks()
    st.toast(f"Chrome launching -- filling {len(ids)} application(s). Nothing will be submitted.")
    return True


def _chrome_badge_ids() -> set:
    """
    desc: Applications whose row should wear the "Filled · review in Chrome"
        badge: launched in a batch from this session and not yet answered with
        Applied ✓ / Not yet. Once the latest report is in, rows it says did
        not fill (blocked, needs login, ...) lose the badge.
    args:
        (none)
    output:
        set[int]: application ids.
    """
    ids = set(st.session_state.get("fill_pending_ids", set()))
    run = st.session_state.get("fill_run")
    if ids and run and not _fill_running():
        report = _load_fill_report()
        try:
            fresh = report is not None and _FILL_REPORT_PATH.stat().st_mtime >= run["started"]
        except OSError:
            fresh = False
        if fresh:
            bad = {r.get("app_id") for r in report.get("results") or []
                   if r.get("status") not in ("filled", "partial")}
            ids -= bad
    return ids


def _clear_fill_checks():
    """
    desc: Uncheck every Queue checkbox (also usable as an on_click callback).
    args:
        (none)
    output:
        None.
    """
    for k in [k for k in st.session_state.keys() if k.startswith("fchk_")]:
        del st.session_state[k]
    st.session_state["fill_checked"] = set()


def _end_review():
    """
    desc: Drop the Review letters page's state (id list and per-card editor
        keys) so the next visit reloads letters from the database.
    args:
        (none)
    output:
        None.
    """
    st.session_state.pop("review_ids", None)
    for k in [k for k in st.session_state.keys() if k.startswith("rv_")]:
        del st.session_state[k]


def _start_review(app_ids: list, return_to: str = "queue", return_app_id: int | None = None):
    """
    desc: Common entry for every Fill Out button: open the Review letters page
        for these applications instead of launching Chrome. Nothing is
        generated or launched here.
    args:
        app_ids (list[int]): applications to fill, in priority order (capped
            at _FILL_CAP).
        return_to (str): view "← Back" returns to ("queue" or "detail").
        return_app_id (int | None): application to reopen when returning to Detail.
    output:
        None.
    """
    ids = [int(i) for i in app_ids][:_FILL_CAP]
    if not ids:
        return
    _end_review()
    st.session_state.review_ids = ids
    st.session_state.review_return = (return_to, return_app_id)
    st.session_state.view = "review"


def _no_letter_count(apps: list) -> int:
    """
    desc: How many of these applications have no saved cover letter.
    args:
        apps (list[dict]): application rows.
    output:
        int: count with an empty/missing cover_letter.
    """
    return sum(1 for a in apps if not (a.get("cover_letter") or "").strip())


def _no_letter_line(n: int) -> str:
    """
    desc: The quiet replacement for a cost warning (Q15).
    args:
        n (int): applications with no letter yet.
    output:
        str: caption text.
    """
    return f"{n} of these have no letter yet — generated only if the form asks."


def _fill_controls(view_apps: list, key_prefix: str):
    """
    desc: Render "Fill Out Top N" (default 5, max _FILL_CAP) over the apps
        currently shown, in display order. The button opens the Review
        letters page; nothing launches until Continue there. Only fillable
        apps are counted.
    args:
        view_apps (list[dict]): apps currently shown (filtered, in display order).
        key_prefix (str): widget-key prefix, unique per view.
    output:
        None.
    """
    running = _fill_running()
    fillable_view = [a for a in view_apps if _fillable(a)]

    cols = st.columns([0.8, 1.5, 5.7], vertical_alignment="center")
    with cols[0]:
        top_n = st.number_input(
            "Top N", min_value=1, max_value=_FILL_CAP,
            value=min(_FILL_TOP_DEFAULT, _FILL_CAP), step=1,
            key=f"{key_prefix}_fill_n", label_visibility="collapsed",
        )
    k = min(int(top_n), len(fillable_view))
    with cols[1]:
        if st.button(f"Fill Out Top {k}", key=f"{key_prefix}_fill_top", type="primary",
                     disabled=running or k == 0,
                     help="Review letters for the first N fillable applications shown "
                          "below, in this order, then fill them."):
            _start_review([a["id"] for a in fillable_view[:k]])
            st.rerun()
    with cols[2]:
        if k:
            st.caption(_no_letter_line(_no_letter_count(fillable_view[:k])))

    st.caption(_FILL_CONFIRM_LINE)
    if len(fillable_view) < len(view_apps):
        st.caption(
            f"Fill Out only acts on **{_fillable_label()}** applications with a URL "
            f"({len(fillable_view)} of the {len(view_apps)} shown). Applied = already "
            "applied, never filled."
        )
    _fill_report_panel()


def _fill_progress() -> tuple:
    """
    desc: Parse the running batch's local log for "[prefill] (n/total)" lines.
    args:
        (none)
    output:
        tuple[int, int, str]: (tabs started filling, total tabs, last
            "#id Company -- Title" line or "").
    """
    try:
        lines = _FILL_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0, 0, ""
    done, total, last = 0, 0, ""
    for line in lines:
        m = re.match(r"\[prefill\] \((\d+)/(\d+)\) (.*)", line)
        if m:
            done, total, last = int(m.group(1)), int(m.group(2)), m.group(3)
    return done, total, last


def _load_fill_report() -> dict | None:
    """
    desc: Read .sessions/prefill_report.json (labels/statuses only, never
        typed values).
    args:
        (none)
    output:
        dict | None: the report, or None if missing/unreadable.
    """
    try:
        return json.loads(_FILL_REPORT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _fill_report_actions(r: dict, container):
    """
    desc: Per-row follow-up prompt in the fill report. Filled/partial rows ask
        "Did you apply?" with Applied ✓ (= Mark applied) / Not yet (leaves it
        in Queue). A closed posting gets a Not interested button instead.
        Nothing is shown once the application has left the Queue.
    args:
        r (dict): one result from prefill_report.json.
        container: streamlit container (column) to draw into.
    output:
        None
    """
    app_id = r.get("app_id")
    if app_id is None:
        return
    status = r.get("status")
    if status not in ("filled", "partial", "closed"):
        return
    current = next((a for a in get_all_applications() if a["id"] == app_id), None)
    if current is None:
        return
    if current["status"] == "applied":
        st.session_state.get("fill_pending_ids", set()).discard(app_id)
        container.caption("Applied ✓")
        return
    if current["status"] != "queued":
        return
    if status == "closed":
        if container.button("Not interested", key=f"fr_dismiss_{app_id}"):
            update_status(app_id, "dismissed", notes="Not interested (posting closed)")
            _cleanup(app_id)
            st.toast(f"{current['company']} marked not interested.")
            st.rerun()
        return
    if app_id in st.session_state.get("fill_not_yet", set()):
        container.caption("Not applied yet -- still in your Queue.")
        return
    container.caption("Did you submit it?")
    yes_col, no_col, _ = container.columns([1, 1, 4])
    if yes_col.button("Applied ✓", key=f"fr_applied_{app_id}"):
        st.session_state.get("fill_pending_ids", set()).discard(app_id)
        _mark_applied(current)
        st.rerun()
    if no_col.button("Not yet", key=f"fr_notyet_{app_id}"):
        st.session_state.setdefault("fill_not_yet", set()).add(app_id)
        st.session_state.get("fill_pending_ids", set()).discard(app_id)
        st.rerun()


def _render_fill_report(report: dict, only_ids: list | None = None):
    """
    desc: Render one row per application from a fill report: status badge,
        company/title, filler, and the field labels Noah still has to answer.
    args:
        report (dict): parsed prefill_report.json.
        only_ids (list[int] | None): restrict to these app ids (Detail view).
    output:
        None.
    """
    results = report.get("results") or []
    if only_ids is not None:
        results = [r for r in results if r.get("app_id") in only_ids]
    if not results:
        st.caption("No fill results for this application in the last report.")
        return
    counts = {}
    for r in results:
        counts[r.get("status")] = counts.get(r.get("status"), 0) + 1
    st.caption(
        f"Generated {report.get('generated_at', '?')} · "
        + " · ".join(f"{_FILL_BADGES.get(s, (s, ''))[0]}: {n}" for s, n in counts.items())
    )
    for r in results:
        label, color = _FILL_BADGES.get(r.get("status"), (r.get("status") or "—", "gray"))
        b_col, t_col = st.columns([1, 6], vertical_alignment="top")
        b_col.badge(label, color=color)
        unfilled = r.get("unfilled") or []
        t_col.markdown(
            f"**{r.get('company') or '—'}** — {r.get('title') or ''}  \n"
            f"<small>#{r.get('app_id')} · {r.get('filler') or 'n/a'} · "
            f"{len(r.get('filled') or [])} filled · {len(unfilled)} to answer</small>",
            unsafe_allow_html=True,
        )
        if unfilled:
            t_col.markdown("To answer yourself: " + "; ".join(f"`{u}`" for u in unfilled))
        _save_answer_controls(r, t_col)
        if r.get("resume"):
            t_col.caption(f"Resume: {r['resume']}")
        if r.get("notes"):
            t_col.caption(r["notes"])
        if r.get("submit_attempts_blocked"):
            t_col.caption(f"Submit guard blocked {r['submit_attempts_blocked']} submit attempt(s) "
                          "while filling -- nothing was sent.")
        _fill_report_actions(r, t_col)


def _fill_report_body(only_ids: list | None = None):
    """
    desc: Body of the Fill report panel: live progress while this session's
        run is going, otherwise the latest report on disk. When a run ends,
        triggers one full rerun so the buttons re-enable and polling stops.
    args:
        only_ids (list[int] | None): restrict the report to these app ids.
    output:
        None.
    """
    run = st.session_state.get("fill_run")
    if run and run["proc"].poll() is None:
        done, total, last = _fill_progress()
        total = total or len(run["ids"])
        st.progress(min(done / total, 1.0) if total else 0.0,
                    text=f"Filling {done}/{total}" + (f" — {last}" if last else " — opening tabs…"))
        st.caption("Tabs stay open in Chrome. Nothing is submitted.")
        return
    if run and not run.get("finished"):
        run["finished"] = True
        st.rerun()   # full app rerun: re-enable Fill buttons, stop polling

    report = _load_fill_report()
    stale = bool(run) and (
        report is None or _FILL_REPORT_PATH.stat().st_mtime < run["started"]
    )
    if stale:
        st.error("The last fill run ended without writing a report "
                 f"(exit code {run['proc'].returncode}).")
        try:
            tail = _FILL_LOG_PATH.read_text(encoding="utf-8", errors="replace")[-3000:]
        except OSError:
            tail = ""
        if tail:
            with st.expander("Run log (local)"):
                st.code(tail)
        return
    if report is None:
        st.caption("No fill report yet.")
        return
    _render_fill_report(report, only_ids)


def _fill_report_panel(only_ids: list | None = None):
    """
    desc: "Fill report" panel. While a run is in progress it is wrapped in an
        auto-refreshing fragment (only the panel reruns, every few seconds);
        otherwise it renders once, collapsed unless this session just ran.
    args:
        only_ids (list[int] | None): restrict the report to these app ids.
    output:
        None.
    """
    run = st.session_state.get("fill_run")
    if _fill_running():
        with st.container(border=True):
            st.markdown("**Fill report** — run in progress")
            st.fragment(run_every=_FILL_POLL_SECONDS)(_fill_report_body)(only_ids)
        return
    if run is None and not _FILL_REPORT_PATH.exists():
        return
    with st.expander("Fill report", expanded=run is not None):
        _fill_report_body(only_ids)


def _save_answer_controls(r: dict, container):
    """
    desc: In the fill report, offer "Save an answer for this question" for each
        free-text question the run could not answer. Save appends an entry
        (escaped-literal pattern of the question, answer stored verbatim) to
        profile/answers.yaml; the next fill pastes it. Company-specific
        questions are never offered (they are never auto-answered).
    args:
        r (dict): one result from prefill_report.json ("text_questions").
        container: streamlit container (column) to draw into.
    output:
        None
    """
    if _answers is None:
        return
    saved = st.session_state.setdefault("answers_saved", set())
    for i, q in enumerate(r.get("text_questions") or []):
        if q in saved:
            container.caption(f"Answer saved for: {q[:80]}")
            continue
        with container.expander(f"Save an answer for this question: {q[:70]}"):
            key = f"ans_new_{r.get('app_id')}_{i}"
            text = st.text_area("Your answer (pasted exactly as written)", key=key, height=120)
            if st.button("Save", key=f"{key}_save", disabled=not text.strip()):
                if _answers.add_answer_for_question(q, text):
                    saved.add(q)
                    st.toast("Answer saved to your answer bank.")
                    st.rerun()


def _resume_panel(app: dict):
    """
    desc: Detail page: show which resume Fill Out will upload (override, else
        the posting's top interest area, else the default) with a dropdown to
        override it for this application.
    args:
        app (dict): application row (title, company, location, description,
            resume_override).
    output:
        None
    """
    if _answers is None:
        return
    st.subheader("Resume")
    override = app.get("resume_override") if app.get("resume_override") in _RESUME_CHOICES else None
    choice = _choose_resume(_interest_area_for(app), override)
    why = {"override": "your override", "interest_area": f"interest area: {choice.area}",
           "default": "default: no interest area matched"}[choice.source]
    st.caption(f"Will use **{choice.path.name}** ({why})")
    if not choice.path.exists():
        st.warning("That resume file is missing from profile/resumes/.")
    options = ["Automatic", *_RESUME_CHOICES]
    picked = st.selectbox("Resume override", options,
                          index=options.index(override) if override else 0,
                          key=f"resume_sel_{app['id']}")
    new = None if picked == "Automatic" else picked
    if new != override:
        set_resume_override(app["id"], new)
        st.rerun()


def _answers_section():
    """
    desc: Preferences "Answers" section: list/edit/delete/add the entries in
        profile/answers.yaml (patterns one regex per line, answer pasted
        verbatim). Regexes are validated before anything is saved.
    args:
        (none)
    output:
        None
    """
    st.subheader("Answers")
    st.caption(
        "Your own answers to recurring free-text questions, pasted exactly as written "
        "into matching questions. Patterns are regexes (one per line, case-insensitive). "
        "Questions that name the company or ask \"why us\" are never filled."
    )
    if _answers is None:
        st.warning("Answer bank unavailable.")
        return
    entries = _answers.load_answers()
    ver = st.session_state.setdefault("answers_ver", 0)   # bumps after a save/delete to reset widget state

    def _commit(new_entries: list):
        _answers.save_answers(new_entries)
        st.session_state["answers_ver"] = ver + 1

    for i, e in enumerate(entries):
        with st.container(border=True):
            pats = st.text_area("Question patterns (one regex per line)", value="\n".join(e["patterns"]),
                                key=f"ans_pat_{ver}_{i}", height=80)
            ans = st.text_area("Answer", value=e["answer"], key=f"ans_txt_{ver}_{i}", height=140)
            save_col, del_col, _sp = st.columns([1, 1, 6])
            if save_col.button("Save", key=f"ans_save_{ver}_{i}"):
                plist = [p.strip() for p in pats.splitlines() if p.strip()]
                errs = _answers.validate_patterns(plist)
                if not plist or not ans.strip():
                    st.error("An entry needs at least one pattern and an answer.")
                elif errs:
                    st.error("Invalid regex: " + "; ".join(errs))
                else:
                    _commit(entries[:i] + [{"patterns": plist, "answer": ans}] + entries[i + 1:])
                    st.toast("Answer saved.")
                    st.rerun()
            if del_col.button("Delete", key=f"ans_del_{ver}_{i}"):
                _commit(entries[:i] + entries[i + 1:])
                st.toast("Answer deleted.")
                st.rerun()
    with st.container(border=True):
        st.markdown("**Add new**")
        new_pats = st.text_area("Question patterns (one regex per line)", key=f"ans_new_pat_{ver}", height=80)
        new_ans = st.text_area("Answer", key=f"ans_new_txt_{ver}", height=140)
        if st.button("Add", key=f"ans_add_{ver}"):
            plist = [p.strip() for p in new_pats.splitlines() if p.strip()]
            errs = _answers.validate_patterns(plist)
            if not plist or not new_ans.strip():
                st.error("An entry needs at least one pattern and an answer.")
            elif errs:
                st.error("Invalid regex: " + "; ".join(errs))
            else:
                _commit(entries + [{"patterns": plist, "answer": new_ans}])
                st.toast("Answer added.")
                st.rerun()


# ---------------------------------------------------------------------------
# Page config & init
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Application Queue", page_icon="📋", layout="wide")
_init_db_once()

st.markdown("""
<style>
/* Buttons never wrap text */
[data-testid="stButton"] > button { white-space: nowrap; }
/* Kill default paragraph bottom margin inside table cells */
[data-testid="stMarkdown"] p { margin-bottom: 0 !important; line-height: 1.4; }
/* Slimmer dividers */
hr { margin: 0.25rem 0 !important; }
/* Subtle row hover */
[data-testid="stHorizontalBlock"]:hover { background-color: rgba(255,255,255,0.025); border-radius: 4px; transition: background 0.1s; }
/* Sidebar metric cards */
[data-testid="stMetric"] { background: rgba(255,255,255,0.04); border-radius: 8px; padding: 6px 10px; margin-bottom: 4px; }
/* ⋯ menus: hide the popover chevron */
[data-testid="stPopoverButton"] svg { display: none; }
/* Primary buttons slightly bolder */
[data-testid="stButton"] > button[kind="primary"] { font-weight: 600; }
/* Queue sticky "Fill Out N selected" bar */
.st-key-fill_bar { position: sticky; top: 3.2rem; z-index: 99; padding: 6px 10px;
  background: var(--background-color, #0e1117); border: 1px solid rgba(128,128,128,0.35);
  border-radius: 8px; }
</style>
""", unsafe_allow_html=True)

if "view" not in st.session_state:
    st.session_state.view = "queue"
if "selected_app_id" not in st.session_state:
    st.session_state.selected_app_id = None
if "detail_return" not in st.session_state:   # tab the Detail page was opened from
    st.session_state.detail_return = "queue"
if "checked_ids" not in st.session_state:
    st.session_state.checked_ids = set()
if "n_sf" not in st.session_state:   # number of Discovered filter boxes
    st.session_state.n_sf = 1
if "n_qf" not in st.session_state:   # number of Queue filter boxes
    st.session_state.n_qf = 1

_PAGE_SIZE = 50   # Discovered rows per page
_TABS = [("discovered", "Discovered"), ("queue", "Queue"), ("applied", "Applied")]
_APPLIED_STATUSES = ("applied", "interviewing", "offer", "rejected")


def _duplicate_flags(rows: list) -> dict:
    """
    desc: Flag Discovered postings that duplicate something already in the
        Queue (or already applied to). Detection is review/dedup.py's
        find_duplicate_groups; nothing is dropped automatically.
    args:
        rows (list[dict]): every application row (get_all_applications()).
    output:
        dict: {discovered application_id: "⚠ already in Queue (<platform>)" or
            "⚠ already applied (<platform>)"}.
    """
    pool = [a for a in rows if a["status"] in ("discovered", "queued", "applied")]
    flags = {}
    for group in find_duplicate_groups(pool):
        queued = [a for a in group if a["status"] == "queued"]
        applied = [a for a in group if a["status"] == "applied"]
        for a in group:
            if a["status"] != "discovered":
                continue
            if queued:
                flags[a["id"]] = f"⚠ already in Queue ({queued[0].get('platform') or '—'})"
            elif applied:
                flags[a["id"]] = f"⚠ already applied ({applied[0].get('platform') or '—'})"
    return flags


def _sync_check(app_id: int):
    """
    desc: on_change callback for a Discovered row checkbox -- copies the
        widget's new value into st.session_state.checked_ids.
    args:
        app_id (int): applications.id of the row.
    output:
        None.
    """
    if st.session_state.get(f"chk_{app_id}"):
        st.session_state.checked_ids.add(app_id)
    else:
        st.session_state.checked_ids.discard(app_id)


def _add_to_queue(ids: list):
    """
    desc: Move Discovered applications into the Queue (status 'queued') and
        record the 'queued' learning signal for each. No duplicate dropping.
    args:
        ids (list[int]): applications.id values.
    output:
        None
    """
    for aid in ids:
        update_status(aid, "queued")
        _signal(aid, "queued")
    st.session_state.checked_ids.difference_update(ids)
    st.session_state.pop("queue_sorted_ids", None)
    st.session_state.pop("queue_sort_filter", None)
    st.toast(f"Added {len(ids)} application(s) to the Queue.")


all_apps = get_all_applications()
counts = {}
for _a in all_apps:
    counts[_a["status"]] = counts.get(_a["status"], 0) + 1
_tab_counts = {
    "discovered": counts.get("discovered", 0),
    "queue": counts.get("queued", 0),
    "applied": sum(counts.get(s, 0) for s in _APPLIED_STATUSES),
}


# ---------------------------------------------------------------------------
# Sidebar: Preferences, Export applied (CSV), two stats
# ---------------------------------------------------------------------------

with st.sidebar:
    if _preferences is not None:
        if st.button("⚙️ Preferences", use_container_width=True,
                     type="primary" if st.session_state.view == "preferences" else "secondary"):
            _nav("preferences")
            st.rerun()

    if st.button("Export applied (CSV)", use_container_width=True,
                 help="Write applied/interviewing/offer/rejected rows to export/snapshot.csv "
                      "(local file only; no letters or personal info)"):
        export_script = ROOT / "export" / "csv_export.py"
        result = subprocess.run(
            [sys.executable, str(export_script)],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        if result.returncode == 0:
            st.session_state["sync_result"] = (
                "success", result.stdout.strip() or "export/snapshot.csv updated",
            )
        else:
            st.session_state["sync_result"] = (
                "error", result.stderr.strip() or "Export failed",
            )

    # Rendered outside the button's if-block so it survives the next rerun.
    if "sync_result" in st.session_state:
        _kind, _msg = st.session_state["sync_result"]
        (st.success if _kind == "success" else st.error)(_msg)

    # Weekly unattended scrape: last summary line from .sessions/scrape_log.txt.
    try:
        from scraper.scrape_log import sidebar_line
        st.caption(sidebar_line())
    except Exception:
        st.caption("Last scrape: unknown")

    # Stats come from the free, local, cached evaluator (Queue only).
    if _score_staged is not None:
        st.divider()
        _sidebar_scores = list(_preference_scores().values())
        _ranked = [s for s in _sidebar_scores if not s.get("excluded")]
        if _ranked:
            _avg = sum(s["adjusted_score"] for s in _ranked) / len(_ranked)
            st.metric("Avg score (Queue)", f"{_avg:.0f}/{_ranked[0]['max_score']:.0f}")
        _needs_review = sum(
            1 for s in _sidebar_scores if s.get("eligibility_bucket") == "unknown"
        )
        st.metric(
            "Needs Review", _needs_review,
            help="Queue postings with no explicit cohort/eligibility signal "
                 "detected -- verify the recruiting year before applying.",
        )


# ---------------------------------------------------------------------------
# Top tab bar (always visible): Discovered · Queue · Applied, with counts
# ---------------------------------------------------------------------------

_active_tab = (st.session_state.detail_return if st.session_state.view == "detail"
               else "queue" if st.session_state.view == "review"
               else st.session_state.view)
for _col, (_key, _label) in zip(st.columns([1.4, 1.4, 1.4, 6]), _TABS):
    if _col.button(f"{_label} ({_tab_counts[_key]})", key=f"tab_{_key}",
                   use_container_width=True,
                   type="primary" if _active_tab == _key else "secondary"):
        _end_review()
        _nav(_key)
        st.rerun()
st.divider()


# ---------------------------------------------------------------------------
# Discovered tab  --  every scraped posting, scored, sorted, paginated.
# Bulk actions only: Add to Queue / Not interested. No Fill Out here.
# ---------------------------------------------------------------------------

if st.session_state.view == "discovered":
    col_filter, col_show = st.columns([3, 1])
    with col_filter:
        filter_query = _chained_filters("n_sf", "sf")
    with col_show:
        show_dismissed = st.toggle("Show dismissed", value=False, key="disc_show_dismissed",
                                   help="Also list postings you marked Not interested.")

    st.caption("Exclude levels:")
    exclude_levels = st.multiselect(
        "Exclude levels",
        options=["PhD", "Master's", "MBA", "Senior", "Staff", "Graduate"],
        default=["PhD", "Master's", "MBA"],
        label_visibility="collapsed",
        key="select_exclude",
    )

    apps = get_discovered_applications()
    total = len(apps)
    apps = _filter_apps(apps, filter_query)
    if exclude_levels:
        apps = [a for a in apps if not _level_excluded(a, exclude_levels)]

    pref = _discovered_scores()
    apps = sorted(apps, key=lambda a: _sort_key(a, pref))   # hard-excluded last, stale below fresh
    dismissed = []
    if show_dismissed:
        dismissed = _filter_apps([a for a in all_apps if a["status"] == "dismissed"], filter_query)

    # Selection only ever covers postings that are still Discovered.
    st.session_state.checked_ids &= {a["id"] for a in apps}
    n_checked = len(st.session_state.checked_ids)
    dup_flags = _duplicate_flags(all_apps)

    # Pagination (reset to page 1 whenever the filter changes)
    n_pages = max(1, -(-len(apps) // _PAGE_SIZE))
    _sig = (filter_query, tuple(exclude_levels), show_dismissed)
    if st.session_state.get("disc_sig") != _sig:
        st.session_state.disc_sig = _sig
        st.session_state.disc_page = 1
    page = min(max(1, st.session_state.get("disc_page", 1)), n_pages)
    st.session_state.disc_page = page
    start = (page - 1) * _PAGE_SIZE
    page_apps = apps[start:start + _PAGE_SIZE]

    hcol1, hcol2, hcol3 = st.columns([5, 1.6, 1.6])
    with hcol1:
        st.subheader(f"Discovered — {len(apps)} of {total}" + (
            f" ({n_checked} selected)" if n_checked else ""))
    with hcol2:
        add_clicked = st.button(f"Add {n_checked} to Queue", disabled=n_checked == 0,
                                type="primary", key="disc_add")
    with hcol3:
        dismiss_clicked = st.button(f"Not interested ({n_checked})", disabled=n_checked == 0,
                                    key="disc_dismiss")

    if add_clicked:
        sel = sorted(st.session_state.checked_ids)
        if any(i in dup_flags for i in sel):
            st.session_state["add_confirm"] = sel     # warn, ask before adding
        else:
            _add_to_queue(sel)
            st.rerun()
    if dismiss_clicked:
        by_id = {a["id"]: a for a in apps}
        for aid in sorted(st.session_state.checked_ids):
            _dismiss(by_id[aid])   # no learning signal for Not interested
        st.session_state.checked_ids.clear()
        st.rerun()

    pending_add = [i for i in st.session_state.get("add_confirm", []) if i in {a["id"] for a in apps}]
    if pending_add:
        dups = [i for i in pending_add if i in dup_flags]
        st.warning(
            f"{len(dups)} of the {len(pending_add)} selected posting(s) look like duplicates "
            "of something already in your Queue or already applied to "
            "(marked ⚠ below). Add them anyway?"
        )
        c_any, c_non, c_cancel, _sp = st.columns([1.2, 1.6, 1, 4])
        if c_any.button("Add anyway", key="disc_add_any", type="primary"):
            st.session_state.pop("add_confirm", None)
            _add_to_queue(pending_add)
            st.rerun()
        if c_non.button("Add without duplicates", key="disc_add_non"):
            st.session_state.pop("add_confirm", None)
            _add_to_queue([i for i in pending_add if i not in dup_flags])
            st.rerun()
        if c_cancel.button("Cancel", key="disc_add_cancel"):
            st.session_state.pop("add_confirm", None)
            st.rerun()
    else:
        st.session_state.pop("add_confirm", None)

    if not apps and not dismissed:
        st.info("No applications match this filter." if filter_query.strip()
                else "Nothing here. Run `python main.py` to scrape new jobs.")
        st.stop()

    nav_a, nav_b, nav_c, sa_col, da_col, _sp = st.columns([0.8, 1.2, 0.8, 1.2, 1.2, 4])
    if nav_a.button("◀ Prev", disabled=page <= 1, key="disc_prev"):
        st.session_state.disc_page = page - 1
        st.rerun()
    nav_b.markdown(f"Page {page} of {n_pages}")
    if nav_c.button("Next ▶", disabled=page >= n_pages, key="disc_next"):
        st.session_state.disc_page = page + 1
        st.rerun()
    if sa_col.button("Select page"):
        st.session_state.checked_ids.update(a["id"] for a in page_apps)
        st.rerun()
    if da_col.button("Deselect all"):
        st.session_state.checked_ids.clear()
        st.rerun()

    st.divider()
    _SCOLS = [0.4, 3.0, 3.6, 2, 0.8, 0.9]
    hcols = st.columns(_SCOLS, vertical_alignment="bottom")
    for col, hdr in zip(hcols, ["", "Company", "Title", "Location", "Score", "Link"]):
        col.markdown(f"**{hdr}**")
    st.divider()

    for app in page_apps:
        cols = st.columns(_SCOLS, vertical_alignment="center")
        checked = app["id"] in st.session_state.checked_ids
        # The click is copied into checked_ids by the on_change callback,
        # which runs BEFORE this rerun -- so pushing the set into the widget
        # here (for Select page / Deselect all) no longer undoes the click.
        st.session_state[f"chk_{app['id']}"] = checked
        cols[0].checkbox("Select", key=f"chk_{app['id']}", label_visibility="collapsed",
                         on_change=_sync_check, args=(app["id"],))

        if cols[1].button(f"{_platform_icon(app['platform'])} {app['company']}",
                          key=f"dco_{app['id']}", type="tertiary"):
            _nav("detail", app["id"])
            st.rerun()
        notes = [dup_flags[app["id"]]] if app["id"] in dup_flags else []
        if _is_stale(app):
            notes.append(_STALE_MARK)
        if "may be closed" in (app.get("notes") or ""):
            notes.append("⚠ may be closed")
        for n in notes:
            cols[1].caption(n)

        cols[2].write(app["title"])
        effective, _mx, excluded, has_base = _capped_score(app, pref)
        if excluded:
            cols[2].caption(f"🚫 {(pref.get(app['id']) or {}).get('rationale') or 'excluded'}")
        cols[3].write(app.get("location") or "—")
        cols[4].write("🚫" if excluded else (f"{effective:.0f}" if has_base else "—"))
        url = app.get("url") or ""
        cols[5].markdown(f"[↗ Posting]({url})" if url else "—")

    if dismissed:
        st.divider()
        st.markdown(f"**Not interested — {len(dismissed)}**")
        for app in dismissed:
            cols = st.columns([0.4, 3.0, 3.6, 2, 1.7], vertical_alignment="center")
            cols[1].write(f"{_platform_icon(app['platform'])} {app['company']}")
            cols[2].write(app["title"])
            if (app.get("notes") or "").strip():
                cols[2].caption(f"Reason: {app['notes'].strip()}")   # e.g. "graduate program"
            cols[3].write(app.get("location") or "—")
            if cols[4].button("Restore", key=f"restore_{app['id']}",
                              help="Move back to Discovered"):
                update_status(app["id"], "discovered")
                st.rerun()


# ---------------------------------------------------------------------------
# Queue tab  --  the only fillable status ('queued'); Fill Out acts here
# ---------------------------------------------------------------------------

elif st.session_state.view == "queue":
    sort_by_pref = False
    if _score_staged is not None:
        sort_by_pref = st.checkbox(
            "Sort by preference score", value=True,
            help="Rank best-fit / most-eligible first.",
        )

    filter_query = _chained_filters("n_qf", "qf")

    apps = get_queued_applications()
    total = len(apps)
    _queued_ids = {a["id"] for a in apps}

    apps = _filter_apps(apps, filter_query)

    pref = _preference_scores()
    if sort_by_pref:
        _sf_key = "queue_sort_filter"
        _si_key = "queue_sorted_ids"
        if st.session_state.get(_sf_key) != filter_query or _si_key not in st.session_state:
            _sorted = sorted(apps, key=lambda a: _sort_key(a, pref))
            st.session_state[_si_key] = [a["id"] for a in _sorted]
            st.session_state[_sf_key] = filter_query
        _id_order = {aid: i for i, aid in enumerate(st.session_state[_si_key])}
        apps = sorted(apps, key=lambda a: _id_order.get(a["id"], len(apps)))
    else:
        st.session_state.pop("queue_sorted_ids", None)
        st.session_state.pop("queue_sort_filter", None)

    st.subheader(f"Queue — {len(apps)}" + (f" of {total}" if filter_query.strip() else ""))

    if not apps:
        st.info("Queue is empty. Go to **Discovered** to add applications.")
        st.stop()

    # Batch pre-fill over the apps shown above (filter + sort order). Never
    # submits, never changes status.
    _fill_controls(apps, "q")

    # Checkbox state lives in the fchk_<id> widget keys; fill_checked mirrors it
    # so checks survive a round trip to the Review letters page ("← Back").
    _fillable_ids = {a["id"] for a in apps if _fillable(a)}
    for _aid in st.session_state.get("fill_checked", set()):
        if _aid in _fillable_ids:
            st.session_state.setdefault(f"fchk_{_aid}", True)
    _checked = [a["id"] for a in apps
                if a["id"] in _fillable_ids and st.session_state.get(f"fchk_{a['id']}")]
    # checked rows hidden by the filter stay selected
    _checked += [i for i in st.session_state.get("fill_checked", set())
                 if i in _queued_ids and i not in {a["id"] for a in apps}
                 and st.session_state.get(f"fchk_{i}", True)]
    _checked = _checked[:_FILL_CAP]
    st.session_state["fill_checked"] = set(_checked)
    _at_cap = len(_checked) >= _FILL_CAP

    if _checked:
        with st.container(key="fill_bar"):
            bar_a, bar_b, bar_c = st.columns([1.6, 0.7, 5], vertical_alignment="center")
            if bar_a.button(f"Fill Out {len(_checked)} selected", key="fill_sel",
                            type="primary", disabled=_fill_running(),
                            help=f"Review letters for the checked applications "
                                 f"(max {_FILL_CAP} per batch)."):
                _order = {a["id"]: i for i, a in enumerate(apps)}
                _start_review(sorted(_checked, key=lambda i: _order.get(i, len(_order))))
                st.rerun()
            bar_b.button("Clear", key="fill_clear", type="tertiary",
                         on_click=_clear_fill_checks)
            bar_c.caption(_no_letter_line(_no_letter_count(
                [a for a in apps if a["id"] in set(_checked)])))

    _QCOLS = [0.4, 1.9, 2.9, 1.4, 1.5, 0.7, 0.7, 1.1, 1.7, 0.5]
    hcols = st.columns(_QCOLS, vertical_alignment="bottom")
    for col, hdr in zip(hcols, ["", "Company", "Title", "Location", "Score", "Letter", "Link", "", "", ""]):
        col.markdown(f"**{hdr}**")
    st.divider()

    _badge_ids = _chrome_badge_ids()
    for app in apps:
        cols = st.columns(_QCOLS, vertical_alignment="center")

        # Leftmost checkbox: pick apps for a batch; once the cap is reached the
        # unchecked boxes grey out.
        _is_checked = bool(st.session_state.get(f"fchk_{app['id']}"))
        cols[0].checkbox(
            f"Select {app['company']}", key=f"fchk_{app['id']}",
            label_visibility="collapsed",
            disabled=not _fillable(app) or (_at_cap and not _is_checked),
            help=f"Max {_FILL_CAP} per batch" if _at_cap and not _is_checked else None,
        )
        cols = cols[1:]

        # Company (opens Detail) + small platform icon
        if cols[0].button(f"{_platform_icon(app['platform'])} {app['company']}",
                          key=f"qco_{app['id']}", type="tertiary"):
            _nav("detail", app["id"])
            st.rerun()
        if _is_stale(app):
            cols[0].caption(_STALE_MARK)
        if "may be closed" in (app.get("notes") or ""):
            cols[0].caption("⚠ may be closed")

        cols[1].write(app["title"])
        if app["id"] in _badge_ids:
            cols[1].badge("Filled · review in Chrome", color="green")

        loc = app.get("location") or "—"
        cols[2].write(loc if len(loc) <= 26 else loc[:24] + "…")

        # Score (plain number) with ▲ ▼ nudges: +/-5 and a learning signal
        adj = app.get("score_adjustment") or 0
        effective, max_score, excluded, has_base_score = _capped_score(app, pref)
        at_cap = not excluded and effective >= max_score
        if excluded:
            score_str = "🚫"
        elif not has_base_score and adj == 0:
            score_str = "—"
        else:
            score_str = f"{effective:.0f}"
        s_num, s_up, s_down = cols[3].columns([1.2, 1, 1], vertical_alignment="center")
        s_num.write(score_str)
        if s_up.button("▲", key=f"up_{app['id']}", help="Rank higher (+5)", disabled=at_cap):
            bump_score(app["id"], 5)
            _signal(app["id"], "up")
            st.session_state.pop("queue_sorted_ids", None)
            st.rerun()
        if s_down.button("▼", key=f"down_{app['id']}", help="Rank lower (-5)"):
            bump_score(app["id"], -5)
            _signal(app["id"], "down")
            st.session_state.pop("queue_sorted_ids", None)
            st.rerun()

        # Letter ✓/— and the posting link (kept visible)
        has_letter = bool((app.get("cover_letter") or "").strip())
        url = app.get("url") or ""
        cols[4].markdown("✓" if has_letter else "—")
        cols[5].markdown(f"[↗]({url})" if url else "")

        # Fill Out -- pre-fills just this app in its own new tab via the batch
        # path. Never submits; a letter is generated then only if the form asks.
        _can_fill = _fillable(app)
        if cols[6].button(
            "Fill Out", key=f"fill_{app['id']}", type="primary",
            disabled=not _can_fill or _fill_running(),
            help=("Review the letter, then pre-fill this application in a new Chrome tab. "
                  "Nothing is submitted."
                  if _can_fill else
                  f"Fill Out only acts on {_fillable_label()} applications with a URL."),
        ):
            _start_review([app["id"]])
            st.rerun()

        # Mark applied -- Noah's own marker that he ALREADY submitted it himself.
        if cols[7].button("Mark applied", key=f"approve_q_{app['id']}",
                          help="Mark as applied (you submitted it yourself)"):
            _mark_applied(app)
            st.rerun()

        # ⋯ menu
        with cols[8].popover("⋯"):
            if st.button("Back to Discovered", key=f"rm_{app['id']}",
                         use_container_width=True):
                remove_from_queue(app["id"])   # no learning signal
                st.session_state.pop("queue_sorted_ids", None)
                st.toast(f"{app['company']} is back in Discovered.")
                st.rerun()
            if st.button("Not interested", key=f"ni_{app['id']}", use_container_width=True):
                _dismiss(app)
                st.session_state.pop("queue_sorted_ids", None)
                st.rerun()


# ---------------------------------------------------------------------------
# Detail / Edit view
# ---------------------------------------------------------------------------

elif st.session_state.view == "detail":
    app_id = st.session_state.selected_app_id
    _back_tab = st.session_state.detail_return
    _back_label = dict(_TABS).get(_back_tab, "Queue")

    # one row joined with its job, including the description (the list queries
    # used elsewhere omit it for non-active statuses)
    app = get_application(app_id)

    if app is None:
        st.error("Application not found.")
        _nav(_back_tab)
        st.rerun()

    if st.button(f"← Back to {_back_label}", key="detail_back_top"):
        _nav(_back_tab)
        st.rerun()

    loc = app.get("location") or ""
    plat = app.get("platform") or ""
    title_col, badge_col = st.columns([5, 1], vertical_alignment="center")
    with title_col:
        st.title(f"{app['company']} — {app['title']}")
    with badge_col:
        _status_badge(st, app["status"])
    meta_parts = [f"📍 {loc}" if loc else "", f"🏢 {plat}" if plat else "",
                  _STALE_MARK if _is_stale(app) else ""]
    st.caption("  |  ".join(p for p in meta_parts if p))
    if app.get("url"):
        st.markdown(f"[View Job Posting ↗]({app['url']})")
    if "may be closed" in (app.get("notes") or ""):
        st.caption(f"⚠ {app['notes']}")

    _s = (_preference_scores().get(app_id) or _discovered_scores().get(app_id))
    if _s:
        if _s.get("excluded"):
            st.warning(f"🚫 Excluded: {_s.get('rationale', '')}")
        else:
            st.info(f"Preference score: {_s['score']:.0f}/{_s['max_score']:.0f} — {_s['rationale']}")

    st.divider()

    col1, col2 = st.columns([3, 2])

    with col1:
        _desc_msg_key = f"desc_fetch_msg_{app_id}"
        if len((app.get("description") or "").strip()) < 200 and app.get("url"):
            # explicit single fetch: one read-only page request per click
            st.caption("No description saved.")
            if st.button("Fetch description", key=f"fetch_desc_{app_id}"):
                from scraper.description_fill import fill_one
                with st.spinner("Fetching the posting page..."):
                    _ok, _msg = fill_one(app_id)
                st.session_state[_desc_msg_key] = (_ok, _msg)
                st.rerun()
        if _desc_msg_key in st.session_state:
            _ok, _msg = st.session_state.pop(_desc_msg_key)
            (st.success if _ok else st.warning)(_msg)
        with st.expander("Job Description", expanded=False):
            st.write(app.get("description") or "No description available.")

        st.subheader("Cover Letter")
        editor_key = f"cl_editor_{app_id}"
        pending_key = f"cl_pending_{app_id}"

        # Apply any pending regenerated value BEFORE the widget is rendered
        if pending_key in st.session_state:
            st.session_state[editor_key] = st.session_state.pop(pending_key)
        elif editor_key not in st.session_state:
            st.session_state[editor_key] = app.get("cover_letter") or ""

        _has_letter = bool(st.session_state[editor_key].strip())
        if not _has_letter:
            st.info("No cover letter yet — one will be generated if the form asks for one.")

        st.text_area(
            label="Cover letter draft (edit freely):",
            height=350,
            key=editor_key,
            label_visibility="collapsed",
        )
        _cl_text = st.session_state[editor_key]
        _n_words = len(_cl_text.split())
        _n_chars = len(_cl_text)
        st.caption(f"{_n_words} words · {_n_chars} characters")

        regen_err_key = f"regen_err_{app_id}"

        btn_col1, btn_col2, btn_col3, _ = st.columns([1, 1, 1.6, 2.4])
        with btn_col1:
            if st.button("Regenerate" if _has_letter else "Generate now", key=f"regen_{app_id}"):
                # Tracks which {{QUALIFICATION}} facts Noah has already seen
                # for this application (across clicks) so each click shuffles
                # to a different real fact instead of the same "best" one.
                tried_key = f"tried_quals_{app_id}"
                tried_quals = st.session_state.setdefault(tried_key, set())
                for cand in get_qualification_candidates():
                    if cand in _cl_text:
                        tried_quals.add(cand)
                try:
                    with st.spinner("Generating…"):
                        new_cl = generate_cover_letter(
                            job_title=app["title"],
                            company=app["company"],
                            job_description=app.get("description") or "",
                            exclude_qualifications=tried_quals,
                        )
                except Exception as e:
                    # Broad on purpose: any failure becomes a readable message.
                    # Persisted to session_state since this path has no rerun.
                    st.session_state[regen_err_key] = f"Couldn't generate a cover letter: {e}"
                else:
                    st.session_state.pop(regen_err_key, None)
                    for cand in get_qualification_candidates():
                        if cand in new_cl:
                            tried_quals.add(cand)
                    # Store in pending — widget owns editor_key this run, can't set it directly
                    st.session_state[pending_key] = new_cl
                    update_cover_letter(app_id, new_cl)
                    st.toast("Cover letter generated and saved.")
                    st.rerun()

        with btn_col2:
            if st.button("Save Draft", key=f"save_{app_id}"):
                update_cover_letter(app_id, st.session_state[editor_key])
                st.toast("Draft saved.")

        with btn_col3:
            # On-demand only -- see evaluator/ai_detection.py for why this is a
            # local sanity check, not a GPTZero equivalent.
            ai_score_key = f"aiscore_{app_id}"
            if st.button("Check AI score (local)", key=f"aicheck_{app_id}"):
                if _score_ai_text is None:
                    st.session_state[ai_score_key] = (
                        "info",
                        "Local AI-detection pre-check isn't installed. See the "
                        "requirements-ai-detection.txt file (not part of the "
                        "core install).",
                    )
                else:
                    try:
                        with st.spinner("Scoring locally (first check loads the model)…"):
                            result = _score_ai_text(st.session_state[editor_key])
                    except ImportError as e:
                        st.session_state[ai_score_key] = ("info", str(e))
                    else:
                        st.session_state[ai_score_key] = (
                            "caption",
                            f"Binoculars ({result['model_pair']}): "
                            f"score={result['score']:.4f} — {result['prediction']}. "
                            "Local approximation only, not a GPTZero-equivalent "
                            "% score — use the manual GPTZero check to confirm.",
                        )

        # Rendered outside the button's if-block so results survive reruns.
        if regen_err_key in st.session_state:
            st.error(st.session_state[regen_err_key])
        if ai_score_key in st.session_state:
            kind, msg = st.session_state[ai_score_key]
            (st.info if kind == "info" else st.caption)(msg)

    with col2:
        st.subheader("Form Fields")

        try:
            fd = json.loads(app.get("form_data") or "{}")
        except Exception:
            fd = {}

        edited_fd = {}
        for field_key, field_val in fd.items():
            if field_key == "eligibility_note":
                st.caption(f"Note: {field_val}")
                edited_fd[field_key] = field_val
                continue
            skey = f"fd_{app_id}_{field_key}"
            if skey not in st.session_state:
                st.session_state[skey] = str(field_val or "")
            st.text_input(
                field_key.replace("_", " ").title(),
                key=skey,
            )
            edited_fd[field_key] = st.session_state[skey]

        st.divider()
        _resume_panel(app)
        st.divider()

        # Actions depend on where the application is in the flow.
        status = app["status"]
        fill_col, act_col, no_col = st.columns([2, 1.3, 1.3])
        with fill_col:
            # Same batch path as the Queue tab, for this one app: saves the
            # edited letter/fields first (the runner reads them from the DB),
            # then pre-fills a new tab. Never submits, never changes status.
            _can_fill = _fillable(app)
            if st.button("Fill Out in Chrome", key=f"chrome_{app_id}", type="primary",
                         use_container_width=True, disabled=not _can_fill or _fill_running(),
                         help=(_FILL_CONFIRM_LINE if _can_fill else
                               f"Fill Out only acts on {_fillable_label()} (Queue) "
                               "applications with a URL.")):
                update_cover_letter(app_id, st.session_state.get(editor_key, ""))
                if edited_fd:
                    update_form_data(app_id, edited_fd)
                _start_review([app_id], "detail", app_id)
                st.rerun()
        with act_col:
            if status == "queued":
                # "Applied" = Noah ALREADY submitted it himself; nothing is sent.
                if st.button("Mark applied", key=f"approve_{app_id}", use_container_width=True,
                             help="Mark as applied (you submitted it yourself)"):
                    _mark_applied(app)
                    _nav(_back_tab)
                    st.rerun()
            elif status == "discovered":
                if st.button("Add to Queue", key=f"addq_{app_id}", use_container_width=True):
                    _add_to_queue([app_id])
                    _nav(_back_tab)
                    st.rerun()
            elif status == "applied":
                if st.button("Back to Queue", key=f"undo_{app_id}", use_container_width=True,
                             help="Undo: clears the applied date"):
                    update_status(app_id, "queued")
                    _nav(_back_tab)
                    st.rerun()
            elif status == "dismissed":
                if st.button("Restore", key=f"restore_{app_id}", use_container_width=True,
                             help="Move back to Discovered"):
                    update_status(app_id, "discovered")
                    _nav(_back_tab)
                    st.rerun()
        with no_col:
            if status in ("queued", "discovered"):
                if st.button("Not interested", key=f"skip_{app_id}", use_container_width=True):
                    _dismiss(app)
                    _nav(_back_tab)
                    st.rerun()

        st.caption(_FILL_CONFIRM_LINE)
        _fill_report_panel(only_ids=[app_id])


# ---------------------------------------------------------------------------
# Review letters view  --  every Fill Out button lands here first (Q24).
# One card per application. Nothing is generated unless Noah presses a
# Generate / Regenerate button; apps still without a letter at Continue keep
# lazy fill-time generation (only if the form asks). Continue launches the
# existing batch fill; Back returns without launching.
# ---------------------------------------------------------------------------

def _generate_letter(app: dict):
    """
    desc: Generate a cover letter for one application and save it (overwrites
        any saved letter). Reuses the Detail page's "tried qualifications"
        set so repeated clicks vary the {{QUALIFICATION}} fact.
    args:
        app (dict): application row (needs id, title, company, description).
    output:
        str | None: error message, or None on success.
    """
    app_id = app["id"]
    tried = st.session_state.setdefault(f"tried_quals_{app_id}", set())
    current = st.session_state.get(f"rv_ed_{app_id}") or app.get("cover_letter") or ""
    for cand in get_qualification_candidates():
        if cand in current:
            tried.add(cand)
    try:
        new_cl = generate_cover_letter(
            job_title=app["title"], company=app["company"],
            job_description=app.get("description") or "",
            exclude_qualifications=tried,
        )
    except Exception as e:
        # Broad on purpose: any failure becomes a readable per-card message.
        return f"Couldn't generate a cover letter: {e}"
    for cand in get_qualification_candidates():
        if cand in new_cl:
            tried.add(cand)
    update_cover_letter(app_id, new_cl)
    st.session_state[f"rv_pending_{app_id}"] = new_cl   # applied before the widget renders
    return None


if st.session_state.view == "review":
    _rv_ids = st.session_state.get("review_ids") or []
    _rv_back, _rv_back_app = st.session_state.get("review_return", ("queue", None))
    _by_id = {a["id"]: a for a in get_queued_applications()}
    _rv_apps = [_by_id[i] for i in _rv_ids if i in _by_id and _fillable(_by_id[i])]

    def _rv_leave():
        _end_review()
        if _rv_back == "detail" and _rv_back_app is not None:
            st.session_state.view = "detail"
            st.session_state.selected_app_id = _rv_back_app
        else:
            st.session_state.view = "queue"

    if st.button("← Back", key="rv_back"):
        _rv_leave()
        st.rerun()

    st.subheader(f"Review letters — {len(_rv_apps)}")
    if not _rv_apps:
        st.info("Nothing to review -- these applications are no longer fillable.")
        st.stop()

    # Apply pending (just generated) values before any editor widget renders.
    for a in _rv_apps:
        ek, pk = f"rv_ed_{a['id']}", f"rv_pending_{a['id']}"
        if pk in st.session_state:
            st.session_state[ek] = st.session_state.pop(pk)
        elif ek not in st.session_state:
            st.session_state[ek] = a.get("cover_letter") or ""

    _missing = [a for a in _rv_apps if not (a.get("cover_letter") or "").strip()]
    top_a, top_b, top_c = st.columns([1.7, 1.7, 4], vertical_alignment="center")
    if top_a.button(f"Generate all missing ({len(_missing)})", key="rv_gen_all",
                    disabled=not _missing):
        with st.spinner("Generating…"):
            for a in _missing:
                err = _generate_letter(a)
                if err:
                    st.session_state[f"rv_err_{a['id']}"] = err
                else:
                    st.session_state.pop(f"rv_err_{a['id']}", None)
        st.rerun()
    if top_b.button("Continue to Fill Out →", key="rv_continue", type="primary",
                    disabled=_fill_running(),
                    help="Opens one Chrome tab per application and fills them. "
                         "Nothing is submitted."):
        # Save any edits first: the runner reads letters from the database.
        for a in _rv_apps:
            edited = st.session_state.get(f"rv_ed_{a['id']}", "")
            if edited != (a.get("cover_letter") or ""):
                update_cover_letter(a["id"], edited)
        if _launch_fill([a["id"] for a in _rv_apps]):
            _rv_leave()
            st.rerun()
    top_c.caption(_FILL_CONFIRM_LINE + " Unsaved edits are saved when you continue.")
    st.divider()

    for a in _rv_apps:
        aid = a["id"]
        ek = f"rv_ed_{aid}"
        with st.container(border=True):
            st.markdown(f"**{a['company']}** — {a['title']}")
            if st.session_state.get(f"rv_err_{aid}"):
                st.error(st.session_state[f"rv_err_{aid}"])
            if (a.get("cover_letter") or "").strip() or st.session_state[ek].strip():
                st.text_area("Cover letter (edit freely)", key=ek, height=220,
                             label_visibility="collapsed")
                b1, b2, _ = st.columns([1, 1, 5])
                if b1.button("Edit (save)", key=f"rv_save_{aid}"):
                    update_cover_letter(aid, st.session_state[ek])
                    st.toast("Letter saved.")
                    st.rerun()
                if b2.button("Regenerate", key=f"rv_regen_{aid}",
                             help="Overwrites the saved letter with a new one."):
                    with st.spinner("Generating…"):
                        err = _generate_letter(a)
                    if err:
                        st.session_state[f"rv_err_{aid}"] = err
                    else:
                        st.session_state.pop(f"rv_err_{aid}", None)
                    st.rerun()
            else:
                st.caption("No letter yet — generated only if the form asks, "
                           "or press Generate now.")
                if st.button("Generate", key=f"rv_gen_{aid}"):
                    with st.spinner("Generating…"):
                        err = _generate_letter(a)
                    if err:
                        st.session_state[f"rv_err_{aid}"] = err
                    else:
                        st.session_state.pop(f"rv_err_{aid}", None)
                    st.rerun()


# ---------------------------------------------------------------------------
# Preferences view  —  tune scorer weights / locations / interest priorities
# without touching Python. Saved to evaluator/user_overrides.json and takes
# effect immediately (evaluator/preferences.py mutates its module-level
# WEIGHTS/TARGET_LOCATIONS/INTEREST_AREAS in place, so score.py's next call
# picks it up with no restart).
# ---------------------------------------------------------------------------

elif st.session_state.view == "preferences":
    st.title("Preferences")
    st.caption("Tune how the preference scorer ranks staged postings. "
               "Changes apply immediately, no restart needed.")

    if _preferences is None:
        st.warning("Evaluator subsystem unavailable — preferences can't be edited.")
        st.stop()

    st.subheader("Score weights")
    st.caption("Points each factor can contribute (higher = matters more to the ranking).")
    weight_cols = st.columns(3)
    new_weights = {}
    # "learned" has its own section below (it is a cap, not a factor)
    for i, (key, val) in enumerate((k, v) for k, v in _preferences.WEIGHTS.items() if k != "learned"):
        with weight_cols[i % 3]:
            new_weights[key] = st.slider(
                key.replace("_", " ").title(), min_value=0, max_value=50,
                value=int(val), key=f"pref_weight_{key}",
            )

    st.divider()
    st.subheader("Learned")
    st.caption(
        "The scorer learns from what you do: adding to Queue, marking applied, and "
        "the ▲ ▼ nudges count as votes for that posting's interest area, company, "
        "prestige tier and location, and similar postings score a little higher "
        "(or lower). Removing from Queue or Not interested teaches nothing. "
        "This is the most points learning can add or subtract; 0 turns it off."
    )
    new_weights["learned"] = st.slider(
        "Learned", min_value=0, max_value=30,
        value=int(_preferences.WEIGHTS.get("learned", 0)), key="pref_weight_learned",
    )
    if _reset_signals is not None and st.button("Reset learning", key="pref_reset_learning",
                                                 help="Forget everything learned so far"):
        n_cleared = _reset_signals()
        st.toast(f"Learning reset ({n_cleared} signal(s) cleared).")
        st.rerun()

    st.divider()
    st.subheader("Interest areas")
    st.caption("Priority for each interest area (1.0 = highest priority).")
    new_priorities = {}
    for area in _preferences.INTEREST_AREAS:
        new_priorities[area["name"]] = st.slider(
            area["label"], min_value=0.0, max_value=1.0,
            value=float(area["priority"]), step=0.01,
            key=f"pref_priority_{area['name']}",
        )

    st.divider()
    st.subheader("Preferred locations")
    st.caption(
        "One per line (city names or common abbreviations, e.g. \"NYC\" or "
        "\"New York\"). A posting gets full location credit if its location "
        "canonicalizes to one of these — aliases of the same city (\"NYC\", "
        "\"New York, NY\") are automatically merged."
    )
    # TARGET_LOCATIONS holds canonical keys (e.g. "new_york"); show them in a
    # human-readable form -- canonicalize_location() re-derives the same key
    # from this text on Save regardless of exact spelling/casing typed back.
    display_locations = [loc.replace("_", " ").title() for loc in _preferences.TARGET_LOCATIONS]
    locations_text = st.text_area(
        "Preferred locations", value="\n".join(display_locations),
        height=120, label_visibility="collapsed", key="pref_locations",
    )

    st.divider()
    save_col, reset_col, _sp = st.columns([1, 1, 4])
    with save_col:
        if st.button("Save", type="primary"):
            typed = [l.strip() for l in locations_text.splitlines() if l.strip()]
            canonical_keys, new_buckets = [], []
            for loc in typed:
                key, _segments = _preferences.canonicalize_location(loc)
                if key not in canonical_keys:
                    canonical_keys.append(key)
                if key not in _KNOWN_LOCATION_KEYS and key not in new_buckets:
                    new_buckets.append(key)
            _preferences.save_overrides(
                weights=new_weights,
                target_locations=canonical_keys,
                interest_priorities=new_priorities,
            )
            # Stashed in session_state and rendered below, OUTSIDE this
            # if-block -- same fix as the Export-CSV `sync_result`
            # message above: st.rerun() immediately below would otherwise
            # discard an st.info() called in this same run before the
            # browser ever gets to show it.
            st.session_state["pref_new_buckets"] = new_buckets
            st.toast("Preferences saved — takes effect immediately.")
            st.rerun()
    if st.session_state.get("pref_new_buckets"):
        st.info(
            "Tracking as new, unrecognized-alias location bucket(s): "
            + ", ".join(st.session_state["pref_new_buckets"])
            + " — double-check the spelling if that wasn't intended."
        )
        del st.session_state["pref_new_buckets"]
    with reset_col:
        if st.button("Reset to Defaults"):
            _preferences.reset_overrides()
            # Widgets keep their old values once keyed; drop them so the
            # defaults show and the next Save cannot re-persist stale numbers.
            for _k in [k for k in st.session_state
                       if k.startswith(("pref_weight_", "pref_priority_"))
                       or k == "pref_locations"]:
                del st.session_state[_k]
            st.toast("Preferences reset to defaults.")
            st.rerun()

    st.divider()
    _answers_section()



# ---------------------------------------------------------------------------
# Applied tab  --  tracker for everything past the Queue: applied (Noah
# submitted it himself), interviewing, offer, and rejected (the company
# turned him down; "Not interested" is the separate 'dismissed' status).
# Read-only history + status-progression actions; nothing here fills out or
# submits an application itself.
# ---------------------------------------------------------------------------

elif st.session_state.view == "applied":
    tracked = [a for a in all_apps if a["status"] in _APPLIED_STATUSES]
    tracked.sort(key=lambda a: a.get("submitted_at") or a.get("updated_at") or "", reverse=True)

    show_filter = st.selectbox(
        "Show",
        ["All", "Applied", "Interviewing", "Offer", "Rejected"],
        key="applied_show",
    )
    _SHOW_TO_STATUS = {
        "Applied": "applied",
        "Interviewing": "interviewing",
        "Offer": "offer",
        "Rejected": "rejected",
    }
    if show_filter in _SHOW_TO_STATUS:
        tracked = [a for a in tracked if a["status"] == _SHOW_TO_STATUS[show_filter]]

    st.caption(f"{len(tracked)} application(s).")

    if not tracked:
        st.info("Nothing tracked yet. Mark an application applied to see it here.")
        st.stop()

    _ACOLS = [2.0, 2.2, 1.2, 1.3, 0.9, 1.2, 1.0, 1.0]
    hcols = st.columns(_ACOLS, vertical_alignment="bottom")
    for col, hdr in zip(hcols, ["Company", "Title", "Status", "Submitted", "Link", "Advance", "", ""]):
        col.markdown(f"**{hdr}**")
    st.divider()

    # status -> (label for the "move forward" button, next status). Omitted
    # for terminal-ish states (offer, rejected) -- nothing to advance to.
    _NEXT_STAGE = {
        "applied": ("Interview", "interviewing"),
        "interviewing": ("Offer", "offer"),
    }

    for app in tracked:
        cols = st.columns(_ACOLS, vertical_alignment="center")
        if cols[0].button(app["company"], key=f"aco_{app['id']}", type="tertiary"):
            _nav("detail", app["id"])
            st.rerun()
        cols[1].write(app["title"])
        _status_badge(cols[2], app["status"])
        submitted_at = app.get("submitted_at")
        cols[3].write(str(submitted_at)[:10] if submitted_at else "—")
        url = app.get("url") or ""
        cols[4].markdown(f"[↗ Posting]({url})" if url else "—")

        # Advance column
        advance = _NEXT_STAGE.get(app["status"])
        if advance:
            label, next_status = advance
            if cols[5].button(label, key=f"advance_{app['id']}"):
                update_status(app["id"], next_status)
                st.toast(f"{app['company']} → {next_status.title()}")
                st.rerun()
        else:
            cols[5].write("")

        # Undo (applied -> back to Queue, clears submitted_at) / Reject columns
        if app["status"] == "applied":
            if cols[6].button("Undo", key=f"unapprove_{app['id']}",
                              help="Move back to Queue"):
                update_status(app["id"], "queued")
                st.toast(f"{app['company']} moved back to Queue.")
                st.rerun()
        else:
            cols[6].write("")
        if app["status"] in ("applied", "interviewing"):
            if cols[7].button("Reject", key=f"reject_{app['id']}",
                              help="Record that this company turned you down"):
                update_status(app["id"], "rejected", notes="Rejected after applying")
                st.toast(f"{app['company']} marked rejected.")
                st.rerun()
        else:
            cols[7].write("")

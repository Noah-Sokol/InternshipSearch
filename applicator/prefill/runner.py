"""
Batch pre-fill runner. Attaches to ONE Chrome over CDP (launching it with the
persistent debug profile if needed), opens each application in its own tab,
arms the submit guard, runs the matching filler, disarms the guard and leaves
the tab open for Noah to review and submit himself.

Only applications whose status is in FILLABLE_STATUSES ('queued' = in the
process of applying) are ever opened; 'applied' means Noah already applied
and is always skipped. Nothing here changes an application's status, and
nothing here ever clicks submit.

The JSON report (.sessions/prefill_report.json, gitignored) holds statuses and
field LABELS only -- never the values typed into them.
"""

import json
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

from config import CHROME_CDP_PORT, CHROME_DEBUG_PROFILE, PROFILE_PATH, RESUME_PATH, SESSION_DIR
from applicator.prefill import answers, get_filler
from applicator.prefill.base import FillResult, detect_blocker, detect_closed
from applicator.prefill.resumes import DEFAULT_NOTE, ResumeChoice, resume_for_app
from applicator.prefill.safe import install_submit_guard, remove_submit_guard

FILLABLE_STATUSES = ("queued",)
REPORT_PATH = SESSION_DIR / "prefill_report.json"
DEFAULT_MAX_TABS = 10

# Optional explicit answers Noah may add under `application_answers:` in
# profile/noah.yaml (or in an application's form_data). Never defaulted.
SENSITIVE_KEYS = (
    "sponsorship", "work_authorization", "gender", "hispanic", "race",
    "veteran", "disability",
)
_FORM_DATA_SKIP = {"eligibility_note"}


# ---------------------------------------------------------------------------
# Chrome over CDP (shared with tools/open_application.py)
# ---------------------------------------------------------------------------

def find_chrome() -> str:
    """
    desc: Locate the Google Chrome executable.
    args:
        (none)
    output:
        str: path to chrome.exe. Raises FileNotFoundError if absent.
    """
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    found = shutil.which("chrome") or shutil.which("google-chrome")
    if found:
        return found
    raise FileNotFoundError("Google Chrome not found.")


def cdp_port_up(port: int, timeout: float = 25.0) -> bool:
    """
    desc: Poll until something is listening on the local CDP port.
    args:
        port (int): remote-debugging port.
        timeout (float): seconds to wait (0 = check once).
    output:
        bool: True if the port accepted a connection.
    """
    deadline = time.time() + timeout
    while True:
        with socket.socket() as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def launch_chrome(profile_dir: Path, port: int, start_url: str = "about:blank") -> subprocess.Popen:
    """
    desc: Launch a normal Chrome with a remote-debugging port and the
        persistent debug profile (keeps ATS/SSO logins across runs).
    args:
        profile_dir (Path): --user-data-dir.
        port (int): remote-debugging port.
        start_url (str): first tab's URL.
    output:
        subprocess.Popen: the Chrome process (left running for Noah).
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([
        find_chrome(),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        start_url,
    ])
    if not cdp_port_up(port):
        proc.terminate()
        raise RuntimeError(f"Chrome debug port {port} never came up.")
    return proc


def ensure_chrome(port: int = CHROME_CDP_PORT, profile_dir: Path = CHROME_DEBUG_PROFILE) -> bool:
    """
    desc: Reuse the debug Chrome if it is already listening, else launch it.
    args:
        port (int): remote-debugging port.
        profile_dir (Path): debug profile directory.
    output:
        bool: True if a new Chrome was launched.
    """
    if cdp_port_up(port, timeout=0):
        return False
    launch_chrome(profile_dir, port)
    return True


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _s(v) -> str:
    return " ".join(str(v).splitlines()).strip() if v not in (None, "") else ""


def _url(v: str) -> str:
    v = _s(v)
    if v and not re.match(r"^https?://", v, re.I):
        v = "https://" + v
    return v


def load_profile_yaml(path: Path = PROFILE_PATH) -> dict:
    """
    desc: Read profile/noah.yaml (local only; never sent anywhere).
    args:
        path (Path): profile path.
    output:
        dict: parsed YAML ({} if missing).
    """
    import yaml
    if not Path(path).exists():
        return {}
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def build_profile(app_row: dict, profile_yaml: dict | None = None) -> dict:
    """
    desc: Build the flat profile dict fillers consume: base values from
        profile/noah.yaml, overridden by the application's form_data. Only
        explicit values are included -- sensitive/eligibility keys
        (SENSITIVE_KEYS) appear only if Noah set them under
        `application_answers:` in the YAML or in form_data. Empty values are
        dropped so fillers report the field as unfilled instead of guessing.
    args:
        app_row (dict): application row; uses 'form_data' (JSON str or dict).
        profile_yaml (dict | None): parsed noah.yaml; None loads it from disk.
    output:
        dict: flat str->str profile (first_name, last_name, full_name, email,
            phone, location, city, linkedin, github, school, school_short,
            degree, major, minor, gpa, grad_year, grad_date, ...).
    """
    y = load_profile_yaml() if profile_yaml is None else (profile_yaml or {})
    personal = y.get("personal") or {}
    edu = (y.get("education") or [{}])[0] or {}
    out: dict = {}

    name = _s(personal.get("name"))
    if name:
        parts = name.split()
        out["full_name"] = name
        out["first_name"] = parts[0]
        if len(parts) > 1:
            out["last_name"] = parts[-1]
    for k in ("email", "phone", "location"):
        out[k] = _s(personal.get(k))
    out["city"] = out["location"].split(",")[0].strip() if out["location"] else ""
    out["linkedin"] = _url(personal.get("linkedin"))
    out["github"] = _url(personal.get("github"))
    out["website"] = _url(personal.get("website"))

    out["school"] = _s(edu.get("school"))
    out["school_short"] = _s(edu.get("school_short"))
    degree = _s(edu.get("degree"))
    out["degree"] = degree
    # "<Degree type>, <Major>" -> the part after the last comma; else "... in X"
    m = re.search(r"\b(?:in|of)\s+(.+)$", degree)
    if "," in degree:
        out["major"] = degree.rsplit(",", 1)[1].strip() or degree
    else:
        out["major"] = m.group(1).strip() if m else degree
    out["minor"] = _s(edu.get("minor"))
    out["gpa"] = _s(edu.get("gpa")).split("/")[0].strip()
    end = _s(edu.get("end"))
    out["grad_date"] = end
    years = re.findall(r"\b(?:19|20)\d{2}\b", end)
    out["grad_year"] = years[-1] if years else ""

    answers = y.get("application_answers") or {}
    for k, v in answers.items():
        out[str(k)] = _s(v)

    fd = app_row.get("form_data") or {}
    if isinstance(fd, str):
        try:
            fd = json.loads(fd or "{}")
        except ValueError:
            fd = {}
    for k, v in (fd or {}).items():
        if k in _FORM_DATA_SKIP or v in (None, ""):
            continue
        out[k] = _url(v) if k in ("linkedin", "github", "website") else _s(v)

    return {k: v for k, v in out.items() if v not in (None, "")}


def load_apps(app_ids: list) -> list:
    """
    desc: Load application rows (joined with their job) by id, in the given order.
    args:
        app_ids (list[int]): application ids.
    output:
        list[dict]: rows found (missing ids are absent).
    """
    from database.db import get_conn
    ids = [int(i) for i in app_ids]
    if not ids:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT a.*,
                   j.platform, j.company, j.title, j.url, j.location, j.description
            FROM applications a JOIN jobs j ON a.job_id = j.id
            WHERE a.id IN ({",".join("?" * len(ids))})
            """,
            ids,
        ).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def top_queued_ids(n: int) -> list:
    """
    desc: Best-first ids of the top-N 'queued' applications, ranked with the
        evaluator's score_application (IB-excluded dropped). Falls back to
        manual ▲/▼ adjustment then recency if the evaluator is unavailable.
    args:
        n (int): how many.
    output:
        list[int]: application ids.
    """
    from database.db import get_queued_applications
    apps = [a for a in get_queued_applications() if a.get("url")]
    try:
        from evaluator import preferences as P
        from evaluator.score import score_application, _parse_today, _load_named_targets
        today, named = _parse_today(P.DEFAULT_TODAY), _load_named_targets()
        scored = []
        for a in apps:
            try:
                s = score_application(a, today, named)
            except Exception:
                continue
            if not s.get("excluded"):
                scored.append((s.get("adjusted_score", s.get("score", 0)), a["id"]))
        scored.sort(key=lambda t: -t[0])
        return [i for _, i in scored[:n]]
    except Exception:
        apps.sort(key=lambda a: -(a.get("score_adjustment") or 0))
        return [a["id"] for a in apps[:n]]


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------

def scrub(text: str, profile: dict) -> str:
    """
    desc: Redact any profile value that leaked into a note/label (e.g. an
        exception message or a page that echoes the name back).
    args:
        text (str): text destined for the report or console.
        profile (dict): flat profile whose values must not appear.
    output:
        str: text with profile values replaced by [redacted].
    """
    text = str(text or "")
    for v in sorted({str(v) for v in profile.values() if v and len(str(v)) >= 3}, key=len, reverse=True):
        text = re.sub(re.escape(v), "[redacted]", text, flags=re.I)
    return text


def _entry(app: dict, status: str, notes: str = "", filler: str = "", filled=None,
           unfilled=None, blocked: int = 0, profile: dict | None = None,
           text_questions=None, resume: str = "") -> dict:
    p = profile or {}
    return {
        "app_id": app.get("id"),
        "company": app.get("company") or "",
        "title": app.get("title") or "",
        "url": app.get("url") or "",
        "filler": filler,
        "status": status,
        "filled": [scrub(x, p) for x in (filled or [])],
        "unfilled": [scrub(x, p) for x in (unfilled or [])],
        "notes": scrub(notes, p)[:300],
        "submit_attempts_blocked": blocked,
        # required free-text questions nothing answered (report offers "Save an answer")
        "text_questions": [scrub(x, p) for x in (text_questions or [])],
        "resume": resume,   # label of the resume used ('' when not chosen)
    }


def partition_apps(app_ids: list, rows: list, max_tabs: int,
                   statuses: tuple = FILLABLE_STATUSES) -> tuple:
    """
    desc: Split requested apps into those to open and report entries for the
        rest (not found, wrong status, no URL, over the tab limit).
    args:
        app_ids (list[int]): requested ids (order kept).
        rows (list[dict]): rows from load_apps.
        max_tabs (int): maximum tabs to open.
        statuses (tuple[str]): statuses allowed to be filled.
    output:
        tuple[list[dict], list[dict]]: (apps to open, skipped report entries).
    """
    by_id = {r["id"]: r for r in rows}
    todo, skipped, seen = [], [], set()
    for i in app_ids:
        i = int(i)
        if i in seen:
            continue
        seen.add(i)
        app = by_id.get(i)
        if app is None:
            skipped.append(_entry({"id": i}, "skipped", "Application not found."))
        elif app.get("status") not in statuses:
            why = " ('applied' = already applied)" if app.get("status") == "applied" else ""
            skipped.append(_entry(app, "skipped",
                                  f"Status is '{app.get('status')}'{why}; only "
                                  f"{'/'.join(statuses)} applications are filled."))
        elif not app.get("url"):
            skipped.append(_entry(app, "skipped", "No application URL."))
        elif len(todo) >= max_tabs:
            skipped.append(_entry(app, "skipped", f"Over the {max_tabs}-tab limit; run again."))
        else:
            todo.append(app)
    return todo, skipped


def write_report(entries: list, path: Path = REPORT_PATH) -> Path:
    """
    desc: Write the batch report (labels/statuses only) to local JSON.
    args:
        entries (list[dict]): report entries.
        path (Path): output path.
    output:
        Path: the written path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    counts: dict = {}
    for e in entries:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "counts": counts,
        "results": entries,
    }, indent=2), encoding="utf-8")
    return path


def print_summary(entries: list) -> None:
    """
    desc: Print a per-application summary (statuses and label counts only).
    args:
        entries (list[dict]): report entries.
    output:
        None.
    """
    print("\n[prefill] Summary -- review each tab and click Submit yourself:")
    for e in entries:
        print(f"  #{e['app_id']} {e['company'][:30]} -- {e['status']}"
              f" ({len(e['filled'])} filled, {len(e['unfilled'])} to answer)"
              + (f" [{e['filler']}]" if e["filler"] else ""))
        if e["unfilled"]:
            print("      to answer: " + "; ".join(e["unfilled"][:8])
                  + (" ..." if len(e["unfilled"]) > 8 else ""))
        if e["notes"]:
            print(f"      note: {e['notes']}")


# ---------------------------------------------------------------------------
# Filling
# ---------------------------------------------------------------------------

def make_cover_letter_getter(app: dict):
    """
    desc: Build the lazy get_cover_letter() callable handed to fillers. Fillers
        call it only when they find a cover-letter field. It returns the app's
        saved letter if there is one (a saved letter is NEVER overwritten);
        otherwise it generates once, saves it to the DB, and caches the result
        for the rest of this tab. On any failure it returns '' (also cached, so
        a failing API isn't retried per field) and the filler reports the
        "generation failed" label.
    args:
        app (dict): application row (id, company, title, description,
            cover_letter).
    output (Callable[[], str]):
        zero-arg function returning the letter text or ''.
    """
    cache: dict = {}

    def get() -> str:
        if "text" in cache:
            return cache["text"]
        text = (app.get("cover_letter") or "").strip()
        if not text:
            try:
                from database.db import get_saved_cover_letter, save_cover_letter_if_empty
                # re-read the DB: Noah may have saved one since this row was loaded
                text = get_saved_cover_letter(app["id"])
                if not text:
                    from generator.cover_letter import generate
                    letter = generate(
                        job_title=app.get("title") or "",
                        company=app.get("company") or "",
                        job_description=app.get("description") or "",
                    )
                    text = save_cover_letter_if_empty(app["id"], letter) if (letter or "").strip() else ""
            except Exception:
                text = ""
        cache["text"] = text
        return text

    return get


def fill_tab(page, app: dict, profile: dict, resume_path: Path = RESUME_PATH,
             http_status: int | None = None, resume_choice: ResumeChoice | None = None) -> dict:
    """
    desc: Fill one already-navigated tab: arm the guard, detect login wall /
        CAPTCHA (never bypassed), run the matching filler, then always
        disarm the guard so Noah's own Submit click works. Never raises.
    args:
        page (playwright.sync_api.Page): tab at the application URL.
        app (dict): application row (id, company, title, url, cover_letter).
        profile (dict): flat profile from build_profile.
        resume_path (Path): resume file to upload (ignored when resume_choice is given).
        http_status (int | None): HTTP status of the tab's navigation, used to
            spot a closed (404/410) posting.
        resume_choice (ResumeChoice | None): resume picked for this app; its
            path is uploaded, and a 'default' pick adds a report note.
    output:
        dict: report entry (labels only).
    """
    filler_name, blocked, unanswered = "", 0, []
    if resume_choice is not None:
        resume_path = resume_choice.path
    try:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass
        filler = get_filler(page.url if page.url.startswith("http") else app["url"])
        filler_name = filler.name
        closed = detect_closed(page, http_status)   # read-only; checked before any fill
        if closed:
            # closed page: no guard, no filler, and the cover-letter callable is never called
            return _entry(app, closed[0], closed[1], filler_name, [], [], 0, profile)
        install_submit_guard(page)
        try:
            blk = None if filler.handles_login else detect_blocker(page)
            if blk:
                result = FillResult(blk[0], [], [], blk[1])
            else:
                with answers.answer_context(app.get("company") or "") as actx:
                    try:
                        result = filler.fill(page, profile, make_cover_letter_getter(app),
                                             Path(resume_path))
                    finally:
                        unanswered = list(actx["unanswered"])
                if result.status in ("unsupported", "partial") and not result.filled:
                    blk = detect_blocker(page)
                    if blk:
                        result = FillResult(blk[0], [], result.unfilled, blk[1])
        finally:
            blocked = remove_submit_guard(page)
    except Exception as e:
        msg = str(e).strip().splitlines()[0] if str(e).strip() else ""
        result = FillResult("error", [], [], f"{type(e).__name__}: {msg[:150]}")
    notes = result.notes
    if blocked:
        notes = (notes + f" Blocked {blocked} submit attempt(s) during fill.").strip()
    if resume_choice is not None and resume_choice.source == "default":
        notes = (notes + " " + DEFAULT_NOTE).strip()
    return _entry(app, result.status, notes, filler_name, result.filled, result.unfilled,
                  blocked, profile, unanswered, resume_choice.label if resume_choice else "")


def run_batch(app_ids: list, max_tabs: int = DEFAULT_MAX_TABS, statuses: tuple = FILLABLE_STATUSES,
              resume_path: Path | None = None, report_path: Path = REPORT_PATH,
              cdp_url: str | None = None) -> list:
    """
    desc: Pre-fill many applications, one Chrome tab each, for Noah to review
        and submit by hand. Only apps whose status is in `statuses` are
        opened (default 'queued'); others are skipped with a note. All tabs
        are opened first so they load in parallel, then filled one at a time;
        one tab's failure never stops the batch. Never submits, never changes
        any application status. Tabs are left open.
    args:
        app_ids (list[int]): application ids, in priority order.
        max_tabs (int): maximum tabs to open this run.
        statuses (tuple[str]): statuses allowed to be filled.
        resume_path (Path | None): one resume for every app; None (default)
            picks per app from its interest area / Detail-page override.
        report_path (Path): where the JSON report is written.
        cdp_url (str | None): attach to this CDP endpoint instead of the
            default debug Chrome (launched if not running).
    output:
        list[dict]: report entries (labels/statuses only).
    """
    rows = load_apps(app_ids)
    todo, entries = partition_apps(app_ids, rows, max_tabs, statuses)
    if not todo:
        write_report(entries, report_path)
        print_summary(entries)
        return entries

    from playwright.sync_api import sync_playwright

    if cdp_url is None:
        ensure_chrome()
        cdp_url = f"http://127.0.0.1:{CHROME_CDP_PORT}"
    profile_yaml = load_profile_yaml()

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        blank = [p for p in ctx.pages if p.url in ("about:blank", "chrome://newtab/")]

        tabs = []
        for app in todo:
            page = None
            try:
                page = blank.pop(0) if blank else ctx.new_page()
                resp = page.goto(app["url"], wait_until="commit", timeout=45000)
                status_code = resp.status if resp is not None else None
                tabs.append((app, page, None, status_code))
            except Exception as e:
                tabs.append((app, page, f"Could not open URL ({type(e).__name__}).", None))

        filled_entries = []
        for n, (app, page, open_err, http_status) in enumerate(tabs, 1):
            print(f"[prefill] ({n}/{len(tabs)}) #{app['id']} {app['company']} -- {app['title']}")
            if open_err or page is None:
                filled_entries.append(_entry(app, "error", open_err or "No tab."))
                continue
            profile = build_profile(app, profile_yaml)
            filled_entries.append(fill_tab(
                page, app, profile, resume_path or RESUME_PATH, http_status,
                None if resume_path else resume_for_app(app)))

        try:
            tabs[0][1].bring_to_front()
        except Exception:
            pass
        # Disconnect only; Chrome and every filled tab stay open for Noah.

    entries = filled_entries + entries
    write_report(entries, report_path)
    print_summary(entries)
    print(f"[prefill] Report: {report_path}")
    return entries

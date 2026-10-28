"""
Internship Application Orchestrator

Usage:
    python main.py             # Scrape new jobs and stage applications
    python main.py --unattended  # Weekly scheduled discovery: no-login sources only,
                               # never prompts/logs in/fills; logs to .sessions/scrape_log.txt
    python main.py --submit   # DISABLED: 'applied' means already applied;
                               # use the review UI's Fill Out (never submits)
"""

import sys
import time

_STARTED = time.time()


def _log_startup_failure(exc: BaseException) -> None:
    """
    desc: Unattended runs only: a crash before main() (bad profile YAML, broken
        playwright/scraper import) still leaves one "fail" scrape-log line.
    args:
        exc (BaseException): what stopped the start-up.
    output:
        None
    """
    try:
        from scraper.scrape_log import append_run, short_error
        append_run("fail", 0, 0, 0, time.time() - _STARTED, [short_error("startup", exc)])
    except Exception:
        pass


try:
    import socket
    import shutil
    import getpass
    import argparse
    import subprocess
    import yaml
    from pathlib import Path
    from playwright.sync_api import sync_playwright

    from config import (
        PROFILE_PATH, SESSION_DIR,
        SEARCH_QUERIES, NAMED_TARGETS,
        CHROME_CDP_PORT, CHROME_DEBUG_PROFILE,
    )
    from database.db import init_db, upsert_job, create_application
    from scraper.linkedin import LinkedInScraper
    from scraper.linkedin_guest import LinkedInGuestScraper
    from scraper.handshake import HandshakeScraper
    from scraper.direct_check import check_all_targets, external_id_for
    from scraper.greenhouse_lever import AtsScraper
    from scraper.coverage import discover_coverage_internships, CoverageEngine

    PROFILE = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))

    FORM_DATA = {
        "first_name": "Noah",
        "last_name": "Sokol",
        "email": PROFILE["personal"]["email"],
        "phone": PROFILE["personal"]["phone"],
        "location": PROFILE["personal"]["location"],
        "linkedin": f"https://{PROFILE['personal']['linkedin']}",
        "gpa": PROFILE["education"][0]["gpa"].split("/")[0],
        # Derived from profile/noah.yaml's education[0].end (e.g. "May 2029") so
        # this can't silently drift out of sync with the profile if Noah's
        # expected graduation date ever changes -- it was previously a bare
        # "2029" literal duplicated here with no link back to the source of truth.
        "grad_year": PROFILE["education"][0]["end"].split()[-1],
        "school": PROFILE["education"][0]["school"],
    }
except BaseException as _exc:   # import-time crash: log it for the scheduled run
    if "--unattended" in sys.argv:
        _log_startup_failure(_exc)
        raise SystemExit(1)
    raise


# Postings found again this process that were already tracked (read by the
# unattended run's summary line).
_RESEEN: set = set()   # unique (platform, external_id), not per-sighting


def already_applied(platform: str, external_id: str) -> bool:
    """
    desc: Check whether an application already exists for this platform +
        external_id pair, so a re-run doesn't re-stage the same posting.
    args:
        platform (str): the source platform, e.g. "linkedin", "handshake",
            "direct", or an ATS name.
        external_id (str): the posting's stable ID on that platform.
    output (bool):
        True if a matching job already has an application row (and, if so,
        its last_seen_at is refreshed).
    """
    from database.db import get_conn, touch_job_seen
    with get_conn() as conn:
        row = conn.execute(
            "SELECT j.id FROM jobs j JOIN applications a ON a.job_id = j.id "
            "WHERE j.platform=? AND j.external_id=?",
            (platform, external_id),
        ).fetchone()
    if row is not None:
        # re-found by this scrape: still listed, so it is not stale
        touch_job_seen(platform, external_id)
        _RESEEN.add((platform, external_id))
    return row is not None


def evaluate_posting(scraper, job, base_notes: str):
    """
    Post-fetch check run once the full description is available (title-only
    filtering happens earlier, inside the scraper's search()). Returns None
    if the posting should be skipped entirely (new-grad/full-time/senior
    language found in the body text), otherwise returns the notes string to
    attach to the staged application -- with a cycle-mismatch warning
    appended if the target year isn't mentioned anywhere in the posting.
    """
    full_text = f"{job.title} {job.description}"

    if scraper._is_excluded(full_text):
        return None

    notes = base_notes
    if not scraper._mentions_target_cycle(full_text):
        warning = "Target cycle (2027) not mentioned in posting -- verify recruiting year before applying."
        notes = f"{notes} {warning}".strip() if notes else warning

    return notes


def build_query_list():
    """
    Combine generic keyword searches with named pipeline-program searches.
    Returns a list of (query_string, notes) tuples -- notes is "" for
    generic searches, or the eligibility/timing note for named targets.
    """
    queries = [(q, "") for qs in SEARCH_QUERIES.values() for q in qs]
    for target in NAMED_TARGETS:
        note = f"[{target['company']} - {target['program']}] {target['notes']}".strip()
        for term in target["search_terms"]:
            queries.append((term, note))
    return queries


def check_named_targets(playwright):
    """
    Visit each NAMED_TARGETS direct_url directly rather than relying on
    keyword search -- named pipeline programs (Citadel Launch, Optiver
    Quantitative Intern, etc.) frequently don't surface from generic
    LinkedIn/Handshake keyword searches at all. No login needed, these
    are public career pages.
    """
    print("\n[direct-check] Checking named program pages...")
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()

    results = check_all_targets(page, NAMED_TARGETS)
    browser.close()

    staged = 0
    print("\n[direct-check] Summary:")
    for target, result in zip(
        [t for t in NAMED_TARGETS if t.get("eligibility") != "not_eligible"], results
    ):
        print(f"  {result.status.upper():8s} {result.company} - {result.program}")

        if result.status != "open":
            continue

        external_id = external_id_for(target)
        if already_applied("direct", external_id):
            continue

        title = result.title or target["program"]
        job_id = upsert_job(
            "direct", external_id, target["company"], title,
            result.location, result.url, result.description, result.date_posted,
        )
        note = f"[Direct check] {target['notes']}".strip()
        form_data = {**FORM_DATA, "eligibility_note": note}
        create_application(job_id, "", form_data)  # letter is generated lazily at fill time
        staged += 1

    print(f"[direct-check] {staged} named-target postings staged.")
    return staged


def check_ats_boards():
    """
    No-login bulk discovery from free, public ATS + aggregator JSON endpoints
    (Greenhouse/Lever/Ashby boards in config.ATS_TARGETS, plus the SimplifyJobs /
    vanshb03 Summer2027 lists). LinkedIn and Handshake both require login and get
    skipped on an unauthenticated run, so this is what actually surfaces
    companies. Deliberately does NOT generate cover letters -- this is a wide net
    and letters would burn API budget on postings Noah may never pursue; they get
    written later, per-application, in the review queue. Each hit is staged with
    an empty cover letter and a stable external_id so re-runs dedupe cleanly.
    Staging only -- nothing here submits an application.
    """
    print("\n[ats] Discovering postings from public ATS boards (no login)...")
    scraper = AtsScraper()

    staged = 0
    scanned = 0
    for job in scraper.discover():
        scanned += 1
        if already_applied(job.platform, job.external_id):
            continue

        # Post-discovery exclusion on title (+ any description we have). Most
        # aggregator rows carry no body, so this is mainly the title gate; the
        # cycle-mismatch flag mirrors the browser path so review sees the warning.
        final_notes = evaluate_posting(scraper, job, "[ATS discovery]")
        if final_notes is None:
            continue

        job_id = upsert_job(
            job.platform, job.external_id, job.company,
            job.title, job.location, job.url, job.description,
        )
        form_data = {**FORM_DATA, "eligibility_note": final_notes}
        create_application(job_id, cover_letter="", form_data=form_data)
        staged += 1

    print(f"[ats] Scanned {scanned} internship postings; staged {staged} new.")
    return staged


def _persistent_context(playwright, profile_name: str, headless: bool = False):
    """
    Launch Chrome against a persistent on-disk profile so a login done once
    (cookies, session) survives across runs -- no need to re-authenticate,
    and no credentials are stored anywhere by us. headless=True is used for the
    terminal LinkedIn login (no window); the windowed flows keep the default.
    """
    return playwright.chromium.launch_persistent_context(
        str(SESSION_DIR / profile_name),
        headless=headless,
        channel="chrome",
        args=["--disable-blink-features=AutomationControlled"],
    )


def _find_chrome() -> str:
    """
    Locate the installed Chrome binary. Needed because the Handshake path
    launches Chrome as a plain subprocess (not via Playwright) so it clears
    Cloudflare -- see _launch_debug_chrome.
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
    raise FileNotFoundError(
        "Google Chrome not found. Install it, or edit _find_chrome() with its path."
    )


def _cdp_port_up(port: int, timeout: float = 25.0) -> bool:
    """Poll until Chrome's remote-debugging port accepts connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


def _launch_debug_chrome(profile_dir: Path, port: int, start_url: str = "about:blank"):
    """
    Launch a normal Chrome (as a subprocess, NOT via Playwright) with a
    remote-debugging port and a dedicated on-disk profile. A self-launched
    Chrome clears Cloudflare's bot challenge that fronts Handshake, where a
    Playwright-launched browser gets stuck on the "verifying you are human"
    interstitial (confirmed 2026-09-14). The profile persists the WashU-SSO
    session across runs. Returns the Popen handle (terminate it when done).
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([
        _find_chrome(),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        start_url,
    ])
    if not _cdp_port_up(port):
        proc.terminate()
        raise RuntimeError(
            f"Chrome debug port {port} never came up. Is another Chrome already "
            f"using --user-data-dir={profile_dir} or port {port}?"
        )
    return proc


def _attach_cdp(playwright, port: int):
    """
    Attach Playwright to the self-launched Chrome over CDP and return
    (browser, context). The context is the one Chrome already opened, so its
    cleared-Cloudflare cookies and any saved session are reused.
    """
    browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    return browser, ctx


def _stop_debug_chrome(proc):
    """Terminate the debug-Chrome subprocess; the profile on disk is the saved
    session, so nothing is lost by closing the browser."""
    if not proc:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()




def _windowed_login(playwright, name, profile, scraper_cls, instruction, markers) -> bool:
    """
    Open <name> in a visible persistent-profile window and wait for a hand
    login (2FA / SSO / Duo push / CAPTCHA), then save the session. Login is
    detected automatically (no ENTER needed), so it works whether or not stdin
    is interactive. This is the fallback for anything that can't be completed
    purely from the terminal. Returns True once logged in.
    """
    context = _persistent_context(playwright, profile, headless=False)
    scraper = scraper_cls(context.new_page())
    try:
        if scraper.is_logged_in():
            print("  Already logged in.")
            scraper.save_session()
            return True
        print(f"  Browser is open. {instruction}")
        try:
            input("  Press ENTER when logged in > ")
        except (EOFError, KeyboardInterrupt):
            pass
        scraper.save_session()
        print(f"  {name} session saved.")
        return True
    finally:
        context.close()


def _terminal_login_linkedin(playwright) -> bool:
    """
    Attempt a terminal-only LinkedIn login: prompt for credentials (used once,
    never stored), drive a headless browser, and prompt for a one-time 2FA code
    in the terminal if LinkedIn asks. Falls back to a visible window only if
    LinkedIn throws a CAPTCHA / non-OTP checkpoint that a terminal can't solve.
    Returns True once logged in.
    """
    print("\n[login] linkedin")

    def _fallback():
        return _windowed_login(
            playwright, "linkedin", "linkedin_profile", LinkedInScraper,
            "Log into LinkedIn (complete any 2FA on your phone).",
            ["linkedin.com/feed"],
        )

    context = _persistent_context(playwright, "linkedin_profile", headless=True)
    scraper = LinkedInScraper(context.new_page())

    if scraper.is_logged_in():
        print("  Already logged in.")
        scraper.save_session()
        context.close()
        return True

    print("  Terminal login. Credentials are used once and never stored.")
    try:
        email = input("  LinkedIn email: ").strip()
        password = getpass.getpass("  LinkedIn password (hidden): ")
    except (EOFError, KeyboardInterrupt):
        email = password = ""

    if not email or not password:
        print("  No credentials entered -- opening a window instead.")
        context.close()
        return _fallback()

    def _get_otp():
        try:
            return input("  LinkedIn verification code (blank to open a window): ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    submitted = scraper.submit_credentials(email, password)
    password = None  # drop the plaintext reference as soon as it's been used
    if not submitted:
        print("  Couldn't submit the login form -- opening a window instead.")
        context.close()
        return _fallback()

    result = scraper.resolve_login(_get_otp)
    if result == "success":
        scraper.save_session()
        print("  linkedin session saved.")
        context.close()
        return True

    context.close()
    if result == "captcha":
        print("  LinkedIn wants a security check that can't be done from the")
        print("  terminal -- opening a window so you can finish it.")
    else:
        print("  Terminal login didn't complete -- opening a window instead.")
    return _fallback()




def _cdp_handshake_login(playwright) -> bool:
    """
    Log into Handshake via a self-launched Chrome attached over CDP. This is the
    only way past the Cloudflare bot challenge that fronts Handshake: a Chrome we
    launch as a normal subprocess clears it, then we attach and let the user
    complete WashU SSO + the Duo push by hand in that same window. Login is
    auto-detected (no ENTER needed), and the session persists in
    CHROME_DEBUG_PROFILE so later scrape runs are already authenticated.
    Returns True once logged in.
    """
    print("\n[login] handshake (self-launched Chrome + CDP -- clears Cloudflare)")
    proc = _launch_debug_chrome(
        CHROME_DEBUG_PROFILE, CHROME_CDP_PORT,
        start_url="https://app.joinhandshake.com/login",
    )
    try:
        _, ctx = _attach_cdp(playwright, CHROME_CDP_PORT)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        scraper = HandshakeScraper(page)

        if scraper.is_logged_in():
            print("  Already logged in.")
            return True

        print("  A Chrome window is open on the Handshake login page.")
        print("  Click 'Continue with email', enter your @wustl.edu address, then")
        print("  complete WashU SSO and approve the Duo push on your phone.")
        try:
            input("  Press ENTER when logged in > ")
        except (EOFError, KeyboardInterrupt):
            pass
        print("  handshake session saved (persists in the debug profile).")
        return True
    finally:
        _stop_debug_chrome(proc)


def login_flow(playwright):
    """
    One-time login. LinkedIn is attempted entirely from the terminal
    (credentials prompted here and never stored; 2FA code entered in the
    terminal), falling back to a visible window only if LinkedIn throws a
    CAPTCHA / non-OTP checkpoint. Handshake is done in a self-launched Chrome
    attached over CDP -- the only way past its Cloudflare bot wall -- where you
    complete WashU SSO + the Duo push by hand; login is auto-detected (no ENTER
    needed). Once logged in, the session lives in the debug profile and later
    scrape runs are already authenticated.
    """
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    _terminal_login_linkedin(playwright)

    _cdp_handshake_login(playwright)

    print("\n[login] Done. Now run:  python main.py")


def _scrape_platform_query(scraper, query: str, notes: str) -> int:
    """
    Run one search query on an already-logged-in scraper and stage every hit
    that survives the post-fetch exclusion check. Returns the number of new
    applications staged. Shared by the LinkedIn and Handshake passes, which
    are otherwise identical.
    """
    staged = 0
    print(f"  Searching: '{query}'" + (f"  ({notes})" if notes else ""))
    for job in scraper.search(query):
        if already_applied(job.platform, job.external_id):
            continue
        job.description = scraper.fetch_description(job)
        final_notes = evaluate_posting(scraper, job, notes)
        if final_notes is None:
            print(f"    Skipped (new-grad/senior/full-time language): {job.title} at {job.company}")
            continue
        job_id = upsert_job(
            job.platform, job.external_id, job.company,
            job.title, job.location, job.url, job.description,
        )
        form_data = {**FORM_DATA, "eligibility_note": final_notes} if final_notes else FORM_DATA
        create_application(job_id, "", form_data)
        print(f"    Added to Discovered: {job.title} at {job.company}")
        staged += 1
    return staged


def check_coverage() -> int:
    """
    desc: Context-aware breadth engine (free, no-login sources; self-improving).
        Unlike the raw ATS aggregators, this pass applies a relevance gate driven
        by Noah's profile, so it surfaces broad-but-relevant postings only.
    args: none
    output:
        int: number of new applications staged.
    """
    print("\n[coverage] Context-aware breadth discovery...")
    staged = 0
    cov_scraper = CoverageEngine()  # subclasses BaseScraper -> exclusion/cycle helpers
    for job in discover_coverage_internships():
        if already_applied(job.platform, job.external_id):
            continue
        # description already fetched by the engine; run the shared post-fetch gate
        final_notes = evaluate_posting(cov_scraper, job, "[coverage] breadth-engine source")
        if final_notes is None:
            continue
        job_id = upsert_job(job.platform, job.external_id, job.company,
                            job.title, job.location, job.url, job.description)
        create_application(job_id, "", {**FORM_DATA, "eligibility_note": final_notes})
        staged += 1
    return staged


def scrape_linkedin_guest(queries) -> int:
    """
    desc: No-login LinkedIn discovery via the public guest endpoint (plain HTTP,
        cache-first -- no browser, no login). This replaces the authenticated
        LinkedIn browser scrape for discovery: titles, links, and descriptions
        all come over HTTP, so the cover-letter path is unchanged. The browser
        scraper (LinkedInScraper) is kept in the tree but is no longer used here.
    args:
        queries (list[tuple[str, str]]): (query, notes) pairs from build_query_list().
    output:
        int: number of new applications staged.
    """
    print("\n[linkedin-guest] Scraping public LinkedIn jobs (no login)...")
    guest = LinkedInGuestScraper()
    staged = 0
    for query, notes in queries:
        staged += _scrape_platform_query(guest, query, notes)
    return staged


def scrape_and_stage(playwright):
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    new_jobs = 0

    check_named_targets(playwright)

    # No-login public ATS pass. Runs regardless of LinkedIn/Handshake session
    # state -- it's the source that surfaces companies when those are skipped.
    new_jobs += check_ats_boards()

    new_jobs += check_coverage()

    queries = build_query_list()
    new_jobs += scrape_linkedin_guest(queries)

    # Handshake is the one remaining source that genuinely needs a browser: its
    # postings are WashU-SSO-gated with no public endpoint. It's also behind
    # Cloudflare, so it must be reached via a self-launched Chrome attached over
    # CDP (a Playwright-launched browser gets stuck on the bot challenge). The
    # debug profile carries the session from --login, so this is already
    # authenticated. Skipped cleanly when not logged in or if Chrome can't start.
    proc = None
    try:
        proc = _launch_debug_chrome(CHROME_DEBUG_PROFILE, CHROME_CDP_PORT)
        _, ctx = _attach_cdp(playwright, CHROME_CDP_PORT)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        scraper = HandshakeScraper(page)

        if not scraper.is_logged_in():
            print("\n[handshake] Not logged in -- skipping. Run:  python main.py --login")
        else:
            print("\n[handshake] Scraping jobs...")
            for query, notes in queries:
                new_jobs += _scrape_platform_query(scraper, query, notes)
    except Exception as e:
        print(f"\n[handshake] Skipped (couldn't attach a browser): {e}")
    finally:
        _stop_debug_chrome(proc)

    print(f"\nDone. {new_jobs} new applications added to Discovered.")
    print("Run:  streamlit run review/app.py  — triage the Discovered tab.")


def _count_applications() -> int:
    """
    desc: Total rows in applications (used to count new postings by difference).
    args: none
    output:
        int: row count.
    """
    from database.db import get_conn
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]


def _count_possibly_closed() -> int:
    """
    desc: Active (discovered/queued) applications whose posting has not been
        re-found by a scrape for config.STALE_DAYS or more -- same rule as the
        review UI's "possibly closed" marker.
    args: none
    output:
        int: count.
    """
    import config
    from database.db import get_conn
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM applications a JOIN jobs j ON a.job_id = j.id "
            "WHERE a.status IN ('discovered', 'queued') AND j.last_seen_at IS NOT NULL "
            "AND j.last_seen_at <= datetime('now', ?)",
            (f"-{int(config.STALE_DAYS)} days",),
        ).fetchone()[0]


def _max_application_id() -> int:
    """
    desc: Highest applications.id (new postings in a run have a larger id).
    args: none
    output:
        int: max id, 0 when empty.
    """
    from database.db import get_conn
    with get_conn() as conn:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM applications").fetchone()[0]


DESCRIPTION_CAP_PER_RUN = 100


def fetch_new_descriptions(after_app_id: int, cap: int = DESCRIPTION_CAP_PER_RUN) -> int:
    """
    desc: Fill missing descriptions (read-only page fetches) for postings staged
        after the given application id, at most `cap` pages. Re-runs the
        graduate filter on filled rows.
    args:
        after_app_id (int): only applications with a larger id are considered.
        cap (int): max pages fetched.
    output:
        int: descriptions filled.
    """
    from scraper.description_fill import fill_rows, select_candidates
    rows = select_candidates(limit=cap, min_app_id=after_app_id)
    return fill_rows(rows)["filled"]


def scrape_unattended(playwright) -> str:
    """
    desc: Weekly scheduled discovery. Runs ONLY no-login sources (named target
        pages, public ATS boards, the coverage engine, LinkedIn guest search);
        Handshake and logged-in LinkedIn are skipped without waiting. Never
        prompts, never logs in, never fills or applies -- staging only. Each
        source runs in isolation so one failure does not stop the rest. Appends
        one summary line (and one short line per failed source) to the scrape log.
    args:
        playwright: active sync_playwright() handle (named-target check only).
    output:
        str: log status, "ok" (all sources ran), "warn" (some failed) or
            "fail" (all failed).
    """
    from scraper import net_stats
    from scraper.scrape_log import append_run, short_error
    started = time.time()
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    before = _count_applications()
    before_id = _max_application_id()

    sources = [
        ("named-targets", lambda: check_named_targets(playwright)),
        ("ats", check_ats_boards),
        ("coverage", check_coverage),
        ("linkedin-guest", lambda: scrape_linkedin_guest(build_query_list())),
    ]
    errors = []
    for name, run in sources:
        net_stats.begin(name)
        try:
            run()
        except Exception as e:  # one short line, never a traceback
            errors.append(short_error(name, e))
        else:
            # helpers swallow network failures; a source whose requests mostly
            # failed (offline, blocked, 429) must not log as a clean "ok"
            if net_stats.mostly_failed(name):
                attempted, failed = net_stats.counts(name)
                errors.append(short_error(name, RuntimeError(f"{failed}/{attempted} requests failed")))

    source_errors = len(errors)
    filled = 0
    try:
        filled = fetch_new_descriptions(before_id)
    except Exception as e:  # description fill is best-effort; log one short line
        errors.append(short_error("descriptions", e))

    new = max(0, _count_applications() - before)
    status = "ok" if not errors else ("fail" if source_errors >= len(sources) else "warn")
    # postings already tracked that this run found again; touch_job_seen /
    # upsert_job refresh last_seen_at, so it is counted via the run counter
    line = append_run(status, new, len(_RESEEN), _count_possibly_closed(),
                      time.time() - started, errors, descriptions=filled)
    print("\n" + line)
    return status


def _run_unattended() -> int:
    """
    desc: Entry for --unattended. Any crash outside scrape_unattended's
        per-source handling (init_db, playwright start-up) still appends a
        single "fail" scrape-log line.
    args: none
    output:
        int: process exit code (0 unless the run failed).
    """
    from scraper.scrape_log import append_run, short_error
    started = time.time()
    try:
        init_db()
        with sync_playwright() as pw:
            return 0 if scrape_unattended(pw) != "fail" else 1
    except Exception as e:
        append_run("fail", 0, 0, 0, time.time() - started, [short_error("startup", e)])
        return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", action="store_true",
                        help="One-time login: LinkedIn from the terminal (email/password/2FA, "
                             "window only on CAPTCHA); Handshake via a window (WashU SSO + Duo)")
    parser.add_argument("--submit", action="store_true",
                        help="DISABLED -- 'applied' means you already applied; use Fill Out "
                             "in the review UI (pre-fills tabs, you submit yourself)")
    parser.add_argument("--unattended", action="store_true",
                        help="Weekly scheduled run: no-login sources only, never prompts or logs in, "
                             "discovery only; appends a summary to .sessions/scrape_log.txt")
    args = parser.parse_args()

    if args.unattended:
        sys.exit(_run_unattended())

    init_db()

    with sync_playwright() as pw:
        if args.login:
            login_flow(pw)
        elif args.submit:
            # Disabled: 'applied' means Noah ALREADY applied, so this
            # would re-submit every one of them, and it clicks the real
            # submit button (CLAUDE.md #3). Use the review UI's Fill Out
            # (applicator/prefill), which never submits.
            print("--submit is disabled: 'applied' = already applied. Use Fill Out in "
                  "the review UI (queued apps), then click Submit yourself in each tab.")
        else:
            scrape_and_stage(pw)


if __name__ == "__main__":
    main()

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

_env_file = ROOT / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

PROFILE_PATH = ROOT / "profile" / "noah.yaml"
# APPLICATOR_DB_PATH lets a test run point at a copy instead of the real DB.
DB_PATH = Path(os.environ.get("APPLICATOR_DB_PATH") or ROOT / "database" / "applications.db")
# Resumes live in profile/resumes/. The data-science resume is the default;
# QUANT_RESUME_PATH is wired in per interest area (see review/redesign_decisions.md Q20).
RESUME_DIR = ROOT / "profile" / "resumes"
RESUME_PATH = RESUME_DIR / "Noah-Sokol-Resume-Data-Science.pdf"
QUANT_RESUME_PATH = RESUME_DIR / "Noah-Sokol-Resume-Quant.pdf"
# Interest area (taxonomy name or label) -> resume. Anything not listed, or a
# posting with no interest-area match, uses RESUME_PATH (Q20b).
INTEREST_AREA_RESUMES = {
    "quant_research_trading": QUANT_RESUME_PATH,
    "Quantitative Research / Trading": QUANT_RESUME_PATH,
    "quant_corp_finance": QUANT_RESUME_PATH,
    "Quant / Corporate Finance": QUANT_RESUME_PATH,
    "data_science_ml": RESUME_PATH,
    "sports_analytics": RESUME_PATH,
}
# Resumes Noah can pick per application on the Detail page (label -> path).
RESUME_CHOICES = {
    "Data science": RESUME_PATH,
    "Quant": QUANT_RESUME_PATH,
}
# Answer bank (Noah's own pasted-verbatim answers); gitignored like noah.yaml.
ANSWERS_PATH = Path(os.environ.get("APPLICATOR_ANSWERS_PATH") or ROOT / "profile" / "answers.yaml")

# Job search keywords by interest area.
# "Summer 2027" is appended explicitly to bias search results toward the
# correct recruiting cycle (Noah's sophomore summer) -- plain "intern"
# searches surface a mix of cycles and skew toward roles for graduating
# seniors. The plain-keyword variants are kept too since some postings don't
# name a year in the title at all.
TARGET_CYCLE = "Summer 2027"

SEARCH_QUERIES = {
    "data_science": [
        "data science intern",
        f"data science intern {TARGET_CYCLE}",
        "machine learning intern",
        f"machine learning intern {TARGET_CYCLE}",
        "quantitative analyst intern",
        "data analyst intern",
        "sophomore data science internship",
    ],
    "finance": [
        "investment research intern",
        "finance intern",
        f"finance intern {TARGET_CYCLE}",
        "equity research intern",
        "capital markets intern",
        "asset management intern",
        "sophomore finance program",
    ],
    "sports_analytics": [
        "sports analytics intern",
        f"sports analytics intern {TARGET_CYCLE}",
        "baseball analytics intern",
        "sports data intern",
        "quantitative research sports",
    ],
}

# Named pipeline programs and specific opportunities, from targeted research
# (see research/summer_2027_targets.md). Generic keyword search misses these —
# "Optiver internship" or "Citadel Launch" won't surface from "finance intern".
# Recompute this list each recruiting cycle: it's tied to Noah's class year
# (sophomore as of the Sept 2026 pass -> targeting Summer 2027, his sophomore
# summer; junior summer is Summer 2028, when standard junior-only IB/quant
# summer-analyst programs become the relevant target instead).
NAMED_TARGETS = [
    {
        "company": "Optiver",
        "program": "Quantitative Intern (Summer 2027)",
        "search_terms": ["Optiver internship"],
        "direct_url": "https://www.optiver.com/join-us/jobs/institutional-sales-and-trading/chicago/quantitative-intern-summer-2027/",
        "category": "quant",
        "eligibility": "confirmed",
        "notes": "Live posting confirmed 2026-09-12. Rolling basis; Optiver has an 8-month reapply cooldown after any assessment/interview, so apply carefully once.",
    },
    {
        "company": "Citadel",
        "program": "Citadel Launch",
        "search_terms": ["Citadel Launch"],
        "direct_url": "https://www.citadel.com/careers/students/",
        "category": "quant",
        "eligibility": "needs_verification",
        "notes": "No live Summer 2027 Launch posting found directly (2026-09-12 search only surfaced 2024/2025 cycles via aggregators). Check citadel.com/careers/students directly for when the 2027 cohort posts.",
    },
    {
        "company": "Two Sigma",
        "program": "Quantitative Researcher - Intern (2027 Summer)",
        "search_terms": ["Two Sigma quantitative researcher intern"],
        "direct_url": "https://careers.twosigma.com/careers/JobDetail/New-York-New-York-United-States-Quantitative-Researcher-Internship-2027-Summer/13945",
        "category": "quant",
        "eligibility": "confirmed",
        "notes": "Live posting confirmed 2026-09-12, $4,900/wk (bachelor's). Past the Aug 1-31 'golden window' for fastest review -- apply ASAP.",
    },
    {
        "company": "Hudson River Trading",
        "program": "Sophomore Internship - Summer 2026",
        "search_terms": ["Hudson River Trading sophomore internship"],
        "direct_url": "https://www.hudsonrivertrading.com/student-opportunities/",
        "category": "quant",
        "eligibility": "not_eligible_yet",
        "notes": "CONFIRMED 2026-09-12: the only live posting is 'Sophomore Internship - Summer 2026', which requires eligibility for full-time roles in 2028 -- that's next year's cohort, one grad-year ahead of Noah (2029). His equivalent (Summer 2027, for 2029 grads) has not been posted yet. Watch hudsonrivertrading.com/student-opportunities/ for it.",
    },
    {
        "company": "American Express",
        "program": "Campus Undergraduate Summer Internship Program - Sophomore Finance (2027)",
        "search_terms": ["American Express Sophomore Finance Program 2027"],
        "direct_url": "https://www.americanexpress.com/en-us/careers/student-programs/global-students-page.html",
        "category": "finance",
        "eligibility": "confirmed",
        "notes": "A '2027 Sophomore Finance' posting was found via an aggregator (interninsider.me) on 2026-09-12; verify current status on Amex's own student-programs page before applying, since older cycle postings with mismatched grad-date requirements also turned up in the same search.",
    },
    {
        "company": "Federal Reserve Bank of New York",
        "program": "Sophomore Career Exploration Program",
        "search_terms": ["New York Fed sophomore career exploration"],
        "direct_url": "https://www.newyorkfed.org/careers/student-programs-and-internships/sophomore-career-exploration-program",
        "category": "finance",
        "eligibility": "check_now",
        "notes": "CONFIRMED 2026-09-12: 2027 cycle info not yet posted (Summer 2026 cycle already closed). Check this URL now -- info expected this month.",
    },
    {
        "company": "Apple",
        "program": "Applied Data Solutions Program, Internships - Summer 2027",
        "search_terms": ["Apple Applied Data Solutions internship"],
        "direct_url": "https://jobs.apple.com/en-us/details/200673612-0157/applied-data-solutions-program-internships-summer-2027?team=STDNT",
        "category": "data_science",
        "eligibility": "confirmed",
        "notes": "Live posting confirmed 2026-09-12. No fixed deadline (rolling US pipeline posting) but strongest review window is Sept-Dec 2026 -- apply now.",
    },
    {
        "company": "BNY",
        "program": "2027 Summer Internship Program (Engineering - Data Science)",
        "search_terms": ["BNY summer internship data science"],
        "direct_url": "https://interninsider.me/internships/bny",
        "category": "data_science",
        "eligibility": "not_eligible",
        "notes": "CORRECTED 2026-09-12: the listed posting requires expected graduation Dec 2027-June 2028 -- that's the current-junior cohort, NOT sophomore-eligible, and does not fit Noah's 2029 grad date. Original research assumption was wrong. Deprioritize unless a separate sophomore-specific BNY posting turns up.",
    },
    {
        "company": "NVIDIA",
        "program": "NVIDIA Ignite",
        "search_terms": ["NVIDIA Ignite internship"],
        "direct_url": "https://www.nvidia.com/en-us/about-nvidia/careers/university-recruiting/",
        "category": "data_science",
        "eligibility": "time_sensitive",
        "notes": "CONFIRMED 2026-09-12: prior cohort's window was Oct 6-19, 2025 (named '2026 Ignite' despite opening a year early, same pattern as 'Summer 2027' cycles opening in 2026). Expect the next window (likely named '2027 Ignite') to open around October 2026 -- check the university recruiting page weekly starting now.",
    },
    {
        "company": "Google",
        "program": "Associate Software Developer Intern (ASDI)",
        "search_terms": ["Google ASDI internship"],
        "direct_url": "https://careers.google.com/jobs/results/?q=%22Associate%20Software%20Developer%20Intern%22",
        "category": "data_science",
        "eligibility": "time_sensitive",
        "notes": "CONFIRMED 2026-09-12: typically posts late September-early October and fills fast by location. Check this search link weekly starting now.",
    },
    # ---- 2026-09-13 pass: additional sophomore-eligible Summer 2027 targets ----
    {
        "company": "Microsoft",
        "program": "Explore Microsoft Internship (Summer 2027)",
        "search_terms": ["Microsoft Explore internship"],
        "direct_url": "https://careers.microsoft.com/v2/global/en/internship_eligibility",
        "category": "data_science",
        "eligibility": "confirmed",
        "notes": "Checked 2026-09-13 on Microsoft's official eligibility page: Explore is defined by CLASS YEAR, not grad year -- 'Candidate must [be a] 1st [or] 2nd year full time enrolled bachelor student (Freshmen/Sophomore) during [the] academic term immediately before [the] internship.' Noah (current sophomore) fits cleanly. 12-week rotational SWE/data program; must return to school 1 full term after. No fixed 2027 date posted yet -- WATCH the careers page; historically opens late summer/fall. Apply the moment the 2027 track appears (rolling review).",
    },
    {
        "company": "Google",
        "program": "STEP Internship (Student Training in Engineering Program) - Summer 2027",
        "search_terms": ["Google STEP internship"],
        "direct_url": "https://www.google.com/about/careers/applications/jobs/results/?q=STEP%20intern",
        "category": "data_science",
        "eligibility": "confirmed",
        "notes": "Checked 2026-09-13: STEP is explicitly and ONLY for 1st- and 2nd-year undergrads (juniors/seniors are ineligible and routed to the standard SWE internship) -- a dedicated sophomore program, so Noah fits. Requires ~2 CS courses completed by start for 2nd-years and programming experience (Java/Python/C/C++); prefers students returning for 2+ years after (true for a rising junior). Target submission window is late Sept-Oct 2026 for Summer 2027; earlier diversity/early-insight deadlines close 2-3 wks sooner. WATCH the careers page now and APPLY when the 2027 STEP req posts.",
    },
    {
        "company": "Citadel Securities",
        "program": "Discover Citadel Securities (US) - Spring 2027 event",
        "search_terms": ["Discover Citadel Securities"],
        "direct_url": "https://www.citadelsecurities.com/careers/programs-and-events/discover-citadel-securities/",
        "category": "quant",
        "eligibility": "confirmed",
        "notes": "Checked 2026-09-13 (multiple sources; official page 403'd to the fetch tool but Citadel's student T&Cs are consistent): open to 1st- or 2nd-year undergrads with expected graduation Dec 2027-June 2029 -- Noah (sophomore, 2029) fits the window. NOT gender-restricted (distinct from Citadel Securities Ignite, which is women-only, see separate entry). 2-day insight event, all expenses paid, feeds Quant Research / SWE / Trading pipelines. US event ~early April 2027; historical application deadline ~March 5, 2027. This is an early-insight on-ramp, not a paid summer internship. WATCH the page for the 2027 US application to open (~winter 2026-27).",
    },
    {
        "company": "Susquehanna International Group (SIG)",
        "program": "Discovery Day (Sophomore track) - quant early-insight",
        "search_terms": ["SIG Discovery Day sophomore"],
        "direct_url": "https://careers.sig.com/quantitative-trading-discovery-programs/jobs",
        "category": "quant",
        "eligibility": "needs_verification",
        "notes": "Checked 2026-09-13: SIG Discovery Day is a 2-day quant-finance early-exposure program explicitly for 1st- and 2nd-year students, with separate freshman and sophomore tracks (the sophomore track requires an interview). Not gender-restricted. This is the correct SIG on-ramp for Noah -- SIG's STANDARD Quant Trader Internship requires graduation by Aug 2028 (juniors), which Noah is NOT (see separate not_eligible entry). Exact 2027 sophomore-track dates weren't confirmable from the live board (fetch returned garbled content); VERIFY on careers.sig.com and watch for the current-sophomore posting.",
    },
    {
        "company": "Meta",
        "program": "Meta University (MU) - Analytics/Data Science track (Summer 2027)",
        "search_terms": ["Meta University internship analytics"],
        "direct_url": "https://www.metacareers.com/careerprograms/pathways/metauniversity",
        "category": "data_science",
        "eligibility": "needs_verification",
        "notes": "Checked 2026-09-13: Meta University is an 8-10 week PAID internship for 1st- and 2nd-year students (juniors ineligible -> standard SWE intern), with an Analytics track alongside Engineering and Product Design; high performers fast-track to the standard junior internship. Defined by class year, so Noah (sophomore) fits by standing -- but it is a diversity program focused on students from historically underrepresented backgrounds, so confirm Noah meets the intended-applicant criteria before applying. Apps typically open Nov-Jan for the following summer (rolling). WATCH and verify eligibility fit on the official page.",
    },
    {
        "company": "The D. E. Shaw Group",
        "program": "Discovery Fellowship (sophomore insight program) - January 2027",
        "search_terms": ["D.E. Shaw Discovery Fellowship sophomore"],
        "direct_url": "https://fellowships.deshaw.com/",
        "category": "quant",
        "eligibility": "confirmed",
        "notes": "Checked 2026-09-13: Discovery Fellowship is explicitly for current SECOND-YEAR (sophomore) undergrads, all academic disciplines, no finance experience needed, not gender-restricted (the sibling Latitude Fellowship is the diversity-forward one). 3-day insight program at NYC HQ, $1,500 stipend + travel/lodging covered; the on-ramp to D.E. Shaw for underclassmen (standard internships target rising seniors). For a 2029 grad, this is the fall-2026-apply / January-2027 cycle. Historical Jan-program deadline ~early-to-mid November; a separate May cohort for sophomores has closed ~mid-March in past years. WATCH fellowships.deshaw.com for the 2027 deadline and APPLY when open.",
    },
    {
        "company": "Point72 / Cubist Systematic Strategies",
        "program": "Quantitative Developer Internship - Summer 2027",
        "search_terms": ["Point72 Cubist quantitative developer intern 2027"],
        "direct_url": "https://careers.point72.com/",
        "category": "quant",
        "eligibility": "needs_verification",
        "notes": "Checked 2026-09-13: Live Summer 2027 Cubist Quant Developer intern posting exists (rolling review; open as of mid-2026). Requirement is 'undergraduate degree or higher in math/CS/quantitative field' with strong analytical + programming skills (Python + C++/Java) -- the posting does NOT state a graduation-year window and does NOT explicitly exclude sophomores, but it also does not confirm sophomore eligibility. VERIFY the exact grad-date requirement on the live req before applying (do NOT assume sophomore-eligible). Note: the Point72 Academy is a separate fundamental/equities analyst track, not this quant-dev role. Distinct from Optiver-style confirmed-sophomore roles.",
    },
    {
        "company": "Zelus Analytics",
        "program": "Data Scientist Intern - Sports Analytics (off-cycle / rolling)",
        "search_terms": ["Zelus Analytics data scientist intern sports"],
        "direct_url": "https://zelusanalytics.com/careers/",
        "category": "sports_analytics",
        "eligibility": "needs_verification",
        "notes": "Checked 2026-09-13: Zelus (sports-analytics DS firm; baseball/basketball/football/soccer/etc.) explicitly welcomes off-cycle resume submissions even when no specific cycle is open ('we welcome folks pursuing a career as a Data Scientist in sports analytics to submit their resume for future consideration'). No stated class-year bar found -- fit is skills/portfolio-driven (Python/R, large datasets, relational DBs), which suits Noah's ML/sports-analytics interest. No live Summer 2027 intern req confirmed; VERIFY current openings on their careers page and consider the general-interest submission. Good sports-analytics target that isn't a rising-senior-only league office.",
    },
    # ---- not_eligible / mismatched-cohort (documented like BNY, so they aren't re-researched) ----
    {
        "company": "IMC Trading",
        "program": "Quantitative Trader / Research Intern - Summer 2027",
        "search_terms": ["IMC Trading quantitative intern 2027"],
        "direct_url": "https://www.imc.com/us/careers/students",
        "category": "quant",
        "eligibility": "not_eligible",
        "notes": "Checked 2026-09-13: all IMC US Summer 2027 quant roles require graduation between Sept 2027 and July 2028 -- that's the current-JUNIOR cohort, one grad-year ahead of Noah (2029). No dedicated first-year/sophomore IMC program found. Deprioritize unless IMC posts a separate sophomore/early-insight track. Same mismatch pattern as BNY/HRT.",
    },
    {
        "company": "DRW",
        "program": "Quantitative Trading Analyst / Research / Developer Intern - Summer 2027",
        "search_terms": ["DRW quantitative intern 2027"],
        "direct_url": "https://drw.com/work-at-drw/interns",
        "category": "quant",
        "eligibility": "not_eligible",
        "notes": "Checked 2026-09-13: DRW's Summer 2027 quant intern postings require expected graduation Dec 2027-June/Aug 2028 (current juniors), NOT Noah's 2029 grad date. No dedicated sophomore/first-year track found. Deprioritize unless a sophomore-specific DRW posting appears. Same mismatch pattern as BNY/HRT.",
    },
    {
        "company": "Susquehanna International Group (SIG)",
        "program": "Quantitative Trader Internship - Summer 2027 (standard)",
        "search_terms": ["SIG quantitative trader internship 2027"],
        "direct_url": "https://careers.sig.com/intern-openings/",
        "category": "quant",
        "eligibility": "not_eligible",
        "notes": "Checked 2026-09-13: SIG's standard Quant Trader Internship (Bala Cynwyd + NYC) requires intent to graduate with a bachelor's by AUGUST 2028 -- current-junior cohort, not Noah (2029). For SIG, Noah's fit is the Discovery Day sophomore track instead (see separate entry above), NOT this standard internship. Listed here so it isn't re-researched as a sophomore target.",
    },
    {
        "company": "Citadel Securities",
        "program": "Ignite Women's Trading Program",
        "search_terms": ["Citadel Securities Ignite"],
        "direct_url": "https://www.citadelsecurities.com/careers/programs-and-events/ignite-womens-trading-program/",
        "category": "quant",
        "eligibility": "not_eligible",
        "notes": "Checked 2026-09-13: Ignite is a January program in NYC for SOPHOMORE WOMEN (grad Winter 2028 or Spring 2029), all expenses paid. The grad-year window fits Noah (2029) but the program is gender-restricted to women, so Noah is not eligible. For Citadel Securities, Noah's fit is Discover Citadel Securities (all genders, see separate entry). Documented so it isn't mistaken for a Noah-eligible sophomore program.",
    },
]

# Employer ATS boards to poll directly via free, public, no-login JSON APIs
# (Greenhouse / Lever / Ashby). This is the "no-login source" that fills the
# gap left by LinkedIn/Handshake needing authentication. Each token below was
# verified 2026-09-13 by hitting the live endpoint and confirming the board
# returns JSON with real internship postings (most had Summer-2027 roles). The
# tokens are the ATS slugs, NOT the company display name -- e.g. Akuna's
# Greenhouse token is "akunacapital", not "akuna". Re-verify each cycle; a firm
# can switch ATS or close its board between cycles.
#
# Focused on Noah's interests (quant/DS/ML/trading, sports analytics, big-tech
# data). Deliberately NOT investment-banking firms (FILTERS excludes IB terms).
# To add a company: find its board slug (careers page URL usually shows it) and
# hit the matching endpoint once to confirm it returns 200 + a "jobs"/list body.
#
# ATS endpoint shapes (confirmed live):
#   greenhouse -> https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
#   lever      -> https://api.lever.co/v0/postings/{token}?mode=json
#   ashby      -> https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true
ATS_TARGETS = [
    # --- Greenhouse (quant/trading firms with confirmed live Summer 2027 interns) ---
    {"ats": "greenhouse", "company": "Akuna Capital", "token": "akunacapital"},
    {"ats": "greenhouse", "company": "IMC Trading", "token": "imc"},
    {"ats": "greenhouse", "company": "Jump Trading", "token": "jumptrading"},
    {"ats": "greenhouse", "company": "Five Rings", "token": "fiveringsllc"},
    {"ats": "greenhouse", "company": "Point72", "token": "point72"},
    {"ats": "greenhouse", "company": "Virtu Financial", "token": "virtu"},
    {"ats": "greenhouse", "company": "PDT Partners", "token": "pdtpartners"},
    {"ats": "greenhouse", "company": "Old Mission Capital", "token": "oldmissioncapital"},
    {"ats": "greenhouse", "company": "Flow Traders", "token": "flowtraders"},
    {"ats": "greenhouse", "company": "Jane Street", "token": "janestreet"},
    # --- Greenhouse (data science / ML tech + sports analytics) ---
    {"ats": "greenhouse", "company": "Databricks", "token": "databricks"},
    {"ats": "greenhouse", "company": "Scale AI", "token": "scaleai"},
    {"ats": "greenhouse", "company": "Genius Sports", "token": "geniussports"},
    # --- Lever ---
    {"ats": "lever", "company": "Palantir", "token": "palantir"},
    # --- Ashby ---
    {"ats": "ashby", "company": "OpenAI", "token": "openai"},
    {"ats": "ashby", "company": "Ramp", "token": "ramp"},
]

# Free, structured, open-source internship aggregators (raw JSON on GitHub).
# These are the *Summer 2027* editions (matching Noah's target cycle) of the
# widely-maintained community lists. No auth, no cost. Both expose the same core
# fields (company_name, title, url, locations, active); Simplify adds a
# "category" tag that lets us keep only quant/DS/ML/data rows.
ATS_AGGREGATORS = [
    {
        "name": "SimplifyJobs/Summer2027",
        "url": "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
        # Tightened 2026-09-13: dropped generic "Software"/"Software Engineering"
        # (they were the bulk of a ~2,800-row flood and mostly off-target). Keep
        # only the data/quant categories that match Noah's interests; broad-but-
        # relevant coverage now comes from the relevance-gated coverage engine.
        "keep_categories": {
            "AI/ML/Data", "Quant",
            "Data Science, AI & Machine Learning",
        },
    },
    # vanshb03/Summer2027 disabled 2026-09-13: it has no category field, so it
    # was an uncategorized dump filtered only by title -- the main source of the
    # review-queue flood, and redundant with the relevance-gated coverage engine
    # (scraper/coverage.py). Re-enable if you want maximum raw breadth back.
    # {
    #     "name": "vanshb03/Summer2027",
    #     "url": "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json",
    #     "keep_categories": None,
    # },
]

# Filters applied to every result. "exclude_keywords" and "target_locations"
# used to live here (and, redundantly, in 3 other places -- see taxonomy.py's
# module docstring) but are RETIRED as of the filtering-system overhaul: the
# one canonical exclusion list is taxonomy.SENIORITY_PENALTY +
# taxonomy.IB_HARD_EXCLUDE (read by classify.hard_exclusion()), and the one
# canonical preferred-location set is taxonomy.TARGET_LOCATIONS (tunable via
# the Preferences UI). Nothing reads FILTERS["exclude_keywords"] or
# FILTERS["target_locations"] anymore -- grep-verified before removal.
FILTERS = {
    "min_internship_keywords": ["intern", "internship", "co-op"],
    "prioritize_year": "2027",
}

# Platforms

# Session storage paths (so you only login once)
SESSION_DIR = ROOT / ".sessions"
LINKEDIN_SESSION = SESSION_DIR / "linkedin.json"
HANDSHAKE_SESSION = SESSION_DIR / "handshake.json"

# Handshake sits behind Cloudflare's bot challenge, which blocks a
# Playwright-LAUNCHED browser at a "verifying you are human" interstitial before
# login is even possible (confirmed 2026-09-14: cf_challenge page, Ray ID). The
# workaround is to launch a normal Chrome ourselves with a remote-debugging port
# and *attach* to it over CDP -- a self-launched Chrome clears Cloudflare where a
# Playwright-launched one does not. The debug profile below persists the
# WashU-SSO session across runs, so --login stays a one-time step.
CHROME_CDP_PORT = 9222
CHROME_DEBUG_PROFILE = SESSION_DIR / "chrome_debug_profile"

# Claude model for generation. "claude-sonnet-5" was not a real model ID (would
# 404 on the first cover-letter generation); claude-sonnet-4-6 is the current
# Sonnet-tier model and the right cost/quality fit for per-application letters.
CLAUDE_MODEL = "claude-sonnet-5-5"


# A posting no scrape has re-found for this many days is marked "possibly closed"
# in the review UI and sorted below fresh ones (never hidden or deleted).
STALE_DAYS = 21

# One-line-per-run log written by `main.py --unattended` (gitignored via .sessions/);
# the review sidebar reads its last summary line.
SCRAPE_LOG_PATH = Path(os.environ.get("APPLICATOR_SCRAPE_LOG") or SESSION_DIR / "scrape_log.txt")

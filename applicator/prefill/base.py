"""
Shared types for the prefill package: FillResult, the Filler base class, and
login-wall / CAPTCHA detection. Nothing here ever clicks submit.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

STATUSES = ("filled", "partial", "needs_login", "blocked", "unsupported", "error", "closed")

# Label recorded in FillResult.unfilled when a cover-letter field exists but no
# letter could be produced (generation failed / template or API error).
COVER_LETTER_FAILED = "Cover letter — generation failed, write it yourself"


def call_cover_letter(get_cover_letter) -> str:
    """
    desc: Invoke a lazy cover-letter provider. Fillers call this ONLY once they
        have found a cover-letter field (text or file upload), so pages with no
        such field never trigger letter generation. Never raises.
    args:
        get_cover_letter (Callable[[], str] | str | None): the provider the
            runner passed in (a plain string is tolerated and returned as-is).
    output (str):
        the letter text, or '' if there is none or the provider failed.
    """
    if not get_cover_letter:
        return ""
    if not callable(get_cover_letter):
        return str(get_cover_letter)
    try:
        return (get_cover_letter() or "").strip()
    except Exception:
        return ""


@dataclass
class FillResult:
    """
    desc: Outcome of pre-filling one application tab. filled/unfilled hold
        field LABELS only -- never the values typed into them.
    args:
        status (str): one of STATUSES.
        filled (list[str]): labels of fields that were filled.
        unfilled (list[str]): labels the human still needs to answer.
        notes (str): short free-text note (must not contain profile values).
    output:
        FillResult.
    """
    status: str = "unsupported"
    filled: list = field(default_factory=list)
    unfilled: list = field(default_factory=list)
    notes: str = ""


def host_of(url: str) -> str:
    """
    desc: Lower-cased hostname of a URL ('' if unparseable).
    args:
        url (str): any URL.
    output:
        str.
    """
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def host_matches(url: str, *suffixes: str) -> bool:
    """
    desc: True if the URL's host equals or is a subdomain of any suffix.
    args:
        url (str): page URL.
        *suffixes (str): domains like 'greenhouse.io'.
    output:
        bool.
    """
    host = host_of(url)
    return any(host == s or host.endswith("." + s) for s in suffixes)


class Filler:
    """
    desc: Base class for an ATS-specific (or generic) form filler. Subclasses
        set `name`, override matches() and fill(). A module in
        applicator/prefill/ should expose a module-level `FILLER` instance.
        Fillers must never click submit; use safe.safe_click for the few
        non-submitting clicks they need.
    """
    name: str = "base"
    handles_login: bool = False  # True if the filler copes with login/account pages itself

    def matches(self, url: str) -> bool:
        """
        desc: Whether this filler handles the given application URL.
        args:
            url (str): the application URL.
        output:
            bool.
        """
        return False

    def fill(self, page, profile: dict, get_cover_letter, resume_path: Path) -> FillResult:
        """
        desc: Fill the (already navigated) page without submitting.
        args:
            page (playwright.sync_api.Page): page at the application URL.
            profile (dict): flat profile from runner.build_profile.
            get_cover_letter (Callable[[], str]): lazy provider; call it (via
                call_cover_letter) only if the form has a cover-letter field.
            resume_path (Path): resume PDF path.
        output:
            FillResult.
        """
        raise NotImplementedError


_CHALLENGE_RE = re.compile(
    r"just a moment|verify(ing)? (that )?you are (a )?human|are you a robot|"
    r"checking your browser|attention required|unusual traffic|press (and|&) hold|"
    r"access denied|verify you are not a robot",
    re.I,
)
_LOGIN_URL_RE = re.compile(r"/(log-?in|sign-?in|sso|auth|authwall|uas/login)(/|$|\?|\.)", re.I)

_PROBE_JS = """
() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const inputs = [...document.querySelectorAll('input, select, textarea')].filter(el => {
    const t = (el.type || '').toLowerCase();
    return !['hidden','submit','button','image','reset','checkbox','radio'].includes(t) && vis(el); });
  const pw = inputs.filter(el => el.type === 'password').length;
  const files = document.querySelectorAll('input[type=file]').length;
  const bigCaptcha = [...document.querySelectorAll('iframe')].some(f =>
    /hcaptcha\\.com\\/captcha|recaptcha\\/api2\\/bframe|challenges\\.cloudflare\\.com\\/cdn-cgi\\/challenge/i.test(f.src || '')
    && f.getBoundingClientRect().height > 150);
  return { pw, others: inputs.length - pw, files, bigCaptcha,
           head: (document.body ? document.body.innerText : '').slice(0, 400) };
}
"""


def detect_blocker(page) -> tuple | None:
    """
    desc: Detect a login wall or CAPTCHA/bot challenge on the current page.
        Never tries to bypass either.
    args:
        page (playwright.sync_api.Page): page to inspect.
    output:
        tuple[str, str] | None: (status, note) with status 'needs_login' or
        'blocked', or None if the page looks usable.
    """
    try:
        title = page.title() or ""
        probe = page.evaluate(_PROBE_JS)
    except Exception:
        return None
    if probe["bigCaptcha"] or _CHALLENGE_RE.search(title + " " + probe["head"][:300]):
        return ("blocked", "CAPTCHA / bot challenge present; solve it manually.")
    if probe["pw"] and probe["others"] <= 3:
        return ("needs_login", "Login wall; sign in manually, then use Fill Out again.")
    if _LOGIN_URL_RE.search(page.url) and probe["others"] + probe["pw"] <= 3 and not probe["files"]:
        return ("needs_login", "Redirected to a sign-in page; sign in manually.")
    return None


# Wording job sites show on a posting that is gone. Matched against the page
# title and the top of the visible text only (a live posting could mention
# these phrases deep in its body, e.g. an FAQ).
_CLOSED_RE = re.compile(
    r"no longer accepting (?:applications|applicants)"
    r"|(?:position|role|job|posting|opportunity) (?:has been|is) (?:filled|closed|removed)"
    r"|(?:position|role|job|posting) (?:is )?no longer (?:available|open)"
    r"|this (?:job|position|posting|role) (?:has )?(?:expired|closed)"
    r"|job (?:was )?not found|job not found"
    r"|we couldn.t find (?:that|this) (?:job|position|page)"
    r"|applications? (?:for this (?:job|position|role) )?(?:are|is) closed",
    re.I,
)
_CLOSED_HEAD_CHARS = 1500
_CLOSED_NOTE = "Posting appears closed; nothing was filled."


def closed_reason(text: str, http_status: int | None = None) -> str:
    """
    desc: Pure check for a closed/removed posting from an HTTP status and the
        page's title + top-of-page text. No page access, so it is testable
        on HTML fixtures.
    args:
        text (str): page title plus visible text (only the first
            _CLOSED_HEAD_CHARS characters are inspected).
        http_status (int | None): main-document HTTP status, if known.
    output (str):
        short reason ('HTTP 404', 'closed-posting text') or '' if it looks open.
    """
    if http_status in (404, 410):
        return f"HTTP {http_status}"
    if _CLOSED_RE.search((text or "")[:_CLOSED_HEAD_CHARS]):
        return "closed-posting text"
    return ""


def detect_closed(page, http_status: int | None = None) -> tuple | None:
    """
    desc: Detect a closed/removed posting. Read-only: only reads the title and
        visible text (no clicks, no typing). Pages that show a file-upload
        input are live application forms and are never reported closed on
        text alone (an HTTP 404/410 still counts).
    args:
        page (playwright.sync_api.Page): page to inspect.
        http_status (int | None): HTTP status of the navigation, if known.
    output:
        tuple[str, str] | None: ('closed', note), or None if it looks open.
    """
    if http_status in (404, 410):
        return ("closed", f"{_CLOSED_NOTE} ({closed_reason('', http_status)})")
    try:
        title = page.title() or ""
        probe = page.evaluate(
            "() => ({head: ((document.body && document.body.innerText) || '')"
            f".slice(0, {_CLOSED_HEAD_CHARS}), files: document.querySelectorAll('input[type=file]').length}})"
        )
    except Exception:
        return None
    if probe.get("files"):
        return None
    why = closed_reason(title + " " + probe.get("head", ""))
    return ("closed", f"{_CLOSED_NOTE} ({why})") if why else None

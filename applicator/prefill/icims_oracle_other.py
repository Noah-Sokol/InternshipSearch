"""
Prefill for iCIMS (*.icims.com), Oracle Cloud HCM Candidate Experience
(*.oraclecloud.com), Goldman Sachs Higher (higher.gs.com, which hands off to
an Oracle CX site) and ByteDance / TikTok career sites. Each family only adds
a host-specific way to reach the form (a GET navigation to the apply URL read
from the page, or built from the posting URL) plus a few known first-step
quirks; the actual filling is generic.fill_frames. Never clicks submit,
'Next' or any sign-in button: those steps are left for Noah.

DOM notes (verified 2026-10 against public pages via plain GET, no profile
data sent):
  - iCIMS posting: a.iCIMS_ApplyOnlineButton[href*="mode=apply"]; desktop
    pages wrap the content in iframe#icims_content_iframe (in_iframe=1).
    The apply link leads to form#enterEmailForm.iCIMS_SignIn with
    input#email[name=css_loginName] and input#enterEmailSubmitButton
    (type=submit 'Next', invisible hCaptcha). Everything after that needs
    an iCIMS account, so it was NOT inspected; it is filled generically.
  - Oracle CX: the posting is an SPA at .../sites/<site>/job/<id>; GS links
    straight to .../sites/<site>/job/<id>/apply/email, so the same suffix is
    used for other Oracle sites. The email step's fields render client-side
    and were NOT inspected; the email input / terms checkbox are found by
    type/label, not by Oracle-specific ids. Oracle then emails a one-time
    code, which only the human can enter.
  - ByteDance/TikTok: posting has a[href*="/resume/<id>/apply"]
    (jobs.bytedance.com/en/resume/<id>/apply, careers.tiktok.com ->
    lifeattiktok.com/resume/<id>/apply). The form is a client-rendered SPA
    behind a ByteDance account login and was NOT inspected.
"""

import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from applicator.prefill.base import Filler, FillResult, host_matches, host_of
from applicator.prefill.generic import fill_frames, scan_frame

_BYTEDANCE_HOSTS = ("jobs.bytedance.com", "joinbytedance.com", "lifeattiktok.com", "careers.tiktok.com")
_ICIMS_NON_ATS = ("icims.com", "www.icims.com")  # iCIMS's own marketing site

_ORACLE_JOB_RE = re.compile(r"^(?P<base>.*/CandidateExperience/[^/]+/sites/[^/]+/job/\d+)(?P<rest>/.*)?$", re.I)
_BD_DETAIL_RE = re.compile(r"/position/(?P<id>\d+)(/detail)?/?$", re.I)
_TT_SEARCH_RE = re.compile(r"/search/(?P<id>\d+)/?$", re.I)
_BD_LOGIN_TEXT_RE = re.compile(r"\b(log ?in|sign ?in)\b.{0,40}\b(to apply|to continue|account)\b|"
                               r"verification code|send code|get code", re.I)

_ICIMS_APPLY_JS = r"""
() => {
  const a = document.querySelector('a.iCIMS_ApplyOnlineButton[href], a[href*="mode=apply"]');
  return a ? a.href : '';
}
"""
_ORACLE_LINK_JS = r"""
() => {
  const as = [...document.querySelectorAll('a[href*="oraclecloud.com"]')];
  const a = as.find(x => /\/apply\b/i.test(x.href)) || as.find(x => /\/job\/\d+/i.test(x.href));
  return a ? a.href : '';
}
"""
_BD_APPLY_JS = r"""
() => {
  const a = [...document.querySelectorAll('a[href]')].find(x => /\/resume\/\d+\/apply\b/i.test(x.href));
  return a ? a.href : '';
}
"""
_PAGE_PROBE_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const ins = [...document.querySelectorAll('input, textarea, select')].filter(el => {
    const t = (el.type || '').toLowerCase();
    return !['hidden','submit','button','image','reset'].includes(t) && (t === 'file' || vis(el)); });
  const blob = el => [el.getAttribute('placeholder'), el.getAttribute('aria-label'), el.name, el.id,
                      ...(el.labels ? [...el.labels].map(l => l.innerText) : [])].join(' ');
  return {
    n: ins.filter(el => el.type !== 'file' && el.type !== 'checkbox' && el.type !== 'radio').length,
    files: ins.filter(el => el.type === 'file').length,
    email: ins.filter(el => el.type === 'email' || /e-?mail/i.test(blob(el))).length,
    code: ins.filter(el => el.type === 'password' || /verification|one.time|\bcode\b|\botp\b|passcode/i.test(blob(el))).length,
    boxes: ins.filter(el => el.type === 'checkbox' && !el.checked).length,
    text: (document.body ? document.body.innerText : '').slice(0, 1500),
  };
}
"""


def family_of(url: str) -> str | None:
    """
    desc: Which ATS family (if any) of this module handles a URL.
    args:
        url (str): application or posting URL.
    output:
        str | None: 'icims', 'oracle', 'gs', 'bytedance', or None.
    """
    host = host_of(url)
    if host_matches(url, "icims.com") and host not in _ICIMS_NON_ATS:
        return "icims"
    if host_matches(url, "oraclecloud.com"):
        return "oracle"
    if host == "higher.gs.com":
        return "gs"
    if host_matches(url, *_BYTEDANCE_HOSTS):
        return "bytedance"
    return None


def oracle_apply_url(url: str) -> str | None:
    """
    desc: Oracle CX apply-flow URL (.../job/<id>/apply/email) for a posting
        URL, or None if the URL is already inside the apply flow / unknown.
    args:
        url (str): Oracle CX URL.
    output:
        str | None.
    """
    p = urlparse(url)
    m = _ORACLE_JOB_RE.match(p.path)
    if not m or (m.group("rest") or "").lower().startswith("/apply"):
        return None
    return f"{p.scheme}://{p.netloc}{m.group('base')}/apply/email"


def bytedance_apply_url(url: str) -> str | None:
    """
    desc: ByteDance/TikTok resume-apply URL built from a posting URL
        (jobs.bytedance.com/<lang>/position/<id>/detail ->
        .../<lang>/resume/<id>/apply; lifeattiktok.com/search/<id> ->
        lifeattiktok.com/resume/<id>/apply), or None.
    args:
        url (str): posting URL.
    output:
        str | None.
    """
    p = urlparse(url)
    m = _BD_DETAIL_RE.search(p.path)
    if m:
        prefix = p.path[:m.start()]
        return f"{p.scheme}://{p.netloc}{prefix}/resume/{m.group('id')}/apply"
    m = _TT_SEARCH_RE.search(p.path)
    if m:
        return f"{p.scheme}://{p.netloc}/resume/{m.group('id')}/apply"
    return None


def _goto(target, url: str) -> bool:
    """
    desc: GET-navigate a page or frame (never a form submission).
    args:
        target (Page | Frame): what to navigate.
        url (str): http(s) URL.
    output:
        bool: True on success.
    """
    if not re.match(r"^https?://", url or "", re.I):
        return False
    try:
        target.goto(url, wait_until="domcontentloaded", timeout=30000)
        return True
    except Exception:
        return False


def _eval(target, js: str, default=None):
    try:
        return target.evaluate(js)
    except Exception:
        return default


def _wait_fields(page, seconds: float) -> None:
    """
    desc: Wait (bounded) for a visible field in any frame; SPAs render late.
    args:
        page (Page): page.
        seconds (float): max wait.
    output:
        None.
    """
    end = time.time() + seconds
    while time.time() < end:
        for fr in page.frames:
            try:
                if fr.locator("input:visible, textarea:visible, select:visible").count():
                    time.sleep(0.4)
                    return
            except Exception:
                continue
        time.sleep(0.3)


def _field_count(page) -> int:
    return sum(len(scan_frame(fr, f"q{i}_")) for i, fr in enumerate(page.frames))


def _result(acc, total: int, notes: list, extra_unfilled: list | None = None,
            expect_resume: bool = True) -> FillResult:
    """
    desc: Turn a fill accumulator into a FillResult (labels only).
    args:
        acc (_Acc): accumulator from fill_frames.
        total (int): fillable field count.
        notes (list[str]): note fragments.
        extra_unfilled (list[str] | None): extra items for the human.
        expect_resume (bool): report a missing resume upload field.
    output:
        FillResult.
    """
    for u in extra_unfilled or []:
        acc.miss(u)
    note = " ".join(n for n in notes if n).strip()
    if not acc.filled and total == 0 and acc.file_inputs == 0 and not acc.unfilled:
        return FillResult("unsupported", [], [], (note + " No fillable form fields found; fill manually.").strip())
    if expect_resume and acc.file_inputs == 0 and not acc.resume_done:
        acc.miss("Resume (no upload field found)")
    status = "filled" if acc.filled and not acc.unfilled else "partial"
    return FillResult(status, list(acc.filled), list(acc.unfilled), note)


class IcimsOracleOtherFiller(Filler):
    """iCIMS, Oracle Cloud HCM, Goldman Sachs Higher and ByteDance/TikTok."""
    name = "icims_oracle_other"

    def matches(self, url: str) -> bool:
        return family_of(url) is not None

    def fill(self, page, profile: dict, get_cover_letter, resume_path: Path) -> FillResult:
        """
        desc: Reach the application form for the page's ATS family and fill
            it without submitting; leaves sign-in / 'Next' / Submit to Noah.
        args:
            page (Page): tab at the posting or application URL.
            profile (dict): flat profile.
            get_cover_letter (Callable[[], str]): lazy cover-letter provider (see base.call_cover_letter).
            resume_path (Path): resume file.
        output:
            FillResult.
        """
        fam = family_of(page.url)
        if fam == "gs":
            return self._fill_gs(page, profile, get_cover_letter, resume_path)
        if fam == "oracle":
            return self._fill_oracle(page, profile, get_cover_letter, resume_path, [])
        if fam == "icims":
            return self._fill_icims(page, profile, get_cover_letter, resume_path)
        if fam == "bytedance":
            return self._fill_bytedance(page, profile, get_cover_letter, resume_path)
        return FillResult("unsupported", [], [], "URL not handled by icims_oracle_other.")

    # ------------------------------------------------------------------ iCIMS
    def _fill_icims(self, page, profile, get_cover_letter, resume_path) -> FillResult:
        _wait_fields(page, 4.0)
        notes = []
        gate = self._icims_gate(page)
        if gate is None and _field_count(page) < 2:
            for fr in page.frames:
                href = _eval(fr, _ICIMS_APPLY_JS, "")
                if href and _goto(fr, href):
                    notes.append("Opened the iCIMS apply page.")
                    break
            _wait_fields(page, 6.0)
            gate = self._icims_gate(page)
        if gate is not None:
            return self._icims_email_gate(gate, profile, notes)
        acc, total = fill_frames(page, profile, get_cover_letter, resume_path)
        notes.append("iCIMS: review each step; click Next/Submit yourself.")
        return _result(acc, total, notes)

    @staticmethod
    def _icims_gate(page):
        for fr in page.frames:
            try:
                if fr.locator("form#enterEmailForm, form.iCIMS_SignIn").count():
                    return fr
            except Exception:
                continue
        return None

    @staticmethod
    def _icims_email_gate(frame, profile, notes) -> FillResult:
        filled, unfilled = [], []
        email = profile.get("email")
        box = frame.locator("form#enterEmailForm input#email, form.iCIMS_SignIn input[type=email]").first
        try:
            if not email:
                unfilled.append("Email")
            elif (box.input_value(timeout=2000) or "").strip():
                filled.append("Email (already entered)")
            else:
                box.fill(str(email), timeout=3000)
                filled.append("Email")
        except Exception:
            unfilled.append("Email")
        unfilled.append("iCIMS sign-in: click Next yourself (hCaptcha may appear), then sign in or "
                        "create an account")
        notes.append("iCIMS email step only; the full form appears after sign-in. Use Fill Out "
                     "again once the profile form is showing.")
        return FillResult("partial", filled, unfilled, " ".join(notes))

    # ----------------------------------------------------------------- Oracle
    def _fill_gs(self, page, profile, get_cover_letter, resume_path) -> FillResult:
        _wait_fields(page, 2.0)
        href = _eval(page, _ORACLE_LINK_JS, "")
        if href and _goto(page, href):
            return self._fill_oracle(page, profile, get_cover_letter, resume_path,
                                     ["Opened the GS application (Oracle Candidate Experience)."])
        return FillResult("unsupported", [], [],
                          "GS Higher: no Oracle apply link found on the role page; open Apply manually.")

    def _fill_oracle(self, page, profile, get_cover_letter, resume_path, notes) -> FillResult:
        target = oracle_apply_url(page.url)
        if target and _goto(page, target):
            notes.append("Opened the Oracle apply flow.")
        _wait_fields(page, 15.0)  # Oracle CX renders client-side and is slow
        probe = _eval(page, _PAGE_PROBE_JS, None) or {}
        if probe.get("code") and probe.get("n", 0) <= 3:  # not 'postal code' on the big form
            return FillResult("needs_login", [], ["Oracle verification code"],
                              " ".join(notes + ["Oracle sent a one-time code; enter it yourself, "
                                                "then use Fill Out again."]))
        email_step = bool(probe.get("email")) and probe.get("n", 0) <= 2 and not probe.get("files")
        acc, total = fill_frames(page, profile, get_cover_letter, resume_path)
        if email_step:
            extra = []
            if probe.get("boxes"):
                extra.append("Oracle terms / privacy agreement checkbox (tick it yourself)")
            extra.append("Oracle: click Next yourself; it may email you a verification code")
            notes.append("Oracle email step only; use Fill Out again once the full form is showing.")
            return _result(acc, total, notes, extra, expect_resume=False)
        notes.append("Oracle form: drop-downs and later steps (Next) are left for you.")
        return _result(acc, total, notes)

    # ------------------------------------------------------- ByteDance/TikTok
    def _fill_bytedance(self, page, profile, get_cover_letter, resume_path) -> FillResult:
        notes = []
        if "/resume/" not in urlparse(page.url).path:
            href = _eval(page, _BD_APPLY_JS, "") or bytedance_apply_url(page.url)
            if href:
                href = urljoin(page.url, href)
            if href and _goto(page, href):
                notes.append("Opened the ByteDance/TikTok apply page.")
        _wait_fields(page, 12.0)
        probe = _eval(page, _PAGE_PROBE_JS, None) or {}
        path = urlparse(page.url).path.lower()
        small = probe.get("n", 0) <= 3 and not probe.get("files")  # a login modal, not the form
        login_like = "/login" in path or (small and (
            probe.get("code") or _BD_LOGIN_TEXT_RE.search(probe.get("text", ""))))
        if login_like:
            return FillResult("needs_login", [], [],
                              " ".join(notes + ["ByteDance/TikTok login required; sign in yourself, "
                                                "then use Fill Out again."]))
        acc, total = fill_frames(page, profile, get_cover_letter, resume_path)
        notes.append("ByteDance/TikTok: custom drop-downs (country code, school, dates) are left for you.")
        return _result(acc, total, notes)


FILLER = IcimsOracleOtherFiller()

"""
SmartRecruiters (jobs.smartrecruiters.com) and Workable (apply.workable.com)
fillers. Fill only; never submit, and never advance past a step.

DOM notes (from public apply pages, 2026-10, read-only):

SmartRecruiters "oneclick-ui" form (Angular + 'spl-*' web components, OPEN
shadow DOM, so the generic light-DOM scan sees nothing; Playwright CSS
locators pierce open shadow roots):
  - the posting page has no form. Its "I'm interested" link (a.js-oneclick) has
    no href. The form is at window.ONECLICKDATA.url, filled in with
    (cident, puuid, cident).
  - spl-input hosts carry label="..." and the inner <input> reuses the id:
    #first-name-input, #last-name-input, #email-input, #confirm-email-input,
    #linkedin-input, #facebook-input, #twitter-input, #website-input
  - spl-phone-field > spl-input > input[type=tel] (country-code spl-select defaults to US)
  - spl-autocomplete[data-test=location-autocomplete] "City": needs a suggestion picked
  - spl-dropzone[data-test=resume-upload] input[type=file] (the top
    spl-dropzone[data-test=apply-with-resume-container] is "apply with
    resume" parsing and is left alone)
  - spl-textarea#hiring-manager-message-input: message to the hiring manager
  - "Next" spl-button goes to screening questions; it is never clicked.
  - DataDome bot wall (iframe from captcha-delivery.com) in headless browsers.

Workable apply page (React, light DOM), field ids from the public
/api/v1/jobs/<shortcode>/form endpoint:
  - inputs [data-ui=firstname|lastname|email|headline|address],
    input#input_phone (intl-tel-input), textarea [data-ui=cover_letter|summary]
  - input[type=file][data-ui=resume] and [data-ui=avatar] (photo: never touched)
  - address is pre-filled from IP geolocation; hidden aria-hidden city/postcode/country inputs
  - custom dropdowns: div[data-ui=<id>][data-input-type=select] > input[role=combobox][readonly]
    with listbox id from aria-controls
  - radios: fieldset[role=radiogroup][data-ui=<id>] > label > div[role=radio] > input[type=radio][aria-hidden]
  - consent: [data-ui=gdpr] checkbox. Never ticked.
  - button[data-ui=apply-button][type=submit] "Submit application". Never clicked.
"""

import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from applicator.prefill.base import (
    Filler, FillResult, COVER_LETTER_FAILED, call_cover_letter, host_matches, host_of,
)
from applicator.prefill.generic import _Acc, _norm, _yesno, classify_field, fill_frame, pick_option
from applicator.prefill.safe import safe_click

_DATADOME_JS = r"""
() => [...document.querySelectorAll('iframe')].some(f => /captcha-delivery\.com|geo\.captcha/i.test(f.src || ''))
"""


def _datadome(page) -> bool:
    """
    desc: Whether a DataDome bot wall is showing (SmartRecruiters uses it).
    args:
        page (Page): page to inspect.
    output:
        bool.
    """
    try:
        return bool(page.evaluate(_DATADOME_JS))
    except Exception:
        return False


def _first(page, selector: str, timeout: int = 0):
    """
    desc: First matching locator, or None. Optionally waits for it to attach.
    args:
        page (Page | Frame): where to look (CSS pierces open shadow DOM).
        selector (str): CSS selector.
        timeout (int): ms to wait for attachment (0 = no wait).
    output:
        Locator | None.
    """
    loc = page.locator(selector).first
    try:
        if timeout:
            loc.wait_for(state="attached", timeout=timeout)
        return loc if loc.count() else None
    except Exception:
        return None


def _fill_if_empty(loc, value: str) -> bool:
    """
    desc: Type a value into an empty text field (never overwrites existing text).
    args:
        loc (Locator): input/textarea.
        value (str): value to type.
    output:
        bool: True if typed.
    """
    if (loc.input_value(timeout=3000) or "").strip():
        return False
    loc.fill(str(value), timeout=4000)
    try:
        loc.dispatch_event("change")
        loc.dispatch_event("blur")
    except Exception:
        pass
    return True


def _upload_resume(loc, resume_path: Path, acc: _Acc) -> None:
    """
    desc: Attach the resume to a file input, recording the result label.
    args:
        loc (Locator | None): file input.
        resume_path (Path): resume file.
        acc (_Acc): accumulator.
    output:
        None.
    """
    if loc is None:
        acc.miss("Resume (no upload field found)")
        return
    if not Path(resume_path).exists():
        acc.miss("Resume (file missing)")
        return
    try:
        loc.set_input_files(str(resume_path), timeout=6000)
        acc.resume_done = True
        acc.ok("Resume (file upload)")
    except Exception:
        acc.miss("Resume (upload failed; attach manually)")


def _pick_place(options: list, profile: dict) -> str | None:
    """
    desc: Choose a location suggestion only when it unambiguously matches the
        profile city (and its state/region token, if one is given).
    args:
        options (list[str]): suggestion texts.
        profile (dict): flat profile (city, location).
    output:
        str | None: the suggestion text, or None if not confident.
    """
    city = _norm(profile.get("city") or (profile.get("location") or "").split(",")[0])
    if not city:
        return None
    cands = [o for o in options if _norm(o).startswith(city)]
    parts = [p.strip() for p in (profile.get("location") or "").split(",")[1:] if p.strip()]
    if len(cands) > 1 and parts:
        tok = _norm(parts[0])
        narrowed = [o for o in cands if re.search(r"\b" + re.escape(tok) + r"\b", _norm(o))]
        cands = narrowed or cands
    return cands[0] if len(cands) == 1 else None


# ============================================================ SmartRecruiters

# (css selector inside the oneclick form, profile key, report label)
_SR_TEXT = [
    ("input#first-name-input", "first_name", "First name"),
    ("input#last-name-input", "last_name", "Last name"),
    ("input#email-input", "email", "Email"),
    ("input#confirm-email-input", "email", "Confirm your email"),
    ("spl-phone-field input[type=tel]", "phone", "Phone number"),
    ("input#linkedin-input", "linkedin", "LinkedIn"),
    ("input#website-input", "website", "Website"),
]
_SR_FORM_MARK = "input#first-name-input"
_SR_RESUME = 'spl-dropzone[data-test="resume-upload"] input[type=file]'
_SR_MESSAGE = "textarea#hiring-manager-message-input"
_SR_CITY = 'spl-autocomplete[data-test="location-autocomplete"] input'
_SR_CITY_OPTS = ('[data-sr-id="location-autocomplete-search-menu"] [role=option], '
                 '[data-sr-id="location-autocomplete-search-menu"] spl-select-option')

_SR_ONECLICK_JS = r"""
() => {
  const d = window.ONECLICKDATA;
  if (!d || !d.url || !d.cident || !d.puuid) return '';
  let i = 0; const args = [d.cident, d.puuid, d.cident];
  return d.url.replace(/%s/g, () => encodeURIComponent(args[i++] || ''));
}
"""

# Required, still-empty spl-* fields anywhere (pierces open shadow roots).
# Returns host labels only.
_SR_LEFTOVER_JS = r"""
() => {
  const out = new Set();
  const inner = h => {
    const stack = [h.shadowRoot || h];
    while (stack.length) {
      const r = stack.pop();
      const el = r.querySelector && r.querySelector('input:not([type=file]):not([type=hidden]), textarea');
      if (el) return el;
      if (r.querySelectorAll) r.querySelectorAll('*').forEach(c => { if (c.shadowRoot) stack.push(c.shadowRoot); });
    }
    return null;
  };
  const walk = root => {
    for (const el of root.querySelectorAll('*')) {
      const t = el.tagName.toLowerCase();
      if (t.startsWith('spl-') && el.hasAttribute('required') && t !== 'spl-dropzone') {
        const lab = (el.getAttribute('label') || el.getAttribute('splarialabel') || '').trim();
        let empty;
        if (t === 'spl-checkbox' || t === 'spl-radio-group' || t === 'spl-checkbox-group') {
          empty = !el.hasAttribute('checked') && !(el.getAttribute('value') || '').trim();
        } else if (t === 'spl-select' || t === 'spl-multiselect') {
          empty = !(el.getAttribute('value') || '').trim();
        } else {
          const i = inner(el); empty = i ? !(i.value || '').trim() : false;
        }
        if (empty && lab) out.add(lab.slice(0, 80));
      }
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);
  return [...out];
}
"""


def _sr_open_form(page) -> str:
    """
    desc: From a SmartRecruiters posting page, go to its oneclick-ui
        application form (rebuilt from window.ONECLICKDATA; otherwise
        safe_click the "I'm interested" link). No-op if already on the form.
    args:
        page (Page): tab at a SmartRecruiters URL.
    output:
        str: a short note ('' if nothing was needed).
    """
    if "/oneclick-ui/" in page.url or _first(page, _SR_FORM_MARK):
        return ""
    try:
        target = page.evaluate(_SR_ONECLICK_JS)
    except Exception:
        target = ""
    if target and host_matches(target, "smartrecruiters.com", "smartr.me"):
        page.goto(target, wait_until="domcontentloaded", timeout=30000)
        return "Opened the SmartRecruiters application form."
    link = _first(page, "a.js-oneclick:visible")
    if link is not None:
        safe_click(link, "open application form (I'm interested)")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        return "Opened the SmartRecruiters application form via its apply link."
    return ""


def _sr_city(page, profile: dict, acc: _Acc) -> None:
    """
    desc: Type the city into the location autocomplete and pick a suggestion
        only if exactly one matches the profile; otherwise clear it and leave
        it for the human.
    args:
        page (Page): oneclick form page.
        profile (dict): flat profile.
        acc (_Acc): accumulator.
    output:
        None.
    """
    loc = _first(page, _SR_CITY)
    if loc is None:
        return
    label = "City"
    try:
        if (loc.input_value(timeout=2000) or "").strip():
            return
        query = profile.get("city") or (profile.get("location") or "").split(",")[0].strip()
        if not query:
            acc.miss(label)
            return
        safe_click(loc, "focus city field")
        loc.press_sequentially(query, delay=60, timeout=8000)
        opts = page.locator(_SR_CITY_OPTS)
        try:
            opts.first.wait_for(state="visible", timeout=6000)
        except Exception:
            pass
        texts = [t.strip() for t in opts.all_inner_texts()]
        choice = _pick_place(texts, profile)
        if choice is None:
            loc.fill("", timeout=2000)
            acc.miss(label + " (pick from suggestions)")
            return
        safe_click(opts.nth(texts.index(choice)), "choose location suggestion")
        acc.ok(label)
    except Exception:
        try:
            loc.fill("", timeout=2000)
        except Exception:
            pass
        acc.miss(label + " (pick from suggestions)")


def fill_smartrecruiters(page, profile: dict, get_cover_letter, resume_path: Path) -> FillResult:
    """
    desc: Fill step 1 of a SmartRecruiters oneclick application (personal
        info, resume, links, hiring-manager message). Never clicks Next or
        Submit; screening questions on later steps are left for the human.
    args:
        page (Page): tab at a jobs.smartrecruiters.com posting or oneclick-ui URL.
        profile (dict): flat profile.
        get_cover_letter (Callable[[], str]): lazy cover-letter provider (see base.call_cover_letter).
        resume_path (Path): resume file.
    output:
        FillResult.
    """
    notes = [_sr_open_form(page)]
    if _datadome(page):
        return FillResult("blocked", [], [], "SmartRecruiters bot check (DataDome); solve it, then Fill Out again.")
    if _first(page, _SR_FORM_MARK, timeout=15000) is None:
        if _datadome(page):
            return FillResult("blocked", [], [], "SmartRecruiters bot check (DataDome); solve it, then Fill Out again.")
        return FillResult("unsupported", [], [], " ".join(n for n in notes if n) +
                          " SmartRecruiters form fields not found (layout may have changed); fill manually.")
    acc = _Acc()
    # resume first: SmartRecruiters may parse it and pre-fill fields, which we then never overwrite
    _upload_resume(_first(page, _SR_RESUME), resume_path, acc)
    time.sleep(1.5)
    for sel, key, label in _SR_TEXT:
        loc = _first(page, sel)
        if loc is None:
            continue
        value = profile.get(key)
        try:
            if value in (None, ""):
                if (loc.input_value(timeout=2000) or "").strip():
                    continue
                if key in ("first_name", "last_name", "email", "phone"):
                    acc.miss(label)
                continue
            if _fill_if_empty(loc, value):
                acc.ok(label)
        except Exception:
            acc.miss(label)
    _sr_city(page, profile, acc)
    msg = _first(page, _SR_MESSAGE)
    if msg is not None:
        try:
            if not (msg.input_value(timeout=3000) or "").strip():
                letter = call_cover_letter(get_cover_letter)
                if not letter:
                    acc.miss(COVER_LETTER_FAILED)
                elif _fill_if_empty(msg, letter):
                    acc.ok("Message to hiring manager (cover letter)")
        except Exception:
            acc.miss("Message to hiring manager")
    try:
        for lab in page.evaluate(_SR_LEFTOVER_JS):
            done = [f.lower().split(" (")[0] for f in acc.filled + acc.unfilled]
            if not any(lab.lower().startswith(d) for d in done):
                acc.miss(lab)
    except Exception:
        pass
    notes.append("Step 1 only: review, then click Next yourself; screening questions on later steps are not filled.")
    status = "filled" if acc.filled and not acc.unfilled else "partial"
    return FillResult(status, acc.filled, acc.unfilled, " ".join(n for n in notes if n))


class SmartRecruitersFiller(Filler):
    """SmartRecruiters oneclick-ui filler (shadow-DOM aware)."""
    name = "smartrecruiters"

    def matches(self, url: str) -> bool:
        return host_matches(url, "smartrecruiters.com")

    def fill(self, page, profile, get_cover_letter, resume_path) -> FillResult:
        return fill_smartrecruiters(page, profile, get_cover_letter, resume_path)


# ==================================================================== Workable

_WK_FORM_MARK = '[data-ui="firstname"], [data-ui="email"]'
_WK_RESUME = 'input[type=file][data-ui="resume"]'
_WK_JOB_RE = re.compile(r"^(/[^/]+/j/[0-9A-Za-z]+)/?(apply/?)?$")

# Hide aria-hidden helper inputs (address city/postcode/country, dropdown value
# holders) from the generic scan by marking them readOnly; restored afterwards.
_WK_HIDE_JS = r"""
() => {
  let n = 0;
  document.querySelectorAll('input[aria-hidden="true"]').forEach(el => {
    const t = (el.getAttribute('type') || 'text').toLowerCase();
    if (['radio', 'checkbox', 'file'].includes(t) || el.readOnly) return;
    el.readOnly = true; el.setAttribute('data-pf-wkro', '1'); n++;
  });
  return n;
}
"""
_WK_UNHIDE_JS = r"""
() => document.querySelectorAll('[data-pf-wkro]').forEach(el => { el.readOnly = false; el.removeAttribute('data-pf-wkro'); })
"""
_WK_DROPDOWNS_JS = r"""
() => [...document.querySelectorAll('[data-ui][data-input-type="select"]')].map(w => {
  const cb = w.querySelector('input[role=combobox]');
  const hid = w.querySelector('input[aria-hidden="true"]');
  const ids = (cb && cb.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
  const label = ids.map(i => { const n = document.getElementById(i); return n ? (n.innerText || '').trim() : ''; })
                   .join(' ').replace(/\s+/g, ' ').trim();
  return { ui: w.getAttribute('data-ui'), label, listbox: cb ? (cb.getAttribute('aria-controls') || '') : '',
           filled: !!((hid && hid.value) || (cb && cb.value)), required: !!(hid && hid.required) };
})
"""


def _wk_apply_url(url: str) -> str | None:
    """
    desc: apply.workable.com/<acct>/j/<code>/ -> .../j/<code>/apply/ (None if not a job URL).
    args:
        url (str): Workable URL.
    output:
        str | None.
    """
    p = urlparse(url)
    m = _WK_JOB_RE.match(p.path or "")
    if not m:
        return None
    return f"{p.scheme or 'https'}://{p.netloc}{m.group(1)}/apply/"


def _wk_label(raw: str) -> str:
    """
    desc: Clean a Workable label ('*', '(Optional)', newlines).
    args:
        raw (str): label text.
    output:
        str.
    """
    s = re.sub(r"\(optional\)|\*", " ", raw or "", flags=re.I)
    return re.sub(r"\s+", " ", s).strip()[:80] or "(unlabeled dropdown)"


def _wk_dropdowns(page, profile: dict, acc: _Acc) -> None:
    """
    desc: Answer Workable custom dropdowns only when the question maps to a
        profile key with an explicit value and one option matches; every
        other required dropdown is reported as unfilled.
    args:
        page (Page): Workable apply page.
        profile (dict): flat profile.
        acc (_Acc): accumulator.
    output:
        None.
    """
    try:
        boxes = page.evaluate(_WK_DROPDOWNS_JS)
    except Exception:
        return
    for b in boxes:
        label = _wk_label(b["label"])
        if b["filled"]:
            continue
        key = classify_field({"type": "text", "tag": "input", "label": label, "placeholder": "",
                              "name": "", "elid": "", "autocomplete": "", "group": ""})
        value = profile.get(key) if key else None
        if value in (None, ""):
            if b["required"] or key:
                acc.miss(label)
            continue
        cb = page.locator(f'[data-ui="{b["ui"]}"] input[role=combobox]').first
        try:
            safe_click(cb, "open dropdown")
            sel = f'[id="{b["listbox"]}"] [role=option]' if b["listbox"] else "[role=listbox] [role=option]"
            opts = page.locator(sel)
            opts.first.wait_for(state="visible", timeout=3000)
            texts = [t.strip() for t in opts.all_inner_texts()]
            choice = _strict_pick(texts, value, key)
            if choice is None:
                page.keyboard.press("Escape")
                acc.miss(label)
                continue
            safe_click(opts.nth(texts.index(choice)), "choose dropdown option")
            acc.ok(label)
        except Exception:  # includes SubmitRefused
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            acc.miss(label)


def _strict_pick(options: list, value, key: str) -> str | None:
    """
    desc: pick_option, but reject loose single-word containment matches
        (e.g. a 'Science' subject option for a degree value), which are too
        risky for custom dropdowns.
    args:
        options (list[str]): option texts.
        value (str): profile value.
        key (str): profile key the question mapped to.
    output:
        str | None.
    """
    c = pick_option(options, value)
    if c is None:
        return None
    nc, nv = _norm(c), _norm(str(value))
    if nc == nv or nv in nc or _yesno(value):
        return c
    if key == "degree" and re.search(r"bachelor|master|ph ?d|doctor|associate", nc):
        return c
    if nc in nv and len(nc.split()) >= 2:
        return c
    return None


def fill_workable(page, profile: dict, get_cover_letter, resume_path: Path) -> FillResult:
    """
    desc: Fill a Workable application: resume first (Workable parses it),
        then the standard fields and custom questions via the generic
        heuristics, then custom dropdowns. Never ticks consent, never touches
        the photo upload, never clicks Submit application.
    args:
        page (Page): tab at an apply.workable.com job or /apply URL.
        profile (dict): flat profile.
        get_cover_letter (Callable[[], str]): lazy cover-letter provider (see base.call_cover_letter).
        resume_path (Path): resume file.
    output:
        FillResult.
    """
    notes = []
    if _first(page, _WK_FORM_MARK) is None:
        target = _wk_apply_url(page.url)
        if target and target.rstrip("/") != page.url.rstrip("/"):
            page.goto(target, wait_until="domcontentloaded", timeout=30000)
            notes.append("Opened the Workable application form.")
    if _first(page, _WK_FORM_MARK, timeout=15000) is None:
        return FillResult("unsupported", [], [], " ".join(notes) +
                          " Workable form fields not found (job closed or layout changed); fill manually.")
    acc = _Acc()
    _upload_resume(_first(page, _WK_RESUME), resume_path, acc)
    if acc.resume_done:
        time.sleep(2.5)  # let resume parsing populate fields before we fill the gaps
    acc.resume_done = True  # generic pass must never put the resume into the photo input
    tmp = Path(tempfile.mkdtemp(prefix="pf_"))
    try:
        page.evaluate(_WK_HIDE_JS)
        try:
            fill_frame(page.main_frame, profile, get_cover_letter, resume_path, acc, prefix="pfwk_", tmpdir=tmp)
        finally:
            page.evaluate(_WK_UNHIDE_JS)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    _wk_dropdowns(page, profile, acc)
    addr = _first(page, '[data-ui="address"]')
    try:
        if addr is not None and (addr.input_value(timeout=2000) or "").strip() and "Address" not in acc.filled:
            acc.miss("Address (pre-filled from your IP location; verify)")
    except Exception:
        pass
    if _first(page, '[data-ui="gdpr"]') is not None:
        acc.miss("Data privacy consent (tick it yourself)")
    if _first(page, '[data-ui="add-section"][aria-label*="Education" i]') is not None and not _first(
            page, '[data-ui="education"] [data-ui="school"], input[name="school"]'):
        notes.append("Education/Experience sections were not added; use '+ Add' if the resume parse missed them.")
    status = "filled" if acc.filled and not acc.unfilled else "partial"
    return FillResult(status, acc.filled, acc.unfilled, " ".join(notes))


class WorkableFiller(Filler):
    """Workable (apply.workable.com) filler."""
    name = "workable"

    def matches(self, url: str) -> bool:
        host = host_of(url)
        if host in ("jobs.workable.com", "www.workable.com", "workable.com"):
            return False  # job board / marketing site, not an application form
        return host_matches(url, "workable.com")

    def fill(self, page, profile, get_cover_letter, resume_path) -> FillResult:
        return fill_workable(page, profile, get_cover_letter, resume_path)


class SmartRecruitersWorkableFiller(Filler):
    """Registry entry: dispatches to the SmartRecruiters or Workable filler by host."""
    name = "smartrecruiters_workable"

    def __init__(self):
        self._subs = (SmartRecruitersFiller(), WorkableFiller())

    def _pick(self, url: str):
        return next((f for f in self._subs if f.matches(url)), None)

    def matches(self, url: str) -> bool:
        return self._pick(url) is not None

    def fill(self, page, profile, get_cover_letter, resume_path) -> FillResult:
        sub = self._pick(page.url)
        if sub is None:  # e.g. a custom-domain career site embedding one of these forms
            sub = self._subs[1] if (_first(page, _WK_FORM_MARK) is not None
                                    and _first(page, _SR_FORM_MARK) is None) else self._subs[0]
        return sub.fill(page, profile, get_cover_letter, resume_path)


FILLER = SmartRecruitersWorkableFiller()

"""
Workday filler (*.myworkdayjobs.com / *.myworkdaysite.com). Workday is a
React SPA wizard keyed by data-automation-id attributes:

    posting  -> [adventureButton] 'Apply'
    /apply   -> [applyAdventurePage] with [autofillWithResume] / [applyManually]
                / [useMyLastApplication]
    wizard   -> [progressBar] 'Create Account/Sign In', 'My Information',
                'My Experience', 'Application Questions', 'Voluntary
                Disclosures', (Self Identify), 'Review'

DOM confirmed against a live public tenant (Oct 2026): adventureButton (an
<a> to .../apply), applyAdventurePage, applyManually / autofillWithResume /
useMyLastApplication (links), progressBar / progressBarActiveStep ('current
step 1 of 6 Create Account/Sign In'), signInContent, SignInWithEmailButton,
GoogleSignInButton, LinkedInSignInButton. The account wall has NO password
field until 'Sign in with email' is clicked, so base.detect_blocker misses
it -- this filler handles login itself.

Not observable without an account (selectors below are from Workday's known
markup and matched loosely, by id / data-automation-id / data-fkit-id
substrings plus the generic label heuristics): the My Information / My
Experience field ids (legalName--firstName, phoneNumber--phoneNumber,
address--city, education-N--gradeAverage, file-upload-input-ref, ...),
the page containers (applyFlowMyInfoPage, applyFlowMyExpPage, ...) and the
footer button (pageFooterNextButton / bottom-navigation-next-button).

Only the My Information and My Experience steps are ever filled. Workday
dropdowns (<button aria-haspopup=listbox>) and search prompts (country,
phone type, 'How did you hear', degree, field of study) are never touched;
they are reported in `unfilled`. Work-experience entries, dates and radios
('previously worked here?') are never invented. Nothing here clicks submit:
the only clicks are opening the form ('Apply' / 'Apply Manually') via
safe_click, plus an OPT-IN 'Save and Continue' from My Information to My
Experience (advance_to_experience, default False).
"""

import re
import shutil
import tempfile
import time
from pathlib import Path

from applicator.prefill import answers
from applicator.prefill.base import (
    Filler, FillResult, COVER_LETTER_FAILED, call_cover_letter, detect_blocker, host_matches,
)
from applicator.prefill.generic import _Acc, classify_field, field_label, pick_option, scan_frame
from applicator.prefill.safe import SubmitRefused, is_submit_like, safe_click

_ROOT_SEL = ", ".join(f'[data-automation-id="{a}"]' for a in (
    "jobPostingPage", "adventureButton", "applyAdventurePage", "progressBar",
    "signInContent", "applyFlowPage",
))
_LOGIN_SEL = ", ".join([
    '[data-automation-id="signInContent"]', '[data-automation-id="SignInWithEmailButton"]',
    '[data-automation-id="signInSubmitButton"]', '[data-automation-id="createAccountLink"]',
    '[data-automation-id="createAccountSubmitButton"]', '[data-automation-id="signInLink"]',
    'input[type="password"]',
])
_NEXT_SEL = '[data-automation-id="pageFooterNextButton"], [data-automation-id="bottom-navigation-next-button"]'
_NEXT_TEXT_RE = re.compile(r"^\s*(save and continue|continue|next)\s*$", re.I)

# step key -> (progress-bar text regex, page-container data-automation-id)
_STEPS = [
    ("sign_in", r"create account|sign ?in", None),
    ("my_information", r"my information", "applyFlowMyInfoPage"),
    ("my_experience", r"my experience", "applyFlowMyExpPage"),
    ("questions", r"application questions|questions", "applyFlowPrimaryQuestionsPage"),
    ("disclosures", r"voluntary disclosures|disclosures", "applyFlowVoluntaryDisclosuresPage"),
    ("self_identify", r"self ?identify", "applyFlowSelfIdentifyPage"),
    ("review", r"review", "applyFlowReviewPage"),
]
_FILLABLE_STEPS = ("my_information", "my_experience")
_STEP_NAMES = {
    "sign_in": "Create Account/Sign In", "my_information": "My Information",
    "my_experience": "My Experience", "questions": "Application Questions",
    "disclosures": "Voluntary Disclosures", "self_identify": "Self Identify", "review": "Review",
}

# Workday id / automation-id fragments -> profile key. None = never fill.
_ID_RULES = [
    (r"extension|countryphonecode|phonecode|devicetype|phonetype", None),
    (r"workexperience|jobtitle|companyname|roledescription", None),
    (r"datesection|firstyearattended|lastyearattended|startdate|enddate|month|year", None),
    (r"preferredname|middlename|localgivenname|localfamilyname|suffix|prefix|title--", None),
    (r"addressline|postalcode|zip|region|county|state", None),
    (r"legalname--firstname|legalnamesection_firstname|firstname", "first_name"),
    (r"legalname--lastname|legalnamesection_lastname|lastname", "last_name"),
    (r"phonenumber--phonenumber|phone-number|phonenumber", "phone"),
    (r"emailaddress|\bemail\b|--email", "email"),
    (r"address--city|addresssection_city|\bcity\b", "city"),
    (r"linkedin", "linkedin"),
    (r"github", "github"),
    (r"webaddress|website|\burl\b", "website"),
    (r"gradeaverage|\bgpa\b", "gpa"),
    (r"--school|schoolname|\bschool\b", "school"),
]
_COMPILED_ID = [(re.compile(r, re.I), k) for r, k in _ID_RULES]

# Never derive these from a generic label match on Workday: wrong-section
# fills (e.g. a work-experience 'Location', the 'From/To' graduation dates).
_GENERIC_DENY = {"location", "full_name", "grad_year", "degree", "major"}

_AUGMENT_JS = r"""
() => [...document.querySelectorAll('[data-pf-id]')].map(el => {
  const sec = el.closest('[data-automation-id*="workExperience" i], [data-automation-id*="education" i], '
      + '[data-automation-id*="Section" i], [role=group], fieldset');
  return {
    id: el.getAttribute('data-pf-id'),
    aid: el.getAttribute('data-automation-id') || '',
    fkit: el.getAttribute('data-fkit-id') || '',
    uxi: el.getAttribute('data-uxi-widget-type') || '',
    popup: el.getAttribute('aria-haspopup') || '',
    section: sec ? ((sec.getAttribute('data-automation-id') || '') + ' ' + (sec.id || '')) : '',
    prompt: !!el.closest('[data-automation-id*="multiselect" i], [data-automation-id*="selectinput" i], '
      + '[data-uxi-widget-type="multiselect"], [data-uxi-widget-type="selectinput"]'),
  };
})
"""

# Workday dropdowns are buttons, not <select>; list them so the human answers.
_DROPDOWNS_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const txt = e => ((e && (e.innerText || e.textContent)) || '').replace(/\s+/g, ' ').trim();
  const out = [];
  for (const b of document.querySelectorAll('button[aria-haspopup="listbox"], [role=combobox]:not(input)')) {
    if (!vis(b)) continue;
    let lab = '';
    const lb = b.getAttribute('aria-labelledby');
    if (lb) lab = lb.split(/\s+/).map(i => txt(document.getElementById(i))).join(' ');
    if (!lab && b.id) { const l = document.querySelector('label[for="' + CSS.escape(b.id) + '"]'); if (l) lab = txt(l); }
    if (!lab) { const ff = b.closest('[data-automation-id^="formField"]'); if (ff) { const l = ff.querySelector('label'); if (l) lab = txt(l); } }
    if (!lab) lab = b.getAttribute('aria-label') || b.getAttribute('data-automation-id') || '';
    out.push(lab.slice(0, 80));
  }
  return out;
}
"""

_RESUME_DONE_SEL = ('[data-automation-id="file-upload-successful"], '
                    '[data-automation-id="file-upload-item"], [data-automation-id="delete-file"]')


def _visible(page, sel: str) -> bool:
    try:
        return any(el.is_visible() for el in page.query_selector_all(sel))
    except Exception:
        return False


def current_step(page) -> str | None:
    """
    desc: Identify the Workday page/step shown: 'posting', 'adventure', a
        wizard step key from _STEPS, or None if unrecognised.
    args:
        page (Page): Workday tab.
    output:
        str | None.
    """
    try:
        active = page.query_selector('[data-automation-id="progressBarActiveStep"]')
        text = (active.inner_text() if active else "") or ""
    except Exception:
        text = ""
    text = re.sub(r"^\s*current step \d+ of \d+\s*", "", text.replace("\n", " "), flags=re.I)
    for key, rx, container in _STEPS:
        if text and re.search(rx, text, re.I):
            return key
    for key, _rx, container in _STEPS:
        if container and page.query_selector(f'[data-automation-id="{container}"]'):
            return key
    if _visible(page, _LOGIN_SEL):
        return "sign_in"
    if page.query_selector('[data-automation-id="applyAdventurePage"]'):
        return "adventure"
    if page.query_selector('[data-automation-id="adventureButton"]'):
        return "posting"
    return None


def _open_link(page, aid: str, purpose: str) -> bool:
    """
    desc: Follow a Workday link by data-automation-id: navigate to its href
        (a GET) if it has one, else safe_click it. Never a submit control.
    args:
        page (Page): Workday tab.
        aid (str): data-automation-id of the link/button.
        purpose (str): reason passed to safe_click.
    output:
        bool: True if activated.
    """
    sel = f'[data-automation-id="{aid}"]'
    el = page.query_selector(sel)
    if not el:
        return False
    href = el.evaluate("e => e.href || ''") or ""
    try:
        if re.match(r"^(https?|file):", href, re.I):
            page.goto(href, wait_until="domcontentloaded", timeout=30000)
        else:
            safe_click(page, purpose, selector=sel)
    except SubmitRefused:
        return False
    return True


def _wait_root(page, timeout_ms: int = 20000) -> None:
    try:
        page.wait_for_selector(_ROOT_SEL, timeout=timeout_ms)
    except Exception:
        pass
    time.sleep(0.5)


def _wait_fields(page, timeout_ms: int = 8000) -> None:
    try:
        page.wait_for_selector("input:visible, textarea:visible, button[aria-haspopup=listbox]:visible",
                               timeout=timeout_ms)
    except Exception:
        pass
    time.sleep(0.6)  # React finishes binding handlers after first paint


def _workday_key(d: dict, extra: dict) -> tuple:
    """
    desc: Decide what a scanned Workday field should get.
    args:
        d (dict): descriptor from generic.scan_frame.
        extra (dict): Workday attributes (aid, fkit, uxi, popup, section, prompt).
    output:
        tuple[str, str | None]: (action, key) where action is 'fill', 'skip'
            (leave alone, report if required), 'answer' (unclassified: answer
            bank only), or 'manual' (prompt/dropdown).
    """
    if d["type"] == "file":
        return ("fill", classify_field(d))
    if extra.get("prompt") or extra.get("uxi") in ("selectinput", "multiselect") or d.get("combobox") \
            or extra.get("popup") == "listbox":
        return ("manual", None)
    blob = " ".join([d.get("elid", ""), d.get("name", ""), extra.get("aid", ""), extra.get("fkit", "")])
    if re.search(r"workexperience", extra.get("section", ""), re.I):
        return ("skip", None)
    for rx, key in _COMPILED_ID:
        if blob.strip() and rx.search(blob):
            return ("fill", key) if key else ("skip", None)
    key = classify_field(d)
    if key in _GENERIC_DENY:
        return ("skip", None)
    if key:
        return ("fill", key)
    return ("answer", None)   # unclassified: only the answer bank may fill it


def _fill_step(page, profile: dict, get_cover_letter, resume_path: Path, acc: _Acc, step: str) -> None:
    """
    desc: Fill the visible My Information / My Experience step in place.
        Records labels only; never overwrites existing values.
    args:
        page (Page): Workday tab.
        profile (dict): flat profile.
        get_cover_letter (Callable[[], str]): lazy cover-letter provider (see base.call_cover_letter).
        resume_path (Path): resume file.
        acc (_Acc): label accumulator.
        step (str): current step key.
    output:
        None.
    """
    fields = scan_frame(page, f"wd_{step}_")
    try:
        extras = {x["id"]: x for x in page.evaluate(_AUGMENT_JS)}
    except Exception:
        extras = {}
    tmp = None
    radios: dict = {}
    try:
        for d in fields:
            label = field_label(d)
            loc = page.locator(f'[data-pf-id="{d["id"]}"]').first
            try:
                if d["type"] == "radio":
                    radios.setdefault(d["name"] or d["id"], []).append(d)
                    continue
                if d["type"] == "checkbox":
                    if d["required"] and not d["checked"]:
                        acc.miss(label)
                    continue
                action, key = _workday_key(d, extras.get(d["id"], {}))
                if d["type"] == "file":
                    acc.file_inputs += 1
                    if key == "cover_letter_file":
                        if not acc.cover_file_done:
                            letter = call_cover_letter(get_cover_letter)
                            if letter:
                                tmp = tmp or Path(tempfile.mkdtemp(prefix="pf_wd_"))
                                p = tmp / "cover_letter.txt"
                                p.write_text(letter, encoding="utf-8")
                                loc.set_input_files(str(p), timeout=5000)
                                acc.cover_file_done = True
                                acc.ok("Cover letter (file upload)")
                            else:
                                acc.miss(COVER_LETTER_FAILED)
                        else:
                            acc.miss(label)
                    elif key == "resume" and not acc.resume_done:
                        if page.query_selector(_RESUME_DONE_SEL):
                            acc.resume_done = True  # already attached earlier; leave it
                        elif Path(resume_path).exists():
                            loc.set_input_files(str(resume_path), timeout=5000)
                            acc.resume_done = True
                            acc.ok("Resume (file upload)")
                        else:
                            acc.miss("Resume (file missing)")
                    continue
                if action == "manual":
                    acc.miss(label + " (dropdown: choose manually)")
                    continue
                if d["value"].strip() and d["tag"] != "select":
                    continue  # never overwrite
                if action == "answer":
                    # unclassified free text: Noah's answer bank (verbatim;
                    # company-specific / why-us questions never match)
                    if d["tag"] == "textarea" or (d["tag"] == "input" and d["type"] in ("text", "")):
                        question = d["label"] or d["placeholder"]
                        saved = answers.lookup(question)
                        if saved is not None:
                            loc.focus(timeout=3000)
                            loc.fill(saved, timeout=3000)
                            loc.dispatch_event("change")
                            loc.evaluate("el => el.blur()")
                            acc.ok(label)
                            continue
                        if d["required"]:
                            answers.note_unanswered(question)
                if action in ("skip", "answer") or key is None:
                    if d["required"]:
                        acc.miss(label)
                    continue
                if key == "cover_letter":
                    letter = call_cover_letter(get_cover_letter)
                    if letter:
                        loc.fill(letter, timeout=3000)
                        acc.ok("Cover letter")
                    else:
                        acc.miss(COVER_LETTER_FAILED)
                    continue
                value = profile.get(key)
                if value in (None, ""):
                    acc.miss(label)
                    continue
                if d["tag"] == "select":
                    opt = pick_option(d["options"], value)
                    if opt is None:
                        acc.miss(label)
                    else:
                        loc.select_option(label=opt, timeout=3000)
                        acc.ok(label)
                    continue
                loc.focus(timeout=3000)
                loc.fill(str(value), timeout=3000)
                loc.dispatch_event("change")
                loc.evaluate("el => el.blur()")  # Workday commits text fields on blur
                acc.ok(label)
            except Exception:
                acc.miss(label)
        for members in radios.values():  # e.g. 'previously worked here?': never invented
            if not any(m["checked"] for m in members):
                acc.miss((members[0].get("group") or field_label(members[0]))[:80])
        try:
            for lab in page.evaluate(_DROPDOWNS_JS):
                if lab:
                    acc.miss(lab + " (dropdown: choose manually)")
        except Exception:
            pass
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def _advance_to_experience(page) -> str:
    """
    desc: Opt-in: click the footer 'Save and Continue' from My Information
        only. Refuses unless the button text is exactly Save and Continue /
        Continue / Next (the same footer button reads 'Submit' on Review).
    args:
        page (Page): Workday tab on My Information.
    output:
        str: '' on success, else a short note.
    """
    btn = page.query_selector(_NEXT_SEL)
    if not btn:
        return "No Save and Continue button found."
    text = (btn.inner_text() or "").strip()
    if not _NEXT_TEXT_RE.match(text) or is_submit_like(text):
        return "Footer button is not 'Save and Continue'; not clicked."
    try:
        safe_click(btn, "advance Workday My Information to My Experience")
    except Exception as e:
        return f"Did not advance ({type(e).__name__})."
    time.sleep(1.5)
    _wait_fields(page)
    if current_step(page) != "my_experience":
        return "Workday did not move to My Experience (required fields missing?)."
    return ""


class WorkdayFiller(Filler):
    """Workday wizard filler; fills My Information / My Experience only."""
    name = "workday"
    handles_login = True
    advance_to_experience = False  # opt-in: saves My Information as a Workday draft

    def matches(self, url: str) -> bool:
        return host_matches(url, "myworkdayjobs.com", "myworkdaysite.com")

    def fill(self, page, profile: dict, get_cover_letter, resume_path: Path) -> FillResult:
        """
        desc: Open the Workday apply flow ('Apply' -> 'Apply Manually') if on
            the posting, stop at the account wall (needs_login), and fill the
            current My Information or My Experience step. Never touches the
            later steps or the Review/Submit step.
        args:
            page (Page): tab at a Workday posting or apply URL.
            profile (dict): flat profile.
            get_cover_letter (Callable[[], str]): lazy cover-letter provider (see base.call_cover_letter).
            resume_path (Path): resume file.
        output:
            FillResult.
        """
        _wait_root(page)
        notes = []
        step = current_step(page)
        if step == "posting":
            if not _open_link(page, "adventureButton", "open Workday application"):
                return FillResult("unsupported", [], [], "Could not open Workday 'Apply'; open it manually.")
            _wait_root(page)
            step = current_step(page)
        if step == "adventure":
            if not _open_link(page, "applyManually", "choose Workday Apply Manually"):
                return FillResult("unsupported", [], [],
                                  "Workday 'Apply Manually' not found; pick an apply option manually.")
            notes.append("Chose Apply Manually.")
            _wait_root(page)
            time.sleep(1.0)
            step = current_step(page)

        blk = detect_blocker(page)
        if blk and blk[0] == "blocked":
            return FillResult("blocked", [], [], blk[1])
        if step == "sign_in" or (blk and blk[0] == "needs_login"):
            return FillResult("needs_login", [], [],
                              "Workday account wall: sign in or create an account for this company "
                              "in this tab, then click Fill Out again.")
        if step is None:
            return FillResult("unsupported", [], [],
                              "Workday page not recognised (DOM changed or still loading); fill manually.")
        if step not in _FILLABLE_STEPS:
            return FillResult("partial", [], [f"Workday step: {_STEP_NAMES[step]}"],
                              f"Tab is on '{_STEP_NAMES[step]}'; prefill only covers My Information / "
                              "My Experience. Finish this step yourself.")

        _wait_fields(page)
        acc = _Acc()
        _fill_step(page, profile, get_cover_letter, resume_path, acc, step)
        if step == "my_information":
            if self.advance_to_experience and not acc.unfilled:
                note = _advance_to_experience(page)
                if note:
                    notes.append(note)
                else:
                    step = "my_experience"
                    _fill_step(page, profile, get_cover_letter, resume_path, acc, step)
            if step == "my_information":
                notes.append("Filled My Information. Review it, click Save and Continue, then Fill Out "
                             "again to fill My Experience (resume upload).")
        if step == "my_experience":
            if acc.file_inputs == 0 and not acc.resume_done:
                acc.miss("Resume (no upload field found)")
            notes.append("Filled My Experience; work history, education dates and later steps "
                         "are left for you.")
        unfilled = [x for x in acc.unfilled if x not in acc.filled]
        status = "filled" if acc.filled and not unfilled else "partial"
        return FillResult(status, acc.filled, unfilled, " ".join(notes))


FILLER = WorkdayFiller()

"""
Fillers for Lever (jobs.lever.co / jobs.eu.lever.co) and Ashby
(jobs.ashbyhq.com) hosted application forms. Both build on the generic
heuristics but add what those ATSs need: go straight to the form URL
(Lever '/apply', Ashby '/application'), upload the resume into the real
resume input (not Ashby's 'Autofill from resume' box), let Lever's resume
parser finish before typing, pick the location from the typeahead only when
an option clearly matches, and sweep required questions that are still empty
into 'unfilled'. Never clicks submit; the only clicks are typeahead options
and Ashby Yes/No toggles (for explicitly answered questions), all through
safe_click.

DOM notes (learned from public pages, Oct 2026):
- Lever (server-rendered): form#application-form; each question is
  li.application-question > label > div.application-label + div.application-field.
  Core inputs by name: resume (file, #resume-upload-input), name, email,
  phone, location (#location-input, typeahead filling hidden
  input[name=selectedLocation]), org, urls[LinkedIn], urls[GitHub],
  urls[Portfolio] / urls[Personal Website], urls[Other]. Custom cards are
  cards[<uuid>][fieldN]; their required marker is a .required span /
  .required-field wrapper, NOT a `required` attribute. EEO selects are
  eeo[gender], eeo[race], eeo[veteran], eeo[disability]. Submit is #btn-submit.
- Ashby (React SPA): field ids/names equal the form field path
  (_systemfield_name, _systemfield_email, _systemfield_resume,
  _systemfield_location, or a UUID for custom questions), confirmed from the
  public job-posting form schema. Wrapper / title classes
  (.ashby-application-form-field-entry, .ashby-application-form-question-title,
  .ashby-application-form-autofill-input-root) are Ashby's documented
  theming hooks but were NOT seen in a live render here (the page is
  client-rendered); the code falls back to the generic scan if they are absent.
"""

import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from applicator.prefill.base import Filler, FillResult, host_matches
from applicator.prefill.generic import _Acc, _norm, _wait_for_fields, classify_field, fill_frame, pick_option
from applicator.prefill.safe import SubmitRefused, safe_click

# ----------------------------------------------------------------------------- shared helpers

_REQUIRED_SWEEP_JS = r"""
([rowSel, reqSel, titleSel]) => {
  const txt = e => ((e && (e.innerText || e.textContent)) || '').replace(/\s+/g, ' ').trim();
  const out = [];
  for (const row of document.querySelectorAll(rowSel)) {
    const inputs = [...row.querySelectorAll('input:not([type=hidden]), select, textarea')]
      .filter(el => !['submit', 'button', 'image', 'reset'].includes((el.type || '').toLowerCase()));
    if (!inputs.length) continue;
    const required = !!row.querySelector(reqSel) || row.matches(reqSel)
      || inputs.some(el => el.required || el.getAttribute('aria-required') === 'true')
      || /[*✱]\s*$/.test(txt(row.querySelector(titleSel)));
    if (!required) continue;
    const answered = inputs.some(el => {
      const t = (el.type || '').toLowerCase();
      if (t === 'checkbox' || t === 'radio') return el.checked;
      if (t === 'file') return el.files && el.files.length > 0;
      if (el.tagName === 'SELECT') return el.selectedIndex > 0 || (el.value || '') !== '';
      return (el.value || '').trim() !== '';
    }) || !!row.querySelector('[aria-pressed=true], [aria-checked=true], [class*=_active], [class*=selected]');
    if (answered) continue;
    out.push((txt(row.querySelector(titleSel)) || txt(row)).replace(/[*✱]\s*$/, '').trim().slice(0, 80));
  }
  return out;
}
"""


def _sweep_required(frame, acc: _Acc, row_sel: str, req_sel: str, title_sel: str) -> None:
    """
    desc: Add every required question that is still empty to acc.unfilled
        (labels only). Catches custom questions whose 'required' marker is
        visual only, which the generic scan cannot see.
    args:
        frame (Frame | Page): frame holding the form.
        acc (_Acc): accumulator.
        row_sel (str): CSS for one question row.
        req_sel (str): CSS that marks a row (or a descendant) as required.
        title_sel (str): CSS for the row's question text.
    output:
        None.
    """
    try:
        labels = frame.evaluate(_REQUIRED_SWEEP_JS, [row_sel, req_sel, title_sel])
    except Exception:
        return
    for lab in labels:
        if lab and lab not in acc.filled and not any(
                re.sub(r"\s*[*✱]\s*$", "", u).startswith(lab) for u in acc.unfilled):
            acc.miss(lab)


def _form_url(url: str, suffix: str) -> str:
    """
    desc: Turn a posting URL into its application-form URL by appending a
        path segment (Lever '/apply', Ashby '/application'), keeping the query.
    args:
        url (str): posting or form URL.
        suffix (str): 'apply' or 'application'.
    output:
        str: form URL (unchanged if it already ends with the suffix).
    """
    p = urlparse(url)
    path = p.path.rstrip("/")
    if path.endswith("/" + suffix):
        return url
    return urlunparse(p._replace(path=path + "/" + suffix))


def _first(frame, selectors):
    for sel in selectors:
        try:
            loc = frame.locator(sel).first
            if loc.count():
                return loc
        except Exception:
            continue
    return None


def _upload_resume(frame, selectors, resume_path: Path, acc: _Acc) -> bool:
    """
    desc: Upload the resume into the ATS's real resume input.
    args:
        frame (Frame | Page): form frame.
        selectors (list[str]): candidate CSS selectors, first match wins.
        resume_path (Path): resume file.
        acc (_Acc): accumulator (resume_done is set on success).
    output:
        bool: True if a file was attached.
    """
    loc = _first(frame, selectors)
    if loc is None:
        return False
    if not Path(resume_path).exists():
        acc.miss("Resume (file missing)")
        acc.resume_done = True  # don't let the generic pass try another input
        return False
    try:
        if loc.evaluate("el => el.files && el.files.length > 0"):
            acc.resume_done = True
            return False  # already attached by the human; leave it
        loc.set_input_files(str(resume_path), timeout=8000)
        acc.resume_done = True
        acc.ok("Resume (file upload)")
        return True
    except Exception:
        acc.miss("Resume (upload failed)")
        acc.resume_done = True
        return False


def _pick_typeahead(frame, input_loc, typed: str, want: str, option_sels, acc: _Acc, label: str) -> None:
    """
    desc: Type into a location typeahead and choose an option ONLY if one
        clearly starts with the wanted place; otherwise leave the typed text
        and report the field so the human picks.
    args:
        frame (Frame | Page): form frame.
        input_loc (Locator): the typeahead input.
        typed (str): text to type (e.g. the city).
        want (str): profile location used to judge options.
        option_sels (list[str]): CSS for option elements.
        acc (_Acc): accumulator.
        label (str): report label.
    output:
        None.
    """
    try:
        input_loc.focus(timeout=2000)
        input_loc.fill("", timeout=2000)
        input_loc.type(typed, delay=40, timeout=8000)
    except Exception:
        acc.miss(label)
        return
    want_n = _norm(want)
    city_n = _norm(typed)
    deadline = time.time() + 5
    while time.time() < deadline:
        for sel in option_sels:
            try:
                opts = frame.locator(sel)
                n = min(opts.count(), 10)
            except Exception:
                continue
            for i in range(n):
                o = opts.nth(i)
                try:
                    if not o.is_visible():
                        continue
                    t = _norm(o.inner_text(timeout=1000))
                except Exception:
                    continue
                if t and city_n and t.startswith(city_n) and _region_ok(want_n, t):
                    try:
                        safe_click(o, "choose location option")
                        acc.ok(label)
                        return
                    except (SubmitRefused, Exception):
                        acc.miss(label + " (pick from dropdown)")
                        return
        time.sleep(0.3)
    acc.miss(label + " (pick from dropdown)")


_US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california",
    "co": "colorado", "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia",
    "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
    "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland", "ma": "massachusetts",
    "mi": "michigan", "mn": "minnesota", "ms": "mississippi", "mo": "missouri", "mt": "montana",
    "ne": "nebraska", "nv": "nevada", "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico",
    "ny": "new york", "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
    "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah", "vt": "vermont",
    "va": "virginia", "wa": "washington", "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming",
    "dc": "district of columbia",
}


def _region_ok(want_n: str, option_n: str) -> bool:
    """
    desc: When the profile location names a region after the city
        ('St. Louis, MO'), require the option to mention that region (by
        abbreviation or full state name). With no region, accept.
    args:
        want_n (str): normalized profile location.
        option_n (str): normalized option text.
    output:
        bool.
    """
    parts = want_n.split(" ")
    if len(parts) < 2:
        return True
    region = parts[-1]
    full = _US_STATES.get(region)
    return bool(re.search(rf"\b{re.escape(region)}\b", option_n) or (full and full in option_n))


def _clean_labels(acc: _Acc) -> None:
    """
    desc: Strip trailing required markers (* or Lever's heavy asterisk) from
        report labels, dedupe, and drop unfilled labels that were filled.
    args:
        acc (_Acc): accumulator, modified in place.
    output:
        None.
    """
    def clean(xs):
        out = []
        for x in xs:
            x = re.sub(r"\s*[*✱]\s*$", "", x).strip()
            if x and x not in out:
                out.append(x)
        return out
    acc.filled = clean(acc.filled)
    acc.unfilled = [u for u in clean(acc.unfilled) if u not in acc.filled]


def _status(acc: _Acc) -> str:
    if not acc.filled and not acc.unfilled:
        return "unsupported"
    return "filled" if acc.filled and not acc.unfilled else "partial"


def _goto_form(page, suffix: str, ready_sel: str, hosts: tuple) -> str:
    """
    desc: If the tab is on an ATS posting page (not the form), navigate to
        the form URL. Uses navigation, not a click, so no submit-styled
        'Apply' button is ever touched. Pages on other hosts are left alone.
    args:
        page (Page): tab.
        suffix (str): 'apply' or 'application'.
        ready_sel (str): selector that exists only on the form.
        hosts (tuple[str]): ATS host suffixes this applies to.
    output:
        str: note ('' if nothing was done).
    """
    if not host_matches(page.url, *hosts):
        return ""
    target = _form_url(page.url, suffix)
    if target == page.url:
        return ""
    try:
        page.wait_for_selector(ready_sel, timeout=3000)
        return ""  # form is on this page already
    except Exception:
        pass
    try:
        page.goto(target, wait_until="domcontentloaded", timeout=30000)
        return "Opened the application form. "
    except Exception:
        return "Could not open the application form URL. "


# ----------------------------------------------------------------------------- Lever

_LEVER_CORE = [  # (input name, profile key)
    ("name", "full_name"), ("email", "email"), ("phone", "phone"),
    ("urls[LinkedIn]", "linkedin"), ("urls[GitHub]", "github"),
    ("urls[Portfolio]", "website"), ("urls[Personal Website]", "website"),
]


_LEVER_HOSTS = ("jobs.lever.co", "jobs.eu.lever.co")
_LEVER_LABELS = {"name": "Full name", "email": "Email", "phone": "Phone"}


class LeverFiller(Filler):
    """Lever hosted application form (jobs.lever.co, jobs.eu.lever.co)."""
    name = "lever"

    def matches(self, url: str) -> bool:
        """
        desc: Lever-hosted job pages.
        args:
            url (str): application URL.
        output:
            bool.
        """
        return host_matches(url, *_LEVER_HOSTS)

    def fill(self, page, profile: dict, get_cover_letter, resume_path: Path) -> FillResult:
        """
        desc: Open /apply if needed, upload the resume and wait for Lever's
            parser, fill core fields (only those that were empty before the
            upload), choose the location from the typeahead when it clearly
            matches, run the generic pass for the rest, then sweep empty
            required questions. Never submits.
        args:
            page (Page): tab at the Lever posting or /apply URL.
            profile (dict): flat profile.
            get_cover_letter (Callable[[], str]): lazy cover-letter provider (see base.call_cover_letter).
            resume_path (Path): resume file.
        output:
            FillResult.
        """
        notes = _goto_form(page, "apply", "#application-form", _LEVER_HOSTS)
        try:
            page.wait_for_selector("#application-form", timeout=10000)
        except Exception:
            return FillResult("unsupported", [], [], notes + "Lever application form not found; fill manually.")
        form = page.locator("#application-form")
        acc = _Acc()

        def val(n):
            loc = form.locator(f'[name="{n}"]').first
            return loc.input_value() if loc.count() else None

        was_empty = {n for n, _ in _LEVER_CORE if val(n) == ""}

        if _upload_resume(page, ["#resume-upload-input", 'input[type=file][name="resume"]'], resume_path, acc):
            try:  # Lever parses the resume and may type into fields; let it finish first
                page.wait_for_selector(".resume-upload-success:visible, .resume-upload-failure:visible",
                                       timeout=15000)
            except Exception:
                notes += "Resume parse did not finish; check the auto-read fields. "
            time.sleep(0.5)

        for n, key in _LEVER_CORE:
            loc = form.locator(f'[name="{n}"]').first
            if not loc.count() or n not in was_empty:
                continue
            label = _LEVER_LABELS.get(n) or (n[5:-1] + " URL")
            value = profile.get(key)
            if value in (None, ""):
                if val(n) == "":
                    acc.miss(label)
                continue
            try:
                loc.fill(str(value), timeout=3000)
                acc.ok(label)
            except Exception:
                acc.miss(label)

        loc_in = _first(form, ["#location-input", 'input[name="location"]'])
        if loc_in is not None and loc_in.input_value() == "":
            want = profile.get("location") or ""
            typed = profile.get("city") or (want.split(",")[0].strip() if want else "")
            if typed:
                _pick_typeahead(page, loc_in, typed, want, [".dropdown-results > div", "[role=option]"],
                                acc, "Current location")
            else:
                acc.miss("Current location")

        tmp = Path(tempfile.mkdtemp(prefix="pf_"))
        try:
            total = fill_frame(page, profile, get_cover_letter, resume_path, acc, prefix="pfl", tmpdir=tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        _sweep_required(page, acc, "#application-form .application-question",
                        ".required, .required-field, [data-qa=SCL-question-required-asterisk]",
                        ".application-label")
        _clean_labels(acc)
        if not acc.resume_done:
            acc.miss("Resume (no upload field found)")
        if not acc.filled and total == 0:
            return FillResult("unsupported", [], acc.unfilled, notes + "No fillable Lever fields found.")
        return FillResult(_status(acc), acc.filled, acc.unfilled, notes.strip())


# ----------------------------------------------------------------------------- Ashby

_ASHBY_HOSTS = ("jobs.ashbyhq.com",)
_ASHBY_CORE = [  # (field path, profile key, report label)
    ("_systemfield_name", "full_name", "Name"), ("_systemfield_email", "email", "Email"),
    ("_systemfield_phone", "phone", "Phone"),
]
_ASHBY_ROW = ".ashby-application-form-field-entry"
_ASHBY_TITLE = ".ashby-application-form-question-title, label"

_YESNO_JS = r"""
([rowSel, titleSel]) => {
  const txt = e => ((e && (e.innerText || e.textContent)) || '').replace(/\s+/g, ' ').trim();
  const out = []; let n = 0;
  for (const row of document.querySelectorAll(rowSel)) {
    const btns = [...row.querySelectorAll('button')];
    const yes = btns.find(b => /^yes$/i.test(txt(b))), no = btns.find(b => /^no$/i.test(txt(b)));
    if (!yes || !no || btns.length !== 2) continue;
    const id = 'pfyn' + (n++);
    yes.setAttribute('data-pf-yn', id + '-yes'); no.setAttribute('data-pf-yn', id + '-no');
    const pressed = b => b.getAttribute('aria-pressed') === 'true' || /_active|selected/i.test(b.className);
    out.push({ id, question: txt(row.querySelector(titleSel)).replace(/[*✱]\s*$/, '').trim(),
               answered: pressed(yes) || pressed(no) });
  }
  return out;
}
"""


class AshbyFiller(Filler):
    """Ashby hosted application form (jobs.ashbyhq.com)."""
    name = "ashby"

    def matches(self, url: str) -> bool:
        """
        desc: Ashby-hosted job pages.
        args:
            url (str): application URL.
        output:
            bool.
        """
        return host_matches(url, *_ASHBY_HOSTS)

    def fill(self, page, profile: dict, get_cover_letter, resume_path: Path) -> FillResult:
        """
        desc: Open /application if needed, upload the resume into
            #_systemfield_resume (skipping the 'Autofill from resume' box so
            Ashby doesn't overwrite anything), fill the rest with the generic
            pass, pick the location from the typeahead when it clearly
            matches, answer Yes/No toggles only for explicitly known
            sensitive answers, then sweep empty required questions. Never submits.
        args:
            page (Page): tab at the Ashby posting or /application URL.
            profile (dict): flat profile.
            get_cover_letter (Callable[[], str]): lazy cover-letter provider (see base.call_cover_letter).
            resume_path (Path): resume file.
        output:
            FillResult.
        """
        notes = _goto_form(page, "application", "#_systemfield_email, #_systemfield_name", _ASHBY_HOSTS)
        try:  # client-rendered: wait for React to draw the form
            page.wait_for_selector("#_systemfield_email, #_systemfield_name, form input", timeout=15000)
        except Exception:
            return FillResult("unsupported", [], [], notes + "Ashby form did not render; fill manually.")
        _wait_for_fields(page, 2.0)
        acc = _Acc()
        if _first(page, [_ASHBY_ROW]) is None:
            notes += "Ashby theming classes not found; used generic field detection. "

        _upload_resume(page, ["input[type=file]#_systemfield_resume",
                              'input[type=file][name="_systemfield_resume"]'], resume_path, acc)

        for path, key, label in _ASHBY_CORE:  # system fields by id: placeholders confuse the heuristics
            loc = _first(page, [f'input[id="{path}"]', f'input[name="{path}"]'])
            if loc is None or loc.input_value() != "":
                continue
            value = profile.get(key)
            if value in (None, ""):
                acc.miss(label)
                continue
            try:
                loc.fill(str(value), timeout=3000)
                acc.ok(label)
            except Exception:
                acc.miss(label)

        loc_in = _first(page, ["#_systemfield_location", 'input[name="_systemfield_location"]'])
        if loc_in is not None and loc_in.input_value() == "":
            want = profile.get("location") or ""
            typed = profile.get("city") or (want.split(",")[0].strip() if want else "")
            if typed:
                _pick_typeahead(page, loc_in, typed, want, ["[role=option]", "[role=listbox] li"],
                                acc, "Current location")
            else:
                acc.miss("Current location")

        tmp = Path(tempfile.mkdtemp(prefix="pf_"))
        try:
            if not acc.resume_done:
                # unknown layout: let the generic pass find a resume input, but never
                # the 'Autofill from resume' box (it parses and overwrites fields)
                self._hide_autofill(page)
            total = fill_frame(page, profile, get_cover_letter, resume_path, acc, prefix="pfa", tmpdir=tmp)
        finally:
            self._unhide_autofill(page)
            shutil.rmtree(tmp, ignore_errors=True)

        self._yes_no(page, profile, acc)
        _sweep_required(page, acc, _ASHBY_ROW, "[class*=_required], [aria-required=true], [required]",
                        _ASHBY_TITLE)
        _clean_labels(acc)
        if not acc.resume_done:
            acc.miss("Resume (no upload field found)")
        if not acc.filled and total == 0:
            return FillResult("unsupported", [], acc.unfilled, notes + "No fillable Ashby fields found.")
        return FillResult(_status(acc), acc.filled, acc.unfilled, notes.strip())

    @staticmethod
    def _hide_autofill(page) -> None:
        # Temporarily mark the autofill box's file input as a non-file so the
        # generic scan skips it (scan skips type=button).
        try:
            page.evaluate("""() => document.querySelectorAll(
                '.ashby-application-form-autofill-input-root input[type=file]').forEach(el => {
                  el.setAttribute('data-pf-was-file', '1'); el.type = 'button'; })""")
        except Exception:
            pass

    @staticmethod
    def _unhide_autofill(page) -> None:
        try:
            page.evaluate("""() => document.querySelectorAll('[data-pf-was-file]').forEach(el => {
                  el.type = 'file'; el.removeAttribute('data-pf-was-file'); })""")
        except Exception:
            pass

    @staticmethod
    def _yes_no(page, profile: dict, acc: _Acc) -> None:
        """
        desc: Ashby renders Boolean questions as a pair of Yes/No buttons.
            Click one only when the question maps to a profile key with an
            explicit yes/no value; otherwise report it as unfilled.
        args:
            page (Page): form page.
            profile (dict): flat profile.
            acc (_Acc): accumulator.
        output:
            None.
        """
        try:
            groups = page.evaluate(_YESNO_JS, [_ASHBY_ROW, _ASHBY_TITLE])
        except Exception:
            return
        for g in groups:
            label = (g["question"] or "Yes/No question")[:80]
            if g["answered"]:
                continue
            key = classify_field({"type": "radio", "tag": "input", "label": g["question"], "placeholder": "",
                                  "name": "", "elid": "", "group": g["question"]})
            value = profile.get(key) if key else None
            choice = pick_option(["Yes", "No"], value) if value not in (None, "") else None
            if not choice:
                acc.miss(label)
                continue
            try:
                safe_click(page, "answer yes/no question", selector=f'[data-pf-yn="{g["id"]}-{choice.lower()}"]')
                acc.ok(label)
            except Exception:
                acc.miss(label)


LEVER_FILLER = LeverFiller()
ASHBY_FILLER = AshbyFiller()
FILLER = LEVER_FILLER  # registry entries 'lever' / 'ashby' import the specific instance

"""
Greenhouse filler: job-boards.greenhouse.io, boards.greenhouse.io and the EU
boards, plus custom-domain career pages that embed the Greenhouse form in an
iframe (URLs carrying ?gh_jid=..., e.g. akunacapital.com, optiver.com).

DOM (learned from public job-boards.greenhouse.io postings, 2026-10): the form
is <form id="application-form" method="get"> on the posting page itself (no
Apply click needed). Plain inputs carry stable ids (first_name, last_name,
email, phone) and autocomplete hints, so the generic helpers fill them. Most
other fields are react-select comboboxes (classNamePrefix "select"):
<input class="select__input" role="combobox" id="country|candidate-location|
school--0|degree--0|discipline--0|start-month--0|end-month--0|question_N|N"
aria-labelledby="<id>-label">, each followed by a hidden, visible-sized
<input aria-hidden tabindex=-1 class="*requiredInput"> shadow. Resume and
cover letter are <input type=file id="resume|cover_letter">. Multi-answer
questions are <fieldset class="checkbox"> with a <legend>.

This module adds react-select handling on top of generic.fill_frames: open a
menu with ArrowDown (or type a query for async lists like School / Location),
pick an option only on an exact or unique match, and click it with
safe_click. Never presses Enter, never clicks submit, never invents answers.
"""

import re
import time
from pathlib import Path

from applicator.prefill.base import Filler, FillResult, host_matches
from applicator.prefill.generic import _norm, classify_field, field_label, fill_frames, open_apply, pick_option
from applicator.prefill.safe import SubmitRefused, safe_click

_GH_HOSTS = ("job-boards.greenhouse.io", "boards.greenhouse.io",
             "job-boards.eu.greenhouse.io", "boards.eu.greenhouse.io")
_GH_JID_RE = re.compile(r"[?&#]gh_jid=\d+", re.I)

# keys that may be answered on a custom question_* combobox ONLY if the profile has them
_SENSITIVE = {"sponsorship", "work_authorization", "gender", "hispanic", "race", "veteran", "disability"}
_PLAIN_OK = {"gpa", "grad_year", "school", "degree", "major"}
# custom-question phrasing where a plain profile value would be the wrong answer
_CONDITIONAL_RE = re.compile(
    r"\bif (so|yes)\b|plan to|pursu|future|previous|prior|other than|scale|type of|kind of|"
    r"\bwhy\b|describe|explain", re.I)
# (id regex, profile key, async-search list?)
_STD_COMBOS = [
    (r"^country$", "country", False),
    (r"^candidate-location$", "location", True),
    (r"^school--0$", "school", True),
    (r"^degree--0$", "degree", False),
    (r"^discipline--0$", "major", False),
    (r"^end-month--0$", "grad_month", False),
]
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]
_DROPDOWN_SUFFIX = " (dropdown: choose manually)"

_FORM_JS = r"""
() => !!document.querySelector('#application-form, #application_form, form.application--form')
"""
_HAS_EMBED_JS = r"""
() => !!document.querySelector('#grnhse_app, iframe#grnhse_iframe, iframe[src*="greenhouse.io"]')
"""

# Temporarily disable react-select's hidden "required" shadow inputs so the
# generic scan does not report each as "(unlabeled field)". Restored after.
_MASK_JS = r"""
() => {
  let n = 0;
  for (const el of document.querySelectorAll('input[aria-hidden="true"][tabindex="-1"]:not([type=file])')) {
    if (el.id || el.disabled) continue;
    if (!/requiredInput/i.test(el.className || '') && !el.closest('.select, .select__container, .select-shell')) continue;
    el.disabled = true; el.setAttribute('data-pf-gh-masked', '1'); n++;
  }
  return n;
}
"""
_UNMASK_JS = r"""
() => { document.querySelectorAll('[data-pf-gh-masked]').forEach(el => {
  el.disabled = false; el.removeAttribute('data-pf-gh-masked'); }); }
"""

_COMBOS_JS = r"""
(prefix) => {
  const txt = e => ((e && (e.innerText || e.textContent)) || '').replace(/\s+/g, ' ').trim();
  const out = []; let n = 0;
  for (const el of document.querySelectorAll('input[role=combobox], input.select__input')) {
    if (el.disabled || el.readOnly) continue;
    const shell = el.closest('.select__container, .select-shell, .select') || el.parentElement;
    const r = shell.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const id = prefix + (n++);
    el.setAttribute('data-pf-gh', id);
    let label = '';
    const lb = el.getAttribute('aria-labelledby');
    if (lb) label = lb.split(/\s+/).map(i => txt(document.getElementById(i))).join(' ').trim();
    if (!label && el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) label = txt(l);
    }
    if (!label) label = el.getAttribute('aria-label') || '';
    const box = el.closest('.select__control') || shell;
    out.push({
      id, elid: el.id || '', label,
      required: el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label),
      has_value: !!box.querySelector('.select__single-value, .select__multi-value, [class*=singleValue], [class*=multiValue]'),
      multi: !!box.querySelector('.select__value-container--is-multi'),
    });
  }
  return out;
}
"""

_OPTIONS_JS = r"""
() => {
  document.querySelectorAll('[data-pf-opt]').forEach(o => o.removeAttribute('data-pf-opt'));
  const menu = document.querySelector('.select__menu, [class*=select__menu-list], [role=listbox]');
  if (!menu) return { open: false, loading: false, options: [] };
  const loading = !!document.querySelector('.select__menu-notice--loading, [class*=loadingMessage]');
  const opts = [...document.querySelectorAll('.select__menu .select__option, .select__menu [role=option], [role=listbox] [role=option]')];
  const seen = new Set(); const out = [];
  opts.forEach(o => { if (seen.has(o)) return; seen.add(o);
    if (o.getAttribute('aria-disabled') === 'true') return;
    o.setAttribute('data-pf-opt', String(out.length));
    out.push((o.innerText || o.textContent || '').replace(/\s+/g, ' ').trim()); });
  return { open: true, loading, options: out };
}
"""

_CHECKBOX_GROUPS_JS = r"""
() => {
  const txt = e => ((e && (e.innerText || e.textContent)) || '').replace(/\s+/g, ' ').trim();
  const out = [];
  for (const fs of document.querySelectorAll('fieldset')) {
    const boxes = [...fs.querySelectorAll('input[type=checkbox]')];
    if (boxes.length < 2) continue;
    const opts = boxes.map(b => { const l = b.labels && b.labels[0]; return l ? txt(l) : ''; });
    out.push({
      legend: txt(fs.querySelector('legend')),
      options: opts,
      required: fs.getAttribute('aria-required') === 'true' || boxes.some(b => b.required),
      checked: boxes.some(b => b.checked),
    });
  }
  return out;
}
"""


def is_greenhouse_url(url: str) -> bool:
    """
    desc: Whether a URL is a Greenhouse job board (US/EU) or a custom-domain
        page that embeds a Greenhouse application (carries ?gh_jid=).
    args:
        url (str): application URL.
    output:
        bool.
    """
    return host_matches(url, *_GH_HOSTS) or bool(_GH_JID_RE.search(url or ""))


def _safe_eval(frame, js, arg=None):
    try:
        return frame.evaluate(js, arg) if arg is not None else frame.evaluate(js)
    except Exception:
        return None


def gh_frames(page) -> list:
    """
    desc: Frames (main page and/or embedded iframes) that hold a Greenhouse
        application form.
    args:
        page (playwright.sync_api.Page): tab.
    output:
        list[Frame].
    """
    return [fr for fr in page.frames if _safe_eval(fr, _FORM_JS)]


def _wait_for_form(page, seconds: float) -> list:
    end = time.time() + seconds
    frames = gh_frames(page)
    while not frames and time.time() < end:
        time.sleep(0.3)
        frames = gh_frames(page)
    return frames


def choose_option(options: list, value) -> str | None:
    """
    desc: Pick a dropdown option for a profile value: an exact (normalised)
        match wins; otherwise accept only if exactly one option matches via
        generic.pick_option (containment / yes-no / degree level). Ambiguous
        means None -- the human chooses.
    args:
        options (list[str]): visible option texts.
        value (str): profile value.
    output:
        str | None: option text, or None.
    """
    v = _norm(str(value))
    if not v:
        return None
    for o in options:
        if _norm(o) == v:
            return o
    cands = [o for o in options if pick_option([o], value)]
    return cands[0] if len(cands) == 1 else None


def choose_location(options: list, value: str) -> str | None:
    """
    desc: Location options look like 'City, State, Country'. Accept an exact
        match, else options whose city segment equals the profile's city; if
        several remain and the profile gives a state, narrow by it. Ambiguous
        means None.
    args:
        options (list[str]): option texts.
        value (str): profile location, e.g. 'Faketown, ZZ'.
    output:
        str | None.
    """
    exact = next((o for o in options if _norm(o) == _norm(value)), None)
    if exact:
        return exact
    parts = [p.strip() for p in str(value).split(",") if p.strip()]
    if not parts:
        return None
    city = _norm(parts[0])
    cands = [o for o in options if _norm(o.split(",")[0]) == city]
    if len(cands) > 1 and len(parts) > 1:
        st = _norm(parts[1])
        cands = [o for o in cands if len(o.split(",")) > 1 and (
            _norm(o.split(",")[1]) == st or _norm(o.split(",")[1]).startswith(st))]
    return cands[0] if len(cands) == 1 else None


def _grad_month(profile: dict) -> str | None:
    m = re.search(r"[A-Za-z]{3,}", str(profile.get("grad_date") or ""))
    if not m:
        return None
    w = m.group(0).lower()
    return next((mo for mo in _MONTHS if mo.lower().startswith(w[:3])), None)


def _combo_key(d: dict) -> tuple:
    """(profile key | None, async?) for a scanned combobox."""
    for rx, key, is_async in _STD_COMBOS:
        if re.search(rx, d["elid"]):
            return key, is_async
    if re.search(r"--[1-9]\d*$", d["elid"]) or re.match(r"^(start|end)-", d["elid"]):
        return None, False  # extra education rows / start dates: human
    desc = {"type": "text", "tag": "input", "label": d["label"], "name": "", "elid": "",
            "placeholder": "", "autocomplete": "", "group": ""}
    key = classify_field(desc)
    if key in _SENSITIVE:
        return key, False
    lab = field_label(desc)
    if key in _PLAIN_OK and len(lab) <= 70 and not _CONDITIONAL_RE.search(lab):
        return key, key == "school"
    return None, False


def _combo_value(key: str, profile: dict):
    if key == "grad_month":
        return _grad_month(profile)
    if key == "location":
        return profile.get("location") or profile.get("city")
    return profile.get(key)


def _read_options(frame, wait_s: float) -> list:
    end = time.time() + wait_s
    while True:
        st = _safe_eval(frame, _OPTIONS_JS) or {"open": False, "loading": False, "options": []}
        if (st["options"] and not st["loading"]) or time.time() >= end:
            return st["options"]
        time.sleep(0.25)


def _close_menu(loc) -> None:
    try:
        loc.press("Escape")
        loc.evaluate("el => el.blur()")
    except Exception:
        pass


def select_combo(frame, d: dict, value, query: str | None = None, location: bool = False) -> bool:
    """
    desc: Choose an option in one react-select combobox without pressing
        Enter: open with ArrowDown (static lists) or type `query` (async
        lists), pick via choose_option / choose_location, click the option
        with safe_click, then verify a value was set.
    args:
        frame (Frame): frame holding the combobox.
        d (dict): descriptor from the combobox scan.
        value (str): profile value to match.
        query (str | None): text to type for async-search lists.
        location (bool): use the City, State matcher.
    output:
        bool: True if an option was selected.
    """
    pick = choose_location if location else choose_option
    loc = frame.locator(f'[data-pf-gh="{d["id"]}"]').first
    try:
        loc.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    loc.focus(timeout=2000)
    choice = None
    if not query:
        loc.press("ArrowDown")
        choice = pick(_read_options(frame, 1.5), value)
    else:
        loc.press_sequentially(str(query)[:60], delay=15)
        choice = pick(_read_options(frame, 6.0), value)
    if choice is None:
        _close_menu(loc)
        return False
    opts = _read_options(frame, 0.5)
    if choice not in opts:
        _close_menu(loc)
        return False
    try:
        safe_click(frame.locator(f'[data-pf-opt="{opts.index(choice)}"]').first, "choose dropdown option")
    except SubmitRefused:
        _close_menu(loc)
        return False
    time.sleep(0.15)
    ok = _safe_eval(frame, "(id) => { const el = document.querySelector('[data-pf-gh=\"' + id + '\"]');"
                           " const box = el && (el.closest('.select__control') || el.closest('.select__container'));"
                           " return !!(box && box.querySelector('.select__single-value, .select__multi-value,"
                           " [class*=singleValue], [class*=multiValue]')); }", d["id"])
    if not ok:
        _close_menu(loc)
    return bool(ok)


def _settle(acc, label: str) -> None:
    for x in (label, label + _DROPDOWN_SUFFIX):
        while x in acc.unfilled:
            acc.unfilled.remove(x)
    acc.ok(label)


def fill_combos(frame, profile: dict, acc, prefix: str = "gh") -> None:
    """
    desc: Fill the react-select comboboxes in one Greenhouse frame from
        explicit profile values; leave the rest for the human (required ones
        are reported in acc.unfilled).
    args:
        frame (Frame): Greenhouse form frame.
        profile (dict): flat profile.
        acc (generic._Acc): accumulator from generic.fill_frames.
        prefix (str): id prefix for tagging.
    output:
        None.
    """
    for d in _safe_eval(frame, _COMBOS_JS, prefix) or []:
        label = field_label(d)
        if d["has_value"]:
            continue
        key, is_async = _combo_key(d)
        value = _combo_value(key, profile) if key else None
        if d["multi"] or value in (None, ""):
            if d["required"] and (label + _DROPDOWN_SUFFIX) not in acc.unfilled:
                acc.miss(label)
            continue
        query = None
        if is_async:
            query = str(value).split(",")[0].strip() if key == "location" else str(value)
        try:
            ok = select_combo(frame, d, value, query=query, location=(key == "location"))
        except Exception:
            ok = False
        if ok:
            _settle(acc, label)
        elif (label + _DROPDOWN_SUFFIX) not in acc.unfilled:
            acc.miss(label)


def fill_education_dates(frame, profile: dict, acc) -> None:
    """
    desc: Fill 'End date year' of the first education row from grad_year
        (explicit profile value); start dates are left for the human.
    args:
        frame (Frame): Greenhouse form frame.
        profile (dict): flat profile.
        acc (generic._Acc): accumulator.
    output:
        None.
    """
    year = str(profile.get("grad_year") or "").strip()
    loc = frame.locator("#end-year--0")
    try:
        if not year or loc.count() == 0 or loc.first.input_value():
            return
        loc.first.fill(year, timeout=3000)
        loc.first.dispatch_event("change")
        _settle(acc, "End date year")
    except Exception:
        pass


def collapse_checkbox_groups(frame, acc) -> None:
    """
    desc: Replace per-option 'unfilled' entries from multi-checkbox
        fieldsets (generic reports each required checkbox) with the
        fieldset's question text.
    args:
        frame (Frame): Greenhouse form frame.
        acc (generic._Acc): accumulator.
    output:
        None.
    """
    for g in _safe_eval(frame, _CHECKBOX_GROUPS_JS) or []:
        names = {field_label({"label": o}) for o in g["options"] if o}
        before = len(acc.unfilled)
        acc.unfilled[:] = [u for u in acc.unfilled if u not in names]
        if (g["required"] or len(acc.unfilled) != before) and not g["checked"] and g["legend"]:
            acc.miss(field_label({"label": g["legend"]}))


def fill_greenhouse(page, profile: dict, get_cover_letter, resume_path: Path) -> FillResult:
    """
    desc: Pre-fill a Greenhouse application (board page or embedded iframe).
        Plain fields / files / native selects via generic.fill_frames, then
        react-select comboboxes, the education end year and checkbox-group
        reporting. Never submits.
    args:
        page (playwright.sync_api.Page): tab at the application URL.
        profile (dict): flat profile.
        get_cover_letter (Callable[[], str]): lazy cover-letter provider (see base.call_cover_letter).
        resume_path (Path): resume file.
    output:
        FillResult.
    """
    notes = []
    frames = _wait_for_form(page, 8.0 if _safe_eval(page, _HAS_EMBED_JS) else 4.0)
    if not frames and open_apply(page):
        notes.append("Opened application form via Apply.")
        frames = _wait_for_form(page, 10.0)
    for fr in frames:
        _safe_eval(fr, _MASK_JS)
    try:
        acc, total = fill_frames(page, profile, get_cover_letter, resume_path)
    finally:
        for fr in frames:
            _safe_eval(fr, _UNMASK_JS)
    for i, fr in enumerate(frames):
        fill_combos(fr, profile, acc, prefix=f"gh{i}_")
        fill_education_dates(fr, profile, acc)
        collapse_checkbox_groups(fr, acc)
    if not frames:
        notes.append("No Greenhouse form detected; generic heuristics only.")
        if not acc.filled and total == 0 and acc.file_inputs == 0:
            return FillResult("unsupported", [], [], " ".join(notes + ["No fillable fields found; fill manually."]))
    if acc.file_inputs == 0 and not acc.resume_done:
        acc.miss("Resume (no upload field found)")
    status = "filled" if acc.filled and not acc.unfilled else "partial"
    return FillResult(status, acc.filled, acc.unfilled, " ".join(notes))


class GreenhouseFiller(Filler):
    """Greenhouse job boards (US/EU) and gh_jid custom-domain embeds."""
    name = "greenhouse"

    def matches(self, url: str) -> bool:
        return is_greenhouse_url(url)

    def fill(self, page, profile: dict, get_cover_letter, resume_path: Path) -> FillResult:
        return fill_greenhouse(page, profile, get_cover_letter, resume_path)


FILLER = GreenhouseFiller()

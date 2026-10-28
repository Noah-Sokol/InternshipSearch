"""
Heuristic generic form filler. Enumerates inputs/selects/textareas/radios/file
inputs in the page and every iframe, classifies each by label / aria / name /
placeholder / autocomplete, and fills only what the profile explicitly has.
Never clicks submit, never invents answers. Reusable helpers (scan_frame,
classify_field, pick_option, fill_frames, open_apply) are public so ATS
fillers can build on them.
"""

import re
import shutil
import tempfile
import time
from pathlib import Path

from applicator.prefill import answers
from applicator.prefill.base import (
    Filler, FillResult, COVER_LETTER_FAILED, call_cover_letter,
)
from applicator.prefill.safe import safe_click

_SCAN_JS = r"""
(prefix) => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = e => ((e && (e.innerText || e.textContent)) || '').replace(/\s+/g, ' ').trim();
  const nInputs = p => p.querySelectorAll('input:not([type=hidden]), select, textarea').length;
  const labelOf = el => {
    const parts = [];
    const al = el.getAttribute('aria-label'); if (al) parts.push(al);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) lb.split(/\s+/).forEach(i => { const n = document.getElementById(i); if (n) parts.push(txt(n)); });
    if (!parts.length && el.labels) [...el.labels].forEach(l => parts.push(txt(l)));
    if (!parts.length) { const w = el.closest('label'); if (w) parts.push(txt(w)); }
    if (!parts.length) {
      let p = el.parentElement;
      for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
        if (nInputs(p) !== 1) continue;
        const l = p.querySelector('label, legend, [class*=label], [class*=Label]');
        if (l && !l.contains(el)) { const t = txt(l); if (t && t.length < 200) { parts.push(t); break; } }
      }
    }
    return parts.join(' ').trim();
  };
  const groupOf = el => {
    const fs = el.closest('fieldset, [role=radiogroup], [role=group]');
    if (fs) {
      const lg = fs.querySelector('legend'); if (lg) return txt(lg);
      const al = fs.getAttribute('aria-label'); if (al) return al;
      const lb = fs.getAttribute('aria-labelledby');
      if (lb) { const n = document.getElementById(lb.split(/\s+/)[0]); if (n) return txt(n); }
    }
    let p = el.parentElement;
    for (let i = 0; i < 5 && p; i++, p = p.parentElement) {
      const same = el.name ? p.querySelectorAll('input[name="' + CSS.escape(el.name) + '"]').length : 0;
      if (same > 1 && same === nInputs(p)) return ((p.innerText || '').split('\n')[0] || '').trim();
    }
    return '';
  };
  const out = []; let n = 0;
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const tag = el.tagName.toLowerCase();
    const type = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : tag;
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
    if (el.disabled || el.readOnly) continue;
    const choice = type === 'radio' || type === 'checkbox';
    if (type !== 'file' && !choice && !vis(el)) continue;
    const id = prefix + (n++);
    el.setAttribute('data-pf-id', id);
    const label = labelOf(el);
    out.push({
      id, tag, type, label,
      name: el.getAttribute('name') || '', elid: el.id || '',
      placeholder: el.getAttribute('placeholder') || '',
      autocomplete: el.getAttribute('autocomplete') || '',
      required: !!el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label),
      value: choice ? '' : (el.value || ''),
      checked: !!el.checked,
      combobox: el.getAttribute('role') === 'combobox' || !!el.getAttribute('aria-autocomplete'),
      options: tag === 'select' ? [...el.options].map(o => (o.text || '').trim()) : [],
      group: type === 'radio' ? groupOf(el) : '',
    });
  }
  return out;
}
"""

# (key, regex) for ordinary profile fields; first match wins.
_BASIC_RULES = [
    ("first_name", r"\bfirst name|\bgiven name|\bfname|\bfirst\b"),
    ("last_name", r"\blast name|\bfamily name|\bsurname|\blname|\blast\b"),
    ("email", r"e ?mail"),
    ("phone", r"phone|mobile|\bcell\b|\btel\b"),
    ("linkedin", r"linked ?in"),
    ("github", r"git ?hub"),
    ("website", r"website|portfolio|personal (site|url)"),
    ("gpa", r"\bgpa\b|grade point"),
    ("grad_year", r"graduat|grad year|class of|expected (year|date)"),
    ("school", r"\bschool\b|universit|college|institution"),
    ("degree", r"\bdegree\b|level of education"),
    ("major", r"\bmajor\b|field of study|discipline"),
    ("city", r"\bcity\b"),
    ("location", r"\blocation\b|current location|where are you (located|based)"),
    ("full_name", r"^(full |legal |your |candidate )?name$|\bfull name|\blegal name|\byour name"),
]
# Sensitive / eligibility keys: filled ONLY if the profile has an explicit value.
_SENSITIVE_RULES = [
    ("sponsorship", r"sponsor"),
    ("work_authorization", r"authori[sz]ed to work|legally (authori[sz]ed|eligible)|work authori[sz]ation|eligible to work|right to work"),
    ("gender", r"\bgender\b|\bsex\b"),
    ("hispanic", r"hispanic|latino"),
    ("race", r"\brace\b|ethnic"),
    ("veteran", r"veteran|military"),
    ("disability", r"disabilit"),
]
_AUTOCOMPLETE = {
    "given-name": "first_name", "family-name": "last_name", "email": "email",
    "tel": "phone", "name": "full_name",
}
_TEXT_TYPES = {"text", "email", "tel", "url", "number", "search", ""}
_COMPILED_BASIC = [(k, re.compile(r, re.I)) for k, r in _BASIC_RULES]
_COMPILED_SENS = [(k, re.compile(r, re.I)) for k, r in _SENSITIVE_RULES]
_EXCLUDE_NAME_RE = re.compile(r"company|employer|school|university|college|manager|reference|emergency|preferred", re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (s or "").lower())).strip()


def field_label(d: dict) -> str:
    """
    desc: Human-readable label for a scanned field (for reports; never a value).
    args:
        d (dict): descriptor from scan_frame.
    output:
        str.
    """
    lab = re.sub(r"\s*(\*|\(required\)|required)\s*$", "", d.get("label") or "", flags=re.I).strip()
    return (lab or d.get("placeholder") or d.get("name") or d.get("elid") or "(unlabeled field)")[:80]


def classify_field(d: dict) -> str | None:
    """
    desc: Map a scanned field descriptor to a profile key, or None.
    args:
        d (dict): descriptor from scan_frame.
    output:
        str | None: key such as 'email', 'sponsorship', 'cover_letter', 'resume'.
    """
    if d["type"] == "file":
        blob = _norm(" ".join([d["label"], d["name"], d["elid"]]))
        if "cover" in blob:
            return "cover_letter_file"
        if re.search(r"resume|\bcv\b|curriculum", blob) or not blob:
            return "resume"
        return None
    primary = _norm(" ".join([d["label"], d["placeholder"]]))
    secondary = _norm(" ".join([d["name"], d["elid"]]))
    group = _norm(d.get("group") or "")
    if d["tag"] == "textarea":
        if re.search(r"cover letter|letter of", primary + " " + secondary):
            return "cover_letter"
        return None
    for text in (group, primary, secondary):
        if not text:
            continue
        for key, rx in _COMPILED_SENS:
            if rx.search(text):
                return key
    if d["type"] in ("radio", "checkbox"):
        return None
    ac = _AUTOCOMPLETE.get((d.get("autocomplete") or "").lower())
    if ac:
        return ac
    for text in (primary, secondary):
        if not text or (text is primary and len(text) > 80):
            continue
        for key, rx in _COMPILED_BASIC:
            if rx.search(text):
                if key == "full_name" and _EXCLUDE_NAME_RE.search(text):
                    continue
                if key in ("first_name", "last_name") and text is secondary and not re.search(
                        r"name", text):
                    continue
                return key
    return None


def _yesno(v) -> str | None:
    s = str(v).strip().lower()
    if s in ("yes", "y", "true", "1"):
        return "yes"
    if s in ("no", "n", "false", "0"):
        return "no"
    return None


_DEGREE_WORDS = [("bachelor", r"bachelor|\bb\.? ?[as]\b|undergrad"), ("master", r"master|\bm\.? ?[as]\b"),
                 ("phd", r"ph\.? ?d|doctor"), ("associate", r"associate")]


def pick_option(options: list, value) -> str | None:
    """
    desc: Choose the option text best matching a profile value (exact,
        containment, yes/no, degree-level keyword). Never guesses beyond that.
    args:
        options (list[str]): option labels.
        value (str | bool): profile value.
    output:
        str | None: the option text, or None if no confident match.
    """
    real = [o for o in options if o and not re.match(r"^(select|choose|please|--)", o.strip(), re.I)]
    v = _norm(str(value))
    if not v:
        return None
    yn = _yesno(value)
    if yn:
        for o in real:
            if _norm(o).split(" ")[0] == yn:
                return o
    for o in real:
        if _norm(o) == v:
            return o
    for o in real:
        n = _norm(o)
        if n and (n in v or v in n) and len(n) > 1:
            return o
    for _, rx in _DEGREE_WORDS:
        if re.search(rx, str(value), re.I):
            for o in real:
                if re.search(rx, o, re.I):
                    return o
    return None


class _Acc:
    """Accumulates labels (never values) across frames."""

    def __init__(self):
        self.filled, self.unfilled = [], []
        self.resume_done = False
        self.cover_file_done = False
        self.file_inputs = 0

    def ok(self, label):
        if label not in self.filled:
            self.filled.append(label)

    def miss(self, label):
        if label not in self.unfilled and label not in self.filled:
            self.unfilled.append(label)


def scan_frame(frame, prefix: str = "pf") -> list:
    """
    desc: Enumerate fillable fields in one frame; tags elements with
        data-pf-id so they can be located via frame.locator.
    args:
        frame (Frame | Page): frame to scan.
        prefix (str): unique id prefix for this frame.
    output:
        list[dict]: field descriptors (label, type, options, ...).
    """
    try:
        return frame.evaluate(_SCAN_JS, prefix)
    except Exception:
        return []


def _loc(frame, d):
    return frame.locator(f'[data-pf-id="{d["id"]}"]').first


def _fill_text(frame, d, value, acc, label):
    loc = _loc(frame, d)
    loc.fill(str(value), timeout=3000)
    try:
        loc.dispatch_event("change")
    except Exception:
        pass
    acc.ok(label)


def _choose_radio(frame, members, value, acc, label):
    for m in members:
        if pick_option([m["label"]], value):
            loc = _loc(frame, m)
            try:
                loc.check(timeout=2000)
            except Exception:
                loc.evaluate("el => el.click()")
            acc.ok(label)
            return True
    return False


def fill_frame(frame, profile: dict, get_cover_letter, resume_path: Path, acc: _Acc,
               prefix: str = "pf", tmpdir: Path | None = None) -> int:
    """
    desc: Classify and fill every field in one frame. Records labels only.
    args:
        frame (Frame | Page): frame to fill.
        profile (dict): flat profile.
        get_cover_letter (Callable[[], str]): called only when a cover-letter
            field is found; returns the letter text ('' if unavailable).
        resume_path (Path): resume file.
        acc (_Acc): shared accumulator.
        prefix (str): id prefix for this frame.
        tmpdir (Path | None): where a cover-letter .txt is written if needed.
    output:
        int: number of text-like/select/textarea fields found (fillability signal).
    """
    fields = scan_frame(frame, prefix)
    radios: dict = {}
    for d in fields:
        label = field_label(d)
        try:
            if d["type"] == "radio":
                radios.setdefault(d["name"] or d["id"], []).append(d)
                continue
            if d["type"] == "checkbox":
                if d["required"] and not d["checked"]:
                    acc.miss(label)
                continue
            key = classify_field(d)
            if d["type"] == "file":
                acc.file_inputs += 1
                if key == "resume" and not acc.resume_done:
                    if Path(resume_path).exists():
                        _loc(frame, d).set_input_files(str(resume_path), timeout=5000)
                        acc.resume_done = True
                        acc.ok("Resume (file upload)")
                    else:
                        acc.miss("Resume (file missing)")
                elif key == "cover_letter_file":
                    if tmpdir is not None and not acc.cover_file_done:
                        letter = call_cover_letter(get_cover_letter)
                        if letter:
                            p = tmpdir / "cover_letter.txt"
                            p.write_text(letter, encoding="utf-8")
                            _loc(frame, d).set_input_files(str(p), timeout=5000)
                            acc.cover_file_done = True
                            acc.ok("Cover letter (file upload)")
                        else:
                            acc.miss(COVER_LETTER_FAILED)
                    else:
                        acc.miss(label)
                elif d["required"]:
                    acc.miss(label)
                continue
            if d["value"].strip() and d["tag"] != "select":
                continue  # never overwrite existing content
            if key == "cover_letter":
                letter = call_cover_letter(get_cover_letter)
                if letter:
                    _fill_text(frame, d, letter, acc, "Cover letter")
                else:
                    acc.miss(COVER_LETTER_FAILED)
                continue
            if key is None:
                # unclassified free text: try Noah's answer bank (pasted verbatim;
                # company-specific / why-us questions never match)
                if not d["combobox"] and (d["tag"] == "textarea" or (
                        d["tag"] == "input" and d["type"] in ("text", ""))):
                    question = d["label"] or d["placeholder"]
                    saved = answers.lookup(question)
                    if saved is not None:
                        _fill_text(frame, d, saved, acc, label)
                        continue
                    if d["required"]:
                        answers.note_unanswered(question)
                if d["required"]:
                    acc.miss(label)
                continue
            value = profile.get(key)
            if value in (None, ""):
                acc.miss(label)
                continue
            if d["combobox"]:
                acc.miss(label + " (dropdown: choose manually)")
            elif d["tag"] == "select":
                opt = pick_option(d["options"], value)
                if opt is None:
                    acc.miss(label)
                else:
                    _loc(frame, d).select_option(label=opt, timeout=3000)
                    acc.ok(label)
            elif d["tag"] == "input" and d["type"] in _TEXT_TYPES:
                _fill_text(frame, d, value, acc, label)
            else:
                acc.miss(label)
        except Exception:
            acc.miss(label)
    for members in radios.values():
        question = (members[0].get("group") or "").strip()
        label = (question or field_label(members[0]))[:80]
        if any(m["checked"] for m in members):
            continue
        key = classify_field({**members[0], "label": question or members[0]["label"]})
        value = profile.get(key) if key else None
        try:
            if value not in (None, "") and _choose_radio(frame, members, value, acc, label):
                continue
        except Exception:
            pass
        if key or members[0]["required"]:
            acc.miss(label)
    return sum(1 for d in fields if d["type"] not in ("file", "radio", "checkbox"))


_APPLY_RE = r"^\s*(apply( now| here| online)?|apply (for|to) this (job|position|role|internship))\s*$"
_FIND_APPLY_JS = r"""
(rx) => {
  const re = new RegExp(rx, 'i');
  for (const el of document.querySelectorAll('a, button, [role=button]')) {
    const t = (el.innerText || el.getAttribute('aria-label') || '').trim();
    const r = el.getBoundingClientRect();
    if (!re.test(t) || r.width === 0 || r.height === 0) continue;
    // an 'Apply' control inside a <form> may BE that form's submit (JS-driven,
    // invisible to the submit-text guard) -- never treat it as an opener.
    // Same for one inside a modal (modal apply flows: note box + 'Apply' = send).
    if (el.closest('form, dialog, [role=dialog], [role=alertdialog], [aria-modal="true"]')) continue;
    el.setAttribute('data-pf-apply', '1');
    const href = el.tagName === 'A' ? el.href : '';
    return { href: /^https?:/i.test(href) ? href : '' };
  }
  return null;
}
"""


def open_apply(page) -> bool:
    """
    desc: If the page is a posting with an 'Apply' button/link, open the
        application form (navigate to its http(s) href, else safe_click).
        Never touches submit-like controls.
    args:
        page (Page): posting page.
    output:
        bool: True if an Apply control was activated.
    """
    try:
        found = page.evaluate(_FIND_APPLY_JS, _APPLY_RE)
    except Exception:
        return False
    if not found:
        return False
    try:
        if found["href"]:
            page.goto(found["href"], wait_until="domcontentloaded", timeout=30000)
        else:
            safe_click(page, "open application form", selector="[data-pf-apply]")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        return True
    except Exception:
        return False


def _wait_for_fields(page, seconds: float = 4.0) -> None:
    try:
        page.wait_for_selector("input:visible, textarea:visible, select:visible", timeout=int(seconds * 1000))
    except Exception:
        pass
    time.sleep(0.4)


def fill_frames(page, profile: dict, get_cover_letter, resume_path: Path) -> tuple:
    """
    desc: Fill every frame of a page (main + iframes) with the generic heuristics.
    args:
        page (Page): page to fill.
        profile (dict): flat profile.
        get_cover_letter (Callable[[], str]): lazy cover-letter provider.
        resume_path (Path): resume file.
    output:
        tuple[_Acc, int]: accumulator (filled/unfilled labels) and total fillable field count.
    """
    acc, total = _Acc(), 0
    tmp = Path(tempfile.mkdtemp(prefix="pf_"))
    try:
        for i, fr in enumerate(page.frames):
            total += fill_frame(fr, profile, get_cover_letter, resume_path, acc, prefix=f"pf{i}_", tmpdir=tmp)
    finally:
        # set_input_files has already read the file; don't leave the letter on disk
        shutil.rmtree(tmp, ignore_errors=True)
    return acc, total


def fill_generic(page, profile: dict, get_cover_letter, resume_path: Path) -> FillResult:
    """
    desc: Generic pre-fill: wait for fields, open the form via 'Apply' if the
        page has none, then fill all frames. Never submits.
    args:
        page (Page): page at the application URL.
        profile (dict): flat profile.
        get_cover_letter (Callable[[], str]): lazy cover-letter provider.
        resume_path (Path): resume file.
    output:
        FillResult: partial if anything is left for the human.
    """
    _wait_for_fields(page)
    scanned = sum(len(scan_frame(fr, f"s{i}_")) for i, fr in enumerate(page.frames))
    notes = ""
    if scanned < 2 and open_apply(page):
        _wait_for_fields(page, 6.0)
        notes = "Opened application form via Apply. "
    acc, total = fill_frames(page, profile, get_cover_letter, resume_path)
    if not acc.filled and total == 0 and acc.file_inputs == 0:
        return FillResult("unsupported", [], [], notes + "No fillable form fields found; fill manually.")
    if acc.file_inputs == 0 and not acc.resume_done:
        acc.miss("Resume (no upload field found)")
    status = "filled" if acc.filled and not acc.unfilled else "partial"
    return FillResult(status, acc.filled, acc.unfilled, notes.strip())


class GenericFiller(Filler):
    """Fallback filler used when no ATS-specific filler matches."""
    name = "generic"

    def matches(self, url: str) -> bool:
        return True

    def fill(self, page, profile, get_cover_letter, resume_path) -> FillResult:
        return fill_generic(page, profile, get_cover_letter, resume_path)


FILLER = GenericFiller()

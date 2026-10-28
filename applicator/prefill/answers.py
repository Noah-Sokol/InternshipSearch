"""
Answer bank: Noah's own answers to recurring free-text application questions
(profile/answers.yaml, gitignored). A question that no profile rule classifies
is matched against each entry's regex patterns and, on a match, the saved
answer is pasted VERBATIM -- nothing is generated or paraphrased. Questions
that mention the posting's company, or that are "why us" style, are NEVER
filled: they are always left for Noah. Nothing here submits anything.

File format (a YAML list):
    - patterns: ["describe a (challenging )?project"]
      answer: "Noah's own text, pasted exactly."
"""

import re
from contextlib import contextmanager
from pathlib import Path

import yaml

import config

HEADER = """\
# Answer bank: Noah's own answers, pasted verbatim into matching free-text questions.
# Each entry: patterns (regexes, case-insensitive, searched in the question text)
# and answer (text pasted exactly as written). Questions that name the company or
# ask "why us" are never filled. Gitignored; edit here or on the Preferences page.
#
# Example:
# - patterns:
#     - "describe a (challenging )?project"
#   answer: |
#     Your own words here.
"""

# "Why us"-style questions that are always left to Noah.
_WHY_US_RES = [re.compile(p, re.I) for p in (
    r"\bwhy\b.*\b(us|this company|our)\b",
    r"\bwhy\b.*\b(do you want|would you like) to (work|join|intern)",
    r"\bwhy\b.*\b(are you )?interested in\b.*\b(us|our|this (company|firm|role|position|team))\b",
    r"\b(interest|interested) in (working )?(at |with |for )?(us|our|this (company|firm|organization))\b",
    r"\bwhat (interests|excites|attracts|draws) you (to|about)\b",
)]
_COMPANY_STOP = {"the", "federal", "american", "national", "united", "global", "first", "new"}
_LEGAL_SUFFIX = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|group|lp|plc)\b\.?", re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (s or "").lower())).strip()


def clean_question(text: str) -> str:
    """
    desc: Question text as matched: whitespace collapsed, trailing required
        markers removed.
    args:
        text (str): raw field label.
    output (str):
        cleaned text.
    """
    text = re.sub(r"\s+", " ", text or "").strip()
    return re.sub(r"\s*(\*|\(required\)|required)\s*$", "", text, flags=re.I).strip()


def company_variants(company: str) -> list:
    """
    desc: Normalized spellings of a company name a question might use: the
        full name, the name without legal suffixes, any parenthetical, and its
        first word (when long and distinctive).
    args:
        company (str): posting's company name.
    output (list[str]):
        normalized variants (may be empty).
    """
    company = company or ""
    out = set()
    paren = re.findall(r"\(([^)]*)\)", company)
    base = re.sub(r"\([^)]*\)", " ", company)
    for v in [base, _LEGAL_SUFFIX.sub(" ", base), *paren]:
        n = _norm(v)
        if len(n) >= 2:
            out.add(n)
    first = _norm(_LEGAL_SUFFIX.sub(" ", base)).split(" ")[0] if _norm(base) else ""
    if len(first) >= 4 and first not in _COMPANY_STOP:
        out.add(first)
    return sorted(out)


def is_company_specific(question: str, company: str) -> bool:
    """
    desc: Whether a question must never be auto-answered: it mentions the
        posting's company or matches a "why us" pattern.
    args:
        question (str): the question text.
        company (str): posting's company name.
    output (bool):
        True if the question must be left for Noah.
    """
    q = clean_question(question)
    if any(rx.search(q) for rx in _WHY_US_RES):
        return True
    nq = f" {_norm(q)} "
    return any(f" {v} " in nq for v in company_variants(company))


def load_answers(path: Path | None = None) -> list:
    """
    desc: Load the answer bank. Malformed entries are dropped.
    args:
        path (Path | None): YAML file; None uses config.ANSWERS_PATH.
    output (list[dict]):
        entries {"patterns": list[str], "answer": str}; [] if the file is
        missing or unreadable.
    """
    p = Path(path or config.ANSWERS_PATH)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError):
        return []
    out = []
    for e in data if isinstance(data, list) else []:
        if not isinstance(e, dict):
            continue
        pats = e.get("patterns")
        pats = [pats] if isinstance(pats, str) else pats
        ans = e.get("answer")
        if isinstance(pats, list) and isinstance(ans, str) and ans.strip():
            out.append({"patterns": [str(x) for x in pats if str(x).strip()], "answer": ans})
    return out


def save_answers(entries: list, path: Path | None = None) -> Path:
    """
    desc: Write the answer bank (header comment + YAML list). Answers are
        written exactly as given so they round-trip verbatim.
    args:
        entries (list[dict]): {"patterns": list[str], "answer": str}.
        path (Path | None): YAML file; None uses config.ANSWERS_PATH.
    output (Path):
        the written path.
    """
    p = Path(path or config.ANSWERS_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = [{"patterns": list(e["patterns"]), "answer": e["answer"]} for e in entries]
    body = yaml.safe_dump(clean, allow_unicode=True, sort_keys=False, width=10**6)
    p.write_text(HEADER + body, encoding="utf-8", newline="\n")
    return p


def validate_patterns(patterns: list) -> list:
    """
    desc: Check that every pattern compiles as a regex.
    args:
        patterns (list[str]): regex strings.
    output (list[str]):
        human-readable errors (empty if all are valid).
    """
    errs = []
    for pat in patterns:
        try:
            re.compile(pat, re.I)
        except re.error as e:
            errs.append(f"`{pat}`: {e}")
    return errs


def add_answer_for_question(question: str, answer: str, path: Path | None = None) -> bool:
    """
    desc: Append an entry whose pattern is the escaped literal of a question
        (used by the fill report's "Save an answer for this question").
    args:
        question (str): the question text from the report.
        answer (str): Noah's answer, stored verbatim.
        path (Path | None): YAML file; None uses config.ANSWERS_PATH.
    output (bool):
        True if saved (False for an empty question/answer).
    """
    q = clean_question(question)
    if not q or not (answer or "").strip():
        return False
    entries = load_answers(path)
    entries.append({"patterns": [re.escape(q)], "answer": answer})
    save_answers(entries, path)
    return True


def match_answer(question: str, company: str, entries: list) -> str | None:
    """
    desc: Find the saved answer for a question. Company-specific / why-us
        questions never match. First entry with a matching pattern wins;
        invalid patterns are skipped.
    args:
        question (str): the question text.
        company (str): posting's company name.
        entries (list[dict]): from load_answers.
    output (str | None):
        the answer text, verbatim, or None.
    """
    q = clean_question(question)
    if not q or is_company_specific(q, company):
        return None
    for e in entries:
        for pat in e["patterns"]:
            try:
                if re.search(pat, q, re.I):
                    return e["answer"]
            except re.error:
                continue
    return None


# --- per-tab context the generic filler consults -----------------------------
# Fillers have a fixed signature, so the runner opens a context around one
# tab's fill; outside a context lookup() never matches (nothing is filled).
_ctx: dict | None = None


@contextmanager
def answer_context(company: str, entries: list | None = None):
    """
    desc: Activate the answer bank for one tab's fill and collect the free-text
        questions left unanswered.
    args:
        company (str): posting's company name.
        entries (list[dict] | None): answer entries; None loads the file.
    output:
        yields dict with "unanswered" (list[str] of question texts).
    """
    global _ctx
    prev = _ctx
    _ctx = {"company": company or "",
            "entries": load_answers() if entries is None else entries,
            "unanswered": []}
    try:
        yield _ctx
    finally:
        _ctx = prev


def lookup(question: str) -> str | None:
    """
    desc: Saved answer for a question under the active context (None with no
        context, no match, or a company-specific question).
    args:
        question (str): the question text.
    output (str | None):
        verbatim answer or None.
    """
    if _ctx is None:
        return None
    return match_answer(question, _ctx["company"], _ctx["entries"])


def note_unanswered(question: str) -> None:
    """
    desc: Record a required free-text question nothing answered, so the fill
        report can offer "Save an answer for this question". Company-specific
        questions are not recorded (they are never auto-answered).
    args:
        question (str): the question text.
    output:
        None.
    """
    if _ctx is None:
        return
    q = clean_question(question)
    if q and not is_company_specific(q, _ctx["company"]) and q not in _ctx["unanswered"]:
        _ctx["unanswered"].append(q[:300])

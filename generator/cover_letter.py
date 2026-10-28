import json
import re
import yaml
import anthropic
from pathlib import Path
from datetime import datetime, timezone
from config import CLAUDE_MODEL, PROFILE_PATH

_client = None

_BASE_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "profile" / "cover_letter_base.md"
# Mad-libs blanks, not clauses. {{COMPANY}} and {{ROLE}} are filled directly
# from the job record -- no AI call, ever. {{COMPANY_THING}} / {{ROLE_TECH}}
# are OPTIONAL: if a template uses them, they're filled by extracting a
# verbatim phrase from the job posting itself (see _find_verbatim below) --
# Claude's job is to locate a quote, not compose one, and the code
# re-verifies that quote against the source text before trusting it. A
# template that doesn't contain either marker (like the current
# profile/cover_letter_base.md, Noah's real TikTok letter verbatim) skips
# the extraction call entirely -- no API call, no generated text at all,
# not even a verified quote. See profile/cover_letter_base.md's header
# comment and profile/cover_letter_voice_framework.md for why: any
# freshly-composed wording, even a few words, is something an AI-detector's
# classifier can key off of. A verified verbatim quote from the employer's
# own posting (or, failing that, the job title) never is -- but zero
# generated text beats even that.
_COMPANY_MARKER = "{{COMPANY}}"
_ROLE_MARKER = "{{ROLE}}"
_COMPANY_THING_MARKER = "{{COMPANY_THING}}"
_ROLE_TECH_MARKER = "{{ROLE_TECH}}"
# {{QUALIFICATION}} is a SELECTION marker, not a composition one: the AI call
# behind it never writes a word -- it only picks which one of Noah's own,
# already-written bullets from profile/noah.yaml gets spliced in verbatim.
# See _request_fields / _pick_qualification and profile/cover_letter_base.md's header for
# the full rule.
_QUALIFICATION_MARKER = "{{QUALIFICATION}}"
# Word cap per phrase (not per letter) -- each blank is a noun phrase, not a
# clause, so this is intentionally much tighter than a sentence-length cap
# would be.
_PHRASE_MAX_WORDS = 6
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _message_text(message) -> str:
    """
    desc: Safely pull the text out of a Claude response. message.content can
        come back empty (e.g. stop_reason="refusal", or any other case where
        the model produces no text block) -- content[0] would then raise
        IndexError. Callers treat "" the same as an explicit NONE/no-match
        reply and fall back rather than crashing.
    args:
        message: an anthropic Message object from client.messages.create().
    output (str):
        message.content[0].text.strip(), or "" if content is empty.
    """
    if not message.content:
        return ""
    return message.content[0].text.strip()


def _load_base_template() -> str:
    """
    desc: Load the fixed cover-letter skeleton from profile/cover_letter_base.md,
        stripping the explanatory HTML comment at the top so it never leaks into
        a generated letter. {{COMPANY}} is the only required marker -- {{ROLE}},
        {{COMPANY_THING}}, and {{ROLE_TECH}} are all optional; whichever of them
        the template actually contains get filled, so a fully-verbatim letter
        with no per-role tailoring markers at all is valid.
    args:
        (none)
    output (str):
        the template text with the comment removed, all markers intact.
    raises:
        FileNotFoundError: if profile/cover_letter_base.md is missing.
        ValueError: if the file is missing {{COMPANY}}.
    """
    if not _BASE_TEMPLATE_PATH.exists():
        raise FileNotFoundError(
            f"Cover letter base template not found at {_BASE_TEMPLATE_PATH}. "
            "Create it with at least a {{COMPANY}} marker before generating letters."
        )
    text = _HTML_COMMENT_RE.sub("", _BASE_TEMPLATE_PATH.read_text(encoding="utf-8")).strip()
    if _COMPANY_MARKER not in text:
        raise ValueError(f"{_BASE_TEMPLATE_PATH} is missing marker: {_COMPANY_MARKER}")
    return text


# Personal-fact markers: bare string substitution from profile/noah.yaml,
# zero AI, exactly the same tier as {{COMPANY}}/{{ROLE}}. These exist so a
# change to noah.yaml (a new GPA each semester, a class-year rollover, a
# phone/email change) flows into the letter automatically instead of
# needing a manual hand-edit of this file every time -- which is exactly
# how "3.85 GPA" silently went stale before it was caught and hand-fixed
# once. All optional: a template with none of these markers is untouched.
_GPA_MARKER = "{{GPA}}"
_DEGREE_MARKER = "{{DEGREE}}"
_SCHOOL_MARKER = "{{SCHOOL}}"
_PHONE_MARKER = "{{PHONE}}"
_EMAIL_MARKER = "{{EMAIL}}"
_YEAR_STANDING_MARKER = "{{YEAR_STANDING}}"

_MONTH_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_CLASS_LEVELS = ["freshman", "sophomore", "junior", "senior"]


def _parse_month_year(text: str) -> tuple[int, int]:
    """
    desc: Parse a noah.yaml date string like "Aug 2025" or "Sept 2020" into
        (month, year). Only the first three letters of the month are used,
        so both "Sept" and "Sep" (noah.yaml uses both spellings) resolve
        the same way.
    args:
        text (str): a "<Month> <Year>" string from noah.yaml.
    output (tuple[int, int]):
        (month, year) as integers.
    """
    parts = text.strip().split()
    month = _MONTH_ABBR[parts[0][:3].lower()]
    year = int(parts[-1])
    return month, year


def _class_standing(start_text: str) -> str:
    """
    desc: Compute Noah's current class standing (freshman/sophomore/junior/
        senior) from his current program's start date and today's date,
        rather than a hardcoded word that goes stale every fall. Assumes a
        new class year begins on the same month as `start_text`'s month
        each year (e.g. an August start rolls to the next class year every
        August), and clamps to "senior" past the fourth year rather than
        raising.
    args:
        start_text (str): noah.yaml's education[0]["start"], e.g. "Aug 2025".
    output (str):
        one of "freshman", "sophomore", "junior", "senior".
    """
    start_month, start_year = _parse_month_year(start_text)
    today = datetime.now(timezone.utc).replace(tzinfo=None)
    elapsed = today.year - start_year
    if (today.month, today.day) < (start_month, 1):
        elapsed -= 1
    index = max(0, min(elapsed, len(_CLASS_LEVELS) - 1))
    return _CLASS_LEVELS[index]


def _format_degree(degree_text: str) -> str:
    """
    desc: Reformat noah.yaml's "<Degree type>, <Major>" style (e.g.
        "Bachelor of Science, Data Science") into the letter's prose style
        ("Bachelor of Science in Data Science"). Falls back to the raw text
        unchanged if it isn't in the expected two-part comma form.
    args:
        degree_text (str): noah.yaml's education[0]["degree"].
    output (str):
        the reformatted degree string.
    """
    parts = [p.strip() for p in degree_text.split(",")]
    if len(parts) == 2:
        return f"{parts[0]} in {parts[1]}"
    return degree_text


def _load_profile() -> dict:
    """
    desc: Load profile/noah.yaml.
    args:
        (none)
    output (dict):
        the parsed yaml profile.
    """
    return yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))


def _personal_substitutions(profile: dict) -> dict[str, str]:
    """
    desc: Build the {marker: value} map for every personal-fact marker,
        sourced from noah.yaml's current, first (most recent) education
        entry and personal block -- no AI involvement at all.
    args:
        profile (dict): parsed profile/noah.yaml, as returned by
            _load_profile().
    output (dict[str, str]):
        marker string -> substitution value.
    """
    education = profile["education"][0]
    personal = profile["personal"]
    return {
        _GPA_MARKER: education["gpa"].split("/")[0],
        _DEGREE_MARKER: _format_degree(education["degree"]),
        _SCHOOL_MARKER: education["school"],
        _PHONE_MARKER: personal["phone"],
        _EMAIL_MARKER: personal["email"],
        _YEAR_STANDING_MARKER: _class_standing(education["start"]),
    }


def _load_qualification_candidates() -> list[str]:
    """
    desc: Load Noah's own, already-written bullets from profile/noah.yaml
        (projects, experience, extracurriculars) as a fixed, closed list of
        candidate sentences for the {{QUALIFICATION}} marker. Every string
        returned is copied verbatim from the yaml file -- nothing here is
        generated, reworded, or summarized.
    args:
        (none)
    output (list[str]):
        candidate bullet strings, in yaml order.
    raises:
        FileNotFoundError: if profile/noah.yaml is missing.
    """
    profile = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
    candidates: list[str] = []
    for section in ("projects", "experience", "extracurriculars"):
        for entry in profile.get(section, []):
            for bullet in entry.get("bullets", []):
                # A handful of bullets have a literal ": " in them (e.g.
                # "Project 1 (Fall 2025): Built an LLM..."), which YAML
                # parses as a one-key mapping rather than a plain string.
                # Re-joining key/value with ": " restores the exact original
                # text -- still verbatim, just undoing YAML's own parsing.
                if isinstance(bullet, dict):
                    candidates.append(
                        "; ".join(f"{k}: {v}" for k, v in bullet.items())
                    )
                else:
                    candidates.append(bullet)
    return candidates


# ONE structured call fills every AI-assisted blank at once. Rules per field,
# not voice rules -- Claude never writes any letter text:
#  - qualification_index SELECTS one item from a closed numbered list of Noah's
#    own already-written bullets (the code looks the index up and splices that
#    exact text; a bad/0 index falls back to the first remaining candidate).
#  - company_thing / role_tech LOCATE verbatim quotes in the posting (the code
#    re-verifies each against the source with _find_verbatim before use).
# Only the fields whose markers exist in the template are requested; a
# template with none of them makes zero calls.
_INSTRUCTIONS_HEADER = """
You are filling blanks in a cover letter by SELECTING and LOCATING -- never
writing, composing, rephrasing, or summarizing. Reply with a JSON object
containing exactly the fields requested below, and nothing else.
"""

_QUALIFICATION_INSTRUCTIONS = """
- qualification_index (integer): pick ONE item from the fixed numbered ITEMS
  list below. Every item is a sentence Noah Sokol already wrote about himself;
  choose the single number whose item best fits the role and company so it can
  be dropped in word for word. Use the item's number exactly as printed. If
  none are a reasonable fit, use 0 rather than forcing a bad match. Never
  output any wording that isn't an item number.

ITEMS:
{items}
"""

_QUOTE_RULES = """
- Quote fields: each must be {max_words} words or fewer, copied EXACTLY,
  word-for-word and consecutively, from the JOB DESCRIPTION. Do not
  paraphrase, reorder, fix grammar, or join words that aren't adjacent in the
  source. Do not start a quote with "a", "an", or "the". Each must work as a
  NOUN PHRASE dropped into "<Company>'s ___" -- it names a THING (a platform,
  a system, a focus area, a technology), not a verb phrase or clause.
  "Agentic AI Platform" and "fraud detection models" work; "operates its
  trading floor" or "has grown since 1989" do NOT. If nothing in the text forms
  a clean noun phrase for a field, use the string "NONE" for it.
"""
_COMPANY_THING_RULE = """- company_thing (string): a verbatim quote naming one concrete thing about the
  company or its product -- a mechanism, product name, specific technology, a
  team's focus, the company's stage/size. NOT an adjective-only phrase
  ("innovative approach"), NOT a claim about fit.
"""
_ROLE_TECH_RULE = """- role_tech (string): a different verbatim quote naming a specific technical
  focus or responsibility for this role -- a concrete task, system, or domain.
  Must not overlap with company_thing.
"""


def _build_schema(need_qual: bool, need_company_thing: bool, need_role_tech: bool) -> dict:
    """
    desc: Build the JSON schema for the single structured call, containing
        ONLY the fields whose markers the template actually uses.
    args:
        need_qual (bool): include qualification_index.
        need_company_thing (bool): include company_thing.
        need_role_tech (bool): include role_tech.
    output (dict):
        a JSON schema (object, all included fields required, no extras).
    """
    props: dict = {}
    if need_qual:
        props["qualification_index"] = {"type": "integer"}
    if need_company_thing:
        props["company_thing"] = {"type": "string"}
    if need_role_tech:
        props["role_tech"] = {"type": "string"}
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def _request_fields(job_title: str, company: str, source_text: str, extra_context: str,
                    candidates: list[str], need_qual: bool, need_company_thing: bool,
                    need_role_tech: bool) -> dict:
    """
    desc: Make the ONE messages.create call that returns every requested field
        as structured JSON (output_config json_schema). Raw output is never
        trusted: callers validate the index and re-verify quotes. An empty
        content list (e.g. refusal) or unparseable JSON yields {} so callers
        fall back instead of crashing; API errors propagate.
    args:
        job_title (str): the posting's title.
        company (str): the posting's company.
        source_text (str): the job description text (already truncated).
        extra_context (str): optional extra context appended to the prompt.
        candidates (list[str]): the (already exclusion-filtered) numbered
            qualification list; only used if need_qual.
        need_qual (bool): request qualification_index.
        need_company_thing (bool): request company_thing.
        need_role_tech (bool): request role_tech.
    output (dict):
        the parsed JSON object, or {} if unusable.
    """
    system_prompt = _INSTRUCTIONS_HEADER + "\nFIELDS:\n"
    if need_qual:
        numbered = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(candidates))
        system_prompt += _QUALIFICATION_INSTRUCTIONS.format(items=numbered)
    if need_company_thing:
        system_prompt += _COMPANY_THING_RULE
    if need_role_tech:
        system_prompt += _ROLE_TECH_RULE
    if need_company_thing or need_role_tech:
        system_prompt += _QUOTE_RULES.format(max_words=_PHRASE_MAX_WORDS)

    user_prompt = f"""Fill the requested fields for:

ROLE: {job_title} at {company}

JOB DESCRIPTION:
{source_text}

{f"ADDITIONAL CONTEXT: {extra_context}" if extra_context else ""}"""

    message = _get_client().messages.create(
        model=CLAUDE_MODEL,
        # headroom in case the model thinks before emitting the JSON
        max_tokens=1500,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
        output_config={"format": {
            "type": "json_schema",
            "schema": _build_schema(need_qual, need_company_thing, need_role_tech),
        }},
    )
    raw = _message_text(message)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _pick_qualification(data: dict, candidates: list[str]) -> str:
    """
    desc: Turn the model's qualification_index into a candidate's verbatim
        text. A missing, non-integer, 0, or out-of-range index falls back to
        the first remaining candidate -- text is never invented.
    args:
        data (dict): the parsed structured reply (may be {}).
        candidates (list[str]): the numbered candidate list that was sent.
    output (str):
        the chosen candidate, formatted with _format_qualification.
    """
    idx = data.get("qualification_index")
    if isinstance(idx, int) and not isinstance(idx, bool) and 1 <= idx <= len(candidates):
        return _format_qualification(candidates[idx - 1])
    return _format_qualification(candidates[0])


def get_qualification_candidates() -> list[str]:
    """
    desc: Public, formatted view of the {{QUALIFICATION}} candidate list
        (see _load_qualification_candidates), for callers outside this
        module -- e.g. the review UI, to figure out which candidate is
        already sitting in a displayed letter so Regenerate can exclude it
        and shuffle to a different one.
    args:
        (none)
    output (list[str]):
        candidate bullets, each passed through _format_qualification (so
        they match byte-for-byte what would actually appear in a letter).
    """
    return [_format_qualification(c) for c in _load_qualification_candidates()]


def _format_qualification(text: str) -> str:
    """
    desc: Light, purely mechanical cleanup so a resume-style bullet (which
        typically has no trailing period) reads as a complete sentence once
        spliced between two other sentences -- never rewords the bullet
        itself.
    args:
        text (str): a candidate bullet, verbatim from noah.yaml.
    output (str):
        the same text with surrounding whitespace stripped and exactly one
        terminal period/question mark/exclamation point.
    """
    text = text.strip().rstrip(";,: ").strip()
    if not text.endswith((".", "!", "?")):
        text += "."
    return text


_LEADING_ARTICLE_RE = re.compile(r"^(a|an|the)\s+", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def _find_verbatim(candidate: str, source_text: str) -> str | None:
    """
    desc: Verify that `candidate` actually appears in `source_text` (the exact
        job description text sent to the model), whitespace- and
        case-insensitively, and if so return the ORIGINAL-CASE substring pulled
        from `source_text` itself -- never the model's own retyped version, so
        the words that end up in the letter are provably copied from the
        posting rather than trusted from the model's transcription. Returns
        None if the candidate is empty, "NONE", or not actually found.
    args:
        candidate (str): the model's claimed verbatim quote.
        source_text (str): the exact job description text the model saw.
    output:
        str | None: the verified original-case substring, or None if it can't
            be verified.
    """
    candidate = _LEADING_ARTICLE_RE.sub("", candidate.strip())
    if not candidate or candidate.upper() == "NONE":
        return None

    # Build a normalized-whitespace version of the source, keeping a map back
    # to original character offsets, so a candidate that differs only in
    # spacing/newlines from the source still matches, but the returned text
    # is pulled from the untouched original (preserving real casing).
    normalized_chars = []
    offset_map = []  # normalized_chars[i] came from source_text[offset_map[i]]
    prev_was_space = True  # treat leading whitespace as already-collapsed
    for i, ch in enumerate(source_text):
        if ch.isspace():
            if not prev_was_space:
                normalized_chars.append(" ")
                offset_map.append(i)
            prev_was_space = True
        else:
            normalized_chars.append(ch)
            offset_map.append(i)
            prev_was_space = False
    normalized_source = "".join(normalized_chars)
    normalized_candidate = _WHITESPACE_RE.sub(" ", candidate).strip()
    if not normalized_candidate:
        return None

    pos = normalized_source.lower().find(normalized_candidate.lower())
    if pos == -1:
        return None

    start = offset_map[pos]
    end = offset_map[pos + len(normalized_candidate) - 1] + 1
    return source_text[start:end]


# Job descriptions are dominated by imperative-verb bullet points ("Build
# models...", "Design and deploy...", "Collaborate with..."), which are
# genuinely verbatim in the text but break the "<Company>'s ___" noun-phrase
# slot they'd be spliced into ("Figma's Build models... work"). The prompt
# already asks for a noun phrase, but observed real output still occasionally
# grabs a bullet starting with one of these -- this deterministic check
# catches that failure mode without a second API call.
_BULLET_VERB_STARTS = {
    "build", "design", "develop", "drive", "create", "manage", "lead",
    "collaborate", "support", "analyze", "implement", "deploy", "maintain",
    "own", "write", "perform", "conduct", "assist", "help", "work",
    "partner", "ensure", "provide", "improve", "optimize", "research",
    "test", "debug", "monitor", "communicate", "coordinate", "identify",
    "define", "execute", "deliver", "contribute", "participate", "utilize",
    "leverage", "apply", "review", "evaluate", "prepare", "present",
    "document", "troubleshoot", "translate", "partake", "predict",
    "forecast", "estimate", "detect", "generate", "reduce", "increase",
    "enhance", "streamline", "automate", "solve", "understand", "explore",
    "investigate", "measure", "track", "process", "extract", "transform",
    "clean", "collect", "gather", "produce", "publish", "champion",
    "advocate", "influence", "shape", "guide", "mentor", "train", "use",
    "gain", "learn", "explain", "handle", "manipulate", "integrate",
    "validate", "verify", "calculate", "compute", "derive", "interpret",
    "leading", "building", "developing", "designing", "creating",
    "managing", "driving", "collaborating", "supporting", "analyzing",
    "helping", "working", "partnering",
}


def _is_plausible_noun_phrase(phrase: str) -> bool:
    """
    desc: Reject a verified-verbatim phrase that starts with a common
        job-posting bullet-point imperative verb -- it's genuinely in the
        source text, but reads as a broken sentence fragment once spliced
        into "<Company>'s ___", e.g. "Figma's Build models... work".
    args:
        phrase (str): a phrase already confirmed verbatim by _find_verbatim.
    output (bool):
        False if the phrase's first word is a known bullet-verb starter,
        True otherwise.
    """
    first_word = phrase.split(" ", 1)[0].lower().strip(".,;:")
    return first_word not in _BULLET_VERB_STARTS


def generate(job_title: str, company: str, job_description: str,
             extra_context: str = "", exclude_qualifications: set[str] | None = None) -> str:
    """
    desc: Build a cover letter from the fixed base template (profile/
        cover_letter_base.md). {{COMPANY}} and {{ROLE}} are substituted
        directly from the `job_title`/`company` arguments with no AI call.
        {{GPA}}, {{DEGREE}}, {{SCHOOL}}, {{PHONE}}, {{EMAIL}}, and
        {{YEAR_STANDING}} are likewise substituted directly from
        profile/noah.yaml with no AI call -- see _personal_substitutions --
        so those facts stay in sync with noah.yaml automatically instead of
        needing a manual edit here every time Noah's GPA, class year, or
        contact info changes. {{QUALIFICATION}}, if present, triggers
        exactly one Claude call that
        SELECTS (never writes) one of Noah's own real bullets from
        profile/noah.yaml -- see _request_fields / _pick_qualification. {{COMPANY_THING}}
        and {{ROLE_TECH}} are separately OPTIONAL -- only if the template
        contains either one the same single call also asks Claude to locate (not
        compose) short verbatim quotes from the job description, each
        independently re-verified as an actual substring of that description
        before use (falling back to the job title, never unverified model
        output, if a candidate can't be verified). The current template
        (Noah's real TikTok letter, verbatim, plus one {{QUALIFICATION}}
        selection slot) never has Claude compose a single word of the
        letter -- every word is either Noah's own unedited writing (fixed
        template text, or a bullet chosen-not-written by Claude), the
        company name, or the job title.
    args:
        job_title (str): the posting's title.
        company (str): the posting's company.
        job_description (str): the posting's description text (truncated to
            3000 chars before being sent and before verification).
        extra_context (str): optional extra context appended to the prompt.
        exclude_qualifications (set[str] | None): formatted {{QUALIFICATION}}
            candidates (see get_qualification_candidates) to skip -- passed
            through to _request_fields so a repeat call (e.g. the
            review UI's Regenerate button) shuffles to a different real
            fact instead of picking the same one again.
    output (str):
        the full assembled cover letter.
    """
    template = _load_base_template()
    letter = template.replace(_COMPANY_MARKER, company).replace(_ROLE_MARKER, job_title)
    for marker, value in _personal_substitutions(_load_profile()).items():
        letter = letter.replace(marker, value)
    source_text = job_description[:3000]

    need_qual = _QUALIFICATION_MARKER in template
    need_company_thing = _COMPANY_THING_MARKER in template
    need_role_tech = _ROLE_TECH_MARKER in template
    if not (need_qual or need_company_thing or need_role_tech):
        return letter  # zero AI markers -> zero API calls

    candidates: list[str] = []
    if need_qual:
        candidates = _load_qualification_candidates()
        if not candidates:
            raise ValueError("No qualification candidates found in profile/noah.yaml.")
        if exclude_qualifications:
            remaining = [c for c in candidates
                         if _format_qualification(c) not in exclude_qualifications]
            if remaining:
                candidates = remaining

    data = _request_fields(job_title, company, source_text, extra_context, candidates,
                           need_qual, need_company_thing, need_role_tech)

    if need_qual:
        letter = letter.replace(_QUALIFICATION_MARKER, _pick_qualification(data, candidates))

    def _verified_or_fallback(candidate) -> str:
        if not isinstance(candidate, str):
            return job_title
        verified = _find_verbatim(candidate, source_text)
        if verified and _is_plausible_noun_phrase(verified):
            return verified
        return job_title

    if need_company_thing:
        letter = letter.replace(
            _COMPANY_THING_MARKER,
            _sanitize_phrase(_verified_or_fallback(data.get("company_thing"))))
    if need_role_tech:
        letter = letter.replace(
            _ROLE_TECH_MARKER,
            _sanitize_phrase(_verified_or_fallback(data.get("role_tech"))))
    return letter


# Deterministic backstop applied to whatever wins (verified verbatim quote or
# job_title fallback) -- strips wrapping quotes/dashes the source text might
# itself contain around the phrase, and enforces the word cap in case a
# verified quote or the job title itself runs long.
_EM_DASH_RE = re.compile(r"\s*[—–]\s*")  # em dash, en dash
_WRAPPING_QUOTES_RE = re.compile(r'^[\'"]+|[\'"]+$')


def _sanitize_phrase(text: str) -> str:
    """
    desc: Clean up whatever phrase won (a verified verbatim quote or the
        job_title fallback) before splicing it into the letter: strip em/en
        dashes and wrapping quotes the source text might contain around the
        phrase, strip trailing punctuation, and enforce the word cap.
    args:
        text (str): the phrase to clean up.
    output (str):
        the sanitized phrase.
    """
    text = _WHITESPACE_RE.sub(" ", text).strip()
    text = _EM_DASH_RE.sub(" ", text).strip()
    text = _WRAPPING_QUOTES_RE.sub("", text).strip()
    text = text.rstrip(".,;: ").strip()
    words = text.split()
    if len(words) > _PHRASE_MAX_WORDS:
        text = " ".join(words[:_PHRASE_MAX_WORDS])
    return text

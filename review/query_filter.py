"""
Boolean keyword filter for the review queue: AND / OR / NOT / BOTH, quoted
phrases, parentheses, implicit AND, and field-specific filters.

Field filter syntax:
  location=New York        (multi-word values work without quotes)
  location="New York"      (explicit quotes also fine)
  company=Google
  title=quant
  platform=linkedin

Field values end at the next AND/OR/NOT, another field=, a parenthesis, or
the end of the query. Put general keyword terms BEFORE field= filters to
avoid ambiguity, e.g.:  quant location=New York NOT banking

FILTER vs SEARCH, deliberately different precision: `location=` and
`platform=` are exact set-membership checks against the job's canonical
taxonomy.py classification (when a `facts` lookup is supplied to matches()) --
`location=NYC` matches a job stored as "New York, NY" because both
canonicalize to the same key, and `platform=ats` will NOT match a job stored
as "coverage-ats" (they're different platforms, not a substring coincidence).
`company=` and `title=` stay substring search -- there's no closed set of
company names or titles, so fuzzy matching there is intentional, not a bug.
Bare (non-field) TERM tokens are always fuzzy substring search over the full
haystack, also intentionally.
"""
import re

from taxonomy import Platform, canonicalize_location

# The leading `-?` on the first two alternatives keeps a negated field filter
# (e.g. -location="New York") as ONE token. Without it, the plain `\S+`
# fallback -- which doesn't know about quotes -- split on the internal space
# and produced two garbage tokens (`-location="New`, `York"`), silently
# breaking every `-field=value` query (see _not_expr / _parse_term_or_field).
_TOKEN_RE = re.compile(r'-?\w+="[^"]*"|-?\w+=[^\s()]+|"[^"]*"|\(|\)|[^\s()]+')
_AND_WORDS = {"and"}
_OR_WORDS  = {"or"}
_NOT_WORDS = {"not"}
_IGNORE_WORDS = {"both"}
_KNOWN_FIELDS = {"location", "company", "title", "platform"}

# Matches an unquoted field= assignment in the raw query string.
# Matches field= with optional surrounding spaces, not already quoted.
_UNQUOTED_FIELD_RE = re.compile(
    r'\b(location|company|title|platform)\s*=\s*(?!["\'])',
    re.IGNORECASE,
)
# Same but already has a quote — just normalise the spaces around =
_QUOTED_FIELD_RE = re.compile(
    r'\b(location|company|title|platform)\s*=\s*(["\'])',
    re.IGNORECASE,
)
# Zero-width lookahead for where an unquoted field value ends.
_VALUE_END_RE = re.compile(
    r'(?=\s+(?:AND|OR|NOT)\b|\s+\w+\s*=|[()]|\Z)',
    re.IGNORECASE,
)


def _normalise_field_eq(query: str) -> str:
    """Remove spaces around = for already-quoted values: location = "NY" → location="NY"."""
    return _QUOTED_FIELD_RE.sub(lambda m: f'{m.group(1)}={m.group(2)}', query)


def _auto_quote_fields(query: str) -> str:
    """
    Rewrite unquoted multi-word field values so the tokenizer sees them
    as a single token.
      location=New York NOT banking  →  location="New York" NOT banking
      company=Jane Street            →  company="Jane Street"
      location = NY                  →  location="NY"
    Values end at AND/OR/NOT, another field=, a paren, or end-of-string.
    """
    result = []
    pos = 0
    for m in _UNQUOTED_FIELD_RE.finditer(query):
        result.append(query[pos:m.start()])
        field = m.group(1)
        val_start = m.end()
        end_m = _VALUE_END_RE.search(query, val_start)
        val_end = end_m.start() if end_m else len(query)
        val = query[val_start:val_end].strip()
        result.append(f'{field}="{val}"')
        pos = val_end
    result.append(query[pos:])
    return "".join(result)


def _strip_quotes(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        return tok[1:-1]
    return tok


class _Parser:
    """Recursive-descent parser. Precedence (loosest→tightest): OR, AND, NOT."""

    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.pos = 0

    def _peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _advance(self):
        tok = self._peek()
        self.pos += 1
        return tok

    def parse(self):
        return self._or_expr()

    def _or_expr(self):
        node = self._and_expr()
        while (tok := self._peek()) and tok.lower() in _OR_WORDS:
            self._advance()
            node = ("OR", node, self._and_expr())
        return node

    def _and_expr(self):
        node = self._not_expr()
        while (tok := self._peek()) and tok != ")" and tok.lower() not in _OR_WORDS:
            if tok.lower() in _AND_WORDS:
                self._advance()
            node = ("AND", node, self._not_expr())
        return node

    def _not_expr(self):
        tok = self._peek()
        if tok and tok.lower() in _NOT_WORDS:
            self._advance()
            return ("NOT", self._not_expr())
        if tok and len(tok) > 1 and tok[0] == "-" and tok not in ("(", ")"):
            self._advance()
            # Route the stripped remainder through the same field-vs-term
            # check _atom() uses -- a bare TERM wrap here previously turned
            # -company=Google into a literal (never-matching) search for the
            # text `company="Google"` instead of NOT company=Google.
            return ("NOT", _parse_term_or_field(tok[1:]))
        return self._atom()

    def _atom(self):
        tok = self._advance()
        if tok == "(":
            node = self._or_expr()
            if self._peek() == ")":
                self._advance()
            return node
        return _parse_term_or_field(tok)


def _parse_term_or_field(tok: str) -> tuple:
    """
    desc: Classify a single token as a FIELD filter (field=value, where field
        is one of _KNOWN_FIELDS) or a plain TERM. Shared by _atom() and the
        leading-"-" NOT shorthand in _not_expr() so both paths recognize
        field filters the same way.
    args:
        tok (str): a single token, already stripped of any leading "-".
    output (tuple):
        ("FIELD", field_name, value) or ("TERM", value).
    """
    if "=" in tok:
        field, _, raw_val = tok.partition("=")
        if field.lower() in _KNOWN_FIELDS:
            return ("FIELD", field.lower(), _strip_quotes(raw_val))
    return ("TERM", _strip_quotes(tok))


def _evaluate_field(field: str, value: str, app: dict, facts) -> bool:
    """
    desc: Evaluate one field=value filter. location=/platform= are exact,
        canonical-set-membership checks (using `facts`, this app's
        classify.JobFacts, when supplied); company=/title= stay substring --
        see the module docstring for why that split is intentional.
    args:
        field (str): one of _KNOWN_FIELDS.
        value (str): the filter's right-hand-side text, already unquoted.
        app (dict): the application row (used for the substring fields, and
            as a fallback for location=/platform= if `facts` is None).
        facts: this app's classify.JobFacts, or None if not available (e.g.
            an older row whose job_facts hasn't been computed yet).
    output:
        (bool): whether the app satisfies this one field filter.
    """
    if field == "location":
        if facts is not None:
            query_key, _segments = canonicalize_location(value)
            # Membership in the FULL set of locations this posting spans --
            # not just its single "primary" canonical_location -- so a broad
            # multi-office req still matches on any one of its listed cities,
            # not only whichever alias happened to appear first in the raw
            # location text.
            return query_key in facts.all_locations
        # No facts to compare against -- fall back to substring on the raw
        # column rather than silently matching nothing.
        return value.lower() in str(app.get("location") or "").lower()

    if field == "platform":
        actual = str(app.get("platform") or "").strip().lower()
        target = Platform.resolve(value)
        return actual == (target.value if target is not None else value.strip().lower())

    # company= / title= -- intentionally fuzzy substring search.
    field_val = str(app.get(field) or "").lower()
    return value.lower() in field_val


def _evaluate(node, haystack_lower: str, app: dict, facts) -> bool:
    kind = node[0]
    if kind == "TERM":
        term = node[1].lower()
        return term in haystack_lower if term else True
    if kind == "FIELD":
        _, field, value = node
        return _evaluate_field(field, value, app, facts)
    if kind == "NOT":
        return not _evaluate(node[1], haystack_lower, app, facts)
    if kind == "AND":
        return _evaluate(node[1], haystack_lower, app, facts) and _evaluate(node[2], haystack_lower, app, facts)
    if kind == "OR":
        return _evaluate(node[1], haystack_lower, app, facts) or _evaluate(node[2], haystack_lower, app, facts)
    raise ValueError(f"unknown node kind: {kind}")


def matches(haystack: str, query: str, app: dict = None, facts=None) -> bool:
    """
    desc: Evaluate a boolean keyword query against a haystack string with
        optional field-specific filters (location=, company=, title=, platform=).
        Multi-word field values don't need quotes: location=New York works.
        Supports AND / OR / NOT / BOTH (case-insensitive), leading "-" as NOT,
        quoted phrases, parentheses, and implicit AND. Blank query matches
        everything; parse errors fail open so a typo never hides the queue.
    args:
        haystack (str): combined text (company + title + description etc.).
        query (str): e.g. 'quant location=New York NOT banking'.
        app (dict): the application row; required for field= filters.
        facts (classify.JobFacts | None): this app's canonical classification
            (see database/job_facts.py), used to make location=/platform=
            exact rather than substring. Callers that don't have it (or don't
            need field= precision) can omit it -- location=/platform= then
            fall back to substring on the raw app dict, same as before.
    output:
        bool: True if the app satisfies the query.
    """
    query = (query or "").strip()
    if not query:
        return True
    try:
        query = _normalise_field_eq(query)
        query = _auto_quote_fields(query)
        tokens = [t for t in _TOKEN_RE.findall(query) if t.lower() not in _IGNORE_WORDS]
        if not tokens:
            return True
        tree = _Parser(tokens).parse()
        return _evaluate(tree, haystack.lower(), app or {}, facts)
    except Exception:
        return True

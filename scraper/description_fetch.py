"""
Read-only job-description fetcher.

Given a posting URL, issue plain HTTP GETs (polite User-Agent, timeout, no
cookies kept, no logins, no clicks, no JavaScript) and pull out the job
description text. Extraction order:

  1. schema.org JobPosting JSON-LD ("description", "datePosted")
  2. ATS JSON endpoints that need no auth (Greenhouse, Lever, Workday)
  3. known ATS HTML containers (Greenhouse, Lever, Ashby, SmartRecruiters)
  4. a main-content heuristic (<main> / <article> / biggest text block)

When unsure the description is None, never junk: text shorter than
MIN_DESCRIPTION_CHARS, login walls, "enable JavaScript" shells, CAPTCHA and
access-denied pages are all rejected. Nothing here touches the database.
"""

import html as html_lib
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

MIN_DESCRIPTION_CHARS = 200        # shorter text is treated as "no description"
REQUEST_TIMEOUT_S = 15
MIN_INTERVAL_S = 1.0               # per-domain gap between requests
MAX_PAGE_BYTES = 3_000_000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 internship-applicator-personal/1.0"
)
# Sites that only show postings to a logged-in browser: never requested.
LOGIN_ONLY_DOMAINS = ("joinhandshake.com", "linkedin.com")

_BLOCK_PHRASES = re.compile(
    r"enable javascript|javascript (?:is )?(?:required|disabled)|you need to enable javascript"
    r"|sign in to (?:continue|view|see|apply)|log ?in to (?:continue|view|see|apply)"
    r"|please (?:sign|log) ?in|join to apply|verify you are (?:a )?human|are you a robot"
    r"|captcha|access denied|request blocked|unusual traffic|enable cookies",
    re.I,
)
_LOGIN_PATH_RE = re.compile(r"/(?:login|signin|sign-in|sso|authwall|uas/login)(?:[/?]|$)", re.I)
_NOISE_TAGS = ("script", "style", "noscript", "nav", "header", "footer", "aside", "form",
               "svg", "iframe", "template")

# (label, CSS selector) tried in order after JSON-LD / API.
_ATS_SELECTORS = (
    ("greenhouse", "#content .job__description, div.job__description, #app_body, "
                   "div#content[class*=job], div#main #content"),
    ("lever", ".posting-page .content, div.section-wrapper.page-full-width, "
              "[data-qa=job-description]"),
    ("ashby", "[class*=_descriptionText], [class*=descriptionText], "
              "div[class*=ashby-job-posting-description]"),
    ("smartrecruiters", "[itemprop=description], .job-sections, #openings-body, "
                        "section.job-description"),
    ("schema-microdata", "[itemprop=description]"),
)


@dataclass
class DescriptionResult:
    """
    desc: Outcome of one fetch.
    args:
        description (str | None): extracted plain text, None when unsure.
        date_posted (str): ISO-ish date from the page, "" when absent.
        closed (str): closed-posting reason ("HTTP 404", "closed-posting text"), "" if open.
        source (str): which extractor produced the text ("json-ld", "greenhouse-api", ...).
        error (str): short failure reason when description is None, else "".
    """
    description: str | None = None
    date_posted: str = ""
    closed: str = ""
    source: str = ""
    error: str = ""


class DomainThrottle:
    """
    desc: Enforces a minimum gap between requests to the same domain.
    args:
        min_interval (float): seconds between requests per domain.
        sleep (Callable): sleep function (injectable for tests).
        clock (Callable): monotonic clock (injectable for tests).
    """

    def __init__(self, min_interval: float = MIN_INTERVAL_S, sleep=time.sleep,
                 clock=time.monotonic):
        self.min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last: dict = {}

    def wait(self, domain: str) -> None:
        """
        desc: Block until at least min_interval has passed since the last
            request to this domain, then record now.
        args:
            domain (str): host name.
        output:
            None
        """
        last = self._last.get(domain)
        if last is not None:
            gap = self.min_interval - (self._clock() - last)
            if gap > 0:
                self._sleep(gap)
        self._last[domain] = self._clock()


DEFAULT_THROTTLE = DomainThrottle()


def make_session() -> requests.Session:
    """
    desc: A cookie-less session with the polite UA (cookies are cleared after
        every response by _get, so no state carries between postings).
    args: none
    output:
        requests.Session: new session.
    """
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/json;q=0.9,*/*;q=0.5"})
    return s


def html_to_text(markup: str) -> str:
    """
    desc: Strip HTML to plain text (block tags become spaces); unescapes
        entity-encoded HTML first (Greenhouse/JSON-LD often ship it escaped).
    args:
        markup (str): HTML or text.
    output:
        str: whitespace-collapsed text.
    """
    if not markup:
        return ""
    if "<" not in markup and "&lt;" in markup:
        markup = html_lib.unescape(markup)
    soup = BeautifulSoup(markup, "html.parser")
    for t in soup(_NOISE_TAGS):
        t.decompose()
    for br in soup.find_all(["br", "li", "p", "div", "h1", "h2", "h3", "h4"]):
        br.append(" ")
    text = soup.get_text(" ")
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def looks_blocked(text: str) -> bool:
    """
    desc: Whether extracted text is a login wall / JS-required shell / CAPTCHA
        rather than a posting (phrase in the first 600 characters).
    args:
        text (str): extracted page text.
    output:
        bool: True if it looks like junk.
    """
    return bool(_BLOCK_PHRASES.search((text or "")[:600]))


def _accept(text: str, trust_blocks: bool = False) -> str | None:
    text = (text or "").strip()
    if len(text) < MIN_DESCRIPTION_CHARS:
        return None
    if not trust_blocks and looks_blocked(text):
        return None
    return text


def _iter_jsonld(soup) -> list:
    out = []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                stack.extend(node.get("@graph", []) if isinstance(node.get("@graph"), list) else [])
                out.append(node)
    return out


def extract_jsonld(soup) -> tuple:
    """
    desc: Pull description and datePosted from a schema.org JobPosting block.
    args:
        soup (BeautifulSoup): parsed page.
    output:
        tuple[str | None, str]: (description text or None, date_posted or "").
    """
    for node in _iter_jsonld(soup):
        kind = node.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if "JobPosting" not in kinds:
            continue
        date = str(node.get("datePosted") or "")[:32]
        desc = _accept(html_to_text(str(node.get("description") or "")), trust_blocks=True)
        if desc:
            return desc, date
        return None, date
    return None, ""


def _find_date(soup) -> str:
    for node in _iter_jsonld(soup):
        if node.get("datePosted"):
            return str(node["datePosted"])[:32]
    meta = soup.find("meta", attrs={"itemprop": "datePosted"}) or \
        soup.find("meta", attrs={"property": "article:published_time"})
    return (meta.get("content") or "")[:32] if meta else ""


def extract_ats_container(soup) -> tuple:
    """
    desc: Try known ATS description containers (Greenhouse, Lever, Ashby,
        SmartRecruiters, microdata).
    args:
        soup (BeautifulSoup): parsed page.
    output:
        tuple[str | None, str]: (text, extractor label) or (None, "").
    """
    for label, selector in _ATS_SELECTORS:
        try:
            nodes = soup.select(selector)
        except Exception:
            continue
        for node in nodes:
            text = _accept(html_to_text(str(node)))
            if text:
                return text, label
    return None, ""


def extract_heuristic(soup) -> str | None:
    """
    desc: Main-content fallback: <main>/<article>/[role=main], else the div or
        section with the most text. Rejects login walls and short text.
    args:
        soup (BeautifulSoup): parsed page.
    output:
        str | None: text, or None when nothing credible is found.
    """
    candidates = soup.select("main, article, [role=main]")
    if not candidates:
        blocks = soup.find_all(["div", "section"])
        blocks.sort(key=lambda n: len(n.get_text(" ", strip=True)), reverse=True)
        candidates = blocks[:3]
    for node in candidates:
        text = _accept(html_to_text(str(node)))
        if text:
            return text
    return None


def extract_description(markup: str) -> tuple:
    """
    desc: Pure extraction from page HTML (no network): JSON-LD, then ATS
        containers, then the heuristic.
    args:
        markup (str): page HTML.
    output:
        tuple[str | None, str, str]: (description or None, date_posted, source).
    """
    soup = BeautifulSoup(markup or "", "html.parser")
    desc, date = extract_jsonld(soup)
    if desc:
        return desc, date, "json-ld"
    date = date or _find_date(soup)
    text, label = extract_ats_container(soup)
    if text:
        return text, date, label
    text = extract_heuristic(soup)
    if text:
        return text, date, "heuristic"
    return None, date, ""


def api_url_for(url: str) -> tuple:
    """
    desc: Map a posting URL to a trivially available no-auth JSON endpoint.
    args:
        url (str): posting URL.
    output:
        tuple[str, str] | None: (kind, api_url) with kind in greenhouse/lever/workday, or None.
    """
    p = urlparse(url)
    host = (p.hostname or "").lower()
    parts = [x for x in p.path.split("/") if x]
    if host.endswith("greenhouse.io") and "jobs" in parts:
        i = parts.index("jobs")
        if i >= 1 and i + 1 < len(parts) and parts[i + 1].isdigit() and host != "boards-api.greenhouse.io":
            return "greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{parts[i - 1]}/jobs/{parts[i + 1]}"
    if host == "jobs.lever.co" and len(parts) >= 2:
        return "lever", f"https://api.lever.co/v0/postings/{parts[0]}/{parts[1]}"
    if host.endswith(".myworkdayjobs.com") and "job" in parts:
        i = parts.index("job")
        tenant = host.split(".")[0]
        site_parts = parts[:i]
        if site_parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", site_parts[0]):
            site_parts = site_parts[1:]
        if site_parts:
            rest = "/".join(parts[i:])
            return "workday", f"https://{host}/wday/cxs/{tenant}/{site_parts[0]}/{rest}"
    return None


def _parse_api(kind: str, data: dict) -> tuple:
    """(text, date) from an ATS JSON payload."""
    if kind == "greenhouse":
        text = html_to_text(html_lib.unescape(str(data.get("content") or "")))
        date = str(data.get("first_published") or "")[:10]
    elif kind == "lever":
        text = str(data.get("descriptionPlain") or "") or html_to_text(str(data.get("description") or ""))
        extra = " ".join(str(item.get("text", "")) + " " + html_to_text(str(item.get("content", "")))
                         for item in data.get("lists") or [] if isinstance(item, dict))
        text = f"{text} {extra}".strip()
        ts = data.get("createdAt")
        date = ""
        if isinstance(ts, (int, float)):
            from datetime import datetime, timezone
            date = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    else:  # workday
        info = data.get("jobPostingInfo") or {}
        text = html_to_text(str(info.get("jobDescription") or ""))
        date = str(info.get("startDate") or "")[:10]
    return re.sub(r"\s+", " ", text).strip(), date


def _get(session, url: str, throttle: DomainThrottle):
    """GET with per-domain throttle; returns (response | None, error)."""
    domain = (urlparse(url).hostname or "").lower()
    throttle.wait(domain)
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT_S, allow_redirects=True)
    except requests.RequestException as e:
        return None, f"{type(e).__name__}"
    try:
        session.cookies.clear()     # no cookies carried between requests
    except Exception:
        pass
    return resp, ""


_BARE_AUTH_RE = re.compile(r"\b(?:sign|log) ?in\b|\bjoin now\b|\bcreate (?:an )?account\b", re.I)


def _redirected_away(requested: str, final: str) -> bool:
    """
    desc: Whether a redirect landed on a different host or path than requested.
    args:
        requested (str): URL asked for.
        final (str): URL after redirects.
    output:
        bool: True when host or path (ignoring query/trailing slash/www) differ.
    """
    a, b = urlparse(requested), urlparse(final)
    host = lambda u: (u.hostname or "").lower().removeprefix("www.")
    path = lambda u: (u.path or "").rstrip("/").lower()
    return host(a) != host(b) or path(a) != path(b)


def fetch_description(url: str, session=None, throttle: DomainThrottle = None) -> DescriptionResult:
    """
    desc: Fetch one posting page (read-only GETs) and extract its description.
    args:
        url (str): posting URL.
        session (requests.Session | None): injectable for tests; default make_session().
        throttle (DomainThrottle | None): per-domain rate limiter; default shared one.
    output:
        DescriptionResult: description is None (with .error set) when unsure;
        .closed is set whenever the page says the posting is gone.
    """
    from applicator.prefill.base import closed_reason   # shared closed-posting wording
    throttle = throttle or DEFAULT_THROTTLE
    scheme_host = urlparse(url or "")
    if scheme_host.scheme not in ("http", "https") or not scheme_host.hostname:
        return DescriptionResult(error="no usable url")
    host = scheme_host.hostname.lower()
    if any(host == d or host.endswith("." + d) for d in LOGIN_ONLY_DOMAINS):
        return DescriptionResult(error="login-only site (skipped)")
    session = session or make_session()

    api = api_url_for(url)
    if api:
        resp, err = _get(session, api[1], throttle)
        if resp is not None and resp.status_code == 200:
            try:
                text, date = _parse_api(api[0], resp.json())
            except ValueError:
                text, date = "", ""
            text = _accept(text, trust_blocks=True)
            if text:
                return DescriptionResult(text, date, "", f"{api[0]}-api")
        elif resp is not None and resp.status_code in (404, 410):
            return DescriptionResult(closed=f"HTTP {resp.status_code}", error=f"HTTP {resp.status_code}")
        elif resp is not None and resp.status_code == 429:
            return DescriptionResult(error="HTTP 429")   # rate-limited: don't ask the same host again
        # anything else: fall through to the HTML page

    resp, err = _get(session, url, throttle)
    if resp is None:
        return DescriptionResult(error=err)
    status = resp.status_code
    if status in (404, 410):
        return DescriptionResult(closed=f"HTTP {status}", error=f"HTTP {status}")
    if status != 200:
        return DescriptionResult(error=f"HTTP {status}")
    if _LOGIN_PATH_RE.search(urlparse(str(getattr(resp, "url", "") or url)).path or ""):
        return DescriptionResult(error="login wall (redirected)")
    ctype = (resp.headers.get("Content-Type") or "text/html").lower()
    if "html" not in ctype and "xml" not in ctype:
        return DescriptionResult(error=f"not a web page ({ctype.split(';')[0]})")
    markup = (resp.text or "")[:MAX_PAGE_BYTES]

    soup = BeautifulSoup(markup, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    head = html_to_text(str(soup.body or soup))[:400]   # closed wording must be near the top
    closed = closed_reason(f"{title} {head}", status)

    desc, date, source = extract_description(markup)
    # The heuristic is the only source that can pick up a non-posting page:
    # don't trust it after a redirect elsewhere (closed postings often bounce
    # to the careers homepage) or when the page opens with a sign-in prompt.
    if desc is not None and source == "heuristic":
        final_url = str(getattr(resp, "url", "") or url)
        if _redirected_away(url, final_url):
            return DescriptionResult(date_posted=date, closed=closed,
                                     error="redirected away from posting")
        if _BARE_AUTH_RE.search(desc[:200]):
            return DescriptionResult(date_posted=date, closed=closed,
                                     error="login wall / JavaScript-only page")
    if desc is None:
        raw = re.sub(r"\s+", " ", (soup.body or soup).get_text(" ")).strip()   # keeps <noscript>
        shell = looks_blocked(f"{title} {head}") or looks_blocked(raw)
        return DescriptionResult(date_posted=date, closed=closed,
                                 error="login wall / JavaScript-only page" if shell
                                 else "no description found")
    return DescriptionResult(desc, date, closed, source)

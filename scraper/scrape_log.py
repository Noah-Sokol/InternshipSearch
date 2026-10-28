"""
Succinct log for the weekly unattended scrape (main.py --unattended).

One summary line per run, optionally followed by one short line per source
error:

    2026-11-01 09:00  ok  12 new · 340 re-seen · 5 now possibly closed · 9 descriptions · 3m42s
      ! linkedin-guest: TimeoutError: request timed out

Nothing else goes in: no stack traces, no posting contents, no personal data.
"""

import re
from datetime import datetime
from pathlib import Path

import config

_SUMMARY_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})\s+(ok|warn|fail)\s+(\d+) new"
)
_MAX_ERR_CHARS = 120


def format_duration(seconds: float) -> str:
    """
    desc: Render a run time as XmYs.
    args:
        seconds (float): elapsed seconds.
    output:
        str: e.g. "3m42s".
    """
    total = max(0, int(round(seconds)))
    return f"{total // 60}m{total % 60:02d}s"


def short_error(source: str, exc: BaseException) -> str:
    """
    desc: One-line, no-traceback description of a source failure, truncated.
    args:
        source (str): source name, e.g. "ats".
        exc (BaseException): the exception that stopped it.
    output:
        str: "<source>: <ExceptionType>: <message>", single line.
    """
    msg = " ".join(str(exc).split())
    # keep local paths / URLs (and the username in them) out of the log
    msg = re.sub(r"https?://[^\s'\"]+", "<url>", msg)
    msg = re.sub(r"[A-Za-z]:[\\/][^\s'\"]*", "<path>", msg)
    text = f"{source}: {type(exc).__name__}" + (f": {msg}" if msg else "")
    return text[:_MAX_ERR_CHARS]


def append_run(status: str, new: int, reseen: int, closed: int, seconds: float,
               errors: list, when: datetime = None, path: Path = None,
               descriptions: int = 0) -> str:
    """
    desc: Append one run's summary line (plus one line per source error) to the
        scrape log, creating it if needed.
    args:
        status (str): "ok", "warn" or "fail".
        new (int): newly staged postings.
        reseen (int): postings found again that were already tracked.
        closed (int): active postings now past config.STALE_DAYS unseen.
        seconds (float): run duration.
        errors (list[str]): short error strings, one per failed source.
        when (datetime | None): timestamp; defaults to local now.
        path (Path | None): log path; defaults to config.SCRAPE_LOG_PATH.
        descriptions (int): missing descriptions filled for this run's new postings.
    output:
        str: the summary line that was written.
    """
    when = when or datetime.now()
    path = Path(path or config.SCRAPE_LOG_PATH)
    line = (f"{when:%Y-%m-%d %H:%M}  {status}  {new} new · {reseen} re-seen · "
            f"{closed} now possibly closed · {descriptions} descriptions · "
            f"{format_duration(seconds)}")
    lines = [line] + [f"  ! {e[:_MAX_ERR_CHARS]}" for e in errors]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return line


def read_last_run(path: Path = None):
    """
    desc: Parse the most recent summary line of the scrape log.
    args:
        path (Path | None): log path; defaults to config.SCRAPE_LOG_PATH.
    output:
        dict | None: {"date": datetime, "status": str, "new": int}, or None if
            the log is missing, unreadable or has no summary line.
    """
    try:
        text = Path(path or config.SCRAPE_LOG_PATH).read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in reversed(text.splitlines()):
        m = _SUMMARY_RE.match(raw)
        if m:
            try:
                when = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
            except ValueError:
                return None
            return {"date": when, "status": m.group(3), "new": int(m.group(4))}
    return None


def sidebar_line(path: Path = None) -> str:
    """
    desc: Text for the review sidebar's "last scrape" line.
    args:
        path (Path | None): log path; defaults to config.SCRAPE_LOG_PATH.
    output:
        str: "Last scrape: Nov 01 · 12 new · ✓" (⚠ for warn/fail), or
            "Last scrape: none yet" when there is no log.
    """
    run = read_last_run(path)
    if run is None:
        return "Last scrape: none yet"
    mark = "✓" if run["status"] == "ok" else "⚠"
    return f"Last scrape: {run['date']:%b %d} · {run['new']} new · {mark}"

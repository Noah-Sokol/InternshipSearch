"""
Per-source request counters for the unattended scrape.

The fetch helpers swallow every failure (a dead board must not abort the
run), so a fully offline or blocked run would otherwise look like "0 new".
Helpers call record(ok) after each real network request (cache hits are not
counted); scrape_unattended calls begin(source) before each source and
failed_all(source) after it.
"""

_STATS: dict = {}
_CURRENT = {"source": ""}


def begin(source: str) -> None:
    """
    desc: Start counting requests for a source (resets its counters).
    args:
        source (str): source name, e.g. "ats".
    output:
        None
    """
    _CURRENT["source"] = source
    _STATS[source] = [0, 0]


def record(ok: bool) -> None:
    """
    desc: Count one real network request against the current source.
    args:
        ok (bool): True for an HTTP 200 that parsed, False for any failure.
    output:
        None
    """
    stats = _STATS.get(_CURRENT["source"])
    if stats is not None:
        stats[0] += 1
        stats[1] += 0 if ok else 1


def counts(source: str) -> tuple:
    """
    desc: Requests counted for a source.
    args:
        source (str): source name.
    output:
        tuple[int, int]: (attempted, failed).
    """
    attempted, failed = _STATS.get(source, [0, 0])
    return attempted, failed


def mostly_failed(source: str) -> bool:
    """
    desc: True when a source made requests and more than half failed.
    args:
        source (str): source name.
    output:
        bool: whether the source looks dead (offline, blocked, rate-limited).
    """
    attempted, failed = counts(source)
    return attempted > 0 and failed * 2 > attempted

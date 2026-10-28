"""
Duplicate detection for the internship application queue.

Rules:
- Duplicate = same normalized company + same normalized title + compatible location
- Location compatible means: both blank, both same city, or one is blank (unknown)
- Different specific cities (NYC vs Chicago) = separate roles, NOT duplicates
- Team/department suffixes in titles are preserved -- different teams = not duplicates
- Runs across all statuses (staged + queued)
"""
import re
from collections import defaultdict

from taxonomy import canonicalize_location, normalize_company_name, LOCATION_UNKNOWN


def normalize_company(name: str) -> str:
    """Canonical company key for dedup grouping -- delegates to
    taxonomy.normalize_company_name(), the one definition of "the same
    company" shared with any other consumer that needs it."""
    return normalize_company_name(name)


def normalize_title(title: str) -> str:
    t = (title or "").lower().strip()
    # Strip year/season references — "Summer 2027", "2027", "(2027 Summer)"
    t = re.sub(r"\(?(?:summer|fall|spring|winter)?\s*20\d{2}\)?\b", "", t, flags=re.IGNORECASE)
    # Strip leading/trailing punctuation, parens, dashes
    t = re.sub(r"[\s\-\–\—\(\)\[\]]+$", "", t).strip()
    t = re.sub(r"^[\s\-\–\—\(\)\[\]]+", "", t).strip()
    # Collapse spaces
    t = re.sub(r"\s+", " ", t)
    return t


def location_bucket(loc: str) -> str | None:
    """None = unknown/blank (matches anything). Otherwise the canonical
    location key from taxonomy.canonicalize_location() -- the same key
    score_location()/review/query_filter.py's location= filter compare
    against, so a dedup grouping can never disagree with what the rest of
    the app considers "the same city"."""
    key, _segments = canonicalize_location(loc)
    return None if key == LOCATION_UNKNOWN else key


def find_duplicate_groups(apps: list[dict]) -> list[list[dict]]:
    """
    Returns a list of duplicate groups (each a list of 2+ app dicts).

    Within a company+title group:
    - If all apps have the same location (or some/all blank) → one duplicate group
    - If multiple distinct specific locations exist → each location is a separate role;
      only blank-location duplicates within that group are still flagged
    """
    by_key: dict[tuple, list] = defaultdict(list)
    for app in apps:
        key = (
            normalize_company(app.get("company", "")),
            normalize_title(app.get("title", "")),
        )
        by_key[key].append(app)

    result = []
    for _key, group in by_key.items():
        if len(group) < 2:
            continue

        specific_locs = {
            location_bucket(a.get("location"))
            for a in group
            if location_bucket(a.get("location")) is not None
        }

        if len(specific_locs) <= 1:
            # All blank, all same city, or blank + one city → all likely the same posting
            result.append(group)
        else:
            # Multiple distinct cities → separate roles; only flag blank-loc duplicates
            blank_apps = [a for a in group if location_bucket(a.get("location")) is None]
            if len(blank_apps) >= 2:
                result.append(blank_apps)
            # Flag same-city pairs within this group
            loc_map: dict[str, list] = defaultdict(list)
            for app in group:
                b = location_bucket(app.get("location"))
                if b:
                    loc_map[b].append(app)
            for loc_apps in loc_map.values():
                if len(loc_apps) >= 2:
                    result.append(loc_apps)

    return result


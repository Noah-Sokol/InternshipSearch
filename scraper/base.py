from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator

from config import FILTERS
import classify


@dataclass
class JobListing:
    platform: str
    external_id: str
    company: str
    title: str
    location: str
    url: str
    description: str
    easy_apply: bool = False


class BaseScraper(ABC):
    def __init__(self, page):
        self.page = page  # Playwright page object

    @abstractmethod
    def search(self, query: str, location: str = "United States") -> Iterator[JobListing]:
        pass

    def _is_internship(self, title: str) -> bool:
        """Whole-word internship check -- delegates to classify.py, the one
        canonical definition (previously duplicated across this method,
        scraper/greenhouse_lever.py, and scraper/coverage.py)."""
        return classify.is_internship_title(title)

    def _is_excluded(self, text: str) -> bool:
        """
        desc: True if `text` (title pre-fetch, or title+description
            post-fetch) contains IB or senior/new-grad language. Deliberately
            does NOT also require an internship-title match (unlike
            classify.hard_exclusion(), used elsewhere for the combined gate)
            -- callers use this independently of _is_internship, e.g.
            main.py::evaluate_posting() checks only _is_excluded on a
            title+description blob whose title-level internship check
            already happened earlier in the scraper's search().
        args:
            text (str): title, or title+description, to check.
        output:
            (bool): True if excluded.
        """
        facts = classify.classify_job(title=text)
        return facts.ib_excluded or bool(facts.seniority_penalty_hits)

    def _mentions_target_cycle(self, text: str) -> bool:
        """
        True if the target recruiting year (FILTERS['prioritize_year'])
        appears in the given text. Many rolling/pipeline postings don't
        state a year at all, so a False here is a signal to double-check
        manually, not an automatic exclusion.
        """
        return FILTERS["prioritize_year"] in text

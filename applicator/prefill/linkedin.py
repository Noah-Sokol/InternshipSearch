"""
LinkedIn Easy Apply filler (ported from tools/open_application.py's legacy
walk). Opens the Easy Apply modal, fills each step with the generic
heuristics and advances with Next/Review via safe_click, stopping as soon as
a submit-like button is visible. Never clicks Submit application.
"""

import time
from pathlib import Path

from applicator.prefill.base import Filler, FillResult, host_matches
from applicator.prefill.generic import fill_frames
from applicator.prefill.safe import SubmitRefused, is_submit_like, safe_click

_MAX_STEPS = 8
_NEXT_SELECTORS = (
    'button[aria-label="Continue to next step"]',
    'button[aria-label="Review your application"]',
)


def _at_submit_step(page) -> bool:
    """
    desc: Whether any visible button looks like the final submit (any language).
    args:
        page (Page): page with the Easy Apply modal.
    output:
        bool.
    """
    if page.query_selector('button[aria-label="Submit application"]'):
        return True
    for btn in page.query_selector_all("button:visible"):
        try:
            blob = " ".join([btn.get_attribute("aria-label") or "", btn.inner_text() or ""])
        except Exception:
            continue
        if is_submit_like(blob):
            return True
    return False


class LinkedInFiller(Filler):
    """Easy Apply walker; stops at the submit step."""
    name = "linkedin"

    def matches(self, url: str) -> bool:
        return host_matches(url, "linkedin.com")

    def fill(self, page, profile: dict, get_cover_letter, resume_path: Path) -> FillResult:
        """
        desc: Open Easy Apply and fill each step, stopping before Submit.
        args:
            page (Page): tab at the LinkedIn job posting.
            profile (dict): flat profile.
            get_cover_letter (Callable[[], str]): lazy cover-letter provider (see base.call_cover_letter).
            resume_path (Path): resume file.
        output:
            FillResult.
        """
        time.sleep(1.5)
        btn = page.query_selector("button.jobs-apply-button")
        if not btn:
            if page.query_selector("a[href*='login'], a[href*='signup']") and "Easy Apply" not in (
                    page.content() or ""):
                return FillResult("needs_login", [], [], "Sign in to LinkedIn in this tab, then fill again.")
            return FillResult("unsupported", [], [],
                              "No Easy Apply button (external apply); open the company link manually.")
        try:
            safe_click(btn, "open Easy Apply form")
        except SubmitRefused as e:
            return FillResult("unsupported", [], [], str(e))
        time.sleep(1.5)

        filled, unfilled, note = [], [], ""
        for _ in range(_MAX_STEPS):
            acc, _total = fill_frames(page, profile, get_cover_letter, resume_path)
            filled += [x for x in acc.filled if x not in filled]
            unfilled += [x for x in acc.unfilled if x not in unfilled and x not in filled]
            if _at_submit_step(page):
                note = "Reached the submit step; review and click Submit yourself."
                break
            nxt = None
            for sel in _NEXT_SELECTORS:
                nxt = page.query_selector(sel)
                if nxt:
                    break
            if not nxt:
                note = "No Next/Review button; finish the remaining steps by hand."
                break
            try:
                safe_click(nxt, "advance Easy Apply step")
            except Exception as e:
                note = f"Stopped advancing ({type(e).__name__}); finish by hand."
                break
            time.sleep(1)
        unfilled = [x for x in unfilled if x not in filled]
        status = "filled" if filled and not unfilled else ("partial" if filled or unfilled else "unsupported")
        return FillResult(status, filled, unfilled, note)


FILLER = LinkedInFiller()

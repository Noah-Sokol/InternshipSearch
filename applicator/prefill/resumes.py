"""
Resume selection for the pre-fill runner and the Detail page. The resume is
chosen from the posting's top interest area (the same classify.classify_job
facts the scorer uses) via config.INTEREST_AREA_RESUMES; a per-application
override (applications.resume_override, a config.RESUME_CHOICES label) wins.
Postings with no matching interest area use the default (data-science) resume.
Nothing here reads, generates or modifies a resume file.
"""

from dataclasses import dataclass
from pathlib import Path

from config import INTEREST_AREA_RESUMES, RESUME_CHOICES, RESUME_PATH

DEFAULT_NOTE = "Used default resume (no interest area matched)."


@dataclass(frozen=True)
class ResumeChoice:
    """
    desc: The resume picked for one application.
    args:
        path (Path): resume file to upload.
        label (str): RESUME_CHOICES label of that file ('' if not a listed choice).
        source (str): 'override', 'interest_area' or 'default'.
        area (str | None): interest area that decided it (None for override/default).
    output:
        ResumeChoice.
    """
    path: Path
    label: str
    source: str
    area: str | None = None


def _label_for(path: Path) -> str:
    return next((lab for lab, p in RESUME_CHOICES.items() if p == path), "")


def interest_area_for(app: dict) -> str | None:
    """
    desc: Top interest area of an application's posting (classify_job's
        best_interest_area, as the scorer uses). Pure: reads no database.
    args:
        app (dict): row with title, company, location, description.
    output:
        str | None: taxonomy area name, or None if no area matched.
    """
    try:
        from classify import classify_job
        facts = classify_job(app.get("title") or "", app.get("company") or "",
                             app.get("location") or "", app.get("description") or "")
        return facts.best_interest_area
    except Exception:
        return None


def choose_resume(area: str | None, override: str | None = None) -> ResumeChoice:
    """
    desc: Pick the resume: a valid override first, then the interest-area
        mapping, else the default.
    args:
        area (str | None): the posting's top interest area name.
        override (str | None): applications.resume_override (a RESUME_CHOICES
            label); unknown values are ignored.
    output:
        ResumeChoice.
    """
    if override in RESUME_CHOICES:
        return ResumeChoice(RESUME_CHOICES[override], override, "override")
    path = INTEREST_AREA_RESUMES.get(area or "")
    if path is not None:
        return ResumeChoice(path, _label_for(path), "interest_area", area)
    return ResumeChoice(RESUME_PATH, _label_for(RESUME_PATH), "default")


def resume_for_app(app: dict) -> ResumeChoice:
    """
    desc: Resume for an application row: its override, else its posting's top
        interest area, else the default.
    args:
        app (dict): row with title, company, location, description and
            optionally resume_override.
    output:
        ResumeChoice.
    """
    override = app.get("resume_override")
    if override in RESUME_CHOICES:
        return choose_resume(None, override)
    return choose_resume(interest_area_for(app))

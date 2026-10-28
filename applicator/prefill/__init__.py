"""
Prefill package: fills (never submits) application forms in a Playwright page.

Registry: get_filler(url) returns the ATS-specific filler whose matches(url)
is true, else the generic filler. Per-ATS modules are imported lazily; a
missing or broken module is skipped. Each ATS module must expose a
module-level `FILLER` (a Filler instance) in applicator/prefill/<ats>.py and
be listed in ATS_MODULES below.
"""

import importlib
import sys

from applicator.prefill.base import Filler

ATS_MODULES = [
    "greenhouse", "lever", "ashby", "workday", "icims_oracle_other", "smartrecruiters_workable",
    "linkedin",
]

_CACHE: dict = {}


def _load(mod_name: str):
    if mod_name in _CACHE:
        return _CACHE[mod_name]
    full = f"applicator.prefill.{mod_name}"
    filler = None
    try:
        mod = importlib.import_module(full)
        filler = getattr(mod, "FILLER", None)
        if filler is None:
            for obj in vars(mod).values():
                if isinstance(obj, type) and issubclass(obj, Filler) and obj is not Filler \
                        and obj.__module__ == full:
                    filler = obj()
                    break
    except ModuleNotFoundError as e:
        if e.name != full:  # module exists but one of its imports is missing
            print(f"[prefill] {mod_name} failed to import: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[prefill] {mod_name} failed to import: {e}", file=sys.stderr)
    _CACHE[mod_name] = filler
    return filler


def get_filler(url: str) -> Filler:
    """
    desc: Pick the filler for an application URL: the first ATS-specific
        filler whose matches(url) is true, else the generic filler.
    args:
        url (str): application URL.
    output:
        Filler: always returns a usable filler.
    """
    for mod_name in ATS_MODULES:
        filler = _load(mod_name)
        try:
            if filler is not None and filler.matches(url):
                return filler
        except Exception:
            continue
    from applicator.prefill.generic import FILLER
    return FILLER

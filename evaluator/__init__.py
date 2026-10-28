"""
desc: Evaluator subsystem -- scores and ranks the internship postings already
    staged in the review queue by how much Noah should prefer them (best fit,
    most eligible, most urgent first). It is EVALUATION-ONLY: it reads the DB
    read-only and never discovers, applies to, or submits anything. The default
    scoring path is deterministic and makes no external API calls.
args:
    (package; see evaluator.preferences and evaluator.score)
output:
    (re-exports the main scoring helpers)
"""

from evaluator.score import (  # noqa: F401
    score_staged,
    score_application,
    save_scores,
)

__all__ = ["score_staged", "score_application", "save_scores"]
